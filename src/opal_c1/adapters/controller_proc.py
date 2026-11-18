"""The opal-controller adapter: Call mode with the camera's firmware in RAM.

opal-controller (`opalctl serve`) boots the camera's own, unmodified firmware
into RAM with a host-defined graph. The camera then shows the same UVC video
and USB microphone as the shipped product, plus a control channel: manual
focus in Call mode, which the flash firmware cannot offer.

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
from opal_c1.core.model import HYBRID_CALL_CONTROLS, Mode


@dataclass(frozen=True)
class HybridConfig:
    binary: str
    firmware: Path
    tuning: Path
    detection: Optional[Path]
    socket: Path


def data_dir(env: Mapping[str, str] = os.environ) -> Path:
    base = env.get("XDG_DATA_HOME") or str(Path(env.get("HOME", "~")).expanduser() / ".local/share")
    return Path(base) / "opal-controller"


def hybrid_config(env: Mapping[str, str] = os.environ) -> Optional[HybridConfig]:
    """What is needed to run Call mode through the controller, or None.

    Hybrid is the default whenever the controller and the camera's firmware
    files are installed; DECOMPOSER_HYBRID=0 turns it off. The firmware image
    and tuning blob come from the user's own camera and are never shipped.
    """
    if env.get("DECOMPOSER_HYBRID", "1").strip().lower() in ("0", "no", "off", "false"):
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
    detection = data / "detection.blob"
    runtime = env.get("XDG_RUNTIME_DIR") or f"/tmp/decomposer-{os.getuid()}"
    return HybridConfig(
        binary=binary, firmware=firmware, tuning=tuning,
        detection=detection if detection.is_file() else None,
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
            "--socket", str(self.config.socket),
        ]
        if self.config.detection is not None:
            argv += ["--detection-blob", str(self.config.detection)]
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

    # -- commands --------------------------------------------------------

    def _command(self, line: str) -> None:
        try:
            reply = self.request(line)
        except OSError as e:
            raise OSError(f"opal-controller is not answering: {e}") from e
        if not reply.startswith("ok"):
            raise ValueError(reply.removeprefix("error ").strip() or "refused")

    def set_focus(self, lens_position: int) -> None:
        self._command(f"focus {int(lens_position)}")

    def set_autofocus(self) -> None:
        self._command("autofocus")


class HybridBackend(UvcBackend):
    """Call mode through the controller: UVC controls plus manual focus."""

    mode = Mode.CALL
    extra_controls = HYBRID_CALL_CONTROLS

    def __init__(self, controller: HybridController, **kw) -> None:
        super().__init__(**kw)
        self._controller = controller

    def apply_controls(self, values: Mapping[str, object]) -> Tuple[dict, dict]:
        applied, refused = super().apply_controls(values)
        focus = values.get("focus")
        if focus is not None:
            try:
                v = int(focus)
                if v < 0:
                    self._controller.set_autofocus()
                    applied["focus"] = -1
                else:
                    self._controller.set_focus(v)
                    applied["focus"] = v
            except (ValueError, TypeError, OSError) as e:
                refused["focus"] = str(e)
        return applied, refused
