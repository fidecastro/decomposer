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

import errno
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from opal_c1.core import health, model, presets as preset_codec, transitions
from opal_c1.core.model import Mode
from opal_c1.modes import current_mode, wait_until_capturable
from opal_c1.modes import camera_video_node  # engine input node discovery
from opal_c1.ports import FrameSource

# The eight Core Image effects Composer exposed, then its five own looks.
# Order is deliberate: it is the order they appear in the panel.
BUILTIN_LOOKS = [
    "none", "process", "chrome", "fade", "instant",
    "mono", "noir", "tonal", "transfer",
]
CUSTOM_LOOKS = ["G1", "D1", "Q1", "S1", "X1"]


def lut_dir() -> Optional[Path]:
    """Where the extracted .cube LUTs live, if they are installed."""
    here = Path(__file__).resolve().parents[2]
    candidate = here / "luts"
    return candidate if candidate.is_dir() else None


def available_looks() -> list[str]:
    """Built-ins, plus any look with a LUT beside it.

    A LUT is the measured transform rather than an approximation, so when one
    exists for a built-in name the engine prefers it; the name stays the same
    either way.
    """
    looks = list(BUILTIN_LOOKS)
    d = lut_dir()
    if d is None:
        return looks
    have = {p.stem for p in d.glob("*.cube")}
    looks += [n for n in CUSTOM_LOOKS if n in have]
    looks += sorted(
        n for n in have
        if n not in looks and n not in CUSTOM_LOOKS
    )
    return looks


LOOKS = available_looks()

# A look at full strength is the filter as its shader defines it, which is
# stronger than these are usually wanted. Half is a better starting point; each
# look then remembers whatever you dial in for it.
DEFAULT_STRENGTH = 0.5

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



def preset_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    d = Path(base) / "decomposer" / "presets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _preset_path(name: str) -> Path:
    """Resolve a preset name to a file. Validation lives in the pure core."""
    return preset_dir() / f"{preset_codec.validate_name(name)}.json"


@dataclass
class State:
    mode: str = Mode.CALL.value
    look: str = "none"
    strength: float = 1.0
    # Intensity is remembered per look. Composer's filters carry their own
    # intensity, so a strength dialled in for noir should not follow you to G1.
    look_strength: dict = field(default_factory=dict)
    width: int = 1920
    height: int = 1080
    output: str = "/dev/video10"
    overlay: Optional[str] = None
    overlay_x: int = 0
    overlay_y: int = 0
    overlay_w: int = 0
    overlay_h: int = 0
    overlay_opacity: float = 1.0
    mirror_h: bool = False
    mirror_v: bool = False
    running: bool = False
    frames: int = 0
    error: Optional[str] = None
    controls: dict = field(default_factory=dict)


class Daemon:
    def __init__(
        self, output="/dev/video10", width=1920, height=1080, fps=30.0,
        tray_enabled: bool = False,
        default_strength: float = DEFAULT_STRENGTH,
    ):
        self.state = State(output=output, width=width, height=height)
        self.tray_enabled = tray_enabled
        self.default_strength = max(0.0, min(1.0, float(default_strength)))
        self.state.strength = self.default_strength
        self.fps = fps
        self.lock = threading.RLock()
        self.engine: Optional[subprocess.Popen] = None
        self.engine_ctl = runtime_dir() / "engine.sock"
        self.preview_sock = runtime_dir() / "preview.sock"
        self._pump: Optional[threading.Thread] = None
        self._pump_stop = threading.Event()
        # The camera is reached only through a backend implementing
        # ports.CameraBackend; which one depends on the mode. `_sticky`
        # remembers explicit user requests so they can be replayed after the
        # firmware reboots - which it does on every mode switch and every
        # Studio engine restart, silently resetting to defaults otherwise.
        self._backend = None
        self._sticky: dict = {}
        self._shutdown = threading.Event()
        self.engine_log: list[str] = []
        self._engine_started = 0.0
        self.restarts = 0
        # Transitions are mutually exclusive and rate-limited. The ledger's
        # decisions come from the pure core; the lock enforces them across
        # client handler threads and the supervisor.
        self._ledger = transitions.Ledger(current=Mode(self.state.mode))
        self._transition_lock = threading.Lock()

    # -- engine ---------------------------------------------------------

    def _engine_cmd(self, from_stdin: bool) -> list[str]:
        binary = find_engine()
        if binary is None:
            raise RuntimeError(
                "decomposer-engine not found. Build it with: cd engine && cargo build --release"
            )
        # Discovered per attempt: the number can change between retries when
        # the camera has just re-enumerated.
        cam_node = "-" if from_stdin else (camera_video_node() or "/dev/video0")
        return [
            binary,
            "--input", cam_node,
            "--output", self.state.output,
            "--width", str(self.state.width),
            "--height", str(self.state.height),
            "--look", self.state.look,
            "--strength", str(self.state.strength),
            "--flip", str(self._flip_bits()),
            "--overlay-rect",
            f"{self.state.overlay_x},{self.state.overlay_y},"
            f"{self.state.overlay_w},{self.state.overlay_h}",
            "--overlay-opacity", str(self.state.overlay_opacity),
            "--control", str(self.engine_ctl),
            "--preview", str(self.preview_sock),
        ] + (
            ["--lut-dir", str(lut_dir())] if lut_dir() else []
        ) + (
            # Passed at startup too, so an engine restart keeps the overlay.
            ["--overlay", self.state.overlay] if self.state.overlay else []
        )

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
        self._engine_started = time.time()

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

    def _flip_bits(self) -> int:
        """bit 0 mirrors horizontally, bit 1 vertically; both is a 180 turn."""
        return (1 if self.state.mirror_h else 0) | (2 if self.state.mirror_v else 0)

    def set_mirror(self, horizontal=None, vertical=None) -> dict:
        """Mirror the published image.

        Applied in the shader, so it costs nothing and both modes share one
        setting - Studio mode is corrected to Call mode's orientation on the
        device, so a single preference is meaningful across both.
        """
        with self.lock:
            if horizontal is not None:
                self.state.mirror_h = bool(horizontal)
            if vertical is not None:
                self.state.mirror_v = bool(vertical)
            self._tell_engine(f"flip {self._flip_bits()}")
        return self.status()

    def set_overlay(
        self, path=None, x=None, y=None, width=None, height=None, opacity=None
    ) -> dict:
        """Composite an image over the frame, or clear it with path="off".

        Placement is in output pixels; width/height are maximums the image is
        fitted into, keeping its aspect ratio. Zero means unconstrained.
        """
        with self.lock:
            if path is not None:
                if path in ("off", "", None):
                    self.state.overlay = None
                else:
                    resolved = Path(path).expanduser()
                    if not resolved.is_file():
                        raise FileNotFoundError(f"no such overlay image: {resolved}")
                    self.state.overlay = str(resolved)
            for name, value in (
                ("overlay_x", x), ("overlay_y", y),
                ("overlay_w", width), ("overlay_h", height),
            ):
                if value is not None:
                    setattr(self.state, name, max(0, int(value)))
            if opacity is not None:
                self.state.overlay_opacity = max(0.0, min(1.0, float(opacity)))

            st = self.state
            self._tell_engine(
                f"overlay-rect {st.overlay_x} {st.overlay_y} {st.overlay_w} {st.overlay_h}"
            )
            self._tell_engine(f"overlay-opacity {st.overlay_opacity}")
            self._tell_engine(f"overlay {st.overlay or 'off'}")
        return self.status()

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
        """Studio mode: depthai -> engine stdin, until told to stop.

        Polls rather than blocking. A blocking read cannot be interrupted, and
        the camera stops delivering precisely when we most need to stop - during
        a mode switch - which would park this thread inside depthai and make a
        clean close impossible.
        """
        source = self._backend
        assert isinstance(source, FrameSource), "pump started without a frame source"
        stdin = self.engine.stdin if self.engine else None
        try:
            while not self._pump_stop.is_set() and stdin is not None:
                frame = source.try_read_frame()
                if frame is None:
                    # Well under a frame interval, so this costs no latency.
                    self._pump_stop.wait(0.004)
                    continue
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
        """Release the engine and the camera, in an order that cannot deadlock.

        The pump must be gone before the device is closed: closing depthai
        underneath a thread that is still inside it leaves its watchdog running,
        and the watchdog reboots the camera - repeatedly, since nothing ever
        reconnects. That is what a failed mode switch used to look like from
        outside: a camera cycling through its bootloader every fifteen seconds.
        """
        self._pump_stop.set()
        if self._pump is not None:
            self._pump.join(timeout=5)
            if self._pump.is_alive():
                # Should not happen now the pump polls, but closing the device
                # regardless would be worse than saying so.
                print("warning: frame pump did not stop; not closing the device")
                with self.lock:
                    self.state.error = "frame pump would not stop"
                self._pump = None
                self._stop_engine()
                return
            self._pump = None
        self._stop_engine()
        if self._backend is not None:
            try:
                self._backend.release()
            except Exception as e:
                print(f"warning: camera release failed: {e}")
                with self.lock:
                    self.state.error = f"camera release failed: {e}"
            self._backend = None

    def _replay_sticky(self, backend) -> None:
        """Reapply remembered requests after a firmware reboot.

        Every mode entry boots a fresh firmware with default settings, so
        without this the camera silently reverts while status keeps claiming
        the old values. Failures are noted, not fatal: a fresh session with
        defaults beats no session because one replayed value was refused.
        """
        replay = model.sticky_for_mode(self._sticky, backend.mode)
        if not replay:
            return
        try:
            applied, refused = backend.apply_controls(replay)
            with self.lock:
                self.state.controls.update(applied)
            if refused:
                print(f"sticky replay refused: {refused}")
        except Exception as e:
            print(f"sticky replay failed: {e}")

    def enter_call(self) -> None:
        from opal_c1.adapters.uvc_cam import UvcBackend

        with self.lock:
            self._teardown()
            self.state.mode = Mode.CALL.value
            self.state.error = None
        # Leaving Studio mode reboots the camera; /dev/video0 takes ~14s.
        if current_mode() is not Mode.CALL:
            wait_until_capturable(timeout=45)
        else:
            wait_until_capturable(timeout=10)
        backend = UvcBackend()
        backend.attach()
        with self.lock:
            self._backend = backend
            self._start_engine(from_stdin=False)
            self.state.running = True
            self.state.frames = 0
        self._replay_sticky(backend)

    def enter_studio(self) -> None:
        from opal_c1.adapters.depthai_cam import XLinkBackend

        with self.lock:
            self._teardown()
            self.state.mode = Mode.STUDIO.value
            self.state.error = None
            backend = XLinkBackend(
                width=self.state.width, height=self.state.height, fps=self.fps
            )
            backend.attach()
            self._backend = backend
            self._start_engine(from_stdin=True)
            self._pump_stop.clear()
            self._pump = threading.Thread(target=self._pump_frames, daemon=True)
            self._pump.start()
            self.state.running = True
            self.state.frames = 0
        self._replay_sticky(backend)

    def _transition(self, want: Mode, *, enforce_guard: bool) -> None:
        """The single doorway through which every mode entry passes.

        Client switches are guarded — rate-limited and mutually exclusive —
        because every switch reboots the camera's firmware and concurrent
        entries used to interleave teardown with startup. Supervisor
        re-entries share the exclusivity but bypass the rate limit: recovery
        must not be blocked by the thing it is recovering from.
        """
        now = time.monotonic()
        if enforce_guard:
            with self.lock:
                decision = transitions.evaluate_switch(self._ledger, want, now)
            if not decision.allowed:
                raise RuntimeError(decision.reason)
        if not self._transition_lock.acquire(blocking=False):
            raise RuntimeError("a mode transition is already in progress")
        try:
            with self.lock:
                if want is not self._ledger.current:
                    self._ledger.last_switch_at = now
                self._ledger.in_progress = True
            if want is Mode.CALL:
                self.enter_call()
            else:
                self.enter_studio()
        finally:
            with self.lock:
                self._ledger.in_progress = False
                self._ledger.current = Mode(self.state.mode)
            self._transition_lock.release()

    def set_mode(self, mode: str) -> dict:
        want = Mode(mode)
        try:
            self._transition(want, enforce_guard=True)
        except Exception as e:
            # The mode is still what was asked for: leave `running` set so the
            # supervisor keeps working toward it instead of going dormant. A
            # switch issued while the camera is mid-reboot fails exactly once
            # and used to strand the daemon until someone reissued it by hand.
            with self.lock:
                self.state.running = True
                self.state.error = f"{type(e).__name__}: {e}"
            raise
        return self.status()

    # -- controls -------------------------------------------------------

    def set_look(self, look: Optional[str], strength: Optional[float]) -> dict:
        """Select a look and/or its intensity.

        Selecting a look restores the intensity last used for it, so each look
        keeps its own setting rather than inheriting whatever the previous one
        was left at.
        """
        with self.lock:
            if look is not None:
                known = available_looks()
                if look not in known:
                    raise ValueError(f"unknown look {look!r}. Known: {', '.join(known)}")
                self.state.look = look
                self._tell_engine(f"look {look}")
                if strength is None:
                    remembered = self.state.look_strength.get(look, self.default_strength)
                    self.state.strength = remembered
                    self._tell_engine(f"strength {remembered}")
            if strength is not None:
                value = max(0.0, min(1.0, float(strength)))
                self.state.strength = value
                self.state.look_strength[self.state.look] = value
                self._tell_engine(f"strength {value}")
        return self.status()

    def set_camera(self, **kw) -> dict:
        """Apply camera controls through the current mode's backend.

        Mode-level routing comes from the single table in core.model — the
        if-studio branches that used to live here are gone. Only values the
        table allows reach the backend, so a refusal from the backend means
        the hardware itself said no, not that the mode could not try.
        """
        requested = {k: v for k, v in kw.items() if v is not None}
        with self.lock:
            mode = Mode(self.state.mode)
            backend = self._backend
        applied, refused = {}, {}
        allowed = {}
        for key, value in requested.items():
            why = model.refusal_reason(mode, key)
            if why:
                refused[key] = why
            else:
                allowed[key] = value
        if allowed:
            if backend is None:
                for key in allowed:
                    refused[key] = "no camera attached (mode transition in progress?)"
            else:
                got, denied = backend.apply_controls(allowed)
                applied.update(got)
                refused.update(denied)
        with self.lock:
            self.state.controls.update(applied)
            for key, value in applied.items():
                if key in model.STICKY_CONTROLS:
                    self._sticky[key] = value
        out = self.status()
        out["applied"] = applied
        if refused:
            out["refused"] = refused
        return out

    # -- presets ---------------------------------------------------------

    def save_preset(self, name: str) -> dict:
        """Capture the current look, framing and camera settings under a name."""
        with self.lock:
            st = self.state
            data = {
                "version": 1,
                "name": name,
                "mode": st.mode,
                "look": st.look,
                "strength": st.strength,
                "look_strength": dict(st.look_strength),
                "mirror_h": st.mirror_h,
                "mirror_v": st.mirror_v,
                "overlay": {
                    "path": st.overlay,
                    "x": st.overlay_x, "y": st.overlay_y,
                    "width": st.overlay_w, "height": st.overlay_h,
                    "opacity": st.overlay_opacity,
                },
                "controls": self._live_controls(),
            }
        path = _preset_path(name)
        path.write_text(json.dumps(data, indent=2) + "\n")
        out = self.status()
        out["preset_saved"] = str(path)
        return out

    def load_preset(self, name: str, with_mode: bool = False) -> dict:
        """Apply a saved preset.

        The mode is recorded but not switched into unless asked: switching
        reboots the camera and takes about fifteen seconds, which is not
        something a preset should do to you by surprise. Controls the current
        mode cannot reach are reported rather than silently dropped.
        """
        path = _preset_path(name)
        if not path.is_file():
            raise FileNotFoundError(f"no preset named {name!r} in {preset_dir()}")
        # The pure codec normalizes: unknown fields dropped, out-of-range
        # values clamped, and every such repair reported rather than silent.
        data, notes = preset_codec.decode(json.loads(path.read_text()))
        if with_mode and data.get("mode") and data["mode"] != self.state.mode:
            self.set_mode(data["mode"])
        elif data.get("mode") and data["mode"] != self.state.mode:
            notes.append(
                f"saved in {data['mode']} mode; still in {self.state.mode}"
                " (pass with_mode to switch)"
            )

        with self.lock:
            self.state.look_strength.update(data.get("look_strength") or {})
        if data.get("look"):
            self.set_look(data["look"], data.get("strength"))
        self.set_mirror(data.get("mirror_h"), data.get("mirror_v"))

        ov = data.get("overlay") or {}
        try:
            self.set_overlay(
                path=ov.get("path") or "off",
                x=ov.get("x"), y=ov.get("y"),
                width=ov.get("width"), height=ov.get("height"),
                opacity=ov.get("opacity"),
            )
        except FileNotFoundError as e:
            notes.append(f"overlay skipped: {e}")

        controls = data.get("controls") or {}
        if controls:
            applied = self.set_camera(**controls)
            for key, why in (applied.get("refused") or {}).items():
                notes.append(f"{key} skipped: {why}")

        out = self.status()
        out["preset_loaded"] = name
        if notes:
            out["notes"] = notes
        return out

    def list_presets(self) -> list[dict]:
        found = []
        for path in sorted(preset_dir().glob("*.json")):
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            found.append({
                "name": path.stem,
                "look": data.get("look"),
                "strength": data.get("strength"),
                "mode": data.get("mode"),
                "overlay": bool((data.get("overlay") or {}).get("path")),
            })
        return found

    def delete_preset(self, name: str) -> dict:
        path = _preset_path(name)
        if not path.is_file():
            raise FileNotFoundError(f"no preset named {name!r}")
        path.unlink()
        return {"deleted": name}

    # -- status ---------------------------------------------------------

    def _live_controls(self) -> dict:
        """What the camera is actually set to right now.

        Reporting only what this session happened to write is misleading: a
        freshly started daemon would show every control at its minimum while
        the camera sat at its real values, so the panel drew every slider at 0.
        """
        backend = self._backend
        live: dict = {}
        if backend is not None:
            with suppress(Exception):
                live = backend.read_controls()
        # Remembered intent (auto requests, effect/scene) overlays readback;
        # the precedence rules are pure and tested in core.model.
        return model.merge_reported(live, self._sticky)

    def status(self) -> dict:
        with self.lock:
            s = asdict(self.state)
        s["controls"] = self._live_controls()
        s["mode_actual"] = (current_mode().value if current_mode() else None)
        s["engine_alive"] = self.engine is not None and self.engine.poll() is None
        s["looks"] = available_looks()
        s["restarts"] = self.restarts
        with self.lock:
            s["transitioning"] = self._ledger.in_progress
        s["presets"] = [p["name"] for p in self.list_presets()]
        s["preview"] = str(self.preview_sock) if self.preview_sock.exists() else None
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
        """Restart the engine when it dies. Decisions come from core.health.

        This thread gathers facts — did the engine die, how long did it live,
        is the camera on the bus — and executes whatever the pure policy
        answers. The policy is unit-tested against every failure pattern the
        camera has demonstrated (dies-young, vanished, repeated re-entry
        failure), which is the only reason to trust a loop like this.
        """
        policy = health.EnginePolicy()

        while not self._shutdown.wait(2.0):
            with self.lock:
                if not self.state.running:
                    continue
                if self.engine is not None and self.engine.poll() is None:
                    policy.note_alive()
                    continue
                # Covers both an engine that died and one that never started
                # (a mode switch that failed mid-camera-reboot).
                reason = " | ".join(self.engine_log) or "engine not started"
                mode = Mode(self.state.mode)
                uptime = time.time() - self._engine_started if self._engine_started else 0.0

            action = policy.on_death(
                mode, uptime, camera_on_bus=current_mode() is not None
            )

            if action.kind is health.Kind.HOLD_SICK:
                with self.lock:
                    self.state.error = action.message
                print(action.message)
                if self._shutdown.wait(action.delay):
                    return
                continue

            if action.kind is health.Kind.HOLD_VANISHED:
                with self.lock:
                    self.state.error = action.message
                print(action.message)
                # Hold, but come back the moment the camera reappears so a
                # replug recovers in seconds rather than a minute.
                waited = 0.0
                while waited < action.delay:
                    if self._shutdown.wait(health.VANISHED_POLL_SECONDS):
                        return
                    waited += health.VANISHED_POLL_SECONDS
                    if current_mode() is not None:
                        break
                continue

            print(f"engine died ({reason}); retrying {mode.value} in {action.delay:.0f}s")
            if self._shutdown.wait(action.delay):
                return

            if not self._camera_settled():
                # Mid-reboot. Say nothing and come back; attempting now would
                # only add another reset to whatever it is already doing.
                continue

            try:
                self._transition(mode, enforce_guard=False)
            except Exception as e:
                if "already in progress" in str(e):
                    # A client transition is underway; it owns the camera now.
                    continue
                followup = policy.on_reentry_failed(mode, str(e))
                print(f"restart failed: {e}")
                if followup.kind is health.Kind.FALLBACK_TO_CALL:
                    # Studio is the mode that reboots the camera. Falling back
                    # to Call stops the cycling and leaves a usable camera.
                    print("giving up on studio mode; falling back to call")
                    with self.lock:
                        self.state.error = followup.message
                    with suppress(Exception):
                        self._transition(Mode.CALL, enforce_guard=False)
                else:
                    with self.lock:
                        self.state.error = f"engine restart failed: {type(e).__name__}: {e}"
                continue

            policy.on_reentry_ok()
            with self.lock:
                self.restarts += 1
                self.state.error = f"engine restarted after: {reason}"
            print(f"engine restarted (total restarts: {self.restarts})")

    def _camera_settled(self, dwell: float = 3.0) -> bool:
        """True if the camera is on the bus and has stayed put.

        A device in the middle of re-enumerating will answer once and vanish
        again; acting on that first sighting is what turns one failure into a
        loop.
        """
        first = current_mode()
        if first is None:
            return False
        if self._shutdown.wait(dwell):
            return False
        return current_mode() is first

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
            if cmd == "preset_save":
                return {"ok": True, **self.save_preset(req["name"])}
            if cmd == "preset_load":
                return {"ok": True, **self.load_preset(
                    req["name"], bool(req.get("with_mode"))
                )}
            if cmd == "preset_list":
                return {"ok": True, "presets": self.list_presets()}
            if cmd == "preset_delete":
                return {"ok": True, **self.delete_preset(req["name"])}
            if cmd == "set_overlay":
                return {"ok": True, **self.set_overlay(**req.get("values", {}))}
            if cmd == "set_mirror":
                return {"ok": True, **self.set_mirror(
                    req.get("horizontal"), req.get("vertical")
                )}
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

    def _start_tray(self) -> None:
        """Register a StatusNotifierItem, for desktops without the Omarchy plugin.

        Off by default: on Omarchy the QML bar widget is the better button - it
        draws with the bar's own colours, where a tray icon is a scaled pixmap
        that renders poorly - and having both puts two marks in the bar.
        """
        if not self.tray_enabled:
            return
        try:
            from opal_c1 import tray
        except ImportError:
            return

        def toggle() -> None:
            # Spawn rather than call: the overlay is a separate GTK process,
            # and running it again is what toggles it.
            with suppress(Exception):
                subprocess.Popen(
                    [sys.executable, "-m", "opal_c1.cli", "toggle"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )

        with suppress(Exception):
            tray.run_in_thread(toggle)

    def _bind(self, server: socket.socket, path: Path, timeout: float = 20.0) -> bool:
        """Take the socket, waiting out a predecessor that is still shutting down.

        `decomposer stop` returns as soon as the daemon acknowledges the
        request, but that daemon then stops the engine and releases the camera
        before it unlinks the socket. A new daemon started immediately after
        would otherwise hit EADDRINUSE and die. A socket with nothing listening
        is a crash leftover and is safe to remove.
        """
        deadline = time.time() + timeout
        announced = False
        while True:
            if path.exists():
                if self._probe(path):
                    if time.time() > deadline:
                        print(
                            f"another daemon is already listening on {path}. "
                            "Stop it first with: decomposer stop",
                            file=sys.stderr,
                        )
                        return False
                    if not announced:
                        print("waiting for the previous daemon to finish shutting down…")
                        announced = True
                    time.sleep(0.3)
                    continue
                with suppress(OSError):
                    path.unlink()
            try:
                server.bind(str(path))
                return True
            except OSError as e:
                if e.errno != errno.EADDRINUSE or time.time() > deadline:
                    print(f"could not bind {path}: {e}", file=sys.stderr)
                    return False
                time.sleep(0.3)

    def run(self, initial_mode: str = "call") -> int:
        # Redirected output is block-buffered, which silently swallows every
        # diagnostic the daemon prints for as long as it keeps running - which
        # is exactly when they are needed.
        with suppress(Exception):
            sys.stdout.reconfigure(line_buffering=True)
            sys.stderr.reconfigure(line_buffering=True)

        path = socket_path()
        server = socket.socket(socket.AF_UNIX)
        if not self._bind(server, path):
            server.close()
            return 1
        server.listen(8)
        server.settimeout(0.5)
        print(f"daemon listening on {path}")

        threading.Thread(target=self._supervise, daemon=True).start()
        self._start_tray()

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
