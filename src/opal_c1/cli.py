"""CLI for decomposer."""

from __future__ import annotations

import argparse
import sys
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

    return p


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
