"""decomposer control panel.

A client of the daemon, not a second owner of the camera: everything here is a
request over the daemon's socket. That matters because switching modes reboots
the camera and takes up to fifteen seconds, so every call runs on a worker
thread and the window stays responsive while the hardware catches up.

Colours come from the active Omarchy theme, so the panel matches the desktop
rather than imposing its own palette.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from opal_c1 import theme as omtheme  # noqa: E402
from opal_c1.daemon import Client  # noqa: E402

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
}

# Controls the camera exposes, and which mode can actually write them.
# Controls with an automatic mode the camera will take back.
AUTO_CAPABLE = ("focus", "wb")

SLIDERS = [
    ("brightness", "Brightness", 0, 255, 1, "both"),
    ("contrast", "Contrast", 0, 100, 1, "both"),
    ("saturation", "Saturation", 0, 100, 1, "both"),
    ("sharpness", "Sharpness", 0, 4, 1, "both"),
    ("exposure", "Exposure (us)", 1000, 33000, 100, "both"),
    ("iso", "ISO", 100, 1600, 50, "both"),
    ("focus", "Focus", 0, 255, 1, "studio"),
    ("wb", "White balance (K)", 1000, 12000, 100, "studio"),
]


def _worker(fn: Callable[[], dict], done: Callable[[dict], None]) -> None:
    """Run a daemon call off the UI thread and hand the result back on it."""

    def run() -> None:
        try:
            result = fn()
        except Exception as e:  # a dead daemon must not take the GUI with it
            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        GLib.idle_add(done, result)

    threading.Thread(target=run, daemon=True).start()


class Panel(Gtk.Box):
    def __init__(self, theme: omtheme.Theme):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.theme = theme
        self.client = Client()
        self.status: dict = {}
        self.busy = False
        self._pending: dict = {}
        self._debounce: Optional[int] = None
        self._suppress = False
        # Building a Gtk.Scale emits value-changed. Until the first status has
        # been applied those signals carry widget defaults, not camera state,
        # and acting on them would push every control to its minimum.
        self._ready = False

        self.append(self._header())

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        body.set_margin_top(18)
        body.set_margin_bottom(18)
        body.set_margin_start(18)
        body.set_margin_end(18)

        body.append(self._mode_card())
        body.append(self._look_card())
        body.append(self._camera_card())

        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(body)
        self.append(scroll)
        self.append(self._footer())

        self.refresh()
        GLib.timeout_add_seconds(2, self._tick)

    def set_theme(self, theme: omtheme.Theme) -> None:
        """Called when the desktop theme changes under us."""
        self.theme = theme
        self.subtitle.set_text(f"Opal C1  ·  {theme.name}")

    # -- chrome ---------------------------------------------------------

    def _header(self) -> Gtk.Widget:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        bar.add_css_class("dc-header")
        bar.set_margin_top(0)
        title = Gtk.Label(label="decomposer", xalign=0)
        title.add_css_class("dc-title")
        title.set_margin_start(16)
        title.set_margin_top(12)
        title.set_margin_bottom(12)
        bar.append(title)

        self.subtitle = Gtk.Label(label=f"Opal C1  ·  {self.theme.name}", xalign=0)
        self.subtitle.add_css_class("dc-hint")
        self.subtitle.set_valign(Gtk.Align.CENTER)
        bar.append(self.subtitle)

        spacer = Gtk.Box(hexpand=True)
        bar.append(spacer)

        self.mode_pill = Gtk.Label(label="—")
        self.mode_pill.add_css_class("dc-pill")
        self.mode_pill.add_css_class("off")
        self.mode_pill.set_valign(Gtk.Align.CENTER)
        self.mode_pill.set_margin_end(16)
        bar.append(self.mode_pill)
        return bar

    def _card(self, title: str) -> tuple[Gtk.Box, Gtk.Box]:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        label = Gtk.Label(label=title, xalign=0)
        label.add_css_class("dc-section")
        outer.append(label)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        inner.add_css_class("dc-card")
        outer.append(inner)
        return outer, inner

    # -- mode -----------------------------------------------------------

    def _mode_card(self) -> Gtk.Widget:
        outer, inner = self._card("Mode")
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.mode_buttons = {}
        for mode, label in (("call", "Call"), ("studio", "Studio")):
            b = Gtk.Button(label=label)
            b.add_css_class("dc-chip")
            b.connect("clicked", self._on_mode, mode)
            self.mode_buttons[mode] = b
            row.append(b)
        inner.append(row)

        self.mode_hint = Gtk.Label(xalign=0, wrap=True)
        self.mode_hint.add_css_class("dc-hint")
        self.mode_hint.set_text(
            "Call keeps the microphone and /dev/video0. Studio adds manual focus "
            "and white balance, but the camera reboots into firmware with no "
            "audio — the C1 mic disappears until you switch back."
        )
        inner.append(self.mode_hint)
        return outer

    def _on_mode(self, _btn, mode: str) -> None:
        if self.busy or self.status.get("mode") == mode:
            return
        self._set_busy(True, f"Switching to {mode}… the camera reboots, this takes a few seconds")
        _worker(lambda: self.client.request(cmd="set_mode", mode=mode), self._on_result)

    # -- looks ----------------------------------------------------------

    def _look_card(self) -> Gtk.Widget:
        outer, inner = self._card("Look")
        grid = Gtk.FlowBox()
        grid.set_selection_mode(Gtk.SelectionMode.NONE)
        grid.set_max_children_per_line(5)
        grid.set_row_spacing(8)
        grid.set_column_spacing(8)
        self.look_buttons = {}
        for name in LOOK_BLURB:
            b = Gtk.Button(label=name)
            b.add_css_class("dc-chip")
            b.set_tooltip_text(LOOK_BLURB[name])
            b.connect("clicked", self._on_look, name)
            self.look_buttons[name] = b
            grid.append(b)
        inner.append(grid)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        lbl = Gtk.Label(label="Strength", xalign=0)
        lbl.add_css_class("dc-value")
        lbl.set_size_request(110, -1)
        row.append(lbl)
        self.strength = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 1.0, 0.05)
        self.strength.set_hexpand(True)
        self.strength.set_draw_value(True)
        self.strength.set_value(1.0)
        self.strength.connect("value-changed", self._on_strength)
        row.append(self.strength)
        inner.append(row)
        return outer

    def _on_look(self, _btn, name: str) -> None:
        if self.busy:
            return
        _worker(lambda: self.client.request(cmd="set_look", look=name), self._on_result)

    def _on_strength(self, scale: Gtk.Scale) -> None:
        if self._suppress or not self._ready:
            return
        self._queue({"strength": round(scale.get_value(), 2)})

    # -- camera ---------------------------------------------------------

    def _camera_card(self) -> Gtk.Widget:
        outer, inner = self._card("Camera")
        self.sliders = {}
        self.slider_labels = {}
        self.auto_buttons = {}
        for key, label, lo, hi, step, avail in SLIDERS:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            lbl = Gtk.Label(label=label, xalign=0)
            lbl.add_css_class("dc-value")
            lbl.set_size_request(110, -1)
            row.append(lbl)

            scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, step)
            scale.set_hexpand(True)
            scale.set_draw_value(True)
            scale.set_round_digits(0)
            scale.connect("value-changed", self._on_slider, key)
            row.append(scale)

            # Focus and white balance are the two controls with a real automatic
            # mode on this camera; -1 hands them back to it.
            if key in AUTO_CAPABLE:
                auto = Gtk.Button(label="Auto")
                auto.add_css_class("dc-chip")
                auto.set_tooltip_text(f"Hand {label.lower()} back to the camera")
                auto.set_valign(Gtk.Align.CENTER)
                auto.connect("clicked", self._on_auto, key)
                row.append(auto)
                self.auto_buttons[key] = auto

            self.sliders[key] = scale
            self.slider_labels[key] = lbl
            inner.append(row)

        self.camera_hint = Gtk.Label(xalign=0, wrap=True)
        self.camera_hint.add_css_class("dc-hint")
        inner.append(self.camera_hint)
        return outer

    def _on_auto(self, _btn, key: str) -> None:
        if self.busy or not self._ready:
            return
        # Drop any queued drag for this control so it cannot re-apply a manual
        # value immediately after we ask for automatic.
        self._pending.pop(key, None)
        _worker(
            lambda: self.client.request(cmd="set_camera", values={key: -1}),
            self._on_result,
        )

    def _on_slider(self, scale: Gtk.Scale, key: str) -> None:
        if self._suppress or not self._ready:
            return
        self._queue({key: int(scale.get_value())})

    def _queue(self, values: dict) -> None:
        """Coalesce slider traffic: dragging emits continuously."""
        self._pending.update(values)
        if self._debounce is not None:
            GLib.source_remove(self._debounce)
        self._debounce = GLib.timeout_add(180, self._flush)

    def _flush(self) -> bool:
        self._debounce = None
        pending, self._pending = self._pending, {}
        if not pending:
            return False
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

    # -- footer / state -------------------------------------------------

    def _footer(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.add_css_class("dc-header")
        self.footer = Gtk.Label(xalign=0, wrap=True)
        self.footer.add_css_class("dc-hint")
        self.footer.set_margin_start(16)
        self.footer.set_margin_end(16)
        self.footer.set_margin_top(9)
        self.footer.set_margin_bottom(9)
        box.append(self.footer)
        return box

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self.busy = busy
        for b in self.mode_buttons.values():
            b.set_sensitive(not busy)
        for b in self.look_buttons.values():
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
            err = resp.get("error", "unknown error")
            self.mode_pill.set_text("no daemon")
            for cls in ("call", "studio"):
                self.mode_pill.remove_css_class(cls)
            self.mode_pill.add_css_class("off")
            self.footer.set_markup(f"<b>{GLib.markup_escape_text(err)}</b>")
            self.footer.add_css_class("dc-warn")
            for b in list(self.mode_buttons.values()) + list(self.look_buttons.values()):
                b.set_sensitive(False)
            for s in self.sliders.values():
                s.set_sensitive(False)
            return False
        self.footer.remove_css_class("dc-warn")
        self.status = resp
        self._apply_status(resp)
        return False

    def _apply_status(self, st: dict) -> None:
        mode = st.get("mode", "call")
        studio = mode == "studio"

        self.mode_pill.set_text(mode.upper())
        for cls in ("call", "studio", "off"):
            self.mode_pill.remove_css_class(cls)
        self.mode_pill.add_css_class(mode if mode in ("call", "studio") else "off")

        for name, b in self.mode_buttons.items():
            b.set_sensitive(True)
            if name == mode:
                b.add_css_class("selected")
            else:
                b.remove_css_class("selected")

        look = st.get("look", "none")
        for name, b in self.look_buttons.items():
            b.set_sensitive(True)
            if name == look:
                b.add_css_class("selected")
            else:
                b.remove_css_class("selected")

        self._suppress = True
        try:
            self.strength.set_value(float(st.get("strength", 1.0)))
            controls = st.get("controls") or {}
            for key, scale in self.sliders.items():
                available = key not in ("focus", "wb") or studio
                scale.set_sensitive(available)
                if key in self.auto_buttons:
                    self.auto_buttons[key].set_sensitive(available)
                self.slider_labels[key].set_opacity(1.0 if available else 0.45)
                value = controls.get(key)
                on_auto = value == -1
                if key in self.auto_buttons:
                    # -1 means the camera is driving it. Showing that on the
                    # button is honest; clamping it onto the slider would read
                    # as "focus 0", which is a real and very different setting.
                    if on_auto:
                        self.auto_buttons[key].add_css_class("selected")
                    else:
                        self.auto_buttons[key].remove_css_class("selected")
                if value is not None and not on_auto:
                    scale.set_value(float(value))
                if on_auto:
                    self.slider_labels[key].set_opacity(0.55)
        finally:
            self._suppress = False
        # Only now do slider signals represent user intent.
        self._ready = True

        self.camera_hint.set_text(
            "Focus and white balance are live in Studio mode."
            if studio
            else "Focus and white balance are greyed out: Call-mode firmware locks "
                 "them to automatic. Switch to Studio to control them."
        )

        alive = st.get("engine_alive")
        out = st.get("output", "?")
        if alive:
            self.footer.set_text(
                f"Publishing {st.get('width')}x{st.get('height')} to {out}  ·  "
                f"select it as “decomposer” in your camera app"
            )
        else:
            self.footer.set_text(st.get("error") or "Engine is not running")


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="dev.decomposer.Panel")
        self.connect("activate", self.on_activate)

    def on_activate(self, _app) -> None:
        # Launching again while the panel is open should bring it forward
        # rather than stacking a second identical window.
        existing = self.get_windows()
        if existing:
            existing[0].present()
            return

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
        win.set_default_size(560, 760)
        self.panel = Panel(theme)
        win.set_child(self.panel)
        win.present()

        self._watch_desktop()

    def _watch_desktop(self) -> None:
        """Re-read colours and font when the desktop changes them.

        Omarchy switches theme by repointing ~/.local/state/omarchy/current, so
        watching that directory catches a theme change without polling.
        """
        self._reload_pending = None
        self._monitors = []
        try:
            watch_dir = Gio.File.new_for_path(str(omtheme.STATE_THEME.parent))
            monitor = watch_dir.monitor_directory(Gio.FileMonitorFlags.NONE, None)
            monitor.connect("changed", self._on_desktop_changed)
            self._monitors.append(monitor)
        except Exception:
            pass
        try:
            settings = Gio.Settings.new("org.gnome.desktop.interface")
            settings.connect("changed::font-name", self._on_desktop_changed)
            self._settings = settings  # keep alive, or the signal is dropped
        except Exception:
            pass

    def _on_desktop_changed(self, *_args) -> None:
        # A theme switch rewrites several files; coalesce into one reload.
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
        if getattr(self, "panel", None) is not None:
            self.panel.set_theme(theme)
        return False


def main() -> int:
    return App().run(None)
