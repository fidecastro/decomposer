"""The opal-controller adapter: Call mode with the camera's firmware in RAM.

opal-controller (`opalctl serve`) boots the camera's own, unmodified firmware
into RAM with a host-defined graph. The camera then shows the same UVC video
and USB microphone as the shipped product, plus a control channel: manual
focus, white balance, tap-to-focus and effects in Call mode, none of which
the flash firmware can offer.

This module is the only place that knows the controller exists. It starts
the process, waits for it to report ready, relays commands over its Unix
socket and stops it. Frames never pass through here: the engine reads the
camera's V4L2 node exactly as in plain Call mode.

The controller only says `ready` after the firmware's settle time, so nothing
opens the video stream early (see CALL_STREAM_SETTLE_SECONDS in core/model).
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Tuple

from opal_c1.adapters.uvc_cam import UvcBackend
from opal_c1.core.model import (
    CALL_SENSOR_SIZE,
    EFFECTS,
    HYBRID_CALL_CONTROLS,
    SCENES,
    Mode,
    to_sensor_region,
)


@dataclass(frozen=True)
class HybridConfig:
    binary: str
    firmware: Path
    tuning: Path
    socket: Path


def data_dir(env: Mapping[str, str] = os.environ) -> Path:
    base = env.get("XDG_DATA_HOME") or str(Path(env.get("HOME", "~")).expanduser() / ".local/share")
    return Path(base) / "opal-controller"


def hybrid_config(env: Mapping[str, str] = os.environ) -> Optional[HybridConfig]:
    """What is needed to run Call mode through the controller, or None.

    Opt-in: DECOMPOSER_HYBRID=1, with the controller and the camera's
    firmware files installed. It is not the default because a hybrid session
    does not survive its video stream being stopped and started (measured:
    every restart one second after a stop failed, about one in fifteen after
    longer pauses, against none of 42 on the stored firmware). decomposer
    therefore keeps the camera streaming for as long as hybrid Call mode
    runs, light included, which is a choice for the user to make. The
    firmware image and tuning blob come from the user's own camera and are
    never shipped.
    """
    if env.get("DECOMPOSER_HYBRID", "0").strip().lower() not in ("1", "yes", "on", "true"):
        return None
    binary = env.get("DECOMPOSER_OPALCTL") or shutil.which("opalctl")
    if not binary:
        candidate = Path(env.get("HOME", "~")).expanduser() / ".local/bin/opalctl"
        binary = str(candidate) if candidate.is_file() else None
    if not binary:
        return None
    data = data_dir(env)
    firmware, tuning = data / "firmware.mvcmd", data / "camTuning.bin"
    if not (firmware.is_file() and tuning.is_file()):
        return None
    runtime = env.get("XDG_RUNTIME_DIR") or f"/tmp/decomposer-{os.getuid()}"
    return HybridConfig(
        binary=binary, firmware=firmware, tuning=tuning,
        socket=Path(runtime) / "decomposer" / "opal-controller.sock",
    )


class HybridController:
    """One `opalctl serve` process and its command socket."""

    def __init__(
        self,
        config: HybridConfig,
        spawn: Callable[..., subprocess.Popen] = subprocess.Popen,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._spawn = spawn
        self._sleep = sleep
        self._clock = clock
        self._proc: Optional[subprocess.Popen] = None

    # -- the socket ------------------------------------------------------

    def request(self, line: str, timeout: float = 3.0) -> str:
        """One request, one reply line. Raises OSError if nobody answers."""
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect(str(self.config.socket))
            s.sendall(line.encode() + b"\n")
            reply = b""
            while not reply.endswith(b"\n") and len(reply) < 512:
                chunk = s.recv(256)
                if not chunk:
                    break
                reply += chunk
            return reply.decode(errors="replace").strip()
        finally:
            s.close()

    def state(self) -> Optional[str]:
        """The controller's state word, or None if it is not answering.

        Silence is normal for a few seconds: the controller does not answer
        while the boot itself is in progress.
        """
        try:
            reply = self.request("status")
        except OSError:
            return None
        parts = reply.split()
        return parts[1] if len(parts) >= 2 and parts[0] == "ok" else None

    # -- lifecycle -------------------------------------------------------

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def ready(self) -> bool:
        return self.alive() and self.state() == "ready"

    def start(self, timeout: float = 150.0) -> None:
        """Start the controller and wait until the camera is ready to stream.

        The wait covers the camera returning to its flash firmware (up to
        ~20 s after another session lets go), the RAM boot, the controller's
        own retries and the settle time. Raises RuntimeError on failure, with
        the controller stopped.
        """
        self.stop()
        # A controller left behind by a daemon that died still owns the
        # camera; ask it to let go before starting ours.
        try:
            self.request("quit")
            self._sleep(2.0)
        except OSError:
            pass
        self.config.socket.parent.mkdir(parents=True, exist_ok=True)
        argv = [
            self.config.binary, "serve",
            "--fw", str(self.config.firmware),
            "--tuning", str(self.config.tuning),
            "--mic-nodes",
            # decomposer owns the camera and has just stopped any controller
            # of its own, so a camera still running the RAM firmware has no
            # host: its controller died while nothing was streaming, and the
            # firmware carries on without one. The service resets it.
            "--reclaim",
            "--socket", str(self.config.socket),
        ]
        self._proc = self._spawn(argv, stdin=subprocess.DEVNULL)
        deadline = self._clock() + timeout
        while self._clock() < deadline:
            if not self.alive():
                code = self._proc.returncode if self._proc is not None else None
                self._proc = None
                raise RuntimeError(f"opal-controller exited during start (status {code})")
            if self.state() == "ready":
                return
            self._sleep(0.5)
        self.stop()
        raise RuntimeError("opal-controller did not become ready in time")

    def stop(self, timeout: float = 10.0) -> None:
        """End the session. The camera reboots to its flash firmware."""
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            self.request("quit")
        except OSError:
            proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    def reset_camera(self) -> bool:
        """Send the camera back to its bootloader, from where it returns to
        its stored firmware. For a camera left running the RAM firmware by a
        controller that died: it does not go back by itself."""
        try:
            done = subprocess.run(
                [self.config.binary, "reset"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return done.returncode == 0

    # -- commands --------------------------------------------------------

    def command(self, line: str) -> None:
        """Send one control line. ValueError if the controller refuses it,
        OSError if it does not answer."""
        try:
            reply = self.request(line)
        except OSError as e:
            raise OSError(f"opal-controller is not answering: {e}") from e
        if not reply.startswith("ok"):
            raise ValueError(reply.removeprefix("error ").strip() or "refused")

    def telemetry(self) -> dict:
        """What the camera last reported about itself: lens position,
        exposure time and ISO, read from its frames. Empty if unknown."""
        try:
            reply = self.request("status")
        except OSError:
            return {}
        out = {}
        for word in reply.split()[2:]:
            key, _, value = word.partition("=")
            try:
                out[key] = int(value)
            except ValueError:
                pass
        return out


class HybridBackend(UvcBackend):
    """Call mode through the controller.

    The UVC controls of plain Call mode, plus everything Studio offers over
    the firmware's control channel: focus, white balance, effects, scenes
    and the focus and exposure regions. The controller keeps the standing
    settings and re-sends them whenever the video stream restarts, because
    the firmware ignores controls while the sensor is idle.
    """

    mode = Mode.CALL
    extra_controls = HYBRID_CALL_CONTROLS

    def __init__(
        self,
        controller: HybridController,
        frame_size: Callable[[], Tuple[int, int]] = lambda: (1920, 1080),
        **kw,
    ) -> None:
        super().__init__(**kw)
        self._controller = controller
        self._frame_size = frame_size
        # The firmware cannot be asked what focus and white balance are set
        # to; a fresh session starts with both automatic.
        self._requested = {"focus": -1, "wb": -1}
        # True after a tap aimed focus and metering at one spot.
        self._aimed = False

    def _line(self, key: str, value) -> str:
        if key in ("focus", "wb"):
            v = int(value)
            return f"{key} auto" if v < 0 else f"{key} {v}"
        if key in ("effect", "scene"):
            name = str(value).strip().lower()
            known = EFFECTS if key == "effect" else SCENES
            if name not in known:
                raise ValueError(f"unknown {key} {value!r}. Known: {', '.join(known)}")
            return f"{key} {name}"
        if key in ("af_region", "ae_region"):
            x, y, w, h = to_sensor_region(value, self._frame_size())
            return f"{key[:2]}-region {x} {y} {w} {h}"
        raise ValueError(f"unknown control {key!r}")

    def apply_controls(self, values: Mapping[str, object]) -> Tuple[dict, dict]:
        applied, refused = super().apply_controls(values)
        for key in ("focus", "wb", "effect", "scene", "ae_region", "af_region"):
            value = values.get(key)
            if value is None:
                continue
            try:
                self._controller.command(self._line(key, value))
            except (ValueError, TypeError, OSError) as e:
                refused[key] = str(e)
                continue
            if key in ("focus", "wb"):
                value = -1 if int(value) < 0 else int(value)
                self._requested[key] = value
                if key == "focus" and value < 0 and self._aimed:
                    # "Auto" undoes a tap: the controller has gone back to
                    # focusing on the centre, so meter the whole frame too.
                    self._aimed = False
                    try:
                        w, h = CALL_SENSOR_SIZE
                        self._controller.command(f"ae-region 0 0 {w} {h}")
                    except (ValueError, OSError):
                        pass
            elif key in ("effect", "scene"):
                value = str(value).strip().lower()
            else:
                value = [int(v) for v in value]
                self._aimed = True
                if key == "af_region":
                    # Aiming autofocus takes the lens back from manual.
                    self._requested["focus"] = -1
            applied[key] = value
        return applied, refused

    def replay_controls(self, values: Mapping[str, object]) -> Tuple[dict, dict]:
        """Re-apply remembered requests after the engine restarted.

        Unlike a firmware reboot, an engine restart leaves the controller's
        session - and the settings it holds - in place. Automatic focus and
        white balance are where it already is, and asking for automatic
        focus again would start a visible lens sweep for nothing.
        """
        needed = {
            key: value for key, value in values.items()
            if not (key in ("focus", "wb") and int(value) < 0)
        }
        skipped = {key: -1 for key in values if key not in needed}
        applied, refused = self.apply_controls(needed) if needed else ({}, {})
        applied.update(skipped)
        return applied, refused

    def read_controls(self) -> dict:
        out = super().read_controls()
        out.update(self._requested)
        return out
