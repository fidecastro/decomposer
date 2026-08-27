"""CLI for decomposer."""

from __future__ import annotations

import argparse
import os
import sys
from contextlib import suppress
from pathlib import Path



def _daemon_status() -> dict:
    """The running daemon's status, or {} when none is listening."""
    from opal_c1.daemon import Client

    try:
        return Client().request(cmd="status")
    except Exception:
        return {}


def _refuse_if_daemon(what: str) -> bool:
    """Direct-hardware commands and the daemon must not fight over the camera.

    Two owners was a real failure mode, not a hypothetical: a direct command
    rebooting the firmware out from under the daemon's engine looks exactly
    like the camera dying. These commands remain available as diagnostics
    when no daemon is running.
    """
    if not _daemon_status():
        return False
    print(
        f"a decomposer daemon is running and owns the camera; {what}\n"
        "  For direct hardware access, `decomposer stop` first.",
        file=sys.stderr,
    )
    return True


def _cmd_probe_xlink(args: argparse.Namespace) -> int:
    """Ask the vendor bulk interface whether anything is listening.

    Opal's camera-mode firmware does not service this endpoint, which is why
    Studio mode has to reboot the camera. If a firmware update ever changed
    that, this is the check that would show it.
    """
    import usb.core

    from opal_c1.xlink import PID_CAMERA, PID_DEPTHAI, XLinkUSB

    status = _daemon_status()
    if status.get("mode") == "studio":
        print(
            "the daemon is in Studio mode: interface 0 carries its live XLink "
            "session,\nand probing it would disrupt the stream. Switch to call "
            "or `decomposer stop` first.",
            file=sys.stderr,
        )
        return 2

    pid = PID_DEPTHAI if args.depthai_mode else PID_CAMERA
    print(f"Probing interface 0 on 03e7:{pid:04x} (read-only)")
    try:
        with XLinkUSB(pid=pid) as link:
            resp = link.ping(timeout=args.timeout)
    except usb.core.USBTimeoutError:
        print("  no reply: nothing is servicing the endpoint")
        return 1
    except Exception as e:
        print(f"  {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    if resp is None:
        print("  short read")
        return 1
    print(f"  <<< {resp.describe()}")
    return 0


def _cmd_probe_xu(args: argparse.Namespace) -> int:
    from opal_c1 import probe

    selectors = range(args.first, args.last + 1)
    report = probe.run(args.dev, args.unit, selectors, quiet=args.json_only)
    print(probe.format_report(report))
    if args.json:
        probe.write_json(report, args.json)
        print(f"\nWrote {args.json}")
    return 0


def _cmd_camera_info(args: argparse.Namespace) -> int:
    from opal_c1.device import OpalDevice

    if _refuse_if_daemon("use `decomposer status` instead"):
        return 2
    print("Attaching XLink — /dev/video0 will disappear until this exits.")
    with OpalDevice(width=args.width, height=args.height) as cam:
        for k, v in cam.describe().items():
            print(f"  {k:<14} {v}")
        f = cam.read()
        print(f"  {'streaming':<14} {f.width}x{f.height} NV12, {f.data.nbytes} bytes/frame")
        print(f"  {'isp':<14} lens={f.lens} iso={f.iso} exp={f.exposure_us}us wb={f.color_temp}K")
    print("Released. /dev/video0 returns in ~14s.")
    return 0


def _cmd_mode(args: argparse.Namespace) -> int:
    from opal_c1.modes import Mode, current_mode, describe

    now = current_mode()
    if now is None:
        print("Camera is not on the USB bus (it may be mid-switch, which takes ~15s).")
        return 1
    print(f"Current mode: {now.value}\n")
    for m in Mode:
        print(f" {'->' if m is now else '  '} {describe(m)}")
    print(
        "\nSwitching reboots the camera: about 5s into studio, 15s back to call.\n"
        "Studio mode has no microphone - depthai has no audio support at all."
    )
    return 0


def _cmd_control(args: argparse.Namespace) -> int:
    """Apply controls directly to the hardware. Diagnostic path only."""
    if _refuse_if_daemon("use `decomposer set ...` instead"):
        return 2
    import time

    from opal_c1.modes import Mode, current_mode, wait_until_capturable
    from opal_c1.v4l2 import UvcControls

    wants_studio = args.focus is not None or args.wb is not None

    if wants_studio and not args.studio:
        print(
            "Manual focus and white balance need Studio mode.\n"
            "  - the camera reboots (~5s), and ~15s to come back afterwards\n"
            "  - /dev/video0 disappears while it is held\n"
            "  - the C1 microphone disappears too\n"
            "Re-run with --studio to accept that, or adjust exposure/gain/colour\n"
            "instead, which work in Call mode with no interruption.",
            file=sys.stderr,
        )
        return 2

    if not args.studio:
        now = current_mode()
        if now is not Mode.CALL:
            print(
                f"Camera is in {now.value if now else 'no'} mode; "
                "Call-mode controls need /dev/video0.",
                file=sys.stderr,
            )
            return 1
        uvc = UvcControls(args.dev)
        requested = (
            ("brightness", args.brightness), ("contrast", args.contrast),
            ("saturation", args.saturation), ("hue", args.hue),
            ("sharpness", args.sharpness),
        )
        nothing_asked = (
            all(v is None for _, v in requested)
            and args.exposure is None and args.iso is None and not args.auto
        )
        if nothing_asked:
            for c in uvc.list():
                print(" ", c.describe())
            return 0
        rc = 0
        if args.auto:
            try:
                uvc.set_auto_exposure()
                print("  auto_exposure -> Auto Mode")
            except (PermissionError, ValueError, OSError) as e:
                print(f"  auto_exposure: {e}", file=sys.stderr)
                rc = 1
        for name, value in requested:
            if value is None:
                continue
            try:
                print(f"  {name} -> {uvc.set(name, value)}")
            except (PermissionError, ValueError, OSError) as e:
                print(f"  {name}: {e}", file=sys.stderr)
                rc = 1
        if args.exposure is not None or args.iso is not None:
            # These stall unless auto-exposure is switched to Manual Mode first.
            try:
                for k, v in uvc.set_manual_exposure(args.exposure, args.iso).items():
                    print(f"  {k} -> {v}")
            except (PermissionError, ValueError, OSError) as e:
                print(f"  exposure/iso: {e}", file=sys.stderr)
                rc = 1
        return rc

    from opal_c1.device import OpalDevice

    print("Entering Studio mode - /dev/video0 and the C1 mic will disappear.")
    with OpalDevice(width=args.width, height=args.height) as cam:
        if args.auto:
            cam.set_auto()
            print("  all controls -> auto")
        if args.focus is not None:
            cam.set_focus(None if args.focus < 0 else args.focus)
            print(f"  focus -> {'auto' if args.focus < 0 else args.focus}")
        if args.wb is not None:
            cam.set_white_balance(None if args.wb < 0 else args.wb)
            print(f"  white balance -> {'auto' if args.wb < 0 else str(args.wb) + 'K'}")
        if args.exposure is not None or args.iso is not None:
            cam.set_exposure(args.exposure, args.iso)
            print(f"  exposure -> {args.exposure}us  iso -> {args.iso}")

        deadline = time.time() + args.hold
        while time.time() < deadline:
            f = cam.read()
            if f.sequence % 30 == 0:
                print(f"  [{f.sequence:>5}] lens={f.lens} iso={f.iso} "
                      f"exp={f.exposure_us}us wb={f.color_temp}K")

    print("Leaving Studio mode ...")
    t = wait_until_capturable()
    print(
        f"  Call mode restored after {t}s - mic and /dev/video0 are back."
        if t is not None else "  camera has not come back yet"
    )
    return 0


def _cmd_stream_nv12(args: argparse.Namespace) -> int:
    """Stream raw NV12 to stdout for the look engine.

    Studio mode has no V4L2 node, so the engine cannot read the camera itself.
    This holds the XLink connection — which is also what keeps manual focus and
    white balance applied — and pipes frames to it:

        decomposer stream-nv12 --focus 150 | decomposer-engine --input - --output /dev/video10
    """
    if _refuse_if_daemon("its engine already publishes to /dev/video10"):
        return 2
    import signal
    import time

    from opal_c1.device import OpalDevice

    # Exit quietly when the engine downstream goes away.
    with suppress(AttributeError, ValueError):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)

    out = sys.stdout.buffer

    # A default 64 KB pipe means ~48 handoffs per 3 MB frame. Widening it to the
    # kernel maximum cuts the wakeups the consumer has to service.
    with suppress(OSError, AttributeError):
        import fcntl

        F_SETPIPE_SZ = 1031
        target = int(Path("/proc/sys/fs/pipe-max-size").read_text().strip())
        fcntl.fcntl(out.fileno(), F_SETPIPE_SZ, target)

    print("Entering Studio mode - /dev/video0 and the C1 mic will disappear.",
          file=sys.stderr)
    with OpalDevice(width=args.width, height=args.height, fps=args.fps) as cam:
        if args.focus is not None:
            cam.set_focus(None if args.focus < 0 else args.focus)
        if args.wb is not None:
            cam.set_white_balance(None if args.wb < 0 else args.wb)
        if args.exposure is not None or args.iso is not None:
            cam.set_exposure(args.exposure, args.iso)

        first = cam.read()
        print(
            f"streaming {first.width}x{first.height} NV12 "
            f"({len(first.nv12())} bytes/frame) to stdout",
            file=sys.stderr,
        )

        # Write on a separate thread. A blocking write to the pipe otherwise
        # stalls the depthai receive loop, and the camera does not wait: frames
        # arrive late and throughput drops. The queue is deliberately shallow
        # and drops the oldest frame when full, because for a live camera feed
        # a stale frame is worth less than a current one.
        import itertools
        import queue
        import threading

        q: "queue.Queue" = queue.Queue(maxsize=4)
        dropped = 0
        stop = threading.Event()

        def writer() -> None:
            while True:
                buf = q.get()
                if buf is None:
                    return
                try:
                    out.write(buf)
                except (BrokenPipeError, ValueError):
                    stop.set()
                    return

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()

        n = 0
        t0 = time.monotonic()
        try:
            for frame in itertools.chain((first,), cam.frames()):
                if stop.is_set():
                    break
                # nv12() may be a view onto a buffer depthai will reuse, so the
                # handoff to another thread has to own its bytes.
                buf = bytes(frame.nv12())
                try:
                    q.put_nowait(buf)
                except queue.Full:
                    with suppress(queue.Empty):
                        q.get_nowait()
                        dropped += 1
                    with suppress(queue.Full):
                        q.put_nowait(buf)
                n += 1
                if args.frames and n >= args.frames:
                    break
        except (BrokenPipeError, KeyboardInterrupt):
            pass
        finally:
            with suppress(queue.Full):
                q.put_nowait(None)
            thread.join(timeout=3.0)

    dt = time.monotonic() - t0
    note = f", {dropped} dropped" if dropped else ""
    print(
        f"stopped after {n} frames in {dt:.1f}s ({n / dt:.1f} fps{note})",
        file=sys.stderr,
    )
    return 0


def _cmd_install_desktop(args: argparse.Namespace) -> int:
    """Install the launcher so the panel starts from the app menu.

    The shipped .desktop cannot just say `Exec=decomposer gui`: when decomposer
    is installed in a virtualenv, that name is not on PATH and the launcher
    silently does nothing. This writes the absolute path of the running
    interpreter's console script instead, and drops a shim in ~/.local/bin so
    the bare command works in a terminal too.
    """
    import shutil

    exe = Path(sys.argv[0]).resolve()
    if not exe.is_file():
        exe = Path(sys.executable).resolve()
    # Ship the mark as a themed icon so the launcher and window manager
    # show it rather than a generic camera glyph.
    from opal_c1 import logo

    icons = Path.home() / ".local/share/icons/hicolor/scalable/apps"
    icons.mkdir(parents=True, exist_ok=True)
    (icons / "decomposer.svg").write_text(logo.svg(color="#ffffff"))
    print(f"  wrote {icons / 'decomposer.svg'}")

    apps = Path.home() / ".local/share/applications"
    apps.mkdir(parents=True, exist_ok=True)
    target = apps / "decomposer.desktop"

    source = Path(__file__).resolve().parents[2] / "packaging/decomposer.desktop"
    template = source.read_text() if source.is_file() else (
        "[Desktop Entry]\nType=Application\nName=decomposer\n"
        "Comment=Camera controls and looks for the Opal C1\n"
        "Exec=decomposer gui\nIcon=camera-video\nTerminal=false\n"
        "Categories=AudioVideo;Video;Settings;\n"
    )
    template = template.replace("Exec=decomposer gui", f"Exec={exe} gui")
    template = template.replace("Icon=camera-video", "Icon=decomposer")
    target.write_text(template)
    print(f"  wrote {target}")
    print(f"  Exec={exe} gui")

    binhome = Path.home() / ".local/bin"
    binhome.mkdir(parents=True, exist_ok=True)
    shim = binhome / "decomposer"
    if shim.exists() or shim.is_symlink():
        if shim.resolve() == exe:
            print(f"  {shim} already points here")
        else:
            print(f"  {shim} exists and points elsewhere; left alone", file=sys.stderr)
    else:
        shim.symlink_to(exe)
        print(f"  linked {shim} -> {exe}")

    if shutil.which("decomposer") is None:
        print(
            "  note: ~/.local/bin is not on your PATH, so the bare `decomposer` "
            "command still will not resolve in a terminal. The launcher will "
            "work regardless, since it uses the absolute path.",
            file=sys.stderr,
        )
    with suppress(Exception):
        subprocess_update = shutil.which("update-desktop-database")
        if subprocess_update:
            import subprocess

            subprocess.run([subprocess_update, str(apps)], check=False)
    return 0


def _cmd_install_plugin(args: argparse.Namespace) -> int:
    """Install the Omarchy bar widget.

    Omarchy's bar takes QML plugins from ~/.config/omarchy/plugins, which is a
    far better fit than a tray icon: the widget draws the mark with the bar's
    own foreground colour, so it matches whatever theme is set.
    """
    import json
    import shutil
    import subprocess

    from opal_c1 import logo

    module_id = "decomposer.overlay"
    root = Path.home() / ".config/omarchy/plugins" / module_id
    root.mkdir(parents=True, exist_ok=True)

    exe = Path(sys.argv[0]).resolve()
    command = f"{exe} toggle" if exe.is_file() else "decomposer toggle"

    (root / "manifest.json").write_text(logo.qml_manifest(module_id))
    (root / "BarWidget.qml").write_text(logo.qml_widget(module_id, command))
    print(f"  wrote {root}/manifest.json")
    print(f"  wrote {root}/BarWidget.qml  (runs: {command})")

    validate = shutil.which("omarchy")
    if validate:
        result = subprocess.run(
            [validate, "plugin", "validate", str(root)],
            capture_output=True, text=True,
        )
        out = (result.stdout + result.stderr).strip()
        if out:
            print("  " + out.splitlines()[-1])

    shell = Path.home() / ".config/omarchy/shell.json"
    if not args.add_to_bar:
        print(
            f"\n  To show it, add {module_id!r} to the bar in {shell}\n"
            f"  (or re-run with --add-to-bar and I will do it, keeping a backup)."
        )
        return 0

    try:
        config = json.loads(shell.read_text())
    except (OSError, ValueError) as e:
        print(f"  could not read {shell}: {e}", file=sys.stderr)
        return 1

    layout = config.setdefault("bar", {}).setdefault("layout", {})
    side = layout.setdefault(args.side, [])
    if any(item.get("id") == module_id for item in side):
        print(f"  {module_id} is already on the {args.side} of the bar")
        return 0

    backup = shell.with_suffix(".json.bak")
    backup.write_text(shell.read_text())
    side.insert(0, {"id": module_id})
    shell.write_text(json.dumps(config, indent=2) + "\n")
    print(f"  backed up {shell} -> {backup}")
    print(f"  added {module_id} to bar.layout.{args.side}")
    print("  the bar should pick it up shortly; otherwise restart omarchy-shell")
    return 0


def _cmd_gui(args: argparse.Namespace) -> int:
    try:
        from opal_c1.gui import main as gui_main
    except (ImportError, ValueError) as e:
        print(
            f"GUI needs PyGObject with GTK4 and libadwaita ({e}).\n"
            "  Arch: sudo pacman -S python-gobject gtk4 libadwaita\n"
            "  or:   pip install 'decomposer[gui]'",
            file=sys.stderr,
        )
        return 1
    return gui_main(replace=getattr(args, 'replace', False))


def _cmd_daemon(args: argparse.Namespace) -> int:
    from opal_c1.daemon import Daemon

    return Daemon(
        output=args.output, width=args.width, height=args.height, fps=args.fps,
        tray_enabled=args.tray,
        in_width=args.in_width, in_height=args.in_height,
        seg_model=args.seg_model, seg_device=args.seg_device,
        **({} if args.default_strength is None
           else {'default_strength': args.default_strength}),
    ).run(initial_mode=args.mode)


def _print_status(st: dict) -> None:
    mark = "" if st.get("mode") == st.get("mode_actual") else \
        f"  (camera reports: {st.get('mode_actual')})"
    print(f"  mode      {st.get('mode')}{mark}")
    print(f"  look      {st.get('look')} @ {st.get('strength')}")
    print(f"  output    {st.get('output')}  {st.get('width')}x{st.get('height')}")
    print(f"  engine    {'running' if st.get('engine_alive') else 'STOPPED'}")
    if st.get("frames"):
        print(f"  frames    {st['frames']}")
    if st.get("controls"):
        print(f"  controls  {st['controls']}")
    for line in st.get("engine_log") or []:
        print(f"  engine    {line}")
    mode_name = st.get("mode")
    if mode_name in ("call", "studio"):
        from opal_c1.core.model import CAPABILITIES, Mode
        if CAPABILITIES[Mode(mode_name)].microphone:
            print("  mic       live — select 'Opal C1' in your app")
        else:
            print("  mic       none — Studio firmware has no microphone")
    if st.get("notice"):
        print(f"  notice    {st['notice']}")
    if st.get("last_event"):
        print(f"  last      {st['last_event']}")
    if st.get("error"):
        print(f"  error     {st['error']}", file=sys.stderr)


def _client_call(**req) -> dict:
    from opal_c1.daemon import Client

    resp = Client().request(**req)
    if not resp.get("ok"):
        print(f"  {resp.get('error')}", file=sys.stderr)
    return resp


def _cmd_status(_: argparse.Namespace) -> int:
    resp = _client_call(cmd="status")
    if not resp.get("ok"):
        return 1
    _print_status(resp)
    return 0


def _cmd_stop(_: argparse.Namespace) -> int:
    import time

    from opal_c1.daemon import Client, socket_path

    resp = _client_call(cmd="stop")
    if not resp.get("ok"):
        return 1
    print("  daemon stopping", end="", flush=True)

    # Wait for it to actually go. Shutdown releases the camera and stops the
    # engine first, so `decomposer stop && decomposer daemon` would otherwise
    # race and the new daemon would fail to bind.
    path = socket_path()
    deadline = time.time() + 25
    while time.time() < deadline:
        try:
            Client().request(cmd="status")
        except Exception:
            print(" — stopped")
            return 0
        print(".", end="", flush=True)
        time.sleep(0.5)
    print()
    print("  daemon did not exit within 25s", file=sys.stderr)
    return 1


def _cmd_look(args: argparse.Namespace) -> int:
    resp = _client_call(cmd="set_look", look=args.name, strength=args.strength)
    if not resp.get("ok"):
        return 1
    print(f"  look -> {resp.get('look')} @ {resp.get('strength')}")
    return 0


def _cmd_preset(args: argparse.Namespace) -> int:
    action = args.action
    if action == "list":
        resp = _client_call(cmd="preset_list")
        if not resp.get("ok"):
            return 1
        presets = resp.get("presets") or []
        if not presets:
            print("  no presets saved yet")
            return 0
        for p in presets:
            extra = "  +overlay" if p.get("overlay") else ""
            print(f"  {p['name']:<20} {p.get('look')} @ {p.get('strength')}"
                  f"  ({p.get('mode')}){extra}")
        return 0

    if not args.name:
        print(f"  preset {action} needs a name", file=sys.stderr)
        return 2

    if action == "save":
        resp = _client_call(cmd="preset_save", name=args.name)
        if resp.get("ok"):
            print(f"  saved {args.name} -> {resp.get('preset_saved')}")
    elif action == "load":
        resp = _client_call(
            cmd="preset_load", name=args.name, with_mode=args.with_mode
        )
        if resp.get("ok"):
            print(f"  loaded {args.name}: {resp.get('look')} @ {resp.get('strength')}")
            for note in resp.get("notes") or []:
                print(f"    note: {note}", file=sys.stderr)
    elif action == "delete":
        resp = _client_call(cmd="preset_delete", name=args.name)
        if resp.get("ok"):
            print(f"  deleted {args.name}")
    else:
        print(f"  unknown action {action!r}", file=sys.stderr)
        return 2
    return 0 if resp.get("ok") else 1


def _cmd_overlay(args: argparse.Namespace) -> int:
    values = {}
    if args.path is not None:
        values["path"] = args.path
    for name, value in (
        ("x", args.x), ("y", args.y),
        ("width", args.width), ("height", args.height),
        ("opacity", args.opacity),
    ):
        if value is not None:
            values[name] = value
    resp = _client_call(cmd="status") if not values else _client_call(
        cmd="set_overlay", values=values
    )
    if not resp.get("ok"):
        return 1
    if resp.get("overlay"):
        print(f"  overlay {resp['overlay']}")
        print(f"    at {resp['overlay_x']},{resp['overlay_y']} "
              f"fitted into {resp['overlay_w']}x{resp['overlay_h']} "
              f"(0 = unconstrained), opacity {resp['overlay_opacity']}")
    else:
        print("  no overlay")
    return 0


def _cmd_zoom(args: argparse.Namespace) -> int:
    values = {}
    if args.factor is not None:
        values["zoom"] = 1.0 if args.factor in ("off", "1") else float(args.factor)
    if args.x is not None:
        values["pan_x"] = args.x
    if args.y is not None:
        values["pan_y"] = args.y
    resp = _client_call(cmd="status") if not values else _client_call(
        cmd="set_zoom", **values
    )
    if not resp.get("ok"):
        return 1
    print(f"  zoom {resp.get('zoom')}x  pan {resp.get('pan_x')},{resp.get('pan_y')}")
    if resp.get("in_width"):
        print(f"  capture {resp['in_width']}x{resp['in_height']} -> "
              f"{resp.get('width')}x{resp.get('height')}: lossless to "
              f"{resp['in_width'] / resp.get('width', 1):.1f}x")
    else:
        print("  capture equals output: zoom upscales. Run the daemon with "
              "--in-width 3840 --in-height 2160 for lossless 2x.")
    return 0


def _resolutions() -> dict:
    """Short names for every geometry the core knows. Derived, not copied:
    a hand-maintained duplicate of the core's resolution facts went stale
    the first time those facts changed (it kept offering a mode the camera
    cannot deliver)."""
    from opal_c1.core.model import RESOLUTIONS_STUDIO

    out = {}
    for label, w, h, _iw, _ih in RESOLUTIONS_STUDIO:
        short = label.lower().split()[0].replace("k", "k")
        if "\u00b7" in label or "·" in label:
            continue  # the 4K-capture variant has its own flag
        out[short] = (w, h)
    out["12mp"] = out.pop("12", (4000, 3000))
    return out


RESOLUTIONS = _resolutions()


def _cmd_resolution(args: argparse.Namespace) -> int:
    if args.size is None:
        resp = _client_call(cmd="status")
        if not resp.get("ok"):
            return 1
    else:
        w, h = RESOLUTIONS[args.size]
        in_w, in_h = (3840, 2160) if args.capture_4k and h < 2160 else (0, 0)
        print(f"  applying {args.size}"
              + (" with 4K capture" if in_w else "")
              + " - the engine restarts; in Studio the camera reboots too")
        resp = _client_call(
            cmd="set_resolution", width=w, height=h, in_width=in_w, in_height=in_h
        )
        if not resp.get("ok"):
            return 1
    cap = ""
    if resp.get("in_width"):
        cap = f"  (capture {resp['in_width']}x{resp['in_height']})"
    print(f"  publishing {resp.get('width')}x{resp.get('height')}{cap}")
    print("  note: attached applications must reconnect to see a new size")
    return 0


def _cmd_doctor(_args: argparse.Namespace) -> int:
    """Check every piece of the stack and say which one is missing."""
    import ctypes.util
    import shutil as _shutil
    from opal_c1.daemon import find_engine, lut_dir

    problems = 0

    def check(ok, label, detail="", hint=""):
        nonlocal problems
        mark = "\u2713" if ok else "\u2717"
        print(f"  {mark} {label}" + (f"  {detail}" if detail else ""))
        if not ok:
            problems += 1
            if hint:
                print(f"      \u21b3 {hint}")

    engine = find_engine()
    check(engine is not None, "engine binary", engine or "",
          "cargo build --release in engine/, or install the package")

    luts = lut_dir()
    n_luts = len(list(luts.glob("*.cube"))) if luts else 0
    check(n_luts > 0, "LUTs", f"{n_luts} in {luts}" if luts else "",
          "the luts/ directory ships with the repo")

    model = None
    if engine:
        for parent in Path(engine).resolve().parents:
            candidate = parent / "models/selfie_segmentation.onnx"
            if candidate.is_file():
                model = candidate
                break
    check(model is not None, "person-segmentation model", str(model or ""),
          "models/selfie_segmentation.onnx ships with the repo")

    loop = Path("/dev/video10")
    check(loop.exists(), "v4l2loopback node", str(loop),
          "sudo cp packaging/v4l2loopback*.conf /etc/modprobe.d/ "
          "&& sudo modprobe v4l2loopback")
    caps = Path("/sys/module/v4l2loopback/parameters/exclusive_caps")
    # The parameter prints as a bool array ("Y,N,N,..."), one slot per
    # possible device; ours is the first.
    caps_ok = caps.is_file() and caps.read_text().strip().split(",")[0] in ("Y", "1")
    check(caps_ok, "exclusive_caps",
          "", "apps only see the camera when the engine publishes; "
          "set exclusive_caps=1 (packaging/v4l2loopback.conf)")

    rules = Path("/etc/udev/rules.d/60-opal-c1.rules")
    check(rules.is_file(), "udev rules", str(rules),
          "sudo cp packaging/60-opal-c1.rules /etc/udev/rules.d/ "
          "&& sudo udevadm control --reload")

    camera = None
    for dev in Path("/sys/bus/usb/devices").glob("*"):
        vid = dev / "idVendor"
        if vid.is_file() and vid.read_text().strip() == "03e7":
            camera = (dev / "idProduct").read_text().strip()
            break
    labels = {"f63d": "Call firmware", "f63b": "Studio firmware",
              "f63c": "bootloader (transitional)"}
    check(camera is not None, "camera on USB",
          labels.get(camera, camera or ""),
          "plug the Opal C1 into a USB 3 port")

    quirks = Path("/sys/module/usbcore/parameters/quirks")
    quirk_ok = quirks.is_file() and "03e7:f63d" in quirks.read_text()
    check(quirk_ok, "USB NO_LPM quirk", "",
          "optional but recommended: sudo cp packaging/decomposer-usb.conf "
          "/etc/tmpfiles.d/ && sudo systemd-tmpfiles --create")

    layer = ctypes.util.find_library("gtk4-layer-shell")
    check(layer is not None, "gtk4-layer-shell", layer or "",
          "install gtk4-layer-shell for the panel")

    check(_shutil.which("pactl") is not None, "pactl (mic checks)", "",
          "optional: libpulse gives the panel its default-mic warning")

    resp = _client_call(cmd="status")
    if resp.get("ok"):
        check(True, "daemon", f"{resp.get('mode')} mode, engine "
              + ("running" if resp.get("engine_alive") else "stopped"))
    else:
        check(False, "daemon", "", "start it: decomposer daemon "
              "(or decomposer install-service)")

    print()
    if problems:
        print(f"  {problems} problem(s) found")
        return 1
    print("  everything looks healthy")
    return 0


def _cmd_install_service(_args: argparse.Namespace) -> int:
    """Install a systemd user service, resolved to this executable."""
    exe = Path(sys.argv[0]).resolve()
    if not exe.is_file():
        exe = Path(sys.executable).resolve()
    unit = f"""[Unit]
Description=decomposer camera daemon (Opal C1)
After=graphical-session.target

[Service]
ExecStart={exe} daemon
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    path = Path(base) / "systemd/user/decomposer.service"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit)
    print(f"  wrote {path}")
    print("  enable with: systemctl --user enable --now decomposer")
    return 0


def _print_models(models: list) -> None:
    if not models:
        print("  (no models in the chain; the bundled person model still "
              "backs blur/background)")
        return
    for i, m in enumerate(models):
        gone = "  (missing - bypassed)" if m.get("missing") else ""
        print(f"  [{i}] {m['path']}  {m['device']}  strength {m['strength']}{gone}")


def _cmd_model(args: argparse.Namespace) -> int:
    if args.action == "list" or args.action is None:
        resp = _client_call(cmd="status")
        if not resp.get("ok"):
            return 1
        _print_models(resp.get("models") or [])
        return 0
    if args.action == "strength":
        resp = _client_call(
            cmd="set_model_strength",
            index=int(args.arg), strength=float(args.value),
        )
        if not resp.get("ok"):
            return 1
        _print_models(resp.get("models") or [])
        return 0
    # add / rm rewrite the whole chain (engine restart).
    status = _client_call(cmd="status")
    if not status.get("ok"):
        return 1
    models = list(status.get("models") or [])
    if args.action == "add":
        models.append({
            "path": args.arg,
            "device": args.device,
            "strength": args.value if args.value is not None else 1.0,
        })
    elif args.action == "rm":
        i = int(args.arg)
        if not 0 <= i < len(models):
            print(f"no model at index {i}")
            return 1
        models.pop(i)
    resp = _client_call(cmd="set_models", models=models)
    if not resp.get("ok"):
        return 1
    print("  chain updated; the engine restarts")
    _print_models(resp.get("models") or [])
    return 0


def _cmd_fps(args: argparse.Namespace) -> int:
    if args.value is None:
        resp = _client_call(cmd="status")
    else:
        resp = _client_call(cmd="set_fps", fps=float(args.value))
    if not resp.get("ok"):
        return 1
    lo, hi = resp.get("fps_range") or (None, None)
    print(f"  fps       {resp.get('fps')}  (range {lo}-{hi} for "
          f"{resp.get('mode')} at {resp.get('width')}x{resp.get('height')})")
    for note in resp.get("notes") or []:
        print(f"    note: {note}")
    return 0


def _cmd_blur(args: argparse.Namespace) -> int:
    if args.strength is None and args.style is None:
        resp = _client_call(cmd="status")
    else:
        strength = None
        if args.strength is not None:
            strength = 0.0 if args.strength == "off" else float(args.strength)
        resp = _client_call(cmd="set_blur", strength=strength, style=args.style)
    if not resp.get("ok"):
        return 1
    style = "bokeh" if resp.get("blur_style") else "smooth"
    print(f"  background blur {resp.get('blur')} ({style})")
    return 0


def _cmd_background(args: argparse.Namespace) -> int:
    if args.path is None:
        resp = _client_call(cmd="status")
    elif args.path == "off":
        resp = _client_call(cmd="set_background", path=None)
    else:
        resp = _client_call(cmd="set_background", path=args.path)
    if not resp.get("ok"):
        return 1
    print(f"  background {resp.get('background') or '(blur or none)'}")
    return 0


def _cmd_clahe(args: argparse.Namespace) -> int:
    if args.strength is None:
        resp = _client_call(cmd="status")
    else:
        s = 0.0 if args.strength == "off" else float(args.strength)
        resp = _client_call(cmd="set_clahe", strength=s)
    if not resp.get("ok"):
        return 1
    print(f"  clarity (CLAHE) {resp.get('clahe')}")
    return 0


def _cmd_mirror(args: argparse.Namespace) -> int:
    values = {}
    if args.horizontal is not None:
        values["horizontal"] = args.horizontal == "on"
    if args.vertical is not None:
        values["vertical"] = args.vertical == "on"
    if not values:
        resp = _client_call(cmd="status")
    else:
        resp = _client_call(cmd="set_mirror", **values)
    if not resp.get("ok"):
        return 1
    print(f"  mirror horizontal {'on' if resp.get('mirror_h') else 'off'}, "
          f"vertical {'on' if resp.get('mirror_v') else 'off'}")
    return 0


def _cmd_switch(args: argparse.Namespace) -> int:
    # No banner before the daemon agrees: a refused switch (rate limit,
    # transition in progress) used to print "Switching..." and then deny it.
    print(f"  requesting {args.to} mode…")
    resp = _client_call(cmd="set_mode", mode=args.to)
    if not resp.get("ok"):
        return 1
    if args.to == "studio":
        print("  in Studio mode: manual focus and WB live; the C1 mic is off.")
    else:
        print("  in Call mode: mic and /dev/video0 are back.")
    _print_status(resp)
    return 0


def _cmd_set(args: argparse.Namespace) -> int:
    def region(text):
        parts = [int(v) for v in text.replace("x", ",").split(",") if v.strip()]
        if len(parts) != 4:
            raise SystemExit(f"  region must be x,y,w,h — got {text!r}")
        return parts

    values = {
        k: v
        for k, v in (
            ("brightness", args.brightness), ("contrast", args.contrast),
            ("saturation", args.saturation), ("hue", args.hue),
            ("sharpness", args.sharpness), ("exposure", args.exposure),
            ("iso", args.iso), ("focus", args.focus), ("wb", args.wb),
            ("effect", args.effect), ("scene", args.scene),
            ("af_region", region(args.af_region) if args.af_region else None),
            ("ae_region", region(args.ae_region) if args.ae_region else None),
        )
        if v is not None
    }
    if not values:
        return _cmd_status(args)
    resp = _client_call(cmd="set_camera", values=values)
    if not resp.get("ok"):
        return 1
    for k, v in (resp.get("applied") or {}).items():
        print(f"  {k} -> {v}")
    for k, why in (resp.get("refused") or {}).items():
        print(f"  {k}: {why}", file=sys.stderr)
    return 1 if resp.get("refused") else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="decomposer",
        description="Composer-inspired looks for the Opal C1 (Linux-first)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    px = sub.add_parser(
        "probe-xlink",
        help="Check whether the vendor bulk interface answers (read-only)",
    )
    px.add_argument("--depthai-mode", action="store_true",
                    help="Probe pid f63b instead of camera-mode f63d")
    px.add_argument("--timeout", type=int, default=2500)
    px.set_defaults(func=_cmd_probe_xlink)

    x = sub.add_parser(
        "probe-xu",
        help="Map the vendor UVC Extension Unit (read-only)",
        description=(
            "Interrogate the camera's UVC Extension Unit with GET requests only. "
            "This never writes to the device."
        ),
    )
    x.add_argument("--dev", default="/dev/video0", help="V4L2 node (default: /dev/video0)")
    x.add_argument("--unit", type=int, default=None, help="Extension unit ID (default: first found)")
    x.add_argument("--first", type=int, default=1, help="First selector to probe")
    x.add_argument("--last", type=int, default=80, help="Last selector to probe")
    x.add_argument("--json", default=None, help="Also write the full result to this JSON path")
    x.add_argument("--json-only", action="store_true", help="Suppress the progress ticker")
    x.set_defaults(func=_cmd_probe_xu)

    ci = sub.add_parser("camera-info", help="Attach over XLink and report device facts")
    ci.add_argument("--width", type=int, default=1920)
    ci.add_argument("--height", type=int, default=1080)
    ci.set_defaults(func=_cmd_camera_info)

    md = sub.add_parser("mode", help="Show Call/Studio mode and what each offers")
    md.set_defaults(func=_cmd_mode)

    ct = sub.add_parser(
        "control",
        help="Adjust camera controls",
        description=(
            "Exposure, gain and colour work in Call mode with no interruption. "
            "Focus and white balance exist only in Studio mode, which reboots the "
            "camera and takes away /dev/video0 and the microphone, so they need "
            "--studio. With no values given, prints the current controls."
        ),
    )
    ct.add_argument("--dev", default="/dev/video0")
    ct.add_argument("--width", type=int, default=1920)
    ct.add_argument("--height", type=int, default=1080)
    ct.add_argument("--brightness", type=int, default=None, help="0-255")
    ct.add_argument("--contrast", type=int, default=None, help="0-100")
    ct.add_argument("--saturation", type=int, default=None, help="0-100")
    ct.add_argument("--hue", type=int, default=None, help="-180..180")
    ct.add_argument("--sharpness", type=int, default=None, help="0-4")
    ct.add_argument("--exposure", type=int, default=None, help="Exposure time, microseconds")
    ct.add_argument("--iso", type=int, default=None, help="ISO / gain, 100-1600")
    ct.add_argument("--focus", type=int, default=None,
                    help="Lens position 0-255, or -1 for auto (Studio mode only)")
    ct.add_argument("--wb", type=int, default=None,
                    help="White balance 1000-12000 K, or -1 for auto (Studio mode only)")
    ct.add_argument("--studio", action="store_true",
                    help="Accept entering Studio mode (loses /dev/video0 and the mic)")
    ct.add_argument("--auto", action="store_true", help="Return everything to auto")
    ct.add_argument("--hold", type=float, default=6.0,
                    help="Seconds to hold Studio mode (settings last only while held)")
    ct.set_defaults(func=_cmd_control)

    sn = sub.add_parser(
        "stream-nv12",
        help="Studio mode: stream raw NV12 to stdout for the look engine",
        description=(
            "Holds the XLink connection (which is what keeps manual focus and white "
            "balance applied) and writes raw NV12 frames to stdout. Pipe into "
            "decomposer-engine --input -. Costs the microphone and /dev/video0."
        ),
    )
    sn.add_argument("--width", type=int, default=1920)
    sn.add_argument("--height", type=int, default=1080)
    sn.add_argument("--fps", type=float, default=30.0)
    sn.add_argument("--focus", type=int, default=None, help="Lens position 0-255, -1 for auto")
    sn.add_argument("--wb", type=int, default=None, help="White balance 1000-12000 K, -1 for auto")
    sn.add_argument("--exposure", type=int, default=None, help="Exposure time, microseconds")
    sn.add_argument("--iso", type=int, default=None, help="ISO 100-1600")
    sn.add_argument("--frames", type=int, default=0, help="Stop after N frames (0 = forever)")
    sn.set_defaults(func=_cmd_stream_nv12)

    dm = sub.add_parser(
        "daemon",
        help="Run the daemon: owns the camera and publishes the processed feed",
        description=(
            "Holds the camera and the look engine so that Studio-mode settings "
            "persist and /dev/video10 never disappears from under an application."
        ),
    )
    dm.add_argument("--output", default="/dev/video10")
    dm.add_argument("--width", type=int, default=1920)
    dm.add_argument("--height", type=int, default=1080)
    dm.add_argument("--fps", type=float, default=30.0)
    dm.add_argument(
        "--in-width", type=int, default=0,
        help="Capture width; 0 = same as output. 3840 with --in-height 2160 "
             "makes zoom lossless to 2x (costs ~3fps in Call mode)",
    )
    dm.add_argument("--in-height", type=int, default=0)
    dm.add_argument("--mode", choices=("call", "studio"), default="call")
    dm.add_argument(
        "--default-strength", type=float, default=None,
        help=(
            "Intensity a look starts at, 0.0 to 1.0 (default 0.5). The LUTs are "
            "the filters measured at full strength, which is stronger than they "
            "are usually wanted; each look then remembers what you dial in."
        ),
    )
    dm.add_argument(
        "--tray", action="store_true",
        help="Also register a StatusNotifierItem (for desktops without the "
             "Omarchy bar plugin; on Omarchy this duplicates the widget)",
    )
    dm.add_argument(
        "--seg-model", default=None,
        help="Person-segmentation ONNX model for background blur/replacement "
             "(default: the bundled MediaPipe model). Any model with an "
             "image input and a mask output works",
    )
    dm.add_argument(
        "--seg-device", choices=("cpu", "cuda"), default=None,
        help="Where segmentation runs (default cpu; cuda falls back to cpu "
             "if the CUDA runtime is missing)",
    )
    dm.set_defaults(func=_cmd_daemon)

    dr = sub.add_parser(
        "doctor",
        help="Check the whole stack and say what is missing",
    )
    dr.set_defaults(func=_cmd_doctor)

    isv = sub.add_parser(
        "install-service",
        help="Install a systemd user service for the daemon",
    )
    isv.set_defaults(func=_cmd_install_service)

    idt = sub.add_parser(
        "install-desktop",
        help="Install the app-menu launcher with a working absolute path",
    )
    idt.set_defaults(func=_cmd_install_desktop)

    ip = sub.add_parser(
        "install-plugin",
        help="Install the Omarchy bar widget that toggles the overlay",
    )
    ip.add_argument("--add-to-bar", action="store_true",
                    help="Also add it to shell.json (a backup is kept)")
    ip.add_argument("--side", choices=("left", "center", "right"), default="right")
    ip.set_defaults(func=_cmd_install_plugin)

    ui = sub.add_parser("gui", help="Open the overlay")
    ui.add_argument("--replace", action="store_true",
                    help="Take over from a panel that is already running")
    ui.set_defaults(func=_cmd_gui)

    tg = sub.add_parser(
        "toggle",
        help="Show the overlay, or hide it if it is already up",
        description=(
            "Same as `gui`. Running it while the overlay is open hides it, so a "
            "single bar entry or keybind toggles the panel."
        ),
    )
    tg.add_argument("--replace", action="store_true",
                    help="Take over from a panel that is already running")
    tg.set_defaults(func=_cmd_gui)

    st = sub.add_parser("status", help="Show what the daemon is doing")
    st.set_defaults(func=_cmd_status)

    sp = sub.add_parser("stop", help="Stop a running daemon")
    sp.set_defaults(func=_cmd_stop)

    lk = sub.add_parser("look", help="Change the look on a running daemon")
    lk.add_argument("name", nargs="?", default=None, help="Look name")
    lk.add_argument("--strength", type=float, default=None, help="0.0 to 1.0")
    lk.set_defaults(func=_cmd_look)

    ps = sub.add_parser(
        "preset",
        help="Save, load, list or delete a named configuration",
        description=(
            "A preset captures the look and its intensity, mirroring, the "
            "overlay and the camera controls. The mode is recorded but not "
            "switched into on load unless --with-mode is given, since switching "
            "reboots the camera and takes about fifteen seconds."
        ),
    )
    ps.add_argument("action", choices=("save", "load", "list", "delete"))
    ps.add_argument("name", nargs="?", default=None)
    ps.add_argument("--with-mode", action="store_true",
                    help="Also switch to the mode the preset was saved in")
    ps.set_defaults(func=_cmd_preset)

    ov = sub.add_parser(
        "overlay",
        help="Composite an image over the video",
        description=(
            "Places a PNG over the frame - a logo, a watermark, a lower third. "
            "Composited on the GPU before the conversion back to YCbCr, so it "
            "keeps its own colours whatever look is applied. Position is in "
            "output pixels; width and height are maximums it is fitted into, "
            "keeping aspect ratio, and 0 means unconstrained. Pass 'off' to clear."
        ),
    )
    ov.add_argument("path", nargs="?", default=None, help="PNG file, or 'off'")
    ov.add_argument("--x", type=int, default=None)
    ov.add_argument("--y", type=int, default=None)
    ov.add_argument("--width", type=int, default=None, help="Max width, 0 = unconstrained")
    ov.add_argument("--height", type=int, default=None, help="Max height, 0 = unconstrained")
    ov.add_argument("--opacity", type=float, default=None, help="0.0 to 1.0")
    ov.set_defaults(func=_cmd_overlay)

    zo = sub.add_parser(
        "zoom",
        help="Digital zoom and pan",
        description=(
            "Crops and scales in the shader at no cost. Lossless up to the "
            "capture/output ratio; upscaling beyond it. 'off' or 1 resets. "
            "Pan positions the crop window, -1..1 across the available margin."
        ),
    )
    zo.add_argument("factor", nargs="?", default=None, help="1.0-8.0, or 'off'")
    zo.add_argument("--x", type=float, default=None, help="pan x, -1..1")
    zo.add_argument("--y", type=float, default=None, help="pan y, -1..1")
    zo.set_defaults(func=_cmd_zoom)

    rs = sub.add_parser(
        "resolution",
        help="Published resolution (and optional 4K capture)",
        description=(
            "Changes the size published to the virtual camera. The engine "
            "restarts; in Studio the camera session reboots as well. The "
            "loopback keeps its old format while a consumer holds it, so "
            "apps must reconnect."
        ),
    )
    rs.add_argument("size", nargs="?", choices=sorted(RESOLUTIONS), default=None)
    rs.add_argument("--capture-4k", action="store_true",
                    help="Capture 4K while publishing smaller: lossless zoom to 2x")
    rs.set_defaults(func=_cmd_resolution)

    md = sub.add_parser(
        "model",
        help="The ONNX model chain over the feed",
        description=(
            "Run the feed through your own models. A one-channel output "
            "joins the person mask (strength = weight); a three-channel "
            "output recolors the frame (strength = blend), applied as a "
            "detail-preserving residual. Strength changes are live; adding, "
            "removing, or changing device restarts the engine."
        ),
    )
    md.add_argument("action", nargs="?", default=None,
                    choices=("list", "add", "rm", "strength"))
    md.add_argument("arg", nargs="?", default=None,
                    help="add: model path; rm/strength: index")
    md.add_argument("value", nargs="?", type=float, default=None,
                    help="Strength, 0.0 to 1.0")
    md.add_argument("--device", choices=("cpu", "cuda"), default="cpu",
                    help="Where the model runs (add only)")
    md.set_defaults(func=_cmd_model)

    fp = sub.add_parser(
        "fps",
        help="Capture frame rate (Studio only; Call is fixed at 30)",
        description=(
            "Studio mode drives the sensor directly: 1.67-42 fps for the "
            "16:9 resolutions, up to 30 at 4000x3000, up to 10 at "
            "5312x6000. Changing it re-enters Studio, which reboots the "
            "camera's firmware. Call mode's UVC firmware is fixed at 30."
        ),
    )
    fp.add_argument("value", nargs="?", default=None, help="frames per second")
    fp.set_defaults(func=_cmd_fps)

    bl = sub.add_parser(
        "blur",
        help="Background blur",
        description=(
            "Person segmentation (bundled MediaPipe model, or your own via "
            "'daemon --seg-model') masks you out; everything else gets a "
            "disc blur on the GPU. 'off' or 0 disables it. External mask "
            "producers can drive the effect through the engine's mask.sock."
        ),
    )
    bl.add_argument("strength", nargs="?", default=None, help="0.0-1.0, or 'off'")
    bl.add_argument("--style", choices=("smooth", "bokeh"), default=None,
                    help="smooth averages the background; bokeh blooms "
                         "highlights into balls")
    bl.set_defaults(func=_cmd_blur)

    bg = sub.add_parser(
        "background",
        help="Replace the background with an image",
        description=(
            "Uses the same person mask as blur, but composites a PNG behind "
            "you instead. 'off' goes back to blur (or to nothing)."
        ),
    )
    bg.add_argument("path", nargs="?", default=None,
                    help="PNG path, or 'off' to clear")
    bg.set_defaults(func=_cmd_background)

    ch = sub.add_parser(
        "clahe",
        help="Local contrast (CLAHE)",
        description=(
            "Adaptive local contrast on the GPU: per-tile histogram "
            "equalization, clip-limited, blended by strength. 'off' or 0 "
            "disables it and skips the extra passes entirely."
        ),
    )
    ch.add_argument("strength", nargs="?", default=None, help="0.0-1.0, or 'off'")
    ch.set_defaults(func=_cmd_clahe)

    mi = sub.add_parser(
        "mirror",
        help="Mirror the published image",
        description=(
            "Applied on the GPU at no cost. Both modes share one setting, since "
            "Studio mode is corrected to Call mode's orientation on the device. "
            "Both axes together is a 180 degree rotation."
        ),
    )
    mi.add_argument("--horizontal", choices=("on", "off"), default=None)
    mi.add_argument("--vertical", choices=("on", "off"), default=None)
    mi.set_defaults(func=_cmd_mirror)

    sw = sub.add_parser("switch", help="Switch the daemon between call and studio")
    sw.add_argument("to", choices=("call", "studio"))
    sw.set_defaults(func=_cmd_switch)

    se = sub.add_parser(
        "set",
        help="Adjust camera controls via the daemon",
        description=(
            "Routed to whichever path the current mode allows. focus and wb need "
            "Studio mode; everything else works in either."
        ),
    )
    for name, helptext in (
        ("brightness", "0-255"), ("contrast", "0-100"), ("saturation", "0-100"),
        ("hue", "-180..180"), ("sharpness", "0-4"),
        ("exposure", "microseconds"), ("iso", "100-1600"),
        ("focus", "0-255, -1 for auto (Studio)"), ("wb", "1000-12000 K, -1 for auto (Studio)"),
    ):
        se.add_argument(f"--{name}", type=int, default=None, help=helptext)
    se.add_argument("--af-region", default=None, metavar="X,Y,W,H",
                    help="Focus on a frame region (Studio)")
    se.add_argument("--ae-region", default=None, metavar="X,Y,W,H",
                    help="Meter exposure from a frame region (Studio)")
    se.add_argument("--effect", default=None,
                    help="On-device effect: off, sepia, mono, negative, posterize, "
                         "solarize, aqua, blackboard, whiteboard (Studio)")
    se.add_argument("--scene", default=None,
                    help="On-device scene program, e.g. face_priority, portrait, "
                         "night; off resets (Studio)")
    se.set_defaults(func=_cmd_set)

    return p


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
