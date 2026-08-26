#!/usr/bin/env python3
"""Check a reference capture before trusting it.

A capture that clips is not recoverable by differencing. If the baseline reads
255 and the look reads 255, that colour tells us nothing about what the look
did - the information is gone, not merely distorted. So every capture gets
checked as it is taken, rather than discovering at fit time that most of the
cube was destroyed.

    ./scripts/check_reference.py references/look-off.png
    ./scripts/check_reference.py references/look-G1.png   # also checks framing
                                                          # drift vs look-off

With --exact (for references rendered straight through the look shaders, no
camera): clipping is reported but does not fail the check, because a look that
crushes shadows to its floor is *behaving*, not being measured badly. Only the
fiducials and framing still gate.
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

STEPS, PATCH, MARGIN = 16, 22, 44
COLS = ROWS = STEPS * 4
TW = MARGIN * 2 + COLS * PATCH
TH = MARGIN * 2 + ROWS * PATCH + PATCH * 3

# Fiducial centres in target coordinates, clockwise from top-left.
FIDS = np.float32([
    [MARGIN // 2, MARGIN // 2],
    [TW - MARGIN // 2, MARGIN // 2],
    [TW - MARGIN // 2, TH - MARGIN // 2],
    [MARGIN // 2, TH - MARGIN // 2],
])

# Fraction of the cube that may be destroyed before a set is worth redoing.
MAX_DAMAGED = 0.05


def find_fiducials(img):
    """The four white squares with a black core, isolated in a dark surround.

    Thresholding low matters: the screen is dim in-frame, and a high threshold
    lets the bright cube patches flood into blobs that swamp the fiducials.
    """
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _, bw = cv2.threshold(g, 140, 255, cv2.THRESH_BINARY)
    n, _, stats, cent = cv2.connectedComponentsWithStats(bw, 8)
    cands = []
    for i in range(1, n):
        x, y, w, h, _ = stats[i]
        if not (14 <= w <= 60 and 14 <= h <= 60):
            continue
        if abs(w - h) > max(w, h) * 0.45:
            continue
        pad = max(w, h) // 2
        y0, y1 = max(0, y - pad), min(img.shape[0], y + h + pad)
        x0, x1 = max(0, x - pad), min(img.shape[1], x + w + pad)
        ring = g[y0:y1, x0:x1].copy()
        ring[y - y0:y - y0 + h, x - x0:x - x0 + w] = 0
        if np.percentile(ring, 80) > 100:   # a patch, not a fiducial
            continue
        cands.append((cent[i][0], cent[i][1]))
    if len(cands) != 4:
        return None
    pts = np.float32(cands)
    s, d = pts.sum(1), np.diff(pts, axis=1).ravel()
    return np.float32([pts[np.argmin(s)], pts[np.argmin(d)],
                       pts[np.argmax(s)], pts[np.argmax(d)]])


def sample(img, quad):
    """Flatten the chart and read every patch centre."""
    flat = cv2.warpPerspective(img, cv2.getPerspectiveTransform(quad, FIDS), (TW, TH))
    vals, ref = [], []
    for b in range(STEPS):
        for g in range(STEPS):
            for r in range(STEPS):
                col = (b % 4) * STEPS + r
                row = (b // 4) * STEPS + g
                cx = MARGIN + col * PATCH + PATCH // 2
                cy = MARGIN + row * PATCH + PATCH // 2
                blk = flat[cy - 3:cy + 4, cx - 3:cx + 4].reshape(-1, 3)
                vals.append(np.median(blk, 0)[::-1])       # BGR -> RGB
                ref.append([round(r * 255 / 15), round(g * 255 / 15), round(b * 255 / 15)])
    return flat, np.array(vals), np.array(ref, float)


def main(argv):
    exact = "--exact" in argv
    argv = [a for a in argv if a != "--exact"]
    if len(argv) < 2:
        print(__doc__)
        return 2
    path = Path(argv[1])
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None:
        print(f"cannot read {path}")
        return 2
    H, W = img.shape[:2]
    print(f"{path.name}  {W}x{H}")

    quad = find_fiducials(img)
    if quad is None:
        print("FAIL  could not locate all four fiducials.")
        print("      The chart must be fully in frame with a dark border around it.")
        return 1

    area = cv2.contourArea(quad.reshape(-1, 1, 2))
    print(f"fiducials found, chart covers {100 * area / (W * H):.1f}% of frame")

    _, vals, ref = sample(img, quad)

    # Only clipping the target did not ask for destroys information.
    blown = (vals >= 254) & (ref < 255)
    crushed = (vals <= 1) & (ref > 0)
    for i, c in enumerate("RGB"):
        print(f"  {c}: blown {blown[:, i].sum():4d}   crushed {crushed[:, i].sum():4d}")
    damaged = (blown | crushed).any(1)
    frac = damaged.mean()
    print(f"destroyed patches: {damaged.sum()}/{len(vals)} = {100 * frac:.1f}%")

    # Framing must not move between captures or the pair cannot be compared.
    base = path.parent / "look-off.png"
    drift_ok = True
    if path.name != "look-off.png" and base.exists():
        bimg = cv2.imread(str(base), cv2.IMREAD_COLOR)
        bquad = find_fiducials(bimg) if bimg is not None else None
        if bquad is None:
            print("note: baseline unreadable, skipping framing check")
        else:
            d = np.linalg.norm(quad - bquad, axis=1)
            print(f"framing drift vs baseline: max {d.max():.1f}px  mean {d.mean():.1f}px")
            if d.max() > 3.0:
                drift_ok = False
                print("FAIL  the chart or camera moved. This capture cannot be paired.")

    ok = (exact or frac <= MAX_DAMAGED) and drift_ok
    print()
    if exact and frac > MAX_DAMAGED:
        print(f"note: {100 * frac:.1f}% of patches rail-clipped — genuine look output in --exact mode.")
    if ok:
        print(f"PASS  {100 * (1 - frac):.1f}% of the cube is measurable.")
    else:
        if frac > MAX_DAMAGED:
            print(f"FAIL  {100 * frac:.1f}% of the cube is destroyed (limit {100 * MAX_DAMAGED:.0f}%).")
            hi = blown.sum() > crushed.sum()
            print("      Too bright." if hi else "      Too dark.", end=" ")
            print("Lower screen brightness / exposure." if hi else "Raise exposure.")
            rb = blown[:, [0, 2]].sum() + crushed[:, [0, 2]].sum()
            g_ = blown[:, 1].sum() + crushed[:, 1].sum()
            if rb > g_ * 3:
                print("      Red and blue clip far harder than green: saturation is too high.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
