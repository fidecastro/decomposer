"""CLI for decomposer."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2

from opal_c1.capture import grab_still, list_cameras, open_camera, read_frame
from opal_c1.looks import apply_look, list_looks


def _cmd_devices(_: argparse.Namespace) -> int:
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
    for name in list_looks():
        print(name)
    return 0


def _cmd_preview(args: argparse.Namespace) -> int:
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

    return p


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
