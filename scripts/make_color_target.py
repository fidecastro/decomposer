#!/usr/bin/env python3
"""Generate the colour target used for look matching.

To recover what a look does we need pairs: what a colour was, and what it
became. A face against a wall only tells us about a narrow slice of colour
space, and a LUT fitted from that is unconstrained everywhere else - it would
invent the rest. This target walks the whole RGB cube so every region is
measured rather than guessed.

Layout: 16 x 16 x 16 = 4096 patches in a 64 x 64 grid, plus solid fiducials at
the corners so the grid can be located in a photograph of a screen, and a strip
of skin tones along the bottom because that is what these looks are tuned for
and it deserves denser sampling than a uniform cube provides.
"""

import struct
import zlib
from pathlib import Path

STEPS = 16          # per axis -> 4096 patches
PATCH = 22          # pixels per patch
MARGIN = PATCH * 2  # room for fiducials
SKIN_H = PATCH * 3


def rgb_cube():
    """Patches in a 64x64 arrangement: R,G vary within a tile, B across tiles."""
    grid = STEPS * STEPS  # 256 across, but we lay out as 64 x 64
    out = {}
    for b in range(STEPS):
        for g in range(STEPS):
            for r in range(STEPS):
                # Tile index over the blue axis, 4 x 4 tiles of 16 x 16 patches.
                tile_x, tile_y = b % 4, b // 4
                col = tile_x * STEPS + r
                row = tile_y * STEPS + g
                out[(col, row)] = (
                    round(r * 255 / (STEPS - 1)),
                    round(g * 255 / (STEPS - 1)),
                    round(b * 255 / (STEPS - 1)),
                )
    return out, STEPS * 4, STEPS * 4


SKIN = [
    (255, 224, 196), (255, 205, 175), (241, 194, 165), (224, 172, 145),
    (198, 148, 120), (172, 123, 96), (145, 100, 76), (120, 80, 58),
    (95, 62, 44), (72, 46, 32), (54, 34, 24), (38, 24, 17),
]


def build():
    patches, cols, rows = rgb_cube()
    w = MARGIN * 2 + cols * PATCH
    h = MARGIN * 2 + rows * PATCH + SKIN_H
    img = bytearray([24] * (w * h * 3))  # near-black surround

    def fill(x0, y0, x1, y1, rgb):
        for y in range(max(0, y0), min(h, y1)):
            base = y * w * 3
            for x in range(max(0, x0), min(w, x1)):
                i = base + x * 3
                img[i], img[i + 1], img[i + 2] = rgb

    for (col, row), rgb in patches.items():
        x0 = MARGIN + col * PATCH
        y0 = MARGIN + row * PATCH
        fill(x0, y0, x0 + PATCH, y0 + PATCH, rgb)

    # Skin strip along the bottom.
    strip_y = MARGIN + rows * PATCH
    band = (w - MARGIN * 2) // len(SKIN)
    for i, rgb in enumerate(SKIN):
        fill(MARGIN + i * band, strip_y, MARGIN + (i + 1) * band, strip_y + SKIN_H, rgb)

    # Corner fiducials: white squares with a black core, easy to find and
    # unambiguous about orientation (top-left is doubled).
    def fiducial(cx, cy, doubled=False):
        s = MARGIN - 6
        fill(cx - s // 2, cy - s // 2, cx + s // 2, cy + s // 2, (255, 255, 255))
        c = s // 3
        fill(cx - c // 2, cy - c // 2, cx + c // 2, cy + c // 2, (0, 0, 0))
        if doubled:
            fill(cx - s // 8, cy - s // 8, cx + s // 8, cy + s // 8, (255, 255, 255))

    fiducial(MARGIN // 2, MARGIN // 2, doubled=True)
    fiducial(w - MARGIN // 2, MARGIN // 2)
    fiducial(MARGIN // 2, h - MARGIN // 2)
    fiducial(w - MARGIN // 2, h - MARGIN // 2)
    return w, h, bytes(img)


def write_png(path, w, h, rgb):
    raw = b"".join(b"\x00" + rgb[y * w * 3:(y + 1) * w * 3] for y in range(h))

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    Path(path).write_bytes(png)


if __name__ == "__main__":
    w, h, rgb = build()
    out = Path(__file__).resolve().parents[1] / "references" / "color-target.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    write_png(out, w, h, rgb)
    print(f"wrote {out}  {w}x{h}  ({STEPS}^3 = {STEPS**3} patches + {len(SKIN)} skin tones)")
