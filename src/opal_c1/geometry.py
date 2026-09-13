"""Coordinate math for the movable panel, kept away from GTK.

Nothing here imports a toolkit, so the drag arithmetic and the self-view
orientation rule can be tested without a display.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import struct
from pathlib import Path
from typing import Optional

HYPR_CURSOR_REPLY_MAX = 256


def position_from_cursor(
    panel_origin: tuple[int, int],
    cursor_origin: tuple[float, float],
    cursor_now: tuple[float, float],
) -> tuple[int, int]:
    """Place a panel from a compositor-global cursor delta, in logical pixels."""
    return (
        round(panel_origin[0] + cursor_now[0] - cursor_origin[0]),
        round(panel_origin[1] + cursor_now[1] - cursor_origin[1]),
    )


def hypr_cursor_position() -> Optional[tuple[float, float]]:
    """Read Hyprland's global logical cursor position through bounded IPC.

    Wayland intentionally gives GTK only surface-relative pointer coordinates.
    That is unsuitable for moving the surface underneath an active gesture,
    because the coordinate origin moves too.  Omarchy runs Hyprland, whose
    owner-only command socket provides the stable compositor coordinate we
    need without starting a process for every motion event.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    instance = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
    if (
        not runtime or not instance or len(runtime) > 512 or len(instance) > 160
        or not all(c.isalnum() or c in "._-" for c in instance)
    ):
        return None
    path = Path(runtime) / "hypr" / instance / ".socket.sock"
    try:
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            return None
        with socket.socket(socket.AF_UNIX) as command:
            command.settimeout(0.05)
            command.connect(str(path))
            if hasattr(socket, "SO_PEERCRED"):
                raw = command.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED,
                    struct.calcsize("3i"),
                )
                _pid, uid, _gid = struct.unpack("3i", raw)
                if uid != os.getuid():
                    return None
            command.sendall(b"j/cursorpos")
            command.shutdown(socket.SHUT_WR)
            reply = bytearray()
            while len(reply) <= HYPR_CURSOR_REPLY_MAX:
                chunk = command.recv(
                    min(128, HYPR_CURSOR_REPLY_MAX + 1 - len(reply))
                )
                if not chunk:
                    break
                reply.extend(chunk)
            if len(reply) > HYPR_CURSOR_REPLY_MAX:
                return None
        parsed = json.loads(reply)
        if not isinstance(parsed, dict):
            return None
        values = parsed.get("x"), parsed.get("y")
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               for value in values):
            return None
        x, y = float(values[0]), float(values[1])
        if not (-1_000_000 <= x <= 1_000_000 and -1_000_000 <= y <= 1_000_000):
            return None
        return x, y
    except (OSError, ValueError, json.JSONDecodeError, struct.error):
        return None


def preview_correction_flips(
    want_mirrored: bool, send_horizontal: bool, send_vertical: bool
) -> tuple[bool, bool]:
    """Transform a SEND-oriented engine preview into the requested self-view."""
    return bool(want_mirrored) ^ bool(send_horizontal), bool(send_vertical)
