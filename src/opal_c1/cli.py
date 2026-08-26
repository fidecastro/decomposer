"""CLI for decomposer."""

from __future__ import annotations

import argparse
import sys
from contextlib import suppress
from pathlib import Path



def _cmd_devices(_: argparse.Namespace) -> int:
    from opal_c1.capture import list_cameras
    cams = list_cameras()
    if not cams:
        print("No cameras found.")
        return 1
    for c in cams:
        print(f"[{c.index}] {c.name}  backend={c.backend}")
    print(
        "\nTip (macOS): indices usually follow system order — "
        "Opal Composer virtual cam, Opal C1, FaceTime, …"
    )
    return 0


def _cmd_looks(_: argparse.Namespace) -> int:
    from opal_c1.looks import list_looks
    for name in list_looks():
        print(name)
    return 0


def _cmd_preview(args: argparse.Namespace) -> int:
    import cv2

    from opal_c1.capture import open_camera, read_frame
    from opal_c1.looks import apply_look
    cap = open_camera(index=args.device, width=args.width, height=args.height)
    win = "decomposer"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    print(f"Preview device={args.device} look={args.look}  (q to quit)")
    try:
        while True:
            frame = read_frame(cap)
            out = apply_look(frame, args.look)
            cv2.imshow(win, out)
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                break
    finally:
        cap.release()
        cv2.destroyAllWindows()
    return 0


def _cmd_capture_ref(args: argparse.Namespace) -> int:
    import cv2

    from opal_c1.capture import grab_still
    from opal_c1.looks import apply_look
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    frame = grab_still(index=args.device, width=args.width, height=args.height)
    if args.look and args.look != "none":
        frame = apply_look(frame, args.look)
    if not cv2.imwrite(str(out), frame):
        print(f"Failed to write {out}", file=sys.stderr)
        return 1
    print(f"Wrote {out} ({frame.shape[1]}x{frame.shape[0]})")
    return 0


def _cmd_virtual(args: argparse.Namespace) -> int:
    from opal_c1.capture import open_camera, read_frame
    from opal_c1.looks import apply_look
    from opal_c1.virtualcam import frames_to_virtual_cam

    cap = open_camera(index=args.device, width=args.width, height=args.height)

    def gen():
        try:
            while True:
                frame = read_frame(cap)
                yield apply_look(frame, args.look)
        finally:
            cap.release()

    print(f"Streaming device={args.device} look={args.look} → virtual cam  (Ctrl+C to stop)")
    try:
        frames_to_virtual_cam(gen(), width=args.width, height=args.height, fps=args.fps)
    except KeyboardInterrupt:
        print("\nStopped.")
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
    """Apply controls, routing each to the mode that can serve it."""
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


def _cmd_gui(_: argparse.Namespace) -> int:
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
    return gui_main()


def _cmd_daemon(args: argparse.Namespace) -> int:
    from opal_c1.daemon import Daemon

    return Daemon(
        output=args.output, width=args.width, height=args.height, fps=args.fps
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
    resp = _client_call(cmd="stop")
    if resp.get("ok"):
        print("  daemon stopping")
    return 0 if resp.get("ok") else 1


def _cmd_look(args: argparse.Namespace) -> int:
    resp = _client_call(cmd="set_look", look=args.name, strength=args.strength)
    if not resp.get("ok"):
        return 1
    print(f"  look -> {resp.get('look')} @ {resp.get('strength')}")
    return 0


def _cmd_switch(args: argparse.Namespace) -> int:
    if args.to == "studio":
        print("Switching to Studio mode - this takes ~5s and costs the C1 microphone.")
    else:
        print("Switching to Call mode - the camera reboots, so this takes ~15s.")
    resp = _client_call(cmd="set_mode", mode=args.to)
    if not resp.get("ok"):
        return 1
    _print_status(resp)
    return 0


def _cmd_set(args: argparse.Namespace) -> int:
    values = {
        k: v
        for k, v in (
            ("brightness", args.brightness), ("contrast", args.contrast),
            ("saturation", args.saturation), ("hue", args.hue),
            ("sharpness", args.sharpness), ("exposure", args.exposure),
            ("iso", args.iso), ("focus", args.focus), ("wb", args.wb),
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

    d = sub.add_parser("devices", help="List OpenCV camera indices")
    d.set_defaults(func=_cmd_devices)

    l = sub.add_parser("looks", help="List available looks")
    l.set_defaults(func=_cmd_looks)

    def add_device_opts(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--device", type=int, default=0, help="OpenCV camera index")
        sp.add_argument("--width", type=int, default=1280)
        sp.add_argument("--height", type=int, default=720)

    pr = sub.add_parser("preview", help="Local preview window with a look")
    add_device_opts(pr)
    pr.add_argument("--look", default="none", help="Look name (see: decomposer looks)")
    pr.set_defaults(func=_cmd_preview)

    cr = sub.add_parser("capture-ref", help="Save a reference still")
    add_device_opts(cr)
    cr.add_argument("--look", default="none")
    cr.add_argument("--out", required=True, help="Output PNG path")
    cr.set_defaults(func=_cmd_capture_ref)

    v = sub.add_parser("virtual", help="Stream looked frames to a virtual webcam")
    add_device_opts(v)
    v.add_argument("--look", default="process")
    v.add_argument("--fps", type=float, default=30.0)
    v.set_defaults(func=_cmd_virtual)

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
    dm.add_argument("--mode", choices=("call", "studio"), default="call")
    dm.set_defaults(func=_cmd_daemon)

    ui = sub.add_parser("gui", help="Open the control panel")
    ui.set_defaults(func=_cmd_gui)

    st = sub.add_parser("status", help="Show what the daemon is doing")
    st.set_defaults(func=_cmd_status)

    sp = sub.add_parser("stop", help="Stop a running daemon")
    sp.set_defaults(func=_cmd_stop)

    lk = sub.add_parser("look", help="Change the look on a running daemon")
    lk.add_argument("name", nargs="?", default=None, help="Look name")
    lk.add_argument("--strength", type=float, default=None, help="0.0 to 1.0")
    lk.set_defaults(func=_cmd_look)

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
    se.set_defaults(func=_cmd_set)

    return p


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
