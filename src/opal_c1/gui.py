"""decomposer overlay.

Not a settings window. This drops out of the bar like a camera's on-screen
display: preview first, everything else small and one click away. It is a
client of the daemon, so it never opens the camera itself — in Studio mode
there is no V4L2 node to open, and in Call mode a second reader would compete
with the engine. The preview comes from the engine, which already has the frame.

Every daemon call runs on a worker thread: a mode switch reboots the camera and
takes up to fifteen seconds.
"""

from __future__ import annotations

import socket
import struct
from pathlib import Path
import threading
import time
from typing import Callable, Optional

import gi

# gtk4-layer-shell has to be in the process before GTK opens the Wayland
# display, or it never hooks the surface and every window is created as an
# ordinary toplevel the compositor places and tiles as it sees fit. Importing
# the typelib is not enough — the shared object itself must be loaded first,
# which is what makes is_supported() true.
try:
    import ctypes

    ctypes.CDLL("libgtk4-layer-shell.so.0", mode=ctypes.RTLD_GLOBAL)
    gi.require_version("Gtk4LayerShell", "1.0")
    from gi.repository import Gtk4LayerShell as LayerShell

    HAVE_LAYER_SHELL = True
except (ValueError, ImportError, OSError):
    LayerShell = None
    HAVE_LAYER_SHELL = False

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from opal_c1 import theme as omtheme  # noqa: E402
from opal_c1.daemon import Client, runtime_dir  # noqa: E402

WIDTH = 384
PREVIEW_H = 216

# Composer's eight Core Image effects, then its own five. Both groups are
# loaded from LUTs measured off Composer itself, so the descriptions below are
# from measuring what each one does, not from marketing copy.
CI_LOOKS = [
    "process", "chrome", "fade", "instant",
    "mono", "noir", "tonal", "transfer",
]
CUSTOM_LOOKS = ["G1", "D1", "Q1", "S1", "X1"]
LOOKS = ["none"] + CI_LOOKS + CUSTOM_LOOKS
LOOK_BLURB = {
    "none": "Untouched",
    "process": "Cool shadows, lifted greens",
    "chrome": "Punchy and bright",
    "fade": "Lifted blacks, soft",
    "instant": "Warm, instant-film",
    "mono": "Plain black and white",
    "noir": "Dramatic black and white",
    "tonal": "Soft black and white",
    "transfer": "Warm midtones",
    "G1": "Composer's own \u2014 cool lift, subtle",
    "D1": "Composer's own \u2014 black and white, punchy",
    "Q1": "Composer's own \u2014 black and white, soft",
    "S1": "Composer's own \u2014 cool lift, medium",
    "X1": "Composer's own \u2014 cool lift, strong",
}

EFFECTS = ["off", "sepia", "mono", "negative", "posterize",
           "solarize", "aqua", "blackboard", "whiteboard"]

AUTO_CAPABLE = ("focus", "wb")

# Which modes can actually drive each control. Call mode reaches the camera
# over V4L2; Studio mode runs different firmware where /dev/video0 does not
# exist and only the ISP controls are addressable. A control the current mode
# cannot touch is disabled and shown as "-", because drawing it at its minimum
# claims the camera is set to zero when it simply is not reported.
CALL, STUDIO = "call", "studio"
SLIDERS = [
    ("brightness", "Brightness", 0, 255, 1, (CALL,)),
    ("contrast", "Contrast", 0, 100, 1, (CALL,)),
    ("saturation", "Saturation", 0, 100, 1, (CALL,)),
    ("sharpness", "Sharpness", 0, 4, 1, (CALL,)),
    ("exposure", "Exposure", 1000, 33000, 100, (CALL, STUDIO)),
    ("iso", "ISO", 100, 1600, 50, (CALL, STUDIO)),
    ("focus", "Focus", 0, 255, 1, (STUDIO,)),
    ("wb", "White bal.", 1000, 12000, 100, (STUDIO,)),
]
SLIDER_MODES = {key: modes for key, _, _, _, _, modes in SLIDERS}


def _worker(fn: Callable[[], dict], done: Callable[[dict], None]) -> None:
    def run() -> None:
        try:
            result = fn()
        except Exception as e:  # a dead daemon must not take the panel with it
            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        GLib.idle_add(done, result)

    threading.Thread(target=run, daemon=True).start()


class Preview(Gtk.Picture):
    """Live frames from the engine's preview socket.

    Connects only while the overlay is on screen; there is no reason to move
    pixels for a hidden window.
    """

    def __init__(self):
        super().__init__()
        self.set_content_fit(Gtk.ContentFit.COVER)
        self.set_size_request(WIDTH - 20, PREVIEW_H)
        self.add_css_class("dc-preview")
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._path = runtime_dir() / "preview.sock"

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                sock = socket.socket(socket.AF_UNIX)
                sock.settimeout(3.0)
                sock.connect(str(self._path))
                header = self._recv_exact(sock, 8)
                if header is None:
                    raise OSError("no header")
                w, h = struct.unpack("<II", header)
                size = w * h * 3
                sock.settimeout(5.0)
                while not self._stop.is_set():
                    data = self._recv_exact(sock, size)
                    if data is None:
                        break
                    GLib.idle_add(self._show, data, w, h)
            except OSError:
                pass
            finally:
                try:
                    sock.close()
                except Exception:
                    pass
            # The engine restarts on mode switches; just keep trying.
            if self._stop.wait(1.0):
                return

    @staticmethod
    def _recv_exact(sock: socket.socket, n: int) -> Optional[bytes]:
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = sock.recv(n - len(buf))
            except OSError:
                return None
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    def _show(self, data: bytes, w: int, h: int) -> bool:
        try:
            texture = Gdk.MemoryTexture.new(
                w, h, Gdk.MemoryFormat.R8G8B8, GLib.Bytes.new(data), w * 3
            )
            self.set_paintable(texture)
        except Exception:
            pass
        return False


class Panel(Gtk.Box):
    def __init__(self, theme: omtheme.Theme, on_close: Callable[[], None]):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("dc-root")
        self.theme = theme
        self.on_close = on_close
        self.client = Client()
        self.status: dict = {}
        self.busy = False
        self._pending: dict = {}
        self._debounce: Optional[int] = None
        self._suppress = False
        # Building a Gtk.Scale emits value-changed; until the first status
        # arrives those signals carry widget defaults, not camera state.
        self._ready = False

        self.set_size_request(WIDTH, -1)
        self.append(self._header())

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=9)
        body.set_margin_start(10)
        body.set_margin_end(10)
        body.set_margin_bottom(9)

        self.preview = Preview()
        tap = Gtk.GestureClick()
        tap.connect("released", self._on_preview_tap)
        self.preview.add_controller(tap)
        body.append(self.preview)
        body.append(self._mode_row())
        body.append(self._sep())
        body.append(self._look_block())
        body.append(self._overlay_row())
        body.append(self._preset_row())
        body.append(self._sep())
        body.append(self._camera_block())
        self.append(body)
        self.append(self._footer())

        self.refresh()
        GLib.timeout_add_seconds(2, self._tick)

    def set_theme(self, theme: omtheme.Theme) -> None:
        self.theme = theme
        self.subtitle.set_text(theme.name)

    def _sep(self) -> Gtk.Widget:
        s = Gtk.Box()
        s.add_css_class("dc-sep")
        return s

    def _header(self) -> Gtk.Widget:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        bar.add_css_class("dc-header")

        title = Gtk.Label(label="decomposer", xalign=0)
        title.add_css_class("dc-title")
        bar.append(title)

        self.subtitle = Gtk.Label(label=self.theme.name, xalign=0)
        self.subtitle.add_css_class("dc-sub")
        self.subtitle.set_valign(Gtk.Align.CENTER)
        bar.append(self.subtitle)

        bar.append(Gtk.Box(hexpand=True))

        self.mode_pill = Gtk.Label(label="—")
        self.mode_pill.add_css_class("dc-pill")
        self.mode_pill.add_css_class("off")
        self.mode_pill.set_valign(Gtk.Align.CENTER)
        bar.append(self.mode_pill)

        close = Gtk.Button(label="✕")
        close.add_css_class("dc-tiny")
        close.set_valign(Gtk.Align.CENTER)
        close.connect("clicked", lambda *_: self.on_close())
        bar.append(close)
        return bar

    def _mode_row(self) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        lbl = Gtk.Label(label="MODE", xalign=0)
        lbl.add_css_class("dc-section")
        lbl.set_valign(Gtk.Align.CENTER)
        row.append(lbl)

        self.mode_buttons = {}
        for mode, text in (("call", "Call"), ("studio", "Studio")):
            b = Gtk.Button(label=text)
            b.add_css_class("dc-chip")
            b.connect("clicked", self._on_mode, mode)
            self.mode_buttons[mode] = b
            row.append(b)

        row.append(Gtk.Box(hexpand=True))

        # Mirroring is a view preference, not a camera setting: it lives next
        # to the mode because it applies to whatever the mode is publishing.
        self.mirror_buttons = {}
        for key, glyph, tip in (
            ("horizontal", "\u21c4", "Mirror left/right"),
            ("vertical", "\u21c5", "Mirror top/bottom"),
        ):
            b = Gtk.Button(label=glyph)
            b.add_css_class("dc-tiny")
            b.set_valign(Gtk.Align.CENTER)
            b.set_tooltip_text(f"{tip} (both together is a 180\u00b0 turn)")
            b.connect("clicked", self._on_mirror, key)
            self.mirror_buttons[key] = b
            row.append(b)

        self.mode_hint = Gtk.Label(xalign=1)
        self.mode_hint.add_css_class("dc-hint")
        self.mode_hint.set_valign(Gtk.Align.CENTER)
        row.append(self.mode_hint)
        return row

    def _look_block(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        # "none" is not a look, it is the absence of one, so it gets its own
        # column at full height. The other eight then divide evenly 4x2 instead
        # of leaving a ragged gap on the second row.
        grid = Gtk.Grid()
        grid.set_row_spacing(4)
        grid.set_column_spacing(4)
        grid.set_column_homogeneous(True)
        self.look_buttons = {}

        def chip(name: str) -> Gtk.Button:
            b = Gtk.Button(label=name)
            b.add_css_class("dc-chip")
            b.set_tooltip_text(LOOK_BLURB[name])
            b.set_hexpand(True)
            b.connect("clicked", self._on_look, name)
            self.look_buttons[name] = b
            return b

        none_button = chip("none")
        none_button.set_vexpand(True)
        grid.attach(none_button, 0, 0, 1, 2)

        # The eight Core Image looks fill the block beside "none"; Composer's
        # own five get their own row so the two families stay legible.
        for i, name in enumerate(CI_LOOKS):
            grid.attach(chip(name), 1 + i % 4, i // 4, 1, 1)
        for i, name in enumerate(CUSTOM_LOOKS):
            grid.attach(chip(name), i, 2, 1, 1)

        box.append(grid)

        row, self.strength, self.strength_value = self._slider_row(
            "Strength", 0.0, 1.0, 0.05, digits=2
        )
        self.strength.connect("value-changed", self._on_strength)
        box.append(row)
        return box

    def _overlay_row(self) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        lbl = Gtk.Label(label="Overlay", xalign=0)
        lbl.add_css_class("dc-label")
        lbl.set_size_request(64, -1)
        row.append(lbl)

        self.overlay_button = Gtk.Button(label="choose\u2026")
        self.overlay_button.add_css_class("dc-chip")
        self.overlay_button.set_hexpand(True)
        self.overlay_button.connect("clicked", self._on_overlay_choose)
        row.append(self.overlay_button)

        self.overlay_clear = Gtk.Button(label="\u00d7")
        self.overlay_clear.add_css_class("dc-tiny")
        self.overlay_clear.set_valign(Gtk.Align.CENTER)
        self.overlay_clear.set_tooltip_text("Remove the overlay")
        self.overlay_clear.connect("clicked", self._on_overlay_clear)
        row.append(self.overlay_clear)

        self.overlay_opacity = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0.0, 1.0, 0.05
        )
        self.overlay_opacity.set_draw_value(False)
        self.overlay_opacity.set_valign(Gtk.Align.CENTER)
        self.overlay_opacity.set_size_request(70, -1)
        self.overlay_opacity.set_tooltip_text("Overlay opacity")
        self.overlay_opacity.connect("value-changed", self._on_overlay_opacity)
        row.append(self.overlay_opacity)
        return row

    def _preset_row(self) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        lbl = Gtk.Label(label="Preset", xalign=0)
        lbl.add_css_class("dc-label")
        lbl.set_size_request(64, -1)
        row.append(lbl)

        self.preset_names: list[str] = []
        self.preset_drop = Gtk.DropDown.new_from_strings(["\u2014"])
        self.preset_drop.set_hexpand(True)
        self.preset_drop.connect("notify::selected", self._on_preset_selected)
        row.append(self.preset_drop)

        # A popover rather than a dialog: a layer surface cannot parent a
        # dialog, but xdg_popup is part of the protocol and works.
        self.preset_save = Gtk.MenuButton(label="save")
        self.preset_save.add_css_class("dc-tiny")
        self.preset_save.set_valign(Gtk.Align.CENTER)
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_margin_top(6); box.set_margin_bottom(6)
        box.set_margin_start(6); box.set_margin_end(6)
        self.preset_entry = Gtk.Entry()
        self.preset_entry.set_placeholder_text("preset name")
        self.preset_entry.connect("activate", self._on_preset_save)
        box.append(self.preset_entry)
        confirm = Gtk.Button(label="Save")
        confirm.add_css_class("dc-chip")
        confirm.connect("clicked", self._on_preset_save)
        box.append(confirm)
        popover.set_child(box)
        self.preset_save.set_popover(popover)
        row.append(self.preset_save)
        return row

    def _on_preset_selected(self, drop, _param) -> None:
        if self._suppress or not self._ready:
            return
        i = drop.get_selected()
        if i == 0 or i - 1 >= len(self.preset_names):
            return
        name = self.preset_names[i - 1]
        _worker(
            lambda: self.client.request(cmd="preset_load", name=name),
            self._on_result,
        )

    def _on_preset_save(self, _widget) -> None:
        name = self.preset_entry.get_text().strip()
        if not name:
            return
        self.preset_entry.set_text("")
        self.preset_save.popdown()
        _worker(
            lambda: self.client.request(cmd="preset_save", name=name),
            self._on_result,
        )

    def _on_overlay_choose(self, _btn) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title("Choose an overlay image")
        png = Gtk.FileFilter()
        png.set_name("PNG images")
        png.add_mime_type("image/png")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(png)
        dialog.set_filters(filters)
        # No transient parent. The panel is a layer surface, not an
        # xdg_toplevel, and asking the compositor to parent a dialog to one is
        # a protocol error: it disconnects the client, which looks exactly like
        # the panel closing itself when the button is pressed.
        dialog.open(None, None, self._on_overlay_chosen)

    def _on_overlay_chosen(self, dialog, result) -> None:
        try:
            path = dialog.open_finish(result).get_path()
        except Exception:
            return  # cancelled
        if not path:
            return
        _worker(
            lambda: self.client.request(cmd="set_overlay", values={"path": path}),
            self._on_result,
        )

    def _on_overlay_clear(self, _btn) -> None:
        _worker(
            lambda: self.client.request(cmd="set_overlay", values={"path": "off"}),
            self._on_result,
        )

    def _on_overlay_opacity(self, scale: Gtk.Scale) -> None:
        if self._suppress or not self._ready:
            return
        self._queue({"overlay_opacity": round(scale.get_value(), 2)})

    def _slider_row(self, label: str, lo, hi, step, digits: int = 0):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        lbl = Gtk.Label(label=label, xalign=0)
        lbl.add_css_class("dc-label")
        lbl.set_size_request(64, -1)
        row.append(lbl)

        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, step)
        scale.set_hexpand(True)
        scale.set_draw_value(False)  # the number lives in its own label
        scale.set_valign(Gtk.Align.CENTER)
        if digits == 0:
            scale.set_round_digits(0)
        row.append(scale)

        value = Gtk.Label(xalign=1)
        value.add_css_class("dc-value")
        value.set_size_request(38, -1)
        row.append(value)
        return row, scale, value

    def _camera_block(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.sliders = {}
        self.slider_labels = {}
        self.slider_values = {}
        self.auto_buttons = {}

        for key, label, lo, hi, step, _modes in SLIDERS:
            row, scale, value = self._slider_row(label, lo, hi, step)
            scale.connect("value-changed", self._on_slider, key)
            if key in AUTO_CAPABLE:
                auto = Gtk.Button(label="auto")
                auto.add_css_class("dc-tiny")
                auto.set_valign(Gtk.Align.CENTER)
                auto.set_tooltip_text(f"Hand {label.lower()} back to the camera")
                auto.connect("clicked", self._on_auto, key)
                row.append(auto)
                self.auto_buttons[key] = auto
            self.sliders[key] = scale
            self.slider_values[key] = value
            self.slider_labels[key] = row.get_first_child()
            box.append(row)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        lbl = Gtk.Label(label="Effect", xalign=0)
        lbl.add_css_class("dc-label")
        lbl.set_size_request(64, -1)
        row.append(lbl)
        self.effect_drop = Gtk.DropDown.new_from_strings(EFFECTS)
        self.effect_drop.set_hexpand(True)
        self.effect_drop.connect("notify::selected", self._on_effect_selected)
        row.append(self.effect_drop)
        self.effect_label = lbl
        box.append(row)

        self.camera_hint = Gtk.Label(xalign=0, wrap=True)
        self.camera_hint.add_css_class("dc-hint")
        box.append(self.camera_hint)
        return box

    def _footer(self) -> Gtk.Widget:
        box = Gtk.Box()
        self.footer = Gtk.Label(xalign=0, wrap=True)
        self.footer.add_css_class("dc-hint")
        self.footer.set_margin_start(10)
        self.footer.set_margin_end(10)
        self.footer.set_margin_bottom(8)
        box.append(self.footer)
        return box

    # -- actions --------------------------------------------------------

    def _on_preview_tap(self, gesture, _n_press, cx: float, cy: float) -> None:
        """Tap to focus: aim autofocus and exposure metering where clicked."""
        if self.busy or not self._ready:
            return
        if self.status.get("mode") != "studio":
            self.footer.set_text("tap-to-focus needs Studio mode")
            return
        # The preview uses ContentFit.COVER, so the picture may be cropped;
        # undo that mapping before converting to frame coordinates.
        w = self.preview.get_width() or 1
        h = self.preview.get_height() or 1
        pw, ph = 480, 270
        scale = max(w / pw, h / ph)
        ix = (cx - (w - pw * scale) / 2) / scale
        iy = (cy - (h - ph * scale) / 2) / scale
        fx = max(0, min(1919, int(ix / pw * 1920)))
        fy = max(0, min(1079, int(iy / ph * 1080)))
        x0, y0 = max(0, fx - 128), max(0, fy - 128)
        region = [x0, y0, 256, 256]
        self.footer.set_text(f"focusing at {fx},{fy}…")
        _worker(
            lambda: self.client.request(
                cmd="set_camera", values={"af_region": region, "ae_region": region}
            ),
            self._on_result,
        )

    def _on_effect_selected(self, drop, _param) -> None:
        if self._suppress or not self._ready:
            return
        name = EFFECTS[drop.get_selected()]
        _worker(
            lambda: self.client.request(cmd="set_camera", values={"effect": name}),
            self._on_result,
        )

    def _on_mode(self, _btn, mode: str) -> None:
        if self.busy or self.status.get("mode") == mode:
            return
        self._set_busy(True, f"switching to {mode}, the camera reboots…")
        _worker(lambda: self.client.request(cmd="set_mode", mode=mode), self._on_result)

    def _on_mirror(self, _btn, axis: str) -> None:
        if self.busy:
            return
        current = self.status.get("mirror_h" if axis == "horizontal" else "mirror_v")
        _worker(
            lambda: self.client.request(cmd="set_mirror", **{axis: not current}),
            self._on_result,
        )

    def _on_look(self, _btn, name: str) -> None:
        if self.busy:
            return
        _worker(lambda: self.client.request(cmd="set_look", look=name), self._on_result)

    def _on_auto(self, _btn, key: str) -> None:
        if self.busy or not self._ready:
            return
        self._pending.pop(key, None)
        _worker(
            lambda: self.client.request(cmd="set_camera", values={key: -1}),
            self._on_result,
        )

    def _on_strength(self, scale: Gtk.Scale) -> None:
        self.strength_value.set_text(f"{scale.get_value():.2f}")
        if self._suppress or not self._ready:
            return
        self._queue({"strength": round(scale.get_value(), 2)})

    def _on_slider(self, scale: Gtk.Scale, key: str) -> None:
        self.slider_values[key].set_text(f"{int(scale.get_value())}")
        if self._suppress or not self._ready:
            return
        self._queue({key: int(scale.get_value())})

    def _queue(self, values: dict) -> None:
        self._pending.update(values)
        if self._debounce is not None:
            GLib.source_remove(self._debounce)
        self._debounce = GLib.timeout_add(180, self._flush)

    def _flush(self) -> bool:
        self._debounce = None
        pending, self._pending = self._pending, {}
        if not pending:
            return False
        opacity = pending.pop("overlay_opacity", None)
        if opacity is not None:
            _worker(
                lambda: self.client.request(
                    cmd="set_overlay", values={"opacity": opacity}
                ),
                self._on_result,
            )
        strength = pending.pop("strength", None)
        if strength is not None:
            _worker(
                lambda: self.client.request(cmd="set_look", strength=strength),
                self._on_result,
            )
        if pending:
            _worker(
                lambda: self.client.request(cmd="set_camera", values=pending),
                self._on_result,
            )
        return False

    # -- state ----------------------------------------------------------

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self.busy = busy
        for b in list(self.mode_buttons.values()) + list(self.look_buttons.values()):
            b.set_sensitive(not busy)
        if message:
            self.footer.set_text(message)

    def refresh(self) -> None:
        _worker(lambda: self.client.request(cmd="status"), self._on_result)

    def _tick(self) -> bool:
        if not self.busy:
            self.refresh()
        return True

    def _on_result(self, resp: dict) -> bool:
        self._set_busy(False)
        if not resp.get("ok"):
            self.mode_pill.set_text("no daemon")
            for cls in ("call", "studio"):
                self.mode_pill.remove_css_class(cls)
            self.mode_pill.add_css_class("off")
            self.footer.set_text(resp.get("error", "unknown error"))
            self.footer.add_css_class("dc-warn")
            for b in list(self.mode_buttons.values()) + list(self.look_buttons.values()):
                b.set_sensitive(False)
            for s in self.sliders.values():
                s.set_sensitive(False)
            return False
        self.footer.remove_css_class("dc-warn")
        self.status = resp
        self._apply(resp)
        return False

    def _apply(self, st: dict) -> None:
        mode = st.get("mode", "call")
        studio = mode == "studio"

        self.mode_pill.set_text(mode.upper())
        for cls in ("call", "studio", "off"):
            self.mode_pill.remove_css_class(cls)
        self.mode_pill.add_css_class(mode if mode in ("call", "studio") else "off")
        self.mode_hint.set_text("mic off" if studio else "mic on")

        for axis, key in (("horizontal", "mirror_h"), ("vertical", "mirror_v")):
            b = self.mirror_buttons[axis]
            b.set_sensitive(True)
            (b.add_css_class if st.get(key) else b.remove_css_class)("selected")

        for name, b in self.mode_buttons.items():
            b.set_sensitive(True)
            (b.add_css_class if name == mode else b.remove_css_class)("selected")

        look = st.get("look", "none")
        # A look whose LUT is not installed is shown but not offered.
        offered = set(st.get("looks") or LOOKS)
        for name, b in self.look_buttons.items():
            b.set_sensitive(name in offered)
            b.set_opacity(1.0 if name in offered else 0.4)
            (b.add_css_class if name == look else b.remove_css_class)("selected")

        overlay = st.get("overlay")
        self.overlay_button.set_label(
            Path(overlay).name if overlay else "choose\u2026"
        )
        self.overlay_button.set_tooltip_text(overlay or "No overlay")
        self.overlay_clear.set_sensitive(bool(overlay))
        self.overlay_opacity.set_sensitive(bool(overlay))

        self._suppress = True
        try:
            names = list(st.get("presets") or [])
            if names != self.preset_names:
                # Rebuilding the model resets the selection, so it must not be
                # mistaken for the user picking something.
                self.preset_names = names
                self.preset_drop.set_model(
                    Gtk.StringList.new(["\u2014"] + names)
                )
            self.preset_drop.set_sensitive(bool(names))
            self.overlay_opacity.set_value(float(st.get("overlay_opacity", 1.0)))
            self.strength.set_value(float(st.get("strength", 1.0)))
            controls = st.get("controls") or {}
            for key, scale in self.sliders.items():
                available = mode in SLIDER_MODES[key]
                scale.set_sensitive(available)
                self.slider_labels[key].set_opacity(1.0 if available else 0.4)
                value = controls.get(key)
                on_auto = value == -1
                if key in self.auto_buttons:
                    self.auto_buttons[key].set_sensitive(available)
                    # -1 means the camera is driving it. Clamping that onto the
                    # slider would read as "focus 0", a very different setting.
                    (self.auto_buttons[key].add_css_class if on_auto
                     else self.auto_buttons[key].remove_css_class)("selected")
                if value is not None and not on_auto:
                    scale.set_value(float(value))
                if not available:
                    text = "-"
                elif on_auto:
                    text = "auto"
                else:
                    text = f"{int(scale.get_value())}"
                self.slider_values[key].set_text(text)
        finally:
            self._suppress = False
        self._ready = True

        self.camera_hint.set_text(
            "tap the preview to focus \u00b7 colour sliders need Call mode"
            if studio
            else "focus, white balance, effects and tap-to-focus need Studio mode"
        )

        if st.get("engine_alive"):
            self.footer.set_text(
                f"{st.get('width')}×{st.get('height')} → {st.get('output')}"
            )
        else:
            self.footer.set_text(st.get("error") or "engine not running")


class App(Adw.Application):
    def __init__(self, replace: bool = False):
        # ALLOW_REPLACEMENT is always on so a later instance can take over
        # cleanly. Without it the only way to restart the panel is to hunt the
        # process down, and it cannot be found through the compositor while the
        # window is hidden.
        flags = Gio.ApplicationFlags.ALLOW_REPLACEMENT
        if replace:
            flags |= Gio.ApplicationFlags.REPLACE
        super().__init__(application_id="dev.decomposer.Panel", flags=flags)
        self.window: Optional[Gtk.Window] = None
        self.panel: Optional[Panel] = None
        self.connect("activate", self.on_activate)

    def on_activate(self, _app) -> None:
        # Launching again is how the bar entry toggles the overlay.
        if self.window is not None:
            if self.window.get_visible():
                self._hide()
            else:
                self._show()
            return
        self._build()
        self._show()

    def _build(self) -> None:
        theme = omtheme.load()
        Adw.StyleManager.get_default().set_color_scheme(
            Adw.ColorScheme.FORCE_DARK if theme.is_dark else Adw.ColorScheme.FORCE_LIGHT
        )
        self.provider = Gtk.CssProvider()
        self.provider.load_from_data(omtheme.css(theme).encode())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), self.provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        win = Gtk.ApplicationWindow(application=self)
        win.set_title("decomposer")
        win.add_css_class("decomposer")
        win.set_default_size(WIDTH, -1)
        win.set_resizable(False)

        if HAVE_LAYER_SHELL and LayerShell.is_supported():
            # A layer surface drops from the bar instead of being a managed
            # window the compositor will tile or centre.
            LayerShell.init_for_window(win)
            LayerShell.set_layer(win, LayerShell.Layer.OVERLAY)
            LayerShell.set_anchor(win, LayerShell.Edge.TOP, True)
            LayerShell.set_anchor(win, LayerShell.Edge.RIGHT, True)
            LayerShell.set_margin(win, LayerShell.Edge.TOP, 6)
            LayerShell.set_margin(win, LayerShell.Edge.RIGHT, 6)
            LayerShell.set_keyboard_mode(win, LayerShell.KeyboardMode.ON_DEMAND)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        win.add_controller(keys)

        self.panel = Panel(theme, on_close=self._hide)
        win.set_child(self.panel)
        self.window = win
        self._watch_desktop()

    def _on_key(self, _c, keyval, _code, _state) -> bool:
        if keyval == Gdk.KEY_Escape:
            self._hide()
            return True
        return False

    def _show(self) -> None:
        if self.window is None:
            return
        self.window.present()
        if self.panel is not None:
            self.panel.preview.start()
            self.panel.refresh()

    def _hide(self) -> None:
        if self.window is None:
            return
        if self.panel is not None:
            self.panel.preview.stop()
        self.window.set_visible(False)

    # -- follow the desktop ---------------------------------------------

    def _watch_desktop(self) -> None:
        self._reload_pending = None
        self._monitors = []
        try:
            watch = Gio.File.new_for_path(str(omtheme.STATE_THEME.parent))
            monitor = watch.monitor_directory(Gio.FileMonitorFlags.NONE, None)
            monitor.connect("changed", self._on_desktop_changed)
            self._monitors.append(monitor)
        except Exception:
            pass
        try:
            settings = Gio.Settings.new("org.gnome.desktop.interface")
            settings.connect("changed::font-name", self._on_desktop_changed)
            self._settings = settings  # keep alive or the signal is dropped
        except Exception:
            pass

    def _on_desktop_changed(self, *_args) -> None:
        if self._reload_pending is not None:
            GLib.source_remove(self._reload_pending)
        self._reload_pending = GLib.timeout_add(400, self._reload_theme)

    def _reload_theme(self) -> bool:
        self._reload_pending = None
        theme = omtheme.load()
        try:
            self.provider.load_from_data(omtheme.css(theme).encode())
        except Exception:
            return False
        Adw.StyleManager.get_default().set_color_scheme(
            Adw.ColorScheme.FORCE_DARK if theme.is_dark else Adw.ColorScheme.FORCE_LIGHT
        )
        if self.panel is not None:
            self.panel.set_theme(theme)
        return False


def main(replace: bool = False) -> int:
    return App(replace=replace).run(None)
