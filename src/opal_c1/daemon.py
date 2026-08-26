"""The decomposer daemon.

Something has to own the camera. In Studio mode the manual controls only exist
while the XLink connection is held, so a one-shot command cannot leave focus
locked behind it — whoever holds the device *is* the settings. And the look
engine must keep running, because tearing it down would remove /dev/video10 from
under whatever application is using the camera.

So the daemon holds both, and everything else is a client of it:

    Call mode    engine reads /dev/video0 itself; the daemon adjusts the camera
                 over V4L2 and never touches the frames.
    Studio mode  the daemon holds the depthai device, pumps NV12 into the
                 engine's stdin, and drives focus and white balance directly.

Clients talk JSON lines over a Unix socket. One object per line, one response
per request.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from opal_c1.modes import Mode, current_mode, wait_until_capturable
from opal_c1.v4l2 import UvcControls

LOOKS = [
    "none", "process", "chrome", "fade", "instant",
    "mono", "noir", "tonal", "transfer",
]

# Controls the daemon accepts in Call mode, mapped to V4L2 names.
CALL_CONTROLS = {
    "brightness": "brightness",
    "contrast": "contrast",
    "saturation": "saturation",
    "hue": "hue",
    "sharpness": "sharpness",
}


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/decomposer-{os.getuid()}"
    d = Path(base) / "decomposer"
    d.mkdir(parents=True, exist_ok=True)
    return d


def socket_path() -> Path:
    return runtime_dir() / "daemon.sock"


def find_engine() -> Optional[str]:
    """Locate the look engine: PATH first, then a local cargo build."""
    found = shutil.which("decomposer-engine")
    if found:
        return found
    here = Path(__file__).resolve().parents[2]
    for candidate in (
        here / "engine/target/release/decomposer-engine",
        here / "engine/target/debug/decomposer-engine",
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


@dataclass
class State:
    mode: str = Mode.CALL.value
    look: str = "none"
    strength: float = 1.0
    width: int = 1920
    height: int = 1080
    output: str = "/dev/video10"
    running: bool = False
    frames: int = 0
    error: Optional[str] = None
    controls: dict = field(default_factory=dict)


class Daemon:
    def __init__(self, output="/dev/video10", width=1920, height=1080, fps=30.0):
        self.state = State(output=output, width=width, height=height)
        self.fps = fps
        self.lock = threading.RLock()
        self.engine: Optional[subprocess.Popen] = None
        self.engine_ctl = runtime_dir() / "engine.sock"
        self._pump: Optional[threading.Thread] = None
        self._pump_stop = threading.Event()
        self._cam = None  # OpalDevice, Studio mode only
        self._shutdown = threading.Event()
        self.engine_log: list[str] = []
        self.restarts = 0

    # -- engine ---------------------------------------------------------

    def _engine_cmd(self, from_stdin: bool) -> list[str]:
        binary = find_engine()
        if binary is None:
            raise RuntimeError(
                "decomposer-engine not found. Build it with: cd engine && cargo build --release"
            )
        return [
            binary,
            "--input", "-" if from_stdin else "/dev/video0",
            "--output", self.state.output,
            "--width", str(self.state.width),
            "--height", str(self.state.height),
            "--look", self.state.look,
            "--strength", str(self.state.strength),
            "--control", str(self.engine_ctl),
        ]

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        """Keep the engine's last few stderr lines.

        Without this the pipe fills and the engine blocks, and worse, a crash
        leaves no explanation anywhere.
        """
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode(errors="replace").rstrip()
            if not line:
                continue
            with self.lock:
                self.engine_log.append(line)
                del self.engine_log[:-12]

    def _start_engine(self, from_stdin: bool, attempts: int = 3) -> None:
        """Start the engine, retrying a device that is not quite ready.

        Straight after a mode switch the camera can accept an open and then
        fail to stream. Retrying is more reliable than trying to predict how
        long the hardware needs.
        """
        last = None
        for attempt in range(1, attempts + 1):
            try:
                self._spawn_engine(from_stdin)
                return
            except RuntimeError as e:
                last = e
                self._stop_engine()
                if attempt < attempts:
                    time.sleep(2.0 * attempt)
        raise RuntimeError(f"engine would not start after {attempts} attempts: {last}")

    def _spawn_engine(self, from_stdin: bool) -> None:
        # A socket file left by a previous engine would make the readiness
        # check below pass instantly against a process that is already dead.
        with suppress(OSError):
            self.engine_ctl.unlink()
        with self.lock:
            self.engine_log = []

        cmd = self._engine_cmd(from_stdin)
        self.engine = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if from_stdin else subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        threading.Thread(
            target=self._drain_stderr, args=(self.engine,), daemon=True
        ).start()

        deadline = time.time() + 8
        while time.time() < deadline:
            if self.engine.poll() is not None:
                raise RuntimeError(
                    "engine exited immediately: " + (" | ".join(self.engine_log) or "no output")
                )
            if self.engine_ctl.exists():
                return
            time.sleep(0.1)
        raise RuntimeError(
            "engine did not open its control socket within 8s: "
            + (" | ".join(self.engine_log) or "no output")
        )

    def _stop_engine(self) -> None:
        if self.engine is None:
            return
        with suppress(Exception):
            if self.engine.stdin:
                self.engine.stdin.close()
        with suppress(Exception):
            self.engine.terminate()
            self.engine.wait(timeout=5)
        self.engine = None
        with suppress(OSError):
            self.engine_ctl.unlink()

    def _tell_engine(self, line: str) -> None:
        """Send one control line. Ignored if the engine is not up yet."""
        if not self.engine_ctl.exists():
            return
        with suppress(OSError):
            s = socket.socket(socket.AF_UNIX)
            s.settimeout(2.0)
            s.connect(str(self.engine_ctl))
            s.sendall((line + "\n").encode())
            s.close()

    # -- studio frame pump ----------------------------------------------

    def _pump_frames(self) -> None:
        """Studio mode: depthai -> engine stdin, until told to stop."""
        assert self._cam is not None
        stdin = self.engine.stdin if self.engine else None
        try:
            for frame in self._cam.frames():
                if self._pump_stop.is_set() or stdin is None:
                    break
                stdin.write(frame.nv12())
                with self.lock:
                    self.state.frames += 1
        except (BrokenPipeError, ValueError, OSError):
            pass
        except Exception as e:  # surface, do not die silently
            with self.lock:
                self.state.error = f"pump: {e}"

    # -- modes ----------------------------------------------------------

    def _teardown(self) -> None:
        self._pump_stop.set()
        if self._pump is not None:
            self._pump.join(timeout=5)
            self._pump = None
        self._stop_engine()
        if self._cam is not None:
            with suppress(Exception):
                self._cam.close()
            self._cam = None

    def enter_call(self) -> None:
        with self.lock:
            self._teardown()
            self.state.mode = Mode.CALL.value
            self.state.error = None
        # Leaving Studio mode reboots the camera; /dev/video0 takes ~14s.
        if current_mode() is not Mode.CALL:
            wait_until_capturable(timeout=45)
        else:
            wait_until_capturable(timeout=10)
        with self.lock:
            self._start_engine(from_stdin=False)
            self.state.running = True
            self.state.frames = 0

    def enter_studio(self) -> None:
        from opal_c1.device import OpalDevice

        with self.lock:
            self._teardown()
            self.state.mode = Mode.STUDIO.value
            self.state.error = None
            self._cam = OpalDevice(
                width=self.state.width, height=self.state.height, fps=self.fps
            ).open()
            self._start_engine(from_stdin=True)
            self._pump_stop.clear()
            self._pump = threading.Thread(target=self._pump_frames, daemon=True)
            self._pump.start()
            self.state.running = True
            self.state.frames = 0

    def set_mode(self, mode: str) -> dict:
        want = Mode(mode)
        if want is Mode.CALL:
            self.enter_call()
        else:
            self.enter_studio()
        return self.status()

    # -- controls -------------------------------------------------------

    def set_look(self, look: Optional[str], strength: Optional[float]) -> dict:
        with self.lock:
            if look is not None:
                if look not in LOOKS:
                    raise ValueError(f"unknown look {look!r}. Known: {', '.join(LOOKS)}")
                self.state.look = look
                self._tell_engine(f"look {look}")
            if strength is not None:
                self.state.strength = max(0.0, min(1.0, float(strength)))
                self._tell_engine(f"strength {self.state.strength}")
        return self.status()

    def set_camera(self, **kw) -> dict:
        """Apply camera controls, using whichever path the current mode allows."""
        with self.lock:
            mode = Mode(self.state.mode)
            applied, refused = {}, {}

            if mode is Mode.STUDIO and self._cam is not None:
                if "focus" in kw and kw["focus"] is not None:
                    v = int(kw["focus"])
                    self._cam.set_focus(None if v < 0 else v)
                    applied["focus"] = v
                if "wb" in kw and kw["wb"] is not None:
                    v = int(kw["wb"])
                    self._cam.set_white_balance(None if v < 0 else v)
                    applied["wb"] = v
                if kw.get("exposure") is not None or kw.get("iso") is not None:
                    self._cam.set_exposure(kw.get("exposure"), kw.get("iso"))
                    applied["exposure"] = kw.get("exposure")
                    applied["iso"] = kw.get("iso")
            else:
                uvc = UvcControls()
                for key, name in CALL_CONTROLS.items():
                    if kw.get(key) is not None:
                        try:
                            applied[key] = uvc.set(name, int(kw[key]))
                        except (PermissionError, ValueError, OSError) as e:
                            refused[key] = str(e)
                if kw.get("exposure") is not None or kw.get("iso") is not None:
                    try:
                        applied.update(
                            uvc.set_manual_exposure(kw.get("exposure"), kw.get("iso"))
                        )
                    except (PermissionError, ValueError, OSError) as e:
                        refused["exposure"] = str(e)
                for key in ("focus", "wb"):
                    if kw.get(key) is not None:
                        refused[key] = (
                            f"{key} needs Studio mode; the camera locks it to auto "
                            "under Call-mode firmware"
                        )

            self.state.controls.update(applied)
        out = self.status()
        out["applied"] = applied
        if refused:
            out["refused"] = refused
        return out

    # -- status ---------------------------------------------------------

    def status(self) -> dict:
        with self.lock:
            s = asdict(self.state)
        s["mode_actual"] = (current_mode().value if current_mode() else None)
        s["engine_alive"] = self.engine is not None and self.engine.poll() is None
        s["looks"] = LOOKS
        s["restarts"] = self.restarts
        with self.lock:
            s["engine_log"] = list(self.engine_log)
        if self.engine is not None and self.engine.poll() is not None:
            s["error"] = (
                f"engine exited with code {self.engine.returncode}: "
                + (" | ".join(s["engine_log"]) or "no output")
            )
        return s

    # -- supervision ----------------------------------------------------

    def _supervise(self) -> None:
        """Restart the engine if it dies.

        Observed in practice: the camera re-enumerated under a running engine
        and it exited with ENODEV. Without this the daemon keeps reporting a
        mode it is no longer serving, and /dev/video10 stays dark until someone
        notices.
        """
        backoff = 2.0
        while not self._shutdown.wait(2.0):
            with self.lock:
                if not self.state.running or self.engine is None:
                    continue
                if self.engine.poll() is None:
                    backoff = 2.0
                    continue
                reason = " | ".join(self.engine_log) or "no output"
                mode = self.state.mode

            print(f"engine died ({reason}); restarting in {backoff:.0f}s")
            if self._shutdown.wait(backoff):
                return
            try:
                if Mode(mode) is Mode.CALL:
                    self.enter_call()
                else:
                    self.enter_studio()
                with self.lock:
                    self.restarts += 1
                    self.state.error = f"engine restarted after: {reason}"
                print(f"engine restarted (total restarts: {self.restarts})")
                backoff = 2.0
            except Exception as e:
                with self.lock:
                    self.state.error = f"engine restart failed: {type(e).__name__}: {e}"
                print(f"restart failed: {e}")
                backoff = min(backoff * 2, 30.0)

    # -- server ---------------------------------------------------------

    def handle(self, req: dict) -> dict:
        cmd = req.get("cmd")
        try:
            if cmd == "status":
                return {"ok": True, **self.status()}
            if cmd == "looks":
                return {"ok": True, "looks": LOOKS}
            if cmd == "set_look":
                return {"ok": True, **self.set_look(req.get("look"), req.get("strength"))}
            if cmd == "set_mode":
                return {"ok": True, **self.set_mode(req.get("mode", "call"))}
            if cmd == "set_camera":
                return {"ok": True, **self.set_camera(**req.get("values", {}))}
            if cmd == "stop":
                self._shutdown.set()
                return {"ok": True, "stopping": True}
            return {"ok": False, "error": f"unknown command {cmd!r}"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def _serve_client(self, conn: socket.socket) -> None:
        with conn, conn.makefile("rwb") as f:
            for raw in f:
                line = raw.decode().strip()
                if not line:
                    continue
                try:
                    req = json.loads(line)
                except json.JSONDecodeError as e:
                    resp = {"ok": False, "error": f"bad JSON: {e}"}
                else:
                    resp = self.handle(req)
                f.write((json.dumps(resp) + "\n").encode())
                f.flush()

    def run(self, initial_mode: str = "call") -> int:
        path = socket_path()
        # A socket left by a crashed daemon would block bind().
        with suppress(OSError):
            if path.exists() and not self._probe(path):
                path.unlink()

        server = socket.socket(socket.AF_UNIX)
        server.bind(str(path))
        server.listen(8)
        server.settimeout(0.5)
        print(f"daemon listening on {path}")

        threading.Thread(target=self._supervise, daemon=True).start()

        try:
            self.set_mode(initial_mode)
            print(f"mode {self.state.mode}, look {self.state.look}, "
                  f"publishing to {self.state.output}")
        except Exception as e:
            print(f"startup failed: {type(e).__name__}: {e}")
            self.state.error = str(e)

        try:
            while not self._shutdown.is_set():
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    continue
                threading.Thread(
                    target=self._serve_client, args=(conn,), daemon=True
                ).start()
        except KeyboardInterrupt:
            pass
        finally:
            print("\nshutting down")
            self._teardown()
            server.close()
            with suppress(OSError):
                path.unlink()
        return 0

    @staticmethod
    def _probe(path: Path) -> bool:
        """True if something is already listening (so we must not steal the path)."""
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(0.5)
        try:
            s.connect(str(path))
            return True
        except OSError:
            return False
        finally:
            s.close()


class Client:
    """Talks to a running daemon."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path or socket_path()

    def request(self, **req) -> dict:
        if not self.path.exists():
            raise ConnectionError(
                f"no daemon at {self.path}. Start one with: decomposer daemon"
            )
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(60.0)
        try:
            s.connect(str(self.path))
            with s.makefile("rwb") as f:
                f.write((json.dumps(req) + "\n").encode())
                f.flush()
                line = f.readline()
        finally:
            s.close()
        if not line:
            raise ConnectionError("daemon closed the connection without replying")
        return json.loads(line.decode())
