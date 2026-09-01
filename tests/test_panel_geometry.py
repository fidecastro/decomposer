"""Coordinate math for dragging the movable layer surface."""

import socket
import threading

from opal_c1.gui import (
    HYPR_CURSOR_REPLY_MAX,
    _hypr_cursor_position,
    _position_from_cursor,
    _preview_correction_flips,
)


def _cursor_server(tmp_path, monkeypatch, reply: bytes):
    signature = "test-instance"
    directory = tmp_path / "hypr" / signature
    directory.mkdir(parents=True)
    path = directory / ".socket.sock"
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.listen(1)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", signature)

    def respond():
        try:
            connection, _ = server.accept()
            with connection:
                connection.recv(64)
                try:
                    connection.sendall(reply)
                except OSError:
                    pass
        finally:
            server.close()

    thread = threading.Thread(target=respond)
    thread.start()
    return thread


def test_drag_uses_compositor_cursor_delta_from_fixed_origins():
    assert _position_from_cursor(
        panel_origin=(1000, 400),
        cursor_origin=(1220, 450),
        cursor_now=(1460, 525),
    ) == (1240, 475)


def test_drag_math_uses_wayland_logical_pixels_without_dpi_multiplier():
    # On a 2x output this 75-pixel logical move renders as 150 physical pixels
    # for both cursor and panel.  The layer margin must remain 75, not 150.
    assert _position_from_cursor((400, 300), (100, 100), (175, 25)) == (475, 225)


def test_self_preview_orientation_is_independent_of_send_flips():
    for want_mirrored in (False, True):
        for send_horizontal in (False, True):
            for send_vertical in (False, True):
                correction_h, correction_v = _preview_correction_flips(
                    want_mirrored, send_horizontal, send_vertical
                )
                # The engine preview already contains SEND. Applying the local
                # correction must leave only the absolute self-view choice.
                assert send_horizontal ^ correction_h == want_mirrored
                assert send_vertical ^ correction_v is False


def test_hypr_cursor_reader_accepts_small_owned_socket_reply(tmp_path, monkeypatch):
    thread = _cursor_server(tmp_path, monkeypatch, b'{"x": 123.5, "y": -40}\n')
    try:
        assert _hypr_cursor_position() == (123.5, -40.0)
    finally:
        thread.join(timeout=1)


def test_hypr_cursor_reader_rejects_reply_over_limit(tmp_path, monkeypatch):
    reply = b'{"x": 1, "y": 2, "padding":"' + b"x" * HYPR_CURSOR_REPLY_MAX + b'"}'
    thread = _cursor_server(tmp_path, monkeypatch, reply)
    try:
        assert _hypr_cursor_position() is None
    finally:
        thread.join(timeout=1)
