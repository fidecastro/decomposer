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

import copy
import errno
import json
import os
import queue
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
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
from opal_c1.v4l2 import output_ready

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
    for candidate in (here / "luts", Path("/usr/share/decomposer/luts")):
        if candidate.is_dir():
            return candidate
    return None


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

# Undo/redo is intentionally a short, in-memory history of live adjustments.
# It does not try to reverse hardware restarts, power changes, or file
# operations such as deleting a preset. Those commands form a barrier and
# discard stale snapshots whose camera/backend assumptions may no longer be
# true.
UNDO_LIMIT = 32
UNDO_COALESCE_SECONDS = 0.8
UNDOABLE_COMMANDS = frozenset({
    "set_look", "set_model_strength", "set_blur", "set_background",
    "set_overlay", "set_clahe", "set_zoom", "set_mirror", "set_camera",
    "preset_load",
})
UNDO_BARRIER_COMMANDS = frozenset({
    "set_mode", "set_models", "set_power", "set_fps", "set_resolution",
    "preset_delete",
})
UNDO_STATE_FIELDS = (
    "active_preset", "look", "strength", "look_strength",
    "overlay", "overlay_x", "overlay_y", "overlay_w", "overlay_h",
    "overlay_opacity", "blur", "blur_style", "background",
    "mirror_h", "mirror_v", "zoom", "pan_x", "pan_y", "clahe",
)

FFMPEG_PATH = Path("/usr/bin/ffmpeg")
CAPTURE_STDERR_MAX = 8192


def _run_bounded(cmd: list[str], timeout: float, cap: int = 8192) -> dict:
    """Run a producer with a real deadline and capped retained stderr."""
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    tail = bytearray()

    def drain() -> None:
        assert proc.stderr is not None
        for chunk in iter(lambda: proc.stderr.read(4096), b""):
            tail.extend(chunk)
            if len(tail) > cap:
                del tail[:-cap]

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        with suppress(Exception):
            proc.wait(timeout=5)
    finally:
        reader.join(timeout=5)
        with suppress(Exception):
            if proc.stderr is not None:
                proc.stderr.close()
    return {
        "code": -1 if timed_out else proc.returncode,
        "stderr": bytes(tail).decode("utf-8", "replace"),
    }


def _photo_target() -> tuple[str, Path]:
    """Create a private temporary PNG and its final Pictures destination."""
    out = Path.home() / "Pictures" / "decomposer"
    if out.is_symlink():
        raise RuntimeError(f"{out} is a symlink; refusing to capture there")
    out.mkdir(parents=True, exist_ok=True)
    if out.is_symlink() or not out.is_dir():
        raise RuntimeError(f"{out} is not a real directory")
    final = out / (time.strftime("photo-%Y%m%d-%H%M%S") + ".png")
    fd, tmp = tempfile.mkstemp(dir=out, prefix=".part-", suffix=".png")
    os.close(fd)
    return tmp, final

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



def models_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "decomposer" / "models.json"


def preset_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    d = Path(base) / "decomposer" / "presets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def preset_state_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "decomposer" / "preset-state.json"


PRESET_JSON_MAX = 128 * 1024
PRESET_STATE_MAX = 8 * 1024


def _read_regular_json(path: Path, maximum: int):
    """Read one bounded, owner-controlled regular file without following links."""
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as e:
        raise ValueError(f"cannot safely read {path}: {e.strerror}") from e
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError(f"refusing non-regular or foreign-owned file {path}")
        if info.st_size > maximum:
            raise ValueError(f"{path} is larger than {maximum} bytes")
        chunks, retained = [], 0
        while True:
            chunk = os.read(fd, min(16 * 1024, maximum + 1 - retained))
            if not chunk:
                break
            chunks.append(chunk)
            retained += len(chunk)
            if retained > maximum:
                raise ValueError(f"{path} is larger than {maximum} bytes")
        return json.loads(b"".join(chunks).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as e:
        raise ValueError(f"invalid JSON in {path}: {e}") from e
    finally:
        os.close(fd)


def _atomic_write_json(path: Path, value, maximum: int) -> None:
    """Publish a private JSON file atomically within a pinned directory."""
    payload = (json.dumps(value, indent=2) + "\n").encode("utf-8")
    if len(payload) > maximum:
        raise ValueError(f"JSON for {path} is larger than {maximum} bytes")
    path.parent.mkdir(parents=True, exist_ok=True)
    dir_flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0)
    dir_fd = os.open(path.parent, dir_flags)
    temporary = f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    fd = None
    try:
        try:
            current = os.stat(path.name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if current is not None and (
            not stat.S_ISREG(current.st_mode) or current.st_uid != os.getuid()
        ):
            raise ValueError(f"refusing to replace unsafe file {path}")
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
            dir_fd=dir_fd,
        )
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(temporary, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.fsync(dir_fd)
    finally:
        if fd is not None:
            os.close(fd)
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=dir_fd)
        os.close(dir_fd)


def _unlink_regular(path: Path) -> None:
    """Delete only an owned regular entry from the pinned preset directory."""
    dir_flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0)
    dir_fd = os.open(path.parent, dir_flags)
    try:
        info = os.stat(path.name, dir_fd=dir_fd, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError(f"refusing to delete unsafe preset file {path}")
        os.unlink(path.name, dir_fd=dir_fd)
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _preset_path(name: str, mode: str) -> Path:
    """Resolve a preset name to a file. Validation lives in the pure core.

    Presets are namespaced by mode: the two firmwares expose different
    controls, so a Studio preset is not meaningfully loadable in Call. A
    legacy flat-file preset (pre-namespacing) is still found for loading.
    """
    name = preset_codec.validate_name(name)
    namespaced = preset_dir() / mode / f"{name}.json"
    if namespaced.exists() or namespaced.is_symlink():
        return namespaced
    legacy = preset_dir() / f"{name}.json"
    if legacy.exists() or legacy.is_symlink():
        return legacy
    return namespaced


@dataclass
class State:
    mode: str = Mode.CALL.value
    active_preset: Optional[str] = None
    look: str = "none"
    strength: float = 1.0
    # Intensity is remembered per look. Composer's filters carry their own
    # intensity, so a strength dialled in for noir should not follow you to G1.
    look_strength: dict = field(default_factory=dict)
    width: int = 1920
    height: int = 1080
    output: str = "/dev/video10"
    normal_output: str = "/dev/video11"
    overlay: Optional[str] = None
    blur: float = 0.0
    blur_style: int = 0
    background: Optional[str] = None
    # The user's model chain: [{"path", "device", "strength"}, ...].
    # Membership and device changes restart the engine; strengths are live.
    models: list = field(default_factory=list)
    overlay_x: int = 0
    overlay_y: int = 0
    overlay_w: int = 0
    overlay_h: int = 0
    overlay_opacity: float = 1.0
    mirror_h: bool = False
    mirror_v: bool = False
    zoom: float = 1.0
    pan_x: float = 0.0
    pan_y: float = 0.0
    clahe: float = 0.0
    in_width: int = 0
    in_height: int = 0
    running: bool = False
    frames: int = 0
    error: Optional[str] = None
    # History, not a live fault: the last notable recovery or incident.
    last_event: Optional[str] = None
    # Transient user-facing announcement ("camera connected...").
    notice: Optional[str] = None
    controls: dict = field(default_factory=dict)


class Daemon:
    def __init__(
        self, output="/dev/video10", normal_output="/dev/video11",
        width=1920, height=1080, fps=30.0,
        tray_enabled: bool = False,
        default_strength: float = DEFAULT_STRENGTH,
        in_width: int = 0, in_height: int = 0,
        seg_model: Optional[str] = None, seg_device: Optional[str] = None,
    ):
        self.state = State(
            output=output, normal_output=normal_output,
            width=width, height=height,
            in_width=in_width, in_height=in_height,
        )
        self.tray_enabled = tray_enabled
        self.default_strength = max(0.0, min(1.0, float(default_strength)))
        self.state.strength = self.default_strength
        self.fps = fps
        self.lock = threading.RLock()
        # The engine is reachable only through the EngineHandle chokepoint;
        # created lazily so a missing binary fails with advice, not at import.
        self._engine = None
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
        # Segmentation choices are engine-startup facts, not live state.
        self.seg_model = seg_model
        self.seg_device = seg_device
        # The model chain persists across daemon restarts; a saved model
        # whose file has gone missing is flagged and bypassed, not dropped -
        # the user's configuration outlives a moved file.
        try:
            saved = json.loads(models_file().read_text())
            if isinstance(saved, list):
                for m in saved:
                    if isinstance(m, dict) and m.get("path"):
                        self.state.models.append({
                            "path": str(m["path"]),
                            "device": m.get("device", "cpu"),
                            "strength": max(0.0, min(1.0, float(m.get("strength", 1.0)))),
                        })
        except (OSError, ValueError):
            pass
        self._mark_missing_models()
        self._shutdown = threading.Event()
        # Set by the USB hotplug watcher; interrupts supervisor holds so a
        # replug recovers in seconds instead of riding out a blind wait.
        self._camera_event = threading.Event()
        self.restarts = 0
        # Transitions are executed by exactly one worker thread, in order.
        # Clients and the supervisor submit requests; the ledger (pure core)
        # decides admission. Exclusivity is structural, not a mutex.
        self._ledger = transitions.Ledger(current=Mode(self.state.mode))
        self._requests: queue.Queue = queue.Queue()
        # status() must never touch hardware: a poller keeps this snapshot
        # fresh and status reads it. See _status_poller.
        self._snapshot = {"controls": {}, "mode_actual": None}
        self._undo_history: list[dict] = []
        self._redo_history: list[dict] = []
        self._preset_cache: Optional[list] = None
        self._looks_cache: list = available_looks()
        self._last_presets: dict[str, str] = {}
        selection_file = preset_state_file()
        if selection_file.exists() or selection_file.is_symlink():
            try:
                self._last_presets = preset_codec.decode_last_used(
                    _read_regular_json(selection_file, PRESET_STATE_MAX)
                )
            except ValueError as e:
                print(f"preset selection ignored: {e}")

    # -- engine ---------------------------------------------------------

    def _handle(self):
        """The one EngineHandle, created on first use."""
        from opal_c1.adapters.engine_proc import EngineHandle

        if self._engine is None:
            binary = find_engine()
            if binary is None:
                raise RuntimeError(
                    "decomposer-engine not found. Build it with: "
                    "cd engine && cargo build --release"
                )
            self._engine = EngineHandle(
                binary, runtime_dir() / "engine.sock", self.preview_sock
            )
        return self._engine

    def _engine_config(self, from_stdin: bool) -> model.EngineConfig:
        """Desired engine state, assembled from daemon state in one place.

        The input node is discovered at call time: its number changes across
        re-enumerations, which is why the config is rebuilt per start attempt.
        """
        self._mark_missing_models()
        st = self.state
        return model.EngineConfig(
            input="-" if from_stdin else (camera_video_node() or "/dev/video0"),
            output=st.output,
            # Handed over only when the node answers as a video output: a
            # missing node, or a real camera that landed on /dev/video11,
            # leaves the engine publishing SEND alone rather than failing.
            normal_output=(
                st.normal_output if output_ready(st.normal_output) else None
            ),
            width=st.width,
            height=st.height,
            look=st.look,
            strength=st.strength,
            flip=self._flip_bits(),
            overlay=st.overlay,
            overlay_x=st.overlay_x, overlay_y=st.overlay_y,
            overlay_w=st.overlay_w, overlay_h=st.overlay_h,
            overlay_opacity=st.overlay_opacity,
            in_width=st.in_width, in_height=st.in_height,
            zoom=st.zoom, pan_x=st.pan_x, pan_y=st.pan_y,
            clahe=st.clahe,
            blur=st.blur, blur_style=st.blur_style,
            background=st.background,
            seg_model=self.seg_model,
            # "auto"/"camera" are daemon-side placements; the engine's flag
            # only knows host devices, and its bundled model stays as the
            # fallback that yields whenever camera masks flow.
            seg_device=(
                self.seg_device if self.seg_device in ("cpu", "cuda") else None
            ),
            models=tuple(
                (m["path"], m["device"])
                for m in st.models if not m.get("missing")
            ),
            model_strengths=tuple(
                m["strength"] for m in st.models if not m.get("missing")
            ),
            lut_dir=str(lut_dir()) if lut_dir() else None,
        )

    def _engine_up(self, from_stdin: bool, attempts: int = 3) -> None:
        """(Re)start the engine to match current state, retrying a device
        that is not quite ready.

        Straight after a mode switch the camera can accept an open and then
        fail to stream. Retrying is more reliable than trying to predict how
        long the hardware needs.
        """
        handle = self._handle()
        last = None
        for attempt in range(1, attempts + 1):
            try:
                handle.stop()
                handle.start(self._engine_config(from_stdin))
                return
            except RuntimeError as e:
                last = e
                handle.stop()
                if attempt < attempts:
                    time.sleep(2.0 * attempt)
        raise RuntimeError(f"engine would not start after {attempts} attempts: {last}")

    def _flip_bits(self) -> int:
        """bit 0 mirrors horizontally, bit 1 vertically; both is a 180 turn."""
        return (1 if self.state.mirror_h else 0) | (2 if self.state.mirror_v else 0)

    def _sync_engine(self) -> None:
        """Make the running engine match state's live fields.

        The handle diffs against what the engine already has and sends only
        the protocol lines for what changed — and apply_live can never
        restart, so a look change cannot take /dev/video10 down with it.
        """
        engine = self._engine
        if engine is None:
            return
        with self.lock:
            st = self.state
            live = dict(
                look=st.look, strength=st.strength, flip=self._flip_bits(),
                overlay=st.overlay,
                overlay_x=st.overlay_x, overlay_y=st.overlay_y,
                overlay_w=st.overlay_w, overlay_h=st.overlay_h,
                overlay_opacity=st.overlay_opacity,
                zoom=st.zoom, pan_x=st.pan_x, pan_y=st.pan_y,
                clahe=st.clahe,
                blur=st.blur, blur_style=st.blur_style,
                background=st.background,
                model_strengths=tuple(
                    m["strength"] for m in st.models if not m.get("missing")
                ),
            )
        with suppress(Exception):
            engine.apply_live(**live)

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
        self._sync_engine()
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

        self._sync_engine()
        return self.status()

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
        stdin = self._engine.stdin if self._engine else None
        # On-VPU masks ride the engine's external-producer port. The engine
        # then treats the camera exactly like any other mask client: its
        # bundled host model yields, user models merge.
        mask_sock: Optional[socket.socket] = None
        read_mask = getattr(source, "try_read_mask", lambda: None)
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
                mask = read_mask()
                if mask is not None:
                    data, w, h = mask
                    try:
                        if mask_sock is None:
                            mask_sock = socket.socket(socket.AF_UNIX)
                            mask_sock.settimeout(1.0)
                            mask_sock.connect(
                                str(runtime_dir() / "mask.sock")
                            )
                            mask_sock.sendall(
                                w.to_bytes(4, "little")
                                + h.to_bytes(4, "little")
                            )
                        mask_sock.sendall(data)
                    except OSError:
                        # Engine restarting or socket gone: drop and retry
                        # on a later frame. Masks are advisory, frames are not.
                        with suppress(Exception):
                            if mask_sock is not None:
                                mask_sock.close()
                        mask_sock = None
        except (BrokenPipeError, ValueError, OSError):
            pass
        except Exception as e:  # surface, do not die silently
            with self.lock:
                self.state.error = f"pump: {e}"
        finally:
            with suppress(Exception):
                if mask_sock is not None:
                    mask_sock.close()

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
        # Stop the engine before joining the pump: a pump blocked in
        # stdin.write can only exit once the pipe dies, and EngineHandle.stop
        # guarantees that (it kills the process before touching stdin, so the
        # close cannot hang on flushing into a wedged reader).
        engine = self._engine
        if engine is not None:
            engine.stop()
        if self._pump is not None:
            self._pump.join(timeout=5)
            if self._pump.is_alive():
                # Should not happen now the pump polls, but closing the device
                # regardless would be worse than saying so.
                print("warning: frame pump did not stop; not closing the device")
                with self.lock:
                    self.state.error = "frame pump would not stop"
                self._pump = None
                return
            self._pump = None
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
        """Only the transition worker calls this. The state lock is taken for
        mutations only — a mode entry spends seconds in hardware waits, and
        holding the lock across them froze every status() call meanwhile."""
        from opal_c1.adapters.uvc_cam import UvcBackend

        self._teardown()
        with self.lock:
            if self.state.mode != Mode.CALL.value:
                self.state.active_preset = None
            self.state.mode = Mode.CALL.value
            self.state.error = None
        # Leaving Studio mode reboots the camera; /dev/video0 takes ~14s.
        if current_mode() is not Mode.CALL:
            wait_until_capturable(timeout=45)
        else:
            wait_until_capturable(timeout=10)
        backend = UvcBackend()
        backend.attach()
        self._backend = backend
        self._engine_up(from_stdin=False)
        with self.lock:
            self.state.running = True
            self.state.frames = 0
        self._replay_sticky(backend)

    def enter_parked(self) -> None:
        """Feed off, honestly: hold the camera in its Studio personality.

        The hardware cannot power down, and the Opal firmware keeps the
        MICROPHONE alive on the bus whether or not anyone streams video -
        so "off" must not rest there. An idle XLink session pins the
        DepthAI firmware instead: no UVC, no UAC, no frames. Only the
        transition worker calls this."""
        from opal_c1.adapters.depthai_cam import XLinkBackend

        self._teardown()
        with self.lock:
            self.state.running = False
            self.state.error = None
        # A minimal session: tiny output nobody reads (the device drops
        # frames itself once the queue fills), no mask model.
        backend = XLinkBackend(width=640, height=360, fps=5.0, mask_model=False)
        backend.attach()
        self._backend = backend
        with self.lock:
            self.state.notice = (
                "camera off - parked on Studio firmware so the microphone "
                "is off too"
            )

    def enter_studio(self) -> None:
        """Only the transition worker calls this; see enter_call on locking."""
        from opal_c1.adapters.depthai_cam import XLinkBackend

        self._teardown()
        with self.lock:
            if self.state.mode != Mode.STUDIO.value:
                self.state.active_preset = None
            self.state.mode = Mode.STUDIO.value
            self.state.error = None
        # The device delivers the capture size, which may exceed the output.
        backend = XLinkBackend(
            width=self.state.in_width or self.state.width,
            height=self.state.in_height or self.state.height,
            fps=self.fps,
            # The camera's own VPU carries the default person mask unless
            # the user pinned segmentation to the host. User ONNX models
            # keep running host-side either way and merge with it.
            mask_model=self.seg_device in (None, "auto", "camera"),
        )
        backend.attach()
        self._backend = backend
        self._engine_up(from_stdin=True)
        self._pump_stop.clear()
        self._pump = threading.Thread(target=self._pump_frames, daemon=True)
        self._pump.start()
        with self.lock:
            self.state.running = True
            self.state.frames = 0
        self._replay_sticky(backend)

    class _Request:
        def __init__(self, want: Mode):
            self.want = want
            self.done = threading.Event()
            self.error: Optional[BaseException] = None

    def request_transition(
        self, want: Mode, *, enforce_guard: bool, wait: bool = True,
        timeout: float = 120.0,
    ) -> None:
        """Submit a mode entry to the single transition worker.

        Client switches are guarded — rate-limited, and rejected rather than
        queued while another transition runs, because a queued surprise
        firmware reboot is worse than a refusal. Supervisor re-entries bypass
        the rate limit: recovery must not be blocked by the thing it is
        recovering from.
        """
        now = time.monotonic()
        with self.lock:
            busy = self._ledger.in_progress or not self._requests.empty()
            if enforce_guard:
                if busy:
                    raise RuntimeError("a mode transition is already in progress")
                if want is not None:
                    decision = transitions.evaluate_switch(self._ledger, want, now)
                    if not decision.allowed:
                        raise RuntimeError(decision.reason)
            if want is not None and want is not self._ledger.current:
                self._ledger.last_switch_at = now
            request = self._Request(want)
            self._requests.put(request)
        if not wait:
            return
        if not request.done.wait(timeout):
            raise RuntimeError(
                f"transition to {want.value} still running after {timeout:.0f}s"
            )
        if request.error is not None:
            raise request.error

    def _transition_worker(self) -> None:
        """The one thread that ever enters a mode. Interleaved teardown and
        startup — two switches racing on different threads — is structurally
        impossible now, not merely locked away."""
        while not self._shutdown.is_set():
            try:
                request = self._requests.get(timeout=0.5)
            except queue.Empty:
                continue
            with self.lock:
                self._ledger.in_progress = True
            try:
                if request.want is None:
                    self.enter_parked()
                elif request.want is Mode.CALL:
                    self.enter_call()
                else:
                    self.enter_studio()
            except BaseException as e:
                request.error = e
                # The mode is still what was asked for: leave `running` set so
                # the supervisor keeps working toward it instead of going
                # dormant after a switch that failed mid-camera-reboot. A
                # failed PARK is the opposite: stay dormant, report why.
                with self.lock:
                    self.state.running = request.want is not None
                    self.state.error = f"{type(e).__name__}: {e}"
            finally:
                with self.lock:
                    self._ledger.in_progress = False
                    self._ledger.current = Mode(self.state.mode)
                # Presets are namespaced by mode; the list the panel shows
                # changes with the firmware.
                with suppress(Exception):
                    self._refresh_presets()
                # The switch's own re-enumerations set the camera event; a
                # stale one would wake the next supervisor hold instantly.
                self._camera_event.clear()
                request.done.set()

    def set_mode(self, mode: str) -> dict:
        self.request_transition(Mode(mode), enforce_guard=True)
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
                if strength is None:
                    self.state.strength = self.state.look_strength.get(
                        look, self.default_strength
                    )
            if strength is not None:
                value = max(0.0, min(1.0, float(strength)))
                self.state.strength = value
                self.state.look_strength[self.state.look] = value
        self._sync_engine()
        return self.status()

    def set_zoom(self, zoom=None, pan_x=None, pan_y=None) -> dict:
        """Digital zoom and pan, applied in the shader at no cost.

        Lossless up to the capture/output ratio (run the daemon with
        --in-width 3840 --in-height 2160 for true 2x); upscaling beyond.
        """
        with self.lock:
            if zoom is not None:
                self.state.zoom = max(1.0, min(8.0, float(zoom)))
            if pan_x is not None:
                self.state.pan_x = max(-1.0, min(1.0, float(pan_x)))
            if pan_y is not None:
                self.state.pan_y = max(-1.0, min(1.0, float(pan_y)))
        self._sync_engine()
        return self.status()

    def set_resolution(
        self, width, height, in_width=0, in_height=0
    ) -> dict:
        """Change the published (and optionally capture) resolution.

        These are restart fields: the engine is re-entered, and in Studio the
        camera session reboots with it. The loopback keeps its old format
        while any consumer holds the node, so applications must reconnect to
        pick up the new size - the engine will say so if one is pinning it.
        """
        with self.lock:
            mode = Mode(self.state.mode)
            allowed = {
                (r[1], r[2]) for r in model.resolutions_for(mode)
            }
            if (int(width), int(height)) not in allowed:
                raise ValueError(
                    f"{width}x{height} is not a {mode.value}-mode resolution"
                )
            if self._ledger.in_progress or not self._requests.empty():
                raise RuntimeError("a mode transition is already in progress")
            self.state.width = int(width)
            self.state.height = int(height)
            self.state.in_width = int(in_width or 0)
            self.state.in_height = int(in_height or 0)
            # The frame rate rides the geometry: entering a config whose
            # range excludes the current fps silently failing the camera
            # is worse than clamping and saying so.
            self.fps = model.clamp_fps(
                mode, self.state.width, self.state.height, self.fps
            )
        self.request_transition(mode, enforce_guard=False)
        return self.status()

    def set_fps(self, fps) -> dict:
        """Change the capture frame rate. Studio only: Call mode's UVC
        firmware advertises exactly 30 fps. A Studio change re-enters the
        mode, which reboots the camera's firmware."""
        with self.lock:
            mode = Mode(self.state.mode)
            if mode is Mode.CALL:
                raise RuntimeError(
                    "call mode is fixed at 30 fps by the Opal firmware; "
                    "frame rate is adjustable in Studio mode"
                )
            lo, hi = model.fps_limits(mode, self.state.width, self.state.height)
            wanted = float(fps)
            clamped = model.clamp_fps(
                mode, self.state.width, self.state.height, wanted
            )
            if clamped == self.fps:
                # Already there (possibly after clamping): a reboot would
                # buy nothing.
                out = self.status()
                if clamped != wanted:
                    out["notes"] = [f"already at {clamped} (range {lo}-{hi})"]
                return out
            if self._ledger.in_progress or not self._requests.empty():
                raise RuntimeError("a mode transition is already in progress")
            self.fps = clamped
        out_note = (
            None if clamped == wanted
            else f"clamped to {clamped} (range {lo}-{hi})"
        )
        self.request_transition(mode, enforce_guard=False)
        out = self.status()
        if out_note:
            out["notes"] = [out_note]
        return out

    def set_clahe(self, strength) -> dict:
        """Local contrast (CLAHE) on the GPU. 0 disables and skips the passes."""
        with self.lock:
            self.state.clahe = max(0.0, min(1.0, float(strength)))
        self._sync_engine()
        return self.status()

    def set_blur(self, strength=None, style=None) -> dict:
        """Background blur. Needs a mask, so the first frames after enabling
        may pass through unblurred while segmentation warms up.

        `style`: "smooth" averages the background away; "bokeh" weights the
        disc taps by their highlights, blooming bright points into balls."""
        with self.lock:
            if strength is not None:
                self.state.blur = max(0.0, min(1.0, float(strength)))
            if style is not None:
                names = {"smooth": 0, "bokeh": 1, 0: 0, 1: 1}
                if style not in names:
                    raise ValueError(f"unknown blur style {style!r}")
                self.state.blur_style = names[style]
        self._sync_engine()
        return self.status()

    def set_background(self, path=None) -> dict:
        """Replace the background with an image, or None to go back to blur
        (or to nothing, if blur is 0)."""
        with self.lock:
            if path is None:
                self.state.background = None
            else:
                resolved = Path(path).expanduser().resolve()
                if not resolved.is_file():
                    raise FileNotFoundError(f"no such background image: {resolved}")
                self.state.background = str(resolved)
        self._sync_engine()
        return self.status()

    def _mark_missing_models(self) -> None:
        """Stamp each chain entry with whether its file exists right now.

        Called at startup, on chain edits, and per engine start - never from
        status(), which must stay IO-free."""
        with self.lock:
            for m in self.state.models:
                m["missing"] = not Path(m["path"]).is_file()

    def _save_models(self) -> None:
        data = [
            {k: m[k] for k in ("path", "device", "strength")}
            for m in self.state.models
        ]
        models_file().parent.mkdir(parents=True, exist_ok=True)
        models_file().write_text(json.dumps(data, indent=2) + "\n")

    def set_models(self, models) -> dict:
        """Replace the model chain. Membership and devices are session
        facts - the ONNX sessions are built at engine startup - so this
        re-enters the current mode, like a resolution change."""
        cleaned = []
        for m in models or []:
            path = Path(str(m.get("path", ""))).expanduser().resolve()
            device = m.get("device") or "cpu"
            if device not in ("cpu", "cuda"):
                raise ValueError(f"unknown device {device!r} (cpu or cuda)")
            strength = max(0.0, min(1.0, float(m.get("strength", 1.0))))
            # A missing file is flagged and bypassed, never an error: the
            # entry survives so the model comes back when the file does.
            cleaned.append({
                "path": str(path), "device": device, "strength": strength,
                "missing": not path.is_file(),
            })
        with self.lock:
            if self._ledger.in_progress or not self._requests.empty():
                raise RuntimeError("a mode transition is already in progress")
            self.state.models = cleaned
            mode = Mode(self.state.mode)
        self._save_models()
        self.request_transition(mode, enforce_guard=False)
        return self.status()

    def set_model_strength(self, index, strength) -> dict:
        """Live per-model strength: a protocol line, never a restart."""
        with self.lock:
            models = self.state.models
            i = int(index)
            if not 0 <= i < len(models):
                raise IndexError(f"no model at index {i}")
            models[i]["strength"] = max(0.0, min(1.0, float(strength)))
        self._sync_engine()
        return self.status()

    def set_power(self, on: bool) -> dict:
        """Feed on/off without stopping the daemon.

        Off PARKS the camera on Studio firmware - the only resting state
        where the microphone is genuinely off - and sets running=False,
        the supervisor's dormancy flag. On re-enters the remembered mode.
        Parking from Call reboots the firmware; the panel says so first."""
        with self.lock:
            if self._ledger.in_progress or not self._requests.empty():
                raise RuntimeError("a mode transition is already in progress")
            mode = Mode(self.state.mode)
            currently = self.state.running
        if on and not currently:
            self.request_transition(mode, enforce_guard=False)
        elif not on and currently:
            self.request_transition(None, enforce_guard=False)
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
            # Publish accepted hardware values immediately. The poller will
            # refresh them later, but undo/redo snapshots taken at this IPC
            # boundary must not capture the pre-request readback.
            self._snapshot.setdefault("controls", {}).update(applied)
            for key, value in applied.items():
                if key in model.STICKY_CONTROLS:
                    self._sticky[key] = value
        out = self.status()
        out["applied"] = applied
        if refused:
            out["refused"] = refused
        return out

    # -- undo / redo -----------------------------------------------------

    def _undo_snapshot(self) -> dict:
        """Capture only state that can be restored live and safely.

        Restart-level configuration is deliberately absent.  Model membership
        is a barrier, so only the live strengths need to be retained here.

        For the camera, `sticky` is what gets restored: the user's explicit
        requests, with absence meaning automatic. The hardware readback is
        kept only as the previous value of plain sliders that have no
        automatic mode. It is never re-applied wholesale - under
        auto-exposure the readback carries exposure/iso, and re-sending those
        would pin the camera to manual at whatever it happened to report.
        """
        with self.lock:
            readback = self._snapshot.get("controls") or {}
            return {
                "state": {
                    name: copy.deepcopy(getattr(self.state, name))
                    for name in UNDO_STATE_FIELDS
                },
                "models": [
                    (m.get("path"), m.get("device"), m.get("strength", 1.0))
                    for m in self.state.models
                ],
                "controls": {
                    key: copy.deepcopy(value)
                    for key, value in readback.items()
                    if key in model.STICKY_CONTROLS
                },
                "sticky": copy.deepcopy(self._sticky),
            }

    @staticmethod
    def _undo_relevant(snapshot: dict) -> tuple:
        """The parts of a snapshot whose change makes a request undoable.

        The hardware readback is left out: it drifts on its own (auto
        exposure hunting between two snapshots) and must not create
        history entries for requests that changed nothing.
        """
        return snapshot["state"], snapshot["models"], snapshot["sticky"]

    @staticmethod
    def _undo_key(req: dict) -> tuple:
        cmd = str(req.get("cmd"))
        if cmd == "set_camera":
            return (cmd, *sorted((req.get("values") or {}).keys()))
        if cmd == "set_overlay":
            return (cmd, *sorted((req.get("values") or {}).keys()))
        if cmd == "set_zoom":
            return (cmd, *(k for k in ("zoom", "pan_x", "pan_y") if k in req))
        if cmd == "set_look":
            return (cmd, "look" if req.get("look") is not None else "strength")
        if cmd == "set_mirror":
            return (
                cmd,
                *(k for k in ("horizontal", "vertical") if k in req),
            )
        if cmd == "set_model_strength":
            return (cmd, req.get("index"))
        return (cmd,)

    @staticmethod
    def _undo_label(req: dict) -> str:
        cmd = req.get("cmd")
        if cmd == "set_camera":
            names = list((req.get("values") or {}).keys())
            return ", ".join(name.replace("_", " ") for name in names) or "camera adjustment"
        if cmd == "set_look":
            return "look" if req.get("look") is not None else "look strength"
        if cmd == "set_model_strength":
            return "model strength"
        if cmd == "set_blur":
            return "background blur"
        if cmd == "set_background":
            return "background"
        if cmd == "set_overlay":
            return "overlay"
        if cmd == "set_clahe":
            return "clarity"
        if cmd == "set_zoom":
            return "framing"
        if cmd == "set_mirror":
            return "published flip"
        if cmd == "preset_load":
            return f"preset {req.get('name', '')}".rstrip()
        return "adjustment"

    def _record_undo(
        self, req: dict, before: dict, after: dict, touched: list
    ) -> None:
        """Remember `before` as the state to return to.

        `touched` names the camera controls the request applied; undoing the
        entry restores exactly those, and nothing else about the camera.
        """
        if self._undo_relevant(before) == self._undo_relevant(after):
            return
        now = time.monotonic()
        key = self._undo_key(req)
        label = self._undo_label(req)
        with self.lock:
            # Once the user branches from an undone state, the old forward
            # path is no longer truthful and must disappear.
            self._redo_history.clear()
            if (
                self._undo_history
                and self._undo_history[-1]["key"] == key
                and now - self._undo_history[-1]["at"] <= UNDO_COALESCE_SECONDS
            ):
                # A slider drag emits several requests.  Keep the state from
                # before the drag and merely extend its coalescing window.
                entry = self._undo_history[-1]
                entry["at"] = now
                entry["label"] = label
                entry["touched"] = sorted(set(entry["touched"]) | set(touched))
                if self._undo_relevant(entry["snapshot"]) == self._undo_relevant(after):
                    self._undo_history.pop()
                return
            self._undo_history.append({
                "cmd": req.get("cmd"), "key": key, "label": label,
                "at": now, "snapshot": before, "touched": sorted(touched),
            })
            del self._undo_history[:-UNDO_LIMIT]

    def _clear_undo(self) -> None:
        """Clear both directions at a restart/file-operation barrier."""
        with self.lock:
            self._undo_history.clear()
            self._redo_history.clear()

    def _restore_history(
        self,
        source: list[dict],
        destination: list[dict],
        result_key: str,
    ) -> dict:
        with self.lock:
            if not source:
                out = self.status()
                out[result_key] = None
                return out
            entry = source.pop()
            reverse = dict(entry)
            reverse["snapshot"] = self._undo_snapshot()
            # A history traversal is a discrete gesture. A rapid adjustment
            # after Redo must not coalesce back through that boundary.
            reverse["at"] = 0.0
            destination.append(reverse)
            del destination[:-UNDO_LIMIT]
            snapshot = entry["snapshot"]
            touched = list(entry.get("touched") or [])
            current_mode = self.state.mode
            old_active = self.state.active_preset
            for name, value in snapshot["state"].items():
                # Saving a preset after an adjustment changes the selection,
                # but it is not itself an adjustment.  An older undo step must
                # not roll that later selection back.  Preset loading is the
                # one history entry that deliberately restores selection too.
                if name == "active_preset" and entry["cmd"] != "preset_load":
                    continue
                setattr(self.state, name, copy.deepcopy(value))

            saved_models = snapshot["models"]
            for index, model_state in enumerate(saved_models):
                if index >= len(self.state.models):
                    break
                path, device, strength = model_state
                current = self.state.models[index]
                if (current.get("path"), current.get("device")) == (path, device):
                    current["strength"] = strength

            backend = self._backend
            previous_sticky = copy.deepcopy(snapshot["sticky"])
            # Only the controls this request touched go back, each to the
            # user's earlier request for it or to automatic. The rest of the
            # camera is not part of this entry and stays exactly as it is.
            values, unknown = model.restore_values(
                touched, previous_sticky, snapshot["controls"]
            )

        applied, refused = {}, {}
        for key in unknown:
            refused[key] = "no earlier value is known"
        for key in list(values):
            why = model.refusal_reason(Mode(current_mode), key)
            if why:
                refused[key] = why
                del values[key]
        if values:
            if backend is None:
                for key in values:
                    refused[key] = "no camera attached (mode transition in progress?)"
            else:
                try:
                    applied, denied = backend.apply_controls(values)
                    refused.update(denied)
                except Exception as e:
                    refused["camera controls"] = str(e)

        with self.lock:
            for key, value in applied.items():
                self.state.controls[key] = value
                self._snapshot.setdefault("controls", {})[key] = value
                if key not in model.STICKY_CONTROLS:
                    continue
                # Intent follows the hardware: a key the user had never set
                # is automatic again, so status reports auto only when the
                # camera really is.
                if key in previous_sticky:
                    self._sticky[key] = previous_sticky[key]
                else:
                    self._sticky.pop(key, None)
        self._sync_engine()

        active = snapshot["state"].get("active_preset")
        if entry["cmd"] == "preset_load" and active != old_active:
            self._remember_preset(current_mode, active)

        out = self.status()
        out[result_key] = entry["label"]
        if refused:
            out["notes"] = [
                f"{key} could not be restored: {why}"
                for key, why in refused.items()
            ]
        return out

    def undo(self) -> dict:
        return self._restore_history(
            self._undo_history, self._redo_history, "undone"
        )

    def redo(self) -> dict:
        return self._restore_history(
            self._redo_history, self._undo_history, "redone"
        )

    def capture_photo(self) -> dict:
        """Capture and finalize a still in the daemon, not the replaceable UI."""
        with self.lock:
            engine = self._engine
            node = self.state.output
        if engine is None or not engine.alive():
            raise RuntimeError("no feed to photograph")
        try:
            info = FFMPEG_PATH.stat(follow_symlinks=False)
        except OSError as e:
            raise RuntimeError("/usr/bin/ffmpeg is unavailable") from e
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0:
            raise RuntimeError("/usr/bin/ffmpeg is not a trusted system binary")

        tmp, final = _photo_target()
        result = _run_bounded(
            [
                str(FFMPEG_PATH), "-y", "-nostats", "-loglevel", "error",
                "-f", "v4l2", "-i", node, "-frames:v", "1", tmp,
            ],
            timeout=15,
            cap=CAPTURE_STDERR_MAX,
        )
        if result.get("code") != 0 or not os.path.isfile(tmp):
            with suppress(OSError):
                os.unlink(tmp)
            tail = (result.get("stderr") or "").strip().splitlines()
            raise RuntimeError(tail[-1] if tail else "ffmpeg failed")
        try:
            os.replace(tmp, final)
        except OSError:
            with suppress(OSError):
                os.unlink(tmp)
            raise
        return {"saved": str(final)}

    # -- presets ---------------------------------------------------------

    def _remember_preset(self, mode: str, name: Optional[str]) -> None:
        """Persist the last successful preset selection for one firmware mode."""
        with self.lock:
            if name is None:
                self._last_presets.pop(mode, None)
            else:
                self._last_presets[mode] = preset_codec.validate_name(name)
            if self.state.mode == mode:
                self.state.active_preset = name
            document = {
                "version": preset_codec.VERSION,
                "last_by_mode": dict(self._last_presets),
            }
        _atomic_write_json(preset_state_file(), document, PRESET_STATE_MAX)

    def _restore_startup_preset(self, mode: str) -> None:
        """Prepare the last preset before the first engine/camera transition."""
        with self.lock:
            self.state.mode = Mode(mode).value
            name = self._last_presets.get(mode)
        if not name:
            return
        try:
            restored = self.load_preset(name, startup=True)
            notes = restored.get("notes") or []
            detail = f" ({'; '.join(notes)})" if notes else ""
            print(f"restored {mode} preset {name!r} for startup{detail}")
        except Exception as e:
            print(f"startup preset {name!r} ignored: {type(e).__name__}: {e}")
            self._remember_preset(mode, None)

    def save_preset(self, name: str) -> dict:
        """Capture the current look, framing and camera settings under a name."""
        name = preset_codec.validate_name(name)
        with self.lock:
            st = self.state
            mode = st.mode
            data = {
                "version": preset_codec.VERSION,
                "name": name,
                "mode": mode,
                "look": st.look,
                "strength": st.strength,
                "look_strength": dict(st.look_strength),
                "mirror_h": st.mirror_h,
                "mirror_v": st.mirror_v,
                "zoom": st.zoom,
                "pan_x": st.pan_x,
                "pan_y": st.pan_y,
                "clahe": st.clahe,
                "blur": st.blur,
                "blur_style": st.blur_style,
                "background": st.background,
                "overlay": {
                    "path": st.overlay,
                    "x": st.overlay_x, "y": st.overlay_y,
                    "width": st.overlay_w, "height": st.overlay_h,
                    "opacity": st.overlay_opacity,
                },
                "controls": dict(self._snapshot.get("controls") or {}),
            }
        path = preset_dir() / mode / f"{name}.json"
        _atomic_write_json(path, data, PRESET_JSON_MAX)
        self._remember_preset(mode, name)
        self._refresh_presets()
        out = self.status()
        out["preset_saved"] = str(path)
        return out

    def load_preset(
        self, name: str, with_mode: bool = False, startup: bool = False
    ) -> dict:
        """Apply a saved preset.

        The mode is recorded but not switched into unless asked: switching
        reboots the camera and takes about fifteen seconds, which is not
        something a preset should do to you by surprise. Controls the current
        mode cannot reach are reported rather than silently dropped.
        """
        name = preset_codec.validate_name(name)
        path = _preset_path(name, self.state.mode)
        if not path.exists() and not path.is_symlink():
            raise FileNotFoundError(
                f"no preset named {name!r} for {self.state.mode} mode"
            )
        # The pure codec normalizes: unknown fields dropped, out-of-range
        # values clamped, and every such repair reported rather than silent.
        data, notes = preset_codec.decode(
            _read_regular_json(path, PRESET_JSON_MAX)
        )

        with self.lock:
            self.state.look_strength.update(data.get("look_strength") or {})
        if data.get("look"):
            self.set_look(data["look"], data.get("strength"))
        self.set_mirror(data.get("mirror_h"), data.get("mirror_v"))
        self.set_zoom(data.get("zoom"), data.get("pan_x"), data.get("pan_y"))
        if data.get("clahe") is not None:
            self.set_clahe(data["clahe"])
        if data.get("blur") is not None:
            self.set_blur(data["blur"], data.get("blur_style"))
        try:
            self.set_background(data.get("background"))
        except FileNotFoundError as e:
            notes.append(f"background skipped: {e}")

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

        # The mode switch comes AFTER the engine-side settings and BEFORE
        # the camera controls: the settings above apply in any mode, so a
        # switch that fails must not cost them - while the controls below
        # depend on which firmware ends up running, so they wait until the
        # mode question is settled either way.
        if data.get("mode") and data["mode"] != self.state.mode:
            if with_mode:
                try:
                    self.set_mode(data["mode"])
                except Exception as e:
                    notes.append(
                        f"mode switch to {data['mode']} failed ({e}); "
                        f"settings applied in {self.state.mode}"
                    )
            else:
                notes.append(
                    f"saved in {data['mode']} mode; still in {self.state.mode}"
                    " (pass with_mode to switch)"
                )

        controls = data.get("controls") or {}
        reached: dict = {}
        if controls:
            if startup:
                # Mode entry replays this intent after the firmware and its
                # backend exist. Regions are deliberately not sticky.
                with self.lock:
                    self._sticky.update({
                        key: value for key, value in controls.items()
                        if key in model.STICKY_CONTROLS
                    })
            else:
                applied = self.set_camera(**controls)
                reached = applied.get("applied") or {}
                for key, why in (applied.get("refused") or {}).items():
                    notes.append(f"{key} skipped: {why}")

        self._remember_preset(self.state.mode, name)
        out = self.status()
        out["preset_loaded"] = name
        # What reached the camera, so an undo of this load knows which
        # controls to restore.
        out["applied"] = reached
        if notes:
            out["notes"] = notes
        return out

    def list_presets(self) -> list[dict]:
        """Presets for the current mode, plus legacy un-namespaced ones."""
        mode_dir = preset_dir() / self.state.mode
        paths = (
            sorted(mode_dir.glob("*.json"))
            + sorted(preset_dir().glob("*.json"))
        )[:256]
        found = []
        seen = set()
        for path in paths:
            try:
                name = preset_codec.validate_name(path.stem)
                if name in seen:
                    continue
                data = _read_regular_json(path, PRESET_JSON_MAX)
                if not isinstance(data, dict):
                    continue
            except (OSError, ValueError):
                continue
            seen.add(name)
            found.append({
                "name": name,
                "look": data.get("look"),
                "strength": data.get("strength"),
                "mode": data.get("mode"),
                "overlay": bool((data.get("overlay") or {}).get("path")),
            })
        return found

    def delete_preset(self, name: str) -> dict:
        name = preset_codec.validate_name(name)
        path = _preset_path(name, self.state.mode)
        if not path.exists() and not path.is_symlink():
            raise FileNotFoundError(f"no preset named {name!r}")
        mode = self.state.mode
        _unlink_regular(path)
        if self._last_presets.get(mode) == name:
            self._remember_preset(mode, None)
        self._refresh_presets()
        out = self.status()
        out["deleted"] = name
        return out

    # -- status ---------------------------------------------------------

    def _refresh_presets(self) -> None:
        found = self.list_presets()
        with self.lock:
            self._preset_cache = found

    def _status_poller(self) -> None:
        """Keeps the snapshot status() serves. The only periodic hardware
        reader: one place to throttle, one place to blame."""
        ticks = 0
        while not self._shutdown.wait(1.0):
            with self.lock:
                transitioning = self._ledger.in_progress
            if not transitioning:
                controls = self._live_controls()
                mode = current_mode()
                with self.lock:
                    self._snapshot = {
                        "controls": controls,
                        "mode_actual": mode.value if mode else None,
                    }
            ticks += 1
            if self._preset_cache is None or ticks % 10 == 0:
                with suppress(Exception):
                    self._refresh_presets()
                with suppress(Exception):
                    fresh = available_looks()
                    with self.lock:
                        self._looks_cache = fresh

    def _watchdog(self) -> None:
        """Detects the stall: everything alive, zero frames flowing.

        Consumes the engine's preview socket as an ordinary client - the one
        observable that exists in both modes - and feeds the pure
        StallDetector. On a stall it stops the engine; recovery is the
        supervisor's job, through the same policy as any other death.
        """
        detector = health.StallDetector()
        frames = 0
        sock = None

        def drop(connection):
            if connection is not None:
                with suppress(Exception):
                    connection.close()
            return None

        while not self._shutdown.wait(0.5):
            engine = self._engine
            with self.lock:
                transitioning = self._ledger.in_progress
            if engine is None or not engine.alive() or transitioning:
                detector.reset()
                sock = drop(sock)
                continue
            try:
                if sock is None:
                    sock = socket.socket(socket.AF_UNIX)
                    sock.settimeout(1.5)
                    sock.connect(str(self.preview_sock))
                    header = self._recv_exact(sock, 8)
                    if header is None:
                        raise OSError("preview closed during header")
                    w = int.from_bytes(header[0:4], "little")
                    h = int.from_bytes(header[4:8], "little")
                    self._preview_frame_len = w * h * 3
                if self._recv_exact(sock, self._preview_frame_len) is not None:
                    frames += 1
                    # Frames flowing is the arrival the notice promised, in
                    # either mode - state.frames only counts in Studio.
                    if self.state.notice:
                        with self.lock:
                            self.state.notice = None
                else:
                    sock = drop(sock)
            except OSError:
                # Connection trouble is not evidence of a stall by itself;
                # the detector decides based on frame progress over time.
                sock = drop(sock)
            if detector.update(frames, time.monotonic()):
                message = (
                    "pipeline stalled: engine alive but no frames for "
                    f"{detector.window:.0f}s; restarting the engine"
                )
                print(message)
                with self.lock:
                    self.state.error = message
                engine.stop()
                detector.reset()
                sock = drop(sock)
        drop(sock)

    @staticmethod
    def _recv_exact(sock: socket.socket, n: int):
        buf = bytearray()
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

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
        """A snapshot, never a probe: hardware truths come from the poller.

        status() is called constantly (the panel every 2s, plus every setter)
        and used to open the camera node and read sysfs each time - including
        while a transition held the lock, which froze every client.
        """
        with self.lock:
            s = asdict(self.state)
            snapshot = dict(self._snapshot)
        s["controls"] = snapshot.get("controls", {})
        s["mode_actual"] = snapshot.get("mode_actual")
        engine = self._engine
        s["engine_alive"] = engine is not None and engine.alive()
        # The normal node is optional: this says whether the running engine
        # was given one, so nobody reports a feed that is not there.
        s["normal_active"] = bool(
            s["engine_alive"]
            and engine.config is not None
            and engine.config.normal_output
        )
        with self.lock:
            s["looks"] = list(self._looks_cache)
        s["restarts"] = self.restarts
        s["fps"] = self.fps
        with self.lock:
            s["fps_range"] = model.fps_limits(
                Mode(self.state.mode), self.state.width, self.state.height
            )
        with self.lock:
            s["transitioning"] = self._ledger.in_progress
            s["can_undo"] = bool(self._undo_history)
            s["undo_label"] = (
                self._undo_history[-1]["label"] if self._undo_history else None
            )
            s["can_redo"] = bool(self._redo_history)
            s["redo_label"] = (
                self._redo_history[-1]["label"] if self._redo_history else None
            )
        with self.lock:
            cached = self._preset_cache
        s["presets"] = [p["name"] for p in (cached or [])]
        s["preview"] = str(self.preview_sock) if self.preview_sock.exists() else None
        s["engine_log"] = engine.log_lines() if engine is not None else []
        with self.lock:
            powered = self.state.running
        if (powered and engine is not None and not engine.alive()
                and engine.config is not None):
            s["error"] = (
                f"engine exited with code {engine.returncode()}: "
                + (engine.log_text() or "no output")
            )
        return s

    # -- supervision ----------------------------------------------------

    def _on_camera_plug(self) -> None:
        with self.lock:
            if not self.state.running:
                # Powered off on purpose: the enumeration is our own
                # teardown echo (or a replug that must not auto-start).
                return
        print("camera connected: waking recovery")
        with self.lock:
            self.state.notice = "camera connected — starting the feed…"
        self._camera_event.set()

    def _hold(self, seconds: float) -> str:
        """Wait, but wake early for shutdown or a camera replug.

        Returns "shutdown", "replug" or "elapsed".
        """
        waited = 0.0
        while waited < seconds:
            if self._shutdown.wait(0.5):
                return "shutdown"
            waited += 0.5
            if self._camera_event.is_set():
                self._camera_event.clear()
                return "replug"
        return "elapsed"

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
                if self._ledger.in_progress or not self._requests.empty():
                    # A client transition owns the camera; its stopped engine
                    # is not a death and must not feed the policy counters.
                    continue
                engine = self._engine
                if engine is not None and engine.alive():
                    policy.note_alive()
                    if self.state.notice and self.state.frames > 0:
                        self.state.notice = None
                    continue
                # Covers both an engine that died and one that never started
                # (a mode switch that failed mid-camera-reboot).
                reason = (
                    engine.log_text() if engine is not None else ""
                ) or "engine not started"
                mode = Mode(self.state.mode)
                started = engine.started_at if engine is not None else 0.0
                uptime = time.time() - started if started else 0.0

            action = policy.on_death(
                mode, uptime, camera_on_bus=current_mode() is not None
            )

            if action.kind is health.Kind.HOLD_SICK:
                with self.lock:
                    self.state.error = action.message
                print(action.message)
                outcome = self._hold(action.delay)
                if outcome == "shutdown":
                    return
                if outcome == "replug":
                    policy.on_replug()
                continue

            if action.kind is health.Kind.HOLD_VANISHED:
                with self.lock:
                    self.state.error = action.message
                print(action.message)
                # Hold, but come back the moment the camera reappears so a
                # replug recovers in seconds rather than a minute.
                waited = 0.0
                while waited < action.delay:
                    outcome = self._hold(health.VANISHED_POLL_SECONDS)
                    if outcome == "shutdown":
                        return
                    if outcome == "replug":
                        policy.on_replug()
                        break
                    waited += health.VANISHED_POLL_SECONDS
                    if current_mode() is not None:
                        break
                continue

            print(f"engine died ({reason}); retrying {mode.value} in {action.delay:.0f}s")
            outcome = self._hold(action.delay)
            if outcome == "shutdown":
                return
            if outcome == "replug":
                policy.on_replug()

            if not self._camera_settled():
                # Mid-reboot. Say nothing and come back; attempting now would
                # only add another reset to whatever it is already doing.
                continue

            with self.lock:
                if self._ledger.in_progress or not self._requests.empty():
                    # A client transition is underway; it owns the camera now.
                    continue
                if not self.state.running:
                    # Powered off while we were holding: dormant means dormant.
                    continue
                current = self._engine
                if current is not None and current.alive():
                    # The engine came back while we were holding - a client
                    # transition finished, or the hold's own replug wake was
                    # the switch's re-enumeration. Re-entering now would
                    # reboot a healthy camera: the second restart users saw
                    # on every mode switch.
                    policy.note_alive()
                    continue
            try:
                self.request_transition(mode, enforce_guard=False)
            except Exception as e:
                followup = policy.on_reentry_failed(mode, str(e))
                print(f"restart failed: {e}")
                if followup.kind is health.Kind.FALLBACK_TO_CALL:
                    # Studio is the mode that reboots the camera. Falling back
                    # to Call stops the cycling and leaves a usable camera.
                    print("giving up on studio mode; falling back to call")
                    with self.lock:
                        self.state.error = followup.message
                    with suppress(Exception):
                        self.request_transition(Mode.CALL, enforce_guard=False)
                else:
                    with self.lock:
                        self.state.error = f"engine restart failed: {type(e).__name__}: {e}"
                continue

            policy.on_reentry_ok()
            with self.lock:
                self.restarts += 1
                self.state.error = None
                self.state.last_event = f"engine restarted after: {reason}"
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
            undoable = (
                cmd in UNDOABLE_COMMANDS
                and not (cmd == "preset_load" and bool(req.get("with_mode")))
            )
            before = self._undo_snapshot() if undoable else None
            response = self._dispatch(req)
            if response.get("ok"):
                if undoable and before is not None:
                    # The camera controls a request actually reached, as
                    # reported by the backend; regions are moments rather
                    # than policies and are never restored.
                    touched = [
                        key for key in (response.get("applied") or {})
                        if key in model.STICKY_CONTROLS
                    ]
                    self._record_undo(
                        req, before, self._undo_snapshot(), touched
                    )
                elif cmd in UNDO_BARRIER_COMMANDS or (
                    cmd == "preset_load" and bool(req.get("with_mode"))
                ):
                    self._clear_undo()
                with self.lock:
                    response["can_undo"] = bool(self._undo_history)
                    response["undo_label"] = (
                        self._undo_history[-1]["label"]
                        if self._undo_history else None
                    )
                    response["can_redo"] = bool(self._redo_history)
                    response["redo_label"] = (
                        self._redo_history[-1]["label"]
                        if self._redo_history else None
                    )
            return response
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def _dispatch(self, req: dict) -> dict:
        """Execute one already-decoded request; history wraps this boundary."""
        cmd = req.get("cmd")
        if cmd == "status":
            return {"ok": True, **self.status()}
        if cmd == "looks":
            return {"ok": True, "looks": LOOKS}
        if cmd == "undo":
            return {"ok": True, **self.undo()}
        if cmd == "redo":
            return {"ok": True, **self.redo()}
        if cmd == "capture_photo":
            return {"ok": True, **self.capture_photo()}
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
        if cmd == "set_models":
            return {"ok": True, **self.set_models(req.get("models"))}
        if cmd == "set_model_strength":
            return {"ok": True, **self.set_model_strength(
                req.get("index"), req.get("strength"))}
        if cmd == "set_power":
            return {"ok": True, **self.set_power(bool(req.get("on")))}
        if cmd == "set_fps":
            return {"ok": True, **self.set_fps(req.get("fps"))}
        if cmd == "set_blur":
            return {"ok": True, **self.set_blur(
                req.get("strength"), req.get("style"))}
        if cmd == "set_background":
            return {"ok": True, **self.set_background(req.get("path"))}
        if cmd == "set_overlay":
            return {"ok": True, **self.set_overlay(**req.get("values", {}))}
        if cmd == "set_resolution":
            return {"ok": True, **self.set_resolution(
                req["width"], req["height"],
                req.get("in_width", 0), req.get("in_height", 0),
            )}
        if cmd == "set_clahe":
            return {"ok": True, **self.set_clahe(req.get("strength", 0.0))}
        if cmd == "set_zoom":
            return {"ok": True, **self.set_zoom(
                req.get("zoom"), req.get("pan_x"), req.get("pan_y")
            )}
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

        threading.Thread(target=self._transition_worker, daemon=True).start()
        threading.Thread(target=self._supervise, daemon=True).start()
        threading.Thread(target=self._status_poller, daemon=True).start()
        threading.Thread(target=self._watchdog, daemon=True).start()
        try:
            from opal_c1.adapters.bus_events import watch
            from opal_c1.core.model import USB_VID

            watch(USB_VID, self._on_camera_plug, stop_event=self._shutdown)
        except Exception as e:
            print(f"usb hotplug watcher not started: {e}")
        self._start_tray()

        # Load the last named configuration before the first engine starts,
        # avoiding a flash of defaults. Camera controls are staged as sticky
        # intent and replayed once the requested firmware backend is attached.
        self._restore_startup_preset(initial_mode)

        # Entering the initial mode takes seconds to minutes when the camera
        # is settling; clients must be able to ask what is happening from the
        # very first moment, so serve first and enter asynchronously.
        try:
            self.request_transition(Mode(initial_mode), enforce_guard=True, wait=False)
            print(f"entering {initial_mode} mode; serving clients meanwhile")
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
