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

import json
import os
import socket
import struct
from pathlib import Path
import shutil
import subprocess
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

import cairo  # noqa: E402

gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Pango, PangoCairo  # noqa: E402

from opal_c1 import theme as omtheme  # noqa: E402
from opal_c1.daemon import Client, runtime_dir  # noqa: E402
from opal_c1.core.model import (  # noqa: E402
    Mode as _Mode,
    controls_for as _controls_for,
    fps_limits as _fps_limits,
    resolutions_for as _resolutions_for,
)


def model_controls_for(mode: str) -> frozenset:
    try:
        return _controls_for(_Mode(mode))
    except ValueError:
        return frozenset()

WIDTH = 384          # one column: the preview pane, and the controls pane
PANEL_W = 800        # both columns plus margins
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

PANEL_CONFIG = (
    Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    / "decomposer/panel.json"
)


def _panel_pref(key: str, default):
    try:
        return json.loads(PANEL_CONFIG.read_text()).get(key, default)
    except (OSError, ValueError):
        return default


def _save_panel_pref(key: str, value) -> None:
    try:
        data = json.loads(PANEL_CONFIG.read_text())
    except (OSError, ValueError):
        data = {}
    data[key] = value
    PANEL_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    PANEL_CONFIG.write_text(json.dumps(data, indent=2) + "\n")

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

    PLACEHOLDER_STYLES = ("nofeed", "bars")

    def __init__(self):
        super().__init__()
        # CONTAIN, not COVER: the stream's aspect follows the published
        # resolution (4:3 at 12 MP), and the honest rendering letterboxes
        # rather than crops or stretches.
        self.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.stream_w, self.stream_h = 480, 270
        self.set_size_request(WIDTH - 20, PREVIEW_H)
        self.add_css_class("dc-preview")
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._path = runtime_dir() / "preview.sock"
        self.placeholder = _panel_pref("placeholder", "nofeed")
        if self.placeholder not in self.PLACEHOLDER_STYLES:
            self.placeholder = "nofeed"
        self._ph_cache: dict = {}
        self._had_frame = False
        self._showing_placeholder = False
        self.show_placeholder()

    # -- placeholder ------------------------------------------------------

    def cycle_placeholder(self) -> str:
        """Right-click the preview: next placeholder style, persisted."""
        styles = self.PLACEHOLDER_STYLES
        self.placeholder = styles[
            (styles.index(self.placeholder) + 1) % len(styles)
        ]
        _save_panel_pref("placeholder", self.placeholder)
        if self._showing_placeholder:
            self.show_placeholder()
        return self.placeholder

    def show_placeholder(self) -> None:
        GLib.idle_add(self._paint_placeholder)

    def _paint_placeholder(self) -> bool:
        self.set_paintable(self._placeholder_texture(self.placeholder))
        self._showing_placeholder = True
        return False

    def _placeholder_texture(self, style: str):
        if style in self._ph_cache:
            return self._ph_cache[style]
        w, h = 480, 270
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        ctx = cairo.Context(surface)
        if style == "bars":
            self._draw_bars(ctx, w, h)
        else:
            self._draw_nofeed(ctx, w, h)
        surface.flush()
        texture = Gdk.MemoryTexture.new(
            w, h, Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED,
            GLib.Bytes.new(bytes(surface.get_data())), surface.get_stride(),
        )
        self._ph_cache[style] = texture
        return texture

    @staticmethod
    def _draw_nofeed(ctx, w: int, h: int) -> None:
        ctx.set_source_rgb(0.02, 0.02, 0.025)
        ctx.paint()
        layout = PangoCairo.create_layout(ctx)
        family, _ = omtheme.system_font()
        desc = Pango.FontDescription(f"{family} Bold 30")
        layout.set_font_description(desc)
        attrs = Pango.AttrList()
        attrs.insert(Pango.attr_letter_spacing_new(8 * Pango.SCALE))
        layout.set_attributes(attrs)
        layout.set_text("NO FEED", -1)
        _, logical = layout.get_pixel_extents()
        ctx.set_source_rgb(0.72, 0.73, 0.76)
        ctx.move_to((w - logical.width) / 2, (h - logical.height) / 2)
        PangoCairo.show_layout(ctx, layout)

    @staticmethod
    def _draw_bars(ctx, w: int, h: int) -> None:
        """The broadcast test card: 75% SMPTE bars with castellations."""
        bars = [
            (0.75, 0.75, 0.75), (0.75, 0.75, 0.0), (0.0, 0.75, 0.75),
            (0.0, 0.75, 0.0), (0.75, 0.0, 0.75), (0.75, 0.0, 0.0),
            (0.0, 0.0, 0.75),
        ]
        top = int(h * 0.67)
        bw = w / len(bars)
        for i, rgb in enumerate(bars):
            ctx.set_source_rgb(*rgb)
            ctx.rectangle(i * bw, 0, bw + 1, top)
            ctx.fill()
        mid = int(h * 0.08)
        castell = [
            (0.0, 0.0, 0.75), (0.075, 0.075, 0.075), (0.75, 0.0, 0.75),
            (0.075, 0.075, 0.075), (0.0, 0.75, 0.75), (0.075, 0.075, 0.075),
            (0.75, 0.75, 0.75),
        ]
        for i, rgb in enumerate(castell):
            ctx.set_source_rgb(*rgb)
            ctx.rectangle(i * bw, top, bw + 1, mid)
            ctx.fill()
        bottom_y = top + mid
        bottom_h = h - bottom_y
        blocks = [
            ((0.0, 0.129, 0.298), w * 0.25), ((1.0, 1.0, 1.0), w * 0.125),
            ((0.196, 0.0, 0.416), w * 0.25), ((0.075, 0.075, 0.075), w * 0.375),
        ]
        x = 0.0
        for rgb, bwidth in blocks:
            ctx.set_source_rgb(*rgb)
            ctx.rectangle(x, bottom_y, bwidth + 1, bottom_h)
            ctx.fill()
            x += bwidth
        # pluge strip inside the last black block
        for i, lum in enumerate((0.035, 0.075, 0.115)):
            ctx.set_source_rgb(lum, lum, lum)
            ctx.rectangle(w * 0.63 + i * w * 0.04, bottom_y, w * 0.04, bottom_h)
            ctx.fill()

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
                self._had_frame = False
                header = self._recv_exact(sock, 8)
                if header is None:
                    raise OSError("no header")
                w, h = struct.unpack("<II", header)
                self.stream_w, self.stream_h = w, h
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
            # No connection or the stream ended: say so instead of freezing
            # on the last frame forever.
            self.show_placeholder()
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
            self._showing_placeholder = False
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
        self._refreshing = False
        # Which controls the user touched recently: the status poller may not
        # move those sliders for a grace period, or dragging becomes a
        # tug-of-war against the camera's own automatics.
        self._touched: dict = {}

        self.set_size_request(PANEL_W, -1)
        self.append(self._header())

        # Composer's shape: the camera view on the left, controls on the
        # right. The camera-control area is a two-page stack (one page per
        # mode, only that mode's controls on it); the stack sizes itself to
        # the larger page, so the panel's footprint never changes with the
        # mode.
        main = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=9)
        left.set_margin_start(10)
        left.set_margin_end(8)
        left.set_margin_bottom(9)
        left.set_size_request(WIDTH, -1)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=9)
        body.set_margin_start(8)
        body.set_margin_end(10)
        body.set_margin_bottom(9)
        body.set_size_request(WIDTH, -1)
        # Pack from the top: leftover height stays as a quiet gap at the
        # bottom instead of inflating every row.
        body.set_valign(Gtk.Align.START)

        self.preview = Preview()
        tap = Gtk.GestureClick()
        tap.connect("released", self._on_preview_tap)
        self.preview.add_controller(tap)
        scroll = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.VERTICAL
        )
        scroll.connect("scroll", self._on_preview_scroll)
        self.preview.add_controller(scroll)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_pan_begin)
        drag.connect("drag-update", self._on_pan_update)
        self.preview.add_controller(drag)
        right = Gtk.GestureClick()
        right.set_button(3)
        right.connect("released", self._on_preview_menu)
        self.preview.add_controller(right)
        self._pan_base = (0.0, 0.0)
        stage = Gtk.Overlay()
        stage.set_child(self.preview)
        self.count_label = Gtk.Label()
        self.count_label.add_css_class("dc-count")
        self.count_label.set_halign(Gtk.Align.CENTER)
        self.count_label.set_valign(Gtk.Align.CENTER)
        self.count_label.set_can_target(False)
        self.count_label.set_visible(False)
        stage.add_overlay(self.count_label)
        self.rec_badge = Gtk.Label(label="\u25cf REC")
        self.rec_badge.add_css_class("dc-rec")
        self.rec_badge.set_halign(Gtk.Align.START)
        self.rec_badge.set_valign(Gtk.Align.START)
        self.rec_badge.set_margin_start(10)
        self.rec_badge.set_margin_top(8)
        self.rec_badge.set_can_target(False)
        self.rec_badge.set_visible(False)
        stage.add_overlay(self.rec_badge)
        self.flash_box = Gtk.Box()
        self.flash_box.add_css_class("dc-flash")
        self.flash_box.set_can_target(False)
        self.flash_box.set_opacity(0.0)
        stage.add_overlay(self.flash_box)
        left.append(stage)
        left.append(self._mode_row())
        left.append(self._sep())
        left.append(self._camera_block())
        body.append(self._look_block())
        body.append(self._sep())
        body.append(self._overlay_row())
        body.append(self._zoom_row())
        body.append(self._clahe_row())
        body.append(self._blur_row())
        body.append(self._background_row())
        body.append(self._models_section())
        body.append(self._preset_row())
        main.append(left)
        vsep = Gtk.Box()
        vsep.add_css_class("dc-vsep")
        main.append(vsep)
        main.append(body)
        self.append(main)
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

        # A sliding switch, both positions visible. OFF parks the camera
        # on Studio firmware - the only resting state where the microphone
        # is genuinely dead, since Opal's firmware keeps the mic on the bus
        # whether or not video streams.
        self.power_switch = Gtk.Switch()
        self.power_switch.set_valign(Gtk.Align.CENTER)
        self.power_switch.set_tooltip_text(
            "Camera on/off. Off parks it on Studio firmware so the "
            "MICROPHONE turns off too (from Call, that is a firmware reboot)"
        )
        self.power_switch.connect("state-set", self._on_power_state)
        bar.append(self.power_switch)

        # Click: photo after a 3 s count. Hold for a second: recording,
        # click again to stop. A Button's "clicked" cannot see hold
        # duration, so a raw click gesture does the timing.
        self.capture_btn = Gtk.Button(label="\u25c9")
        self.capture_btn.add_css_class("dc-cap")
        self.capture_btn.set_valign(Gtk.Align.CENTER)
        self.capture_btn.set_tooltip_text(
            "Click: photo (3 s timer). Hold 1 s: record; click again to stop"
        )
        cap = Gtk.GestureClick()
        cap.connect("pressed", self._on_capture_pressed)
        cap.connect("released", self._on_capture_released)
        self.capture_btn.add_controller(cap)
        bar.append(self.capture_btn)
        self._hold_timer: Optional[int] = None
        self._hold_fired = False
        self._recorder: Optional[subprocess.Popen] = None
        self._rec_blink: Optional[int] = None
        self._countdown: Optional[int] = None

        # Both selectors are mode-specific: the lists and limits come from
        # the core routing facts, and both dim while a switch is running.
        self._res_choices: list = []
        self._res_mode: str = ""
        self._res_applied: int = 0
        self.res_drop = Gtk.DropDown.new_from_strings(["—"])
        self.res_drop.set_valign(Gtk.Align.CENTER)
        self.res_drop.set_tooltip_text(
            "Published resolution. Applying restarts the engine (and in "
            "Studio, the camera); attached apps must reconnect."
        )
        self.res_drop.connect("notify::selected", self._on_resolution)
        bar.append(self.res_drop)

        self.fps_entry = Gtk.Entry()
        self.fps_entry.add_css_class("dc-entry")
        self.fps_entry.set_has_frame(False)
        self.fps_entry.set_width_chars(4)
        self.fps_entry.set_max_width_chars(5)
        self.fps_entry.set_alignment(1.0)
        self.fps_entry.set_valign(Gtk.Align.CENTER)
        self.fps_entry.connect("activate", self._on_fps_commit)
        # Clamping happens ONLY on Enter. Correcting while someone is still
        # typing makes the box unusable - the sync loop keeps its hands off
        # from the first keystroke until commit (or focus loss reverts).
        self._fps_syncing = False
        self._fps_edited = False

        def fps_changed(_e):
            if not self._fps_syncing:
                self._fps_edited = True

        self.fps_entry.connect("changed", fps_changed)
        focus = Gtk.EventControllerFocus()

        def fps_focus_leave(*_a):
            self._fps_edited = False
            self._sync_fps_entry()

        focus.connect("leave", fps_focus_leave)
        self.fps_entry.add_controller(focus)
        bar.append(self.fps_entry)
        fps_lbl = Gtk.Label(label="fps")
        fps_lbl.add_css_class("dc-hint")
        fps_lbl.set_valign(Gtk.Align.CENTER)
        bar.append(fps_lbl)

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
        blurbs = {
            "call": (
                "The camera's own firmware: the MICROPHONE IS ON, focus and "
                "white balance stay automatic, and you get the colour "
                "controls at a fixed 30 fps. The mode for actual calls."
            ),
            "studio": (
                "Stock DepthAI firmware: manual focus and white balance, "
                "tap-to-focus, effects and free frame rates up to 42 - but "
                "the MICROPHONE IS OFF (this firmware has no audio at all). "
                "Switching reboots the camera, about 15 s."
            ),
        }
        for mode, text in (("call", "Call"), ("studio", "Studio")):
            b = Gtk.Button(label=text)
            b.add_css_class("dc-chip")
            b.set_tooltip_text(blurbs[mode])
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
        self.mic_chip = Gtk.Label(label="MIC")
        self.mic_chip.add_css_class("dc-mic")
        self.mic_chip.set_valign(Gtk.Align.CENTER)
        row.append(self.mic_chip)
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

    def _zoom_row(self) -> Gtk.Widget:
        row, self.zoom_scale, self.zoom_value = self._slider_row(
            "Zoom", 1.0, 4.0, 0.1, digits=1
        )
        self.zoom_scale.connect("value-changed", self._on_zoom)
        self.zoom_scale.set_tooltip_text(
            "Digital zoom. Scroll on the preview to zoom, drag to pan."
        )
        return row

    def _on_zoom(self, scale: Gtk.Scale) -> None:
        self._set_value_text(self.zoom_value, f"{scale.get_value():.1f}")
        if self._suppress or not self._ready:
            return
        self._queue({"zoom": round(scale.get_value(), 2)})

    def _on_preview_scroll(self, _ctrl, _dx, dy: float) -> bool:
        if not self._ready:
            return True
        current = float(self.status.get("zoom") or 1.0)
        target = max(1.0, min(4.0, current - dy * 0.25))
        self._queue({"zoom": round(target, 2)})
        return True

    def _on_pan_begin(self, _gesture, _x, _y) -> None:
        self._pan_base = (
            float(self.status.get("pan_x") or 0.0),
            float(self.status.get("pan_y") or 0.0),
        )

    def _on_pan_update(self, _gesture, dx: float, dy: float) -> None:
        if not self._ready:
            return
        zoom = float(self.status.get("zoom") or 1.0)
        if zoom <= 1.001:
            return
        # Dragging moves the content with the pointer: pan spans [-1, 1]
        # across the crop margin, so scale pixels through the visible span.
        w = max(1, self.preview.get_width())
        h = max(1, self.preview.get_height())
        span = 2.0 * zoom / (zoom - 1.0)
        px = max(-1.0, min(1.0, self._pan_base[0] - dx / w * span))
        py = max(-1.0, min(1.0, self._pan_base[1] - dy / h * span))
        self._queue({"pan_x": round(px, 3), "pan_y": round(py, 3)})

    def _clahe_row(self) -> Gtk.Widget:
        row, self.clahe_scale, self.clahe_value = self._slider_row(
            "Clarity", 0.0, 1.0, 0.05, digits=2
        )
        self.clahe_scale.set_tooltip_text(
            "Local contrast (CLAHE): brightens shadows and recovers detail "
            "region by region rather than globally"
        )
        self.clahe_scale.connect("value-changed", self._on_clahe)
        return row

    def _on_clahe(self, scale: Gtk.Scale) -> None:
        self._set_value_text(self.clahe_value, f"{scale.get_value():.2f}")
        if self._suppress or not self._ready:
            return
        self._queue({"clahe": round(scale.get_value(), 2)})

    def _blur_row(self) -> Gtk.Widget:
        row, self.blur_scale, self.blur_value = self._slider_row(
            "Blur", 0.0, 1.0, 0.05, digits=2
        )
        self.blur_scale.set_tooltip_text(
            "Background blur: person segmentation masks you out, everything "
            "else gets a disc blur. Swap the model or drive the mask from "
            "your own process via the engine's mask socket"
        )
        self.blur_scale.connect("value-changed", self._on_blur)
        self.blur_label = row.get_first_child()
        self.blur_label.add_css_class("dc-clickable")
        self.blur_label.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        self.blur_label.set_tooltip_text("Click to switch between Blur and Bokeh")
        toggle = Gtk.GestureClick()
        toggle.connect("released", self._on_blur_style_toggle)
        self.blur_label.add_controller(toggle)
        return row

    def _on_blur_style_toggle(self, *_a) -> None:
        style = "smooth" if (self.status.get("blur_style") or 0) else "bokeh"
        _worker(
            lambda: self.client.request(cmd="set_blur", style=style),
            self._on_result,
        )

    def _on_blur(self, scale: Gtk.Scale) -> None:
        self._set_value_text(self.blur_value, f"{scale.get_value():.2f}")
        if self._suppress or not self._ready:
            return
        self._queue({"blur": round(scale.get_value(), 2)})

    def _background_row(self) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        lbl = Gtk.Label(label="Backdrop", xalign=0)
        lbl.add_css_class("dc-label")
        lbl.set_size_request(64, -1)
        row.append(lbl)

        self.background_button = Gtk.Button(label="choose…")
        self.background_button.add_css_class("dc-chip")
        self.background_button.set_hexpand(True)
        self.background_button.set_tooltip_text(
            "Replace the background with an image (uses the same person "
            "mask as blur)"
        )
        self.background_button.connect("clicked", self._on_background_choose)
        row.append(self.background_button)

        self.background_clear = Gtk.Button(label="×")
        self.background_clear.add_css_class("dc-tiny")
        self.background_clear.set_valign(Gtk.Align.CENTER)
        self.background_clear.set_tooltip_text("Back to blur (or nothing)")
        self.background_clear.connect("clicked", self._on_background_clear)
        row.append(self.background_clear)
        return row

    def _on_background_choose(self, _btn) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title("Choose a background image")
        png = Gtk.FileFilter()
        png.set_name("PNG images")
        png.add_mime_type("image/png")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(png)
        dialog.set_filters(filters)
        # No transient parent - see _on_overlay_choose.
        dialog.open(None, None, self._on_background_chosen)

    def _on_background_chosen(self, dialog, result) -> None:
        try:
            path = dialog.open_finish(result).get_path()
        except Exception:
            return  # cancelled
        if not path:
            return
        _worker(
            lambda: self.client.request(cmd="set_background", path=path),
            self._on_result,
        )

    def _on_background_clear(self, _btn) -> None:
        _worker(
            lambda: self.client.request(cmd="set_background", path=None),
            self._on_result,
        )

    def _models_section(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        lbl = Gtk.Label(label="Models", xalign=0)
        lbl.add_css_class("dc-label")
        lbl.set_size_request(64, -1)
        lbl.set_tooltip_text(
            "Your own ONNX models over the feed. One-channel output = joins "
            "the person mask; three-channel output = recolors the frame. "
            "Strength is live; add/remove/device restarts the engine"
        )
        head.append(lbl)
        add = Gtk.Button(label="add model…")
        add.add_css_class("dc-chip")
        add.set_hexpand(True)
        add.connect("clicked", self._on_model_add)
        head.append(add)
        self.model_add_btn = add
        box.append(head)
        self.models_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        box.append(self.models_box)
        self._models_cache: list = []
        return box

    def _rebuild_model_rows(self, models: list) -> None:
        child = self.models_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.models_box.remove(child)
            child = nxt
        for i, m in enumerate(models):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
            name = Gtk.Label(label=Path(m["path"]).name, xalign=0)
            name.add_css_class("dc-hint")
            name.set_ellipsize(3)  # Pango.EllipsizeMode.END
            name.set_size_request(110, -1)
            if m.get("missing"):
                name.set_opacity(0.4)
                name.set_tooltip_text(
                    f"{m['path']}\nmissing \u2014 bypassed until the file returns"
                )
            else:
                name.set_tooltip_text(m["path"])
            row.append(name)

            dev = Gtk.Button(label=m["device"])
            dev.add_css_class("dc-tiny")
            dev.set_valign(Gtk.Align.CENTER)
            dev.set_tooltip_text("Toggle cpu/cuda (restarts the engine)")
            dev.connect("clicked", self._on_model_device, i)
            row.append(dev)

            scale = Gtk.Scale.new_with_range(
                Gtk.Orientation.HORIZONTAL, 0.0, 1.0, 0.05
            )
            scale.set_draw_value(False)
            scale.set_hexpand(True)
            scale.set_valign(Gtk.Align.CENTER)
            scale.set_value(float(m["strength"]))
            scale.set_tooltip_text("Strength (live)")
            scale.connect("value-changed", self._on_model_strength, i)
            row.append(scale)

            rm = Gtk.Button(label="×")
            rm.add_css_class("dc-tiny")
            rm.set_valign(Gtk.Align.CENTER)
            rm.set_tooltip_text("Remove from the chain (restarts the engine)")
            rm.connect("clicked", self._on_model_rm, i)
            row.append(rm)
            self.models_box.append(row)

    def _on_model_add(self, _btn) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title("Choose an ONNX model")
        onnx = Gtk.FileFilter()
        onnx.set_name("ONNX models")
        onnx.add_pattern("*.onnx")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(onnx)
        dialog.set_filters(filters)
        # No transient parent - see _on_overlay_choose.
        dialog.open(None, None, self._on_model_chosen)

    def _on_model_chosen(self, dialog, result) -> None:
        try:
            path = dialog.open_finish(result).get_path()
        except Exception:
            return  # cancelled
        if not path:
            return
        # The device is chosen BEFORE anything loads: a heavy model launched
        # on the wrong compute can choke the machine, and by the time a
        # toggle-after-the-fact could fix it, the bad load already happened.
        # A popover, not a dialog - the panel is a layer surface and a
        # parented dialog is a protocol error that kills the client.
        pop = Gtk.Popover()
        pop.set_parent(self.model_add_btn)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(8); box.set_margin_bottom(8)
        box.set_margin_start(10); box.set_margin_end(10)
        title = Gtk.Label(label=f"Run {Path(path).name} on:", xalign=0)
        title.add_css_class("dc-label")
        box.append(title)
        hint = Gtk.Label(
            label="CPU is the safe default. Pick GPU (CUDA) for heavy\n"
                  "models on a machine with the CUDA runtime installed.",
            xalign=0,
        )
        hint.add_css_class("dc-hint")
        box.append(hint)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for label, device in (("CPU", "cpu"), ("GPU (CUDA)", "cuda")):
            b = Gtk.Button(label=label)
            b.add_css_class("dc-chip")
            b.set_hexpand(True)
            b.connect("clicked", self._on_model_device_picked, path, device, pop)
            row.append(b)
        cancel = Gtk.Button(label="Cancel")
        cancel.add_css_class("dc-chip")
        cancel.connect("clicked", lambda _b: pop.popdown())
        row.append(cancel)
        box.append(row)
        pop.set_child(box)
        self._model_pop = pop  # keep alive while open
        pop.popup()

    def _on_model_device_picked(self, _btn, path: str, device: str, pop) -> None:
        pop.popdown()
        models = list(self._models_cache) + [
            {"path": path, "device": device, "strength": 1.0}
        ]
        self._set_busy(True, f"adding model on {device}… the engine restarts")
        _worker(
            lambda: self.client.request(cmd="set_models", models=models),
            self._on_result,
        )

    def _maybe_confirm_restart(self, anchor, what: str, proceed) -> None:
        """An engine restart is a firmware reboot only under Studio (the
        depthai session owns the camera); in Call it just reopens a V4L2
        node and needs no ceremony."""
        if self._current_res_mode() == "studio":
            self._confirm(
                anchor,
                f"{what} restarts the engine, which in Studio mode reboots "
                "the camera's firmware (~15 s).",
                proceed,
            )
        else:
            proceed()

    def _on_model_rm(self, btn, index: int) -> None:
        models = [m for i, m in enumerate(self._models_cache) if i != index]

        def proceed():
            self._set_busy(True, "removing model… the engine restarts")
            _worker(
                lambda: self.client.request(cmd="set_models", models=models),
                self._on_result,
            )

        self._maybe_confirm_restart(btn, "Removing this model", proceed)

    def _on_model_device(self, btn, index: int) -> None:
        models = [dict(m) for m in self._models_cache]
        if not 0 <= index < len(models):
            return
        models[index]["device"] = (
            "cuda" if models[index]["device"] == "cpu" else "cpu"
        )

        def proceed():
            self._set_busy(True, "switching device… the engine restarts")
            _worker(
                lambda: self.client.request(cmd="set_models", models=models),
                self._on_result,
            )

        self._maybe_confirm_restart(btn, "Switching this model's device", proceed)

    def _on_model_strength(self, scale: Gtk.Scale, index: int) -> None:
        if self._suppress or not self._ready:
            return
        value = round(scale.get_value(), 2)
        self._queue({f"model_strength_{index}": value})

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
        scale.set_draw_value(False)  # the number lives in its own entry
        scale.set_valign(Gtk.Align.CENTER)
        if digits == 0:
            scale.set_round_digits(0)
        row.append(scale)

        # The value is an entry dressed as a label: exact numbers are typed,
        # Enter commits (clamped to the slider's range).
        value = Gtk.Entry()
        value.add_css_class("dc-entry")
        value.set_has_frame(False)
        value.set_width_chars(6)
        value.set_max_width_chars(7)
        value.set_alignment(1.0)
        value.set_valign(Gtk.Align.CENTER)
        row.append(value)

        # From the first keystroke until Enter, the box belongs to the
        # user: no clamping, no sync overwrites. Enter commits (clamped to
        # the slider's range); leaving without Enter reverts the display.
        value._dc_edited = False

        def changed(_e):
            if not getattr(value, "_dc_syncing", False):
                value._dc_edited = True

        value.connect("changed", changed)

        def commit(_widget=None):
            value._dc_edited = False
            text = value.get_text().strip().rstrip("x")
            try:
                v = float(text)
            except ValueError:
                self._set_value_text(value, f"{scale.get_value():.{digits}f}")
                return
            adj = scale.get_adjustment()
            scale.set_value(max(adj.get_lower(), min(adj.get_upper(), v)))
            self._set_value_text(value, f"{scale.get_value():.{digits}f}")
            root = self.get_root()
            if root is not None:
                root.set_focus(None)

        def revert(*_a):
            value._dc_edited = False
            self._set_value_text(value, f"{scale.get_value():.{digits}f}")

        value.connect("activate", commit)
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", revert)
        value.add_controller(focus)
        return row, scale, value

    def _set_value_text(self, entry: Gtk.Entry, text: str) -> None:
        """Update a value box unless the user is editing it."""
        if entry.has_focus() or getattr(entry, "_dc_edited", False):
            return
        entry._dc_syncing = True
        try:
            entry.set_text(text)
        finally:
            entry._dc_syncing = False

    def _camera_block(self) -> Gtk.Widget:
        """Camera controls as a stack: one page per mode, each showing only
        the controls that mode's firmware can actually drive. The stack is
        homogeneous, so both pages occupy the larger page's space and the
        panel never changes size with the mode."""
        self.sliders = {}        # key -> [scale, ...] (shared keys: one per page)
        self.slider_labels = {}
        self.slider_values = {}
        self.auto_buttons = {}

        self.camera_stack = Gtk.Stack()
        for page_mode in (CALL, STUDIO):
            self.camera_stack.add_named(self._camera_page(page_mode), page_mode)

        self.camera_hint = Gtk.Label(xalign=0, wrap=True)
        self.camera_hint.add_css_class("dc-hint")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.append(self.camera_stack)
        box.append(self.camera_hint)
        return box

    def _camera_page(self, page_mode: str) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_valign(Gtk.Align.START)
        for key, label, lo, hi, step, modes in SLIDERS:
            if page_mode not in modes:
                continue
            row, scale, value = self._slider_row(label, lo, hi, step)
            scale.connect("value-changed", self._on_slider, key)
            if key in AUTO_CAPABLE:
                auto = Gtk.Button(label="auto")
                auto.add_css_class("dc-tiny")
                auto.set_valign(Gtk.Align.CENTER)
                auto.set_tooltip_text(f"Hand {label.lower()} back to the camera")
                auto.connect("clicked", self._on_auto, key)
                row.append(auto)
                self.auto_buttons.setdefault(key, []).append(auto)
            self.sliders.setdefault(key, []).append(scale)
            self.slider_values.setdefault(key, []).append(value)
            self.slider_labels.setdefault(key, []).append(row.get_first_child())
            box.append(row)

        if page_mode == STUDIO:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
            lbl = Gtk.Label(label="Effect", xalign=0)
            lbl.add_css_class("dc-label")
            lbl.set_size_request(64, -1)
            row.append(lbl)
            self.effect_drop = Gtk.DropDown.new_from_strings(EFFECTS)
            self.effect_drop.set_hexpand(True)
            self.effect_drop.connect("notify::selected", self._on_effect_selected)
            row.append(self.effect_drop)
            box.append(row)
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

    def _confirm(self, anchor, text: str, on_proceed) -> None:
        """Firmware reboots are never free: every action that costs one
        goes through this popover. Same shape as the model-device chooser -
        a popover, because the panel is a layer surface and a parented
        dialog is a protocol error."""
        pop = Gtk.Popover()
        pop.set_parent(anchor)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(8); box.set_margin_bottom(8)
        box.set_margin_start(10); box.set_margin_end(10)
        lbl = Gtk.Label(label=text, xalign=0, wrap=True)
        lbl.add_css_class("dc-hint")
        lbl.set_max_width_chars(38)
        box.append(lbl)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        go = Gtk.Button(label="Proceed")
        go.add_css_class("dc-chip")
        go.set_hexpand(True)
        cancel = Gtk.Button(label="Cancel")
        cancel.add_css_class("dc-chip")
        cancel.set_hexpand(True)

        def proceed(_b):
            pop.popdown()
            on_proceed()

        go.connect("clicked", proceed)
        cancel.connect("clicked", lambda _b: pop.popdown())
        row.append(go)
        row.append(cancel)
        box.append(row)
        pop.set_child(box)
        self._confirm_pop = pop  # keep alive while open
        pop.popup()

    def _current_res_mode(self) -> str:
        return self.status.get("mode", "call")

    def _on_resolution(self, drop, _param) -> None:
        if self._suppress or not self._ready or self.busy:
            return
        index = drop.get_selected()
        if index >= len(self._res_choices):
            return
        label, w, h, iw, ih = self._res_choices[index]
        mode = self._current_res_mode()

        def proceed():
            self._res_applied = index
            self._set_busy(True, "changing resolution… the engine restarts")
            _worker(
                lambda: self.client.request(
                    cmd="set_resolution",
                    width=w, height=h, in_width=iw, in_height=ih,
                ),
                self._on_result,
            )

        def cancel_revert():
            self._suppress = True
            try:
                self.res_drop.set_selected(self._res_applied)
            finally:
                self._suppress = False

        if mode == "studio":
            self._confirm_with_revert(
                self.res_drop,
                f"{label} re-enters Studio mode, which reboots the "
                "camera's firmware (~15 s).",
                proceed, cancel_revert,
            )
        else:
            proceed()

    def _confirm_with_revert(self, anchor, text, on_proceed, on_cancel) -> None:
        pop = Gtk.Popover()
        pop.set_parent(anchor)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(8); box.set_margin_bottom(8)
        box.set_margin_start(10); box.set_margin_end(10)
        lbl = Gtk.Label(label=text, xalign=0, wrap=True)
        lbl.add_css_class("dc-hint")
        lbl.set_max_width_chars(38)
        box.append(lbl)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        done = {"handled": False}

        def proceed(_b):
            done["handled"] = True
            pop.popdown()
            on_proceed()

        def cancel(_b=None):
            if not done["handled"]:
                done["handled"] = True
                on_cancel()
            pop.popdown()

        go = Gtk.Button(label="Proceed")
        go.add_css_class("dc-chip")
        go.set_hexpand(True)
        go.connect("clicked", proceed)
        cxl = Gtk.Button(label="Cancel")
        cxl.add_css_class("dc-chip")
        cxl.set_hexpand(True)
        cxl.connect("clicked", cancel)
        # Dismissing by clicking elsewhere is a cancel too.
        pop.connect("closed", lambda *_: cancel())
        row.append(go)
        row.append(cxl)
        box.append(row)
        pop.set_child(box)
        self._confirm_pop = pop
        pop.popup()

    # -- power ----------------------------------------------------------

    def _on_power_state(self, switch, want: bool) -> bool:
        if self._suppress:
            return False  # status sync: let the switch follow reality
        if self.busy or not self._ready:
            return True  # swallow the flip
        running = bool(self.status.get("running"))
        if want == running:
            return False

        def proceed():
            self._set_busy(
                True,
                "starting the camera\u2026" if want
                else "parking the camera\u2026 the firmware reboots to Studio",
            )
            _worker(
                lambda: self.client.request(cmd="set_power", on=want),
                self._on_result,
            )

        if want:
            text = (
                f"Turning the camera on re-enters "
                f"{self._current_res_mode().capitalize()} mode, rebooting "
                "the firmware (~15 s)."
            )
        else:
            text = (
                "Off parks the camera on Studio firmware - the only resting "
                "state where the MICROPHONE is off too. From Call mode this "
                "reboots the firmware (~15 s)."
            )
        self._confirm(switch, text, proceed)
        # Handled: the switch only slides once status confirms the change.
        return True

    # -- capture --------------------------------------------------------
    #
    # One button, two verbs: a click is a photo behind a 3 s count, a
    # one-second hold starts a recording that the next click stops.

    @staticmethod
    def _capture_dir(kind: str) -> Path:
        special = GLib.get_user_special_dir(
            GLib.UserDirectory.DIRECTORY_PICTURES if kind == "photo"
            else GLib.UserDirectory.DIRECTORY_VIDEOS
        )
        base = Path(special) if special else (
            Path.home() / ("Pictures" if kind == "photo" else "Videos")
        )
        out = base / "decomposer"
        out.mkdir(parents=True, exist_ok=True)
        return out

    def _on_capture_pressed(self, _g, _n, _x, _y) -> None:
        if self._recorder is not None:
            return  # the release handles stop
        self._hold_fired = False
        self._hold_timer = GLib.timeout_add(1000, self._hold_elapsed)

    def _hold_elapsed(self) -> bool:
        self._hold_timer = None
        self._hold_fired = True
        self._start_recording()
        return False

    def _on_capture_released(self, _g, _n, _x, _y) -> None:
        if self._hold_timer is not None:
            GLib.source_remove(self._hold_timer)
            self._hold_timer = None
        if self._hold_fired:
            return  # the hold already started the recording
        if self._recorder is not None:
            self._stop_recording()
        elif self._countdown is None:
            self._start_countdown()

    def _start_countdown(self) -> None:
        if not self.status.get("engine_alive"):
            self._flash("no feed to photograph", 4.0)
            return
        self._count_left = 3
        self.count_label.set_text("3")
        self.count_label.set_visible(True)
        self._countdown = GLib.timeout_add(1000, self._count_tick)

    def _count_tick(self) -> bool:
        self._count_left -= 1
        if self._count_left > 0:
            self.count_label.set_text(str(self._count_left))
            return True
        self._countdown = None
        self.count_label.set_visible(False)
        self._take_photo()
        return False

    def _take_photo(self) -> None:
        # The shutter flash fires at the moment of capture.
        self.flash_box.set_opacity(0.85)
        GLib.timeout_add(60, self._flash_decay)
        out = self._capture_dir("photo") / time.strftime(
            "photo-%Y%m%d-%H%M%S.png"
        )

        def snap() -> dict:
            r = subprocess.run(
                ["ffmpeg", "-y", "-f", "v4l2",
                 "-i", self.status.get("output") or "/dev/video10",
                 "-frames:v", "1", str(out)],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode != 0 or not out.is_file():
                tail = (r.stderr or "").strip().splitlines()
                return {"ok": False,
                        "error": tail[-1] if tail else "ffmpeg failed"}
            return {"ok": True, "saved": str(out)}

        def done(resp: dict) -> bool:
            self._flash(
                f"saved {resp['saved']}" if resp.get("ok")
                else f"photo failed: {resp.get('error')}",
                6.0,
            )
            return False

        _worker(snap, done)

    def _flash_decay(self) -> bool:
        v = self.flash_box.get_opacity() - 0.17
        self.flash_box.set_opacity(max(0.0, v))
        return v > 0.0

    def _start_recording(self) -> None:
        if self._recorder is not None or not self.status.get("engine_alive"):
            return
        out = self._capture_dir("video") / time.strftime(
            "rec-%Y%m%d-%H%M%S.mkv"
        )
        cmd = ["ffmpeg", "-y",
               "-f", "v4l2", "-i", self.status.get("output") or "/dev/video10"]
        if shutil.which("pactl"):
            # Whatever the system's default microphone is - in Call mode
            # that may well be the C1 itself.
            cmd += ["-f", "pulse", "-i", "default", "-c:a", "aac"]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                str(out)]
        try:
            self._recorder = subprocess.Popen(
                cmd, stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError as e:
            self._flash(f"recording failed to start: {e}", 6.0)
            return
        self._rec_path = out
        self.capture_btn.set_label("\u25a0")
        self.capture_btn.set_tooltip_text("Click to stop recording")
        self.rec_badge.set_visible(True)
        self._rec_blink = GLib.timeout_add(600, self._blink_rec)

    def _blink_rec(self) -> bool:
        if self._recorder is None:
            return False
        self.rec_badge.set_visible(not self.rec_badge.get_visible())
        return True

    def _stop_recording(self) -> None:
        rec, self._recorder = self._recorder, None
        if self._rec_blink is not None:
            GLib.source_remove(self._rec_blink)
            self._rec_blink = None
        self.rec_badge.set_visible(False)
        self.capture_btn.set_label("\u25c9")
        self.capture_btn.set_tooltip_text(
            "Click: photo (3 s timer). Hold 1 s: record; click again to stop"
        )
        path = getattr(self, "_rec_path", None)

        def finish() -> dict:
            with suppress(Exception):
                rec.stdin.write(b"q")  # ffmpeg's own clean-finalize knob
                rec.stdin.flush()
            try:
                rec.wait(timeout=10)
            except subprocess.TimeoutExpired:
                rec.terminate()
                with suppress(Exception):
                    rec.wait(timeout=5)
            return {"ok": True, "saved": str(path)}

        def done(resp: dict) -> bool:
            self._flash(f"saved {resp['saved']}", 6.0)
            return False

        _worker(finish, done)

    def _on_fps_commit(self, entry) -> None:
        if self._suppress or not self._ready or self.busy:
            return
        mode = self._current_res_mode()
        self._fps_edited = False
        try:
            wanted = float(entry.get_text().strip())
        except ValueError:
            self._sync_fps_entry()
            return
        lo, hi = self.status.get("fps_range") or (30.0, 30.0)
        clamped = max(lo, min(hi, wanted))
        current = float(self.status.get("fps") or 30.0)
        if mode != "studio" or abs(clamped - current) < 0.01:
            self._sync_fps_entry()
            return
        entry.set_text(f"{clamped:g}")

        def proceed():
            self._set_busy(True, f"setting {clamped:g} fps… the camera reboots")
            _worker(
                lambda: self.client.request(cmd="set_fps", fps=clamped),
                self._on_result,
            )

        self._confirm_with_revert(
            entry,
            f"{clamped:g} fps re-enters Studio mode, which reboots the "
            "camera's firmware (~15 s).",
            proceed, self._sync_fps_entry,
        )

    def _sync_fps_entry(self) -> None:
        if self._fps_edited or self.fps_entry.has_focus():
            return
        self._fps_syncing = True
        try:
            self.fps_entry.set_text(f"{float(self.status.get('fps') or 30.0):g}")
        finally:
            self._fps_syncing = False

    def _on_preview_menu(self, *_args) -> None:
        style = self.preview.cycle_placeholder()
        pretty = {"nofeed": "NO FEED card", "bars": "broadcast bars"}[style]
        self.footer.set_text(f"no-feed placeholder: {pretty}")

    def _on_preview_tap(self, gesture, _n_press, cx: float, cy: float) -> None:
        """Tap to focus: aim autofocus and exposure metering where clicked."""
        if self.busy or not self._ready:
            return
        if self.status.get("mode") != "studio":
            self.footer.set_text("tap-to-focus needs Studio mode")
            return
        # The preview uses ContentFit.CONTAIN (letterboxed), so undo that
        # mapping - with the stream's actual aspect - before converting to
        # frame coordinates.
        w = self.preview.get_width() or 1
        h = self.preview.get_height() or 1
        pw, ph = self.preview.stream_w or 1, self.preview.stream_h or 1
        scale = min(w / pw, h / ph)
        ix = (cx - (w - pw * scale) / 2) / scale
        iy = (cy - (h - ph * scale) / 2) / scale
        if not (0 <= ix < pw and 0 <= iy < ph):
            return  # tapped the letterbox, not the picture
        fw = int(self.status.get("width") or 1920)
        fh = int(self.status.get("height") or 1080)
        fx = max(0, min(fw - 1, int(ix / pw * fw)))
        fy = max(0, min(fh - 1, int(iy / ph * fh)))
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

    @staticmethod
    def _c1_mic_present() -> bool:
        """The truthful check: is the C1's UAC2 card actually registered?

        Mode implies what *should* exist; /proc/asound says what does. The
        two disagree exactly during firmware reboots, which is when a user
        looks at the chip.
        """
        try:
            return "C1" in Path("/proc/asound/cards").read_text()
        except OSError:
            return False

    def _update_mic_chip(self, studio: bool) -> None:
        live = self._c1_mic_present()
        self.mic_chip.set_text("MIC \u25cf" if live else "MIC \u2013")
        for cls in ("live", "dead"):
            self.mic_chip.remove_css_class(cls)
        self.mic_chip.add_css_class("live" if live else "dead")
        if live:
            tip = "The C1's microphone is live — pick “Opal C1” in your call app."
        elif studio:
            tip = (
                "Studio firmware has no microphone — the mic only exists "
                "under Call mode's Opal firmware."
            )
        else:
            tip = "The C1's mic card is not registered (camera rebooting?)."
        self.mic_chip.set_tooltip_text(tip)

    def _on_mode(self, btn, mode: str) -> None:
        if self.busy or self.status.get("mode") == mode:
            return

        def proceed():
            if mode == "studio":
                self._warn_if_default_mic()
            # Dim right now, not at the next status tick: the click is the
            # moment the controls stop being real.
            self.camera_stack.set_sensitive(False)
            self.camera_stack.set_opacity(0.45)
            for widget in (
                list(self.mode_buttons.values())
                + [self.res_drop, self.fps_entry, self.power_switch,
                   self.preset_drop, self.preset_save]
            ):
                widget.set_sensitive(False)
            self._set_busy(True, f"switching to {mode}, the camera reboots…")
            _worker(
                lambda: self.client.request(cmd="set_mode", mode=mode),
                self._on_result,
            )

        self._confirm(
            btn,
            f"Switching to {mode.capitalize()} reboots the camera's "
            "firmware (~15 s)"
            + (" and turns the microphone off." if mode == "studio"
               else " and brings the microphone back."),
            proceed,
        )

    def _warn_if_default_mic(self) -> None:
        """If the system's default mic is the C1, say what Studio costs.

        The check runs off-thread (pactl can dawdle); the warning lands in
        the footer as a timed flash that outlives the switch chatter.
        """
        def check():
            try:
                name = subprocess.run(
                    ["pactl", "get-default-source"],
                    capture_output=True, text=True, timeout=3,
                ).stdout.strip().lower()
            except Exception:
                return
            if "opal" in name or "c1" in name:
                GLib.idle_add(
                    self._flash,
                    "heads-up: the C1 mic disappears in Studio — apps on it "
                    "fall back to another source",
                    12.0,
                )
        threading.Thread(target=check, daemon=True).start()

    def _flash(self, text: str, seconds: float = 8.0) -> bool:
        self._flash_text = text
        self._flash_until = time.monotonic() + seconds
        self.footer.set_text(text)
        return False

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
        self._set_value_text(self.strength_value, f"{scale.get_value():.2f}")
        if self._suppress or not self._ready:
            return
        self._queue({"strength": round(scale.get_value(), 2)})

    def _on_slider(self, scale: Gtk.Scale, key: str) -> None:
        for entry in self.slider_values[key]:
            self._set_value_text(entry, f"{int(scale.get_value())}")
        if self._suppress or not self._ready:
            return
        self._touched[key] = time.monotonic()
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
        for key in [k for k in pending if k.startswith("model_strength_")]:
            index = int(key.rsplit("_", 1)[1])
            value = pending.pop(key)
            _worker(
                lambda i=index, v=value: self.client.request(
                    cmd="set_model_strength", index=i, strength=v
                ),
                self._on_result,
            )
        blur = pending.pop("blur", None)
        if blur is not None:
            _worker(
                lambda: self.client.request(cmd="set_blur", strength=blur),
                self._on_result,
            )
        clahe = pending.pop("clahe", None)
        if clahe is not None:
            _worker(
                lambda: self.client.request(cmd="set_clahe", strength=clahe),
                self._on_result,
            )
        zoomish = {
            k: pending.pop(k)
            for k in ("zoom", "pan_x", "pan_y")
            if k in pending
        }
        if zoomish:
            _worker(
                lambda: self.client.request(cmd="set_zoom", **zoomish),
                self._on_result,
            )
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
        # One in flight, ever: the 2s tick plus a slow daemon used to pile up
        # worker threads, each parked on a 60s socket timeout.
        if self._refreshing:
            return
        self._refreshing = True

        def done(resp: dict) -> None:
            self._refreshing = False
            if self.busy:
                # A long action (a mode switch) is in flight. Show its
                # progress - transitioning dims the controls - but leave
                # busy for the action's own completion to clear, so the
                # buttons stay locked until it actually finishes.
                if resp.get("ok"):
                    self.status = resp
                    self._apply(resp)
                return
            self._on_result(resp)

        _worker(lambda: self.client.request(cmd="status"), done)

    def _tick(self) -> bool:
        # Ticks run during long actions too: status is a cheap snapshot,
        # and a 15-second mode switch with a frozen panel reads as a hang.
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
            self.camera_stack.set_sensitive(False)
            return False
        self.footer.remove_css_class("dc-warn")
        refused = resp.get("refused") or {}
        if refused:
            # ok:True with refusals is routing truth ("effect needs Studio
            # mode"), and silence here is what reads as a dead control.
            self._flash("; ".join(refused.values()), 6.0)
        self.status = resp
        self._apply(resp)
        return False

    def _apply(self, st: dict) -> None:
        mode = st.get("mode", "call")
        studio = mode == "studio"
        transitioning = bool(st.get("transitioning"))

        self.mode_pill.set_text(mode.upper())
        for cls in ("call", "studio", "off"):
            self.mode_pill.remove_css_class(cls)
        self.mode_pill.add_css_class(mode if mode in ("call", "studio") else "off")
        self.mode_hint.set_text("")
        self._update_mic_chip(studio)

        for axis, key in (("horizontal", "mirror_h"), ("vertical", "mirror_v")):
            b = self.mirror_buttons[axis]
            b.set_sensitive(True)
            (b.add_css_class if st.get(key) else b.remove_css_class)("selected")

        for name, b in self.mode_buttons.items():
            b.set_sensitive(not transitioning)
            (b.add_css_class if name == mode else b.remove_css_class)("selected")

        look = st.get("look", "none")
        # A look whose LUT is not installed is shown but not offered.
        offered = set(st.get("looks") or LOOKS)
        for name, b in self.look_buttons.items():
            b.set_sensitive(name in offered)
            b.set_opacity(1.0 if name in offered else 0.4)
            (b.add_css_class if name == look else b.remove_css_class)("selected")

        self._suppress = True
        try:
            if mode != self._res_mode and mode in ("call", "studio"):
                # The menu itself is mode-specific: Studio adds the 4:3 and
                # near-full-res sensor geometries Call's firmware lacks.
                self._res_mode = mode
                self._res_choices = [
                    tuple(r) for r in _resolutions_for(_Mode(mode))
                ]
                self.res_drop.set_model(
                    Gtk.StringList.new([r[0] for r in self._res_choices])
                )
            combo = (
                st.get("width", 1920), st.get("height", 1080),
                st.get("in_width", 0), st.get("in_height", 0),
            )
            for i, (_, w, h, iw, ih) in enumerate(self._res_choices):
                if (w, h, iw, ih) == combo:
                    self.res_drop.set_selected(i)
                    self._res_applied = i
                    break
            self.res_drop.set_sensitive(not transitioning)
            running = bool(st.get("running"))
            self.power_switch.set_active(running)
            self.power_switch.set_state(running)
            self.power_switch.set_sensitive(not transitioning)
            self.capture_btn.set_sensitive(
                bool(st.get("engine_alive")) or self._recorder is not None
            )
            self._sync_fps_entry()
            lo, hi = st.get("fps_range") or (30.0, 30.0)
            fps_editable = studio and not transitioning
            self.fps_entry.set_sensitive(fps_editable)
            self.fps_entry.set_opacity(1.0 if fps_editable else 0.5)
            self.fps_entry.set_tooltip_text(
                f"Capture frame rate: {lo:g}\u2013{hi:g} for this mode and "
                "resolution. Applying reboots the camera"
                if studio else
                "Call mode is fixed at 30 fps by the Opal firmware"
            )
        finally:
            self._suppress = False

        overlay = st.get("overlay")
        self.overlay_button.set_label(
            Path(overlay).name if overlay else "choose\u2026"
        )
        self.overlay_button.set_tooltip_text(overlay or "No overlay")
        self.overlay_clear.set_sensitive(bool(overlay))
        self.overlay_opacity.set_sensitive(bool(overlay))
        models = st.get("models") or []
        def _sig(items):
            return [(m["path"], m["device"], bool(m.get("missing"))) for m in items]
        if _sig(models) != _sig(self._models_cache):
            self._rebuild_model_rows(models)
        self._models_cache = models
        background = st.get("background")
        self.background_button.set_label(
            Path(background).name if background else "choose\u2026"
        )
        self.background_clear.set_sensitive(bool(background))

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
            self.preset_drop.set_sensitive(bool(names) and not transitioning)
            self.preset_save.set_sensitive(not transitioning)
            self.overlay_opacity.set_value(float(st.get("overlay_opacity", 1.0)))
            self.zoom_scale.set_value(float(st.get("zoom", 1.0)))
            self.clahe_scale.set_value(float(st.get("clahe", 0.0)))
            self.blur_scale.set_value(float(st.get("blur", 0.0)))
            self.blur_label.set_text(
                "Bokeh" if st.get("blur_style") else "Blur"
            )
            effect = (st.get("controls") or {}).get("effect", "off")
            if effect in EFFECTS:
                self.effect_drop.set_selected(EFFECTS.index(effect))
            self.strength.set_value(float(st.get("strength", 1.0)))
            controls = st.get("controls") or {}
            now = time.monotonic()
            # The stack shows only the current mode's page; while the feed
            # is down the whole page dims until it restarts.
            self.camera_stack.set_visible_child_name(
                mode if mode in (CALL, STUDIO) else CALL
            )
            feed_ok = bool(st.get("engine_alive")) and not transitioning
            self.camera_stack.set_sensitive(feed_ok)
            self.camera_stack.set_opacity(1.0 if feed_ok else 0.45)
            for key, scales in self.sliders.items():
                if now - self._touched.get(key, 0.0) < 3.0:
                    # The user owns this control right now; the camera's
                    # readback may not yank it out of their hand.
                    continue
                value = controls.get(key)
                on_auto = value == -1
                # A value the current mode cannot report shows "-": drawing
                # the slider at minimum would claim a setting of zero.
                known = key in model_controls_for(mode)
                for auto in self.auto_buttons.get(key, []):
                    # -1 means the camera is driving it.
                    (auto.add_css_class if on_auto
                     else auto.remove_css_class)("selected")
                for scale, entry in zip(scales, self.slider_values[key]):
                    if value is not None and not on_auto:
                        scale.set_value(float(value))
                    if not known:
                        text = "-"
                    elif on_auto:
                        text = "auto"
                    else:
                        text = f"{int(scale.get_value())}"
                    self._set_value_text(entry, text)
        finally:
            self._suppress = False
        self._ready = True

        self.camera_hint.set_text(
            "tap the preview to focus \u00b7 scroll to zoom, drag to pan"
            if studio
            else "focus, white balance and effects live in Studio mode"
        )

        if time.monotonic() < getattr(self, "_flash_until", 0.0):
            self.footer.set_text(self._flash_text)
        elif transitioning:
            self.footer.set_text("switching modes… the camera is rebooting")
        elif st.get("engine_alive"):
            self.footer.set_text(
                f"{st.get('width')}×{st.get('height')} → {st.get('output')}"
            )
        else:
            self.preview.show_placeholder()
            message = st.get("notice") or st.get("error") or "engine not running"
            # Engine logs can be a wall; the footer is one line of truth.
            if len(message) > 160:
                message = message[:157] + "…"
            self.footer.set_text(message)


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
        # Without this, a panel displaced by --replace lingers forever:
        # windowless, invisible, still polling the daemon on outdated code.
        # Two such ghosts were found running side by side.
        self.connect("name-lost", self._on_name_lost)
        self.window: Optional[Gtk.Window] = None
        self.panel: Optional[Panel] = None
        self.connect("activate", self.on_activate)

    def _on_name_lost(self, _app) -> bool:
        self.quit()
        return True

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
        win.set_default_size(PANEL_W, -1)
        win.set_resizable(False)

        if HAVE_LAYER_SHELL and LayerShell.is_supported():
            # A layer surface drops from the bar instead of being a managed
            # window the compositor will tile or centre.
            LayerShell.init_for_window(win)
            # TOP, not OVERLAY: fullscreen surfaces (the Omarchy
            # screensaver, a fullscreen video) render above the top layer,
            # so they cover the panel instead of being haunted by it.
            LayerShell.set_layer(win, LayerShell.Layer.TOP)
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
