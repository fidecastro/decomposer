#!/usr/bin/env python3
"""Extract .cube LUTs from the Composer reference renders.

The references were produced by running references/color-target.png through
Composer's own shaders, so look-off.png is the pristine target and every other
look-*.png is that exact image transformed. Each look was verified to be a pure
colour transform - pixels sharing an input colour always share an output colour,
with zero spread - so a LUT reproduces it exactly rather than approximately.

The target walks a 16^3 cube, which means the extracted LUT is measured at every
one of its own grid points. Nothing is interpolated or invented here; the only
interpolation happens later, in the shader, between measured values.
"""

import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
REFS = ROOT / "references"
OUT = ROOT / "luts"
STEPS = 16  # must match make_color_target.py


def decode(path: Path) -> np.ndarray:
    """PNG -> HxWx3 uint8, via ffmpeg so we do not hand-roll a PNG decoder."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(path)],
        capture_output=True, text=True, check=True,
    )
    w, h = (int(v) for v in probe.stdout.strip().split("x"))
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path),
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True,
    ).stdout
    return np.frombuffer(raw, np.uint8).reshape(h, w, 3)


def build_mapping(base: np.ndarray, look: np.ndarray) -> dict:
    """input colour -> output colour, over every pixel in the reference."""
    b = base.reshape(-1, 3).astype(np.int64)
    o = look.reshape(-1, 3).astype(np.int64)
    key = b[:, 0] * 65536 + b[:, 1] * 256 + b[:, 2]
    uniq, first = np.unique(key, return_index=True)
    return {int(k): tuple(int(v) for v in o[i]) for k, i in zip(uniq, first)}


def write_cube(path: Path, name: str, mapping: dict) -> int:
    """Write a .cube. Red varies fastest, then green, then blue."""
    levels = [round(i * 255 / (STEPS - 1)) for i in range(STEPS)]
    missing = 0
    lines = [
        f'TITLE "{name}"',
        f"LUT_3D_SIZE {STEPS}",
        "DOMAIN_MIN 0.0 0.0 0.0",
        "DOMAIN_MAX 1.0 1.0 1.0",
        "",
    ]
    for bi in range(STEPS):
        for gi in range(STEPS):
            for ri in range(STEPS):
                key = levels[ri] * 65536 + levels[gi] * 256 + levels[bi]
                rgb = mapping.get(key)
                if rgb is None:
                    # Should not happen: the target contains every grid point.
                    missing += 1
                    rgb = (levels[ri], levels[gi], levels[bi])
                lines.append(" ".join(f"{v / 255.0:.6f}" for v in rgb))
    path.write_text("\n".join(lines) + "\n")
    return missing


def main() -> int:
    base_path = REFS / "look-off.png"
    if not base_path.is_file():
        print(f"missing baseline {base_path}", file=sys.stderr)
        return 1
    base = decode(base_path)
    OUT.mkdir(exist_ok=True)

    looks = sorted(
        p for p in REFS.glob("look-*.png") if p.stem != "look-off"
    )
    print(f"{'look':<10} {'grid pts':>9} {'missing':>8}  {'round trip max err':>19}")
    for path in looks:
        name = path.stem.replace("look-", "")
        look = decode(path)
        if look.shape != base.shape:
            print(f"{name:<10} SKIPPED: {look.shape} != baseline {base.shape}")
            continue
        mapping = build_mapping(base, look)
        missing = write_cube(OUT / f"{name}.cube", name, mapping)

        # Verify by applying the LUT back to the baseline and comparing.
        err = verify(base, look, mapping)
        print(f"{name:<10} {len(mapping):>9} {missing:>8}  {err:>19}")
    print(f"\nwrote {len(looks)} LUTs to {OUT}")
    return 0


def verify(base: np.ndarray, look: np.ndarray, mapping: dict) -> int:
    """Largest channel error when the mapping is replayed onto the baseline."""
    b = base.reshape(-1, 3).astype(np.int64)
    key = b[:, 0] * 65536 + b[:, 1] * 256 + b[:, 2]
    uniq, inv = np.unique(key, return_inverse=True)
    table = np.array([mapping[int(k)] for k in uniq], np.int64)
    return int(np.abs(table[inv] - look.reshape(-1, 3).astype(np.int64)).max())


if __name__ == "__main__":
    raise SystemExit(main())
