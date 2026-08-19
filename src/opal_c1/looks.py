"""Host-side look engine approximating Composer photo effects.

P0 looks map from Composer's CIPhotoEffect* pipelines. These are intentional
approximations for Linux/Windows — tuned later against Mac reference stills.
"""

from __future__ import annotations

from typing import Callable

import cv2
import numpy as np

LookFn = Callable[[np.ndarray], np.ndarray]


def _as_float(bgr: np.ndarray) -> np.ndarray:
    return bgr.astype(np.float32) / 255.0


def _as_u8(bgr: np.ndarray) -> np.ndarray:
    return np.clip(bgr * 255.0, 0, 255).astype(np.uint8)


def _grain(bgr: np.ndarray, amount: float = 0.02) -> np.ndarray:
    noise = np.random.normal(0.0, amount, bgr.shape).astype(np.float32)
    return np.clip(bgr + noise, 0.0, 1.0)


def look_none(frame_bgr: np.ndarray) -> np.ndarray:
    return frame_bgr


def look_process(frame_bgr: np.ndarray) -> np.ndarray:
    """Approx CIPhotoEffectProcess — cool shadows, lifted greens."""
    f = _as_float(frame_bgr)
    b, g, r = cv2.split(f)
    b = np.clip(b * 1.08 + 0.02, 0, 1)
    g = np.clip(g * 1.05, 0, 1)
    r = np.clip(r * 0.92, 0, 1)
    out = cv2.merge([b, g, r])
    out = cv2.addWeighted(out, 0.85, _as_float(cv2.cvtColor(cv2.cvtColor(_as_u8(out), cv2.COLOR_BGR2HSV), cv2.COLOR_HSV2BGR)), 0.15, 0)
    # Slight contrast
    out = np.clip((out - 0.5) * 1.12 + 0.5, 0, 1)
    return _as_u8(out)


def look_chrome(frame_bgr: np.ndarray) -> np.ndarray:
    """Approx CIPhotoEffectChrome — punchy contrast, bright."""
    f = _as_float(frame_bgr)
    f = np.clip((f - 0.5) * 1.35 + 0.52, 0, 1)
    f = np.power(f, 0.92)
    return _as_u8(f)


def look_fade(frame_bgr: np.ndarray) -> np.ndarray:
    """Approx CIPhotoEffectFade — lifted blacks, soft."""
    f = _as_float(frame_bgr)
    f = f * 0.82 + 0.12
    f = np.clip((f - 0.5) * 0.85 + 0.5, 0, 1)
    return _as_u8(f)


def look_instant(frame_bgr: np.ndarray) -> np.ndarray:
    """Approx CIPhotoEffectInstant — warm, slight vignette feel via saturation."""
    f = _as_float(frame_bgr)
    b, g, r = cv2.split(f)
    r = np.clip(r * 1.12 + 0.03, 0, 1)
    g = np.clip(g * 1.02, 0, 1)
    b = np.clip(b * 0.88, 0, 1)
    out = cv2.merge([b, g, r])
    out = np.clip((out - 0.5) * 1.1 + 0.48, 0, 1)
    return _as_u8(_grain(out, 0.015))


def look_mono(frame_bgr: np.ndarray) -> np.ndarray:
    """Approx CIPhotoEffectMono."""
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


def look_noir(frame_bgr: np.ndarray) -> np.ndarray:
    """Approx CIPhotoEffectNoir — dramatic B&W."""
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
    gray = np.clip((gray - 0.5) * 1.45 + 0.45, 0, 1)
    gray = np.power(gray, 1.15)
    u8 = (gray * 255).astype(np.uint8)
    return cv2.cvtColor(u8, cv2.COLOR_GRAY2BGR)


def look_tonal(frame_bgr: np.ndarray) -> np.ndarray:
    """Approx CIPhotoEffectTonal — soft B&W."""
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
    gray = gray * 0.9 + 0.08
    gray = np.clip((gray - 0.5) * 0.9 + 0.5, 0, 1)
    u8 = (gray * 255).astype(np.uint8)
    return cv2.cvtColor(u8, cv2.COLOR_GRAY2BGR)


def look_transfer(frame_bgr: np.ndarray) -> np.ndarray:
    """Approx CIPhotoEffectTransfer — warm midtones."""
    f = _as_float(frame_bgr)
    b, g, r = cv2.split(f)
    r = np.clip(r * 1.08 + 0.04, 0, 1)
    g = np.clip(g * 1.04 + 0.02, 0, 1)
    b = np.clip(b * 0.9, 0, 1)
    out = cv2.merge([b, g, r])
    out = np.clip((out - 0.5) * 1.05 + 0.5, 0, 1)
    return _as_u8(out)


# Custom Composer names (P1) — placeholders until LUT-matched
def look_g1(frame_bgr: np.ndarray) -> np.ndarray:
    """Placeholder for custom Metal look G1 — mild CLAHE + warm grade."""
    lab = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    l = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(l)
    out = cv2.cvtColor(cv2.merge([l, a, b]), cv2.COLOR_LAB2BGR)
    return look_transfer(out)


LOOKS: dict[str, LookFn] = {
    "none": look_none,
    "process": look_process,
    "chrome": look_chrome,
    "fade": look_fade,
    "instant": look_instant,
    "mono": look_mono,
    "noir": look_noir,
    "tonal": look_tonal,
    "transfer": look_transfer,
    "g1": look_g1,
}


def list_looks() -> list[str]:
    return sorted(LOOKS.keys())


def apply_look(frame_bgr: np.ndarray, name: str) -> np.ndarray:
    key = name.strip().lower()
    if key not in LOOKS:
        known = ", ".join(list_looks())
        raise ValueError(f"Unknown look {name!r}. Known: {known}")
    return LOOKS[key](frame_bgr)
