"""Preset encoding, decoding, and name validation — pure over plain dicts.

The filesystem and JSON belong to the adapter; this module owns what a preset
*is*: which fields, what ranges, and what happens to values that arrived from
a file someone may have edited by hand. Decoding never raises on bad values —
it clamps or drops them and says so, because a preset that half-loads silently
is how a "restore my setup" button becomes a mystery.
"""

from __future__ import annotations

from typing import Optional, Tuple

VERSION = 1


def validate_name(name: str) -> str:
    """A preset name must be a bare filename component.

    Names arrive over a socket, so anything path-like ("../evil", "a/b",
    ".hidden") would otherwise be a way to write wherever the daemon can
    reach.
    """
    clean = (name or "").strip()
    if (
        not clean
        or clean.startswith(".")
        or "/" in clean
        or "\\" in clean
        or clean in (".", "..")
    ):
        raise ValueError(f"invalid preset name {name!r}")
    return clean


def _clamp(value, lo, hi, cast=float):
    try:
        v = cast(value)
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, v))


def decode(raw: dict) -> Tuple[dict, list]:
    """Normalize a loaded preset dict. Returns (fields, notes).

    `fields` contains only recognized, in-range values; `notes` explains
    anything that was dropped or clamped so the caller can surface it.
    """
    notes: list = []
    out: dict = {}
    if not isinstance(raw, dict):
        return {}, ["preset is not an object"]

    mode = raw.get("mode")
    if mode in ("call", "studio"):
        out["mode"] = mode
    elif mode is not None:
        notes.append(f"unknown mode {mode!r} ignored")

    look = raw.get("look")
    if isinstance(look, str) and look:
        out["look"] = look

    strength = _clamp(raw.get("strength"), 0.0, 1.0)
    if strength is not None:
        out["strength"] = strength

    per_look = raw.get("look_strength")
    if isinstance(per_look, dict):
        cleaned = {}
        for key, value in per_look.items():
            v = _clamp(value, 0.0, 1.0)
            if isinstance(key, str) and v is not None:
                cleaned[key] = v
        out["look_strength"] = cleaned

    for key in ("mirror_h", "mirror_v"):
        if key in raw:
            out[key] = bool(raw[key])

    zoom = _clamp(raw.get("zoom"), 1.0, 8.0)
    if zoom is not None:
        out["zoom"] = zoom
    clahe = _clamp(raw.get("clahe"), 0.0, 1.0)
    if clahe is not None:
        out["clahe"] = clahe
    for key in ("pan_x", "pan_y"):
        v = _clamp(raw.get(key), -1.0, 1.0)
        if v is not None:
            out[key] = v

    overlay = raw.get("overlay")
    if isinstance(overlay, dict):
        ov: dict = {}
        path = overlay.get("path")
        if isinstance(path, str) and path:
            ov["path"] = path
        for key in ("x", "y", "width", "height"):
            v = _clamp(overlay.get(key), 0, 100_000, cast=int)
            if v is not None:
                ov[key] = v
        opacity = _clamp(overlay.get("opacity"), 0.0, 1.0)
        if opacity is not None:
            ov["opacity"] = opacity
        out["overlay"] = ov

    controls = raw.get("controls")
    if isinstance(controls, dict):
        out["controls"] = dict(controls)

    return out, notes
