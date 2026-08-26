"""The decomposer mark.

Opal Composer's logo is a circle and a triangle in black and white, where the
overlap flips colour. This borrows that idea and states it in Omarchy's pixel
vocabulary: a pixelated semicircle standing in for the D, with a triangle
driven into it, and the intersection punched out rather than filled.

Everything is generated from the grid below, so the SVG and the tray pixmap
are always the same shape.
"""

from __future__ import annotations

GRID = 16

# Geometry tuned so both shapes stay readable. Earlier attempts drove the
# triangle deep into the bowl, which hollowed the D out into a bracket or an
# outline; the overlap wants to be a seam between two solid forms, the way
# Composer's is, not a subtraction that destroys one of them.
_CX, _R = 1.0, 7.0
_TX0, _TX1 = 7.5, 15.0
_TY0, _TY1 = 1.5, 14.5


def _in_bowl(x: float, y: float) -> bool:
    cy = GRID / 2
    return x >= _CX and (x - _CX) ** 2 + (y - cy) ** 2 <= _R * _R


def _in_triangle(x: float, y: float) -> bool:
    cy = GRID / 2

    def side(x1, y1, x2, y2):
        return (x2 - x1) * (y - y1) - (y2 - y1) * (x - x1)

    s = [
        side(_TX0, _TY0, _TX0, _TY1),
        side(_TX0, _TY1, _TX1, cy),
        side(_TX1, cy, _TX0, _TY0),
    ]
    return all(v >= 0 for v in s) or all(v <= 0 for v in s)


def cells() -> list[list[int]]:
    """The mark as a GRID x GRID bitmap. Overlap is punched out, not filled."""
    grid = [
        [
            1 if _in_bowl(x + 0.5, y + 0.5) != _in_triangle(x + 0.5, y + 0.5) else 0
            for x in range(GRID)
        ]
        for y in range(GRID)
    ]
    # A lone pixel is noise at bar size, not detail.
    out = [row[:] for row in grid]
    for y in range(GRID):
        for x in range(GRID):
            if not grid[y][x]:
                continue
            neighbours = sum(
                grid[y + dy][x + dx]
                for dy in (-1, 0, 1)
                for dx in (-1, 0, 1)
                if (dy or dx) and 0 <= y + dy < GRID and 0 <= x + dx < GRID
            )
            if neighbours <= 1:
                out[y][x] = 0
    return out


def svg(color: str = "#ffffff", cell: int = 8, pad: int = 1) -> str:
    """The mark as SVG, one rect per pixel so it stays crisp at any size."""
    grid = cells()
    size = (GRID + pad * 2) * cell
    rects = [
        f'<rect x="{(x + pad) * cell}" y="{(y + pad) * cell}" '
        f'width="{cell}" height="{cell}"/>'
        for y, row in enumerate(grid)
        for x, v in enumerate(row)
        if v
    ]
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 {size} {size}">'
        f'<g fill="{color}" shape-rendering="crispEdges">{"".join(rects)}</g></svg>'
    )


def argb_pixmap(size: int = 22) -> tuple[int, int, bytes]:
    """(width, height, ARGB32 big-endian) for a StatusNotifierItem icon.

    Drawn by nearest-neighbour from the grid: the mark is pixel art, so
    sampling is the correct scaler rather than a compromise.
    """
    grid = cells()
    pad = 1
    span = GRID + pad * 2
    buf = bytearray()
    for py in range(size):
        gy = py * span // size - pad
        for px in range(size):
            gx = px * span // size - pad
            on = 0 <= gx < GRID and 0 <= gy < GRID and grid[gy][gx]
            # White, fully opaque where the mark is; transparent elsewhere, so
            # the bar's own background shows through.
            buf.extend(b"\xff\xff\xff\xff" if on else b"\x00\x00\x00\x00")
    return size, size, bytes(buf)


def ascii_art() -> str:
    return "\n".join(
        "".join("##" if v else "  " for v in row) for row in cells()
    )
