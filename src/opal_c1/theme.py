"""Read the active Omarchy theme so the GUI matches the rest of the desktop.

Omarchy keeps the selected theme's palette at
~/.local/state/omarchy/current/theme/colors.toml. Reading it at startup means
decomposer inherits whatever theme is set rather than shipping its own colours
and looking like a stranger on the desktop.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

STATE_THEME = Path.home() / ".local/state/omarchy/current/theme"
NAME_FILE = Path.home() / ".local/state/omarchy/current/theme.name"

# Used when Omarchy is not installed, or its palette cannot be read. Chosen to
# be legible rather than to imitate any particular theme.
FALLBACK = {
    "mode": "dark",
    "background": "#16181d",
    "dark_background": "#101216",
    "lighter_background": "#242830",
    "foreground": "#d6d8de",
    "dark_foreground": "#9aa0ac",
    "bright_foreground": "#f2f4f8",
    "accent": "#5c9cf5",
    "selection": "#2b3a52",
    "muted": "#555b66",
    "red": "#e2586a",
    "green": "#5fbf87",
    "yellow": "#e0b755",
}


def system_font() -> tuple[str, float]:
    """The desktop's UI font, as (family, points).

    GTK already honours this, but stating it in our own CSS keeps the panel
    consistent when the rest of the stylesheet sets sizes in rem.
    """
    desc = ""
    try:
        from gi.repository import Gio

        desc = Gio.Settings.new("org.gnome.desktop.interface").get_string("font-name")
    except Exception:
        pass
    if not desc:
        return ("sans-serif", 11.0)
    parts = desc.split()
    # Pango descriptions end with the size: "Adwaita Sans 11".
    if len(parts) > 1:
        try:
            return (" ".join(parts[:-1]), float(parts[-1]))
        except ValueError:
            pass
    return (desc, 11.0)


@dataclass
class Theme:
    name: str
    colors: dict

    def color(self, key: str, default: str = "#888888") -> str:
        return self.colors.get(key) or FALLBACK.get(key) or default

    @property
    def is_dark(self) -> bool:
        return str(self.colors.get("mode", "dark")).lower() != "light"


def load() -> Theme:
    name = "fallback"
    colors = dict(FALLBACK)
    try:
        if NAME_FILE.is_file():
            name = NAME_FILE.read_text().strip() or name
        path = STATE_THEME / "colors.toml"
        if path.is_file():
            import tomllib

            parsed = tomllib.loads(path.read_text())
            # Keep the fallbacks underneath: themes are not obliged to define
            # every key we use.
            colors.update({k: v for k, v in parsed.items() if isinstance(v, str)})
            if "mode" in parsed:
                colors["mode"] = parsed["mode"]
    except (OSError, ValueError, ImportError):
        pass
    return Theme(name=name, colors=colors)


def css(t: Theme) -> str:
    """GTK CSS built from the theme palette and the desktop's UI font.

    Sizing is deliberately tight. This is an overlay dropped from the bar, not
    a settings window: it should read like a camera's on-screen display, so
    type is small, padding is minimal, and the preview is the largest element.
    """
    family, size = system_font()
    small = size - 2.0

    bg = t.color("background")
    bg_dark = t.color("dark_background", bg)
    bg_light = t.color("lighter_background")
    fg = t.color("foreground")
    fg_dim = t.color("dark_foreground")
    fg_bright = t.color("bright_foreground", fg)
    accent = t.color("accent")
    sel = t.color("selection")
    muted = t.color("muted")
    red = t.color("red")
    green = t.color("green")

    return f"""
    window.decomposer {{
        background: transparent;
        font-family: "{family}", sans-serif;
        font-size: {small:.1f}pt;
    }}
    .dc-root {{
        background: {bg_dark};
        border: 1px solid {sel};
        border-radius: 12px;
        color: {fg};
    }}

    .dc-header {{ padding: 7px 10px 5px 10px; }}
    .dc-title {{ font-weight: 700; color: {fg_bright}; font-size: {small:.1f}pt; }}
    .dc-sub {{ color: {muted}; font-size: {small - 1.5:.1f}pt; }}
    .dc-section {{
        color: {muted}; font-size: {small - 2.0:.1f}pt; font-weight: 700;
        letter-spacing: 0.9px; padding: 0 2px;
    }}
    .dc-hint {{ color: {muted}; font-size: {small - 1.5:.1f}pt; }}
    .dc-value {{ color: {fg_dim}; font-size: {small - 1.0:.1f}pt; }}
    .dc-label {{ color: {fg_dim}; font-size: {small - 0.5:.1f}pt; }}

    .dc-preview {{
        background: #000; border-radius: 7px; border: 1px solid {sel};
    }}
    .dc-sep {{ background: {sel}; min-height: 1px; }}

    button.dc-chip {{
        background: {bg_light}; color: {fg_dim};
        border: 1px solid transparent; border-radius: 6px;
        padding: 2px 8px; margin: 0;
        font-size: {small - 1.0:.1f}pt; font-weight: 600;
        min-height: 0; min-width: 0;
    }}
    button.dc-chip:hover {{ background: {sel}; color: {fg_bright}; }}
    button.dc-chip.selected {{
        background: {accent}; color: {bg_dark}; border-color: {accent};
    }}
    button.dc-chip:disabled {{ color: {muted}; background: transparent; }}

    button.dc-tiny {{
        background: transparent; color: {muted};
        border: 1px solid {sel}; border-radius: 5px;
        padding: 0 6px; margin: 0; min-height: 0; min-width: 0;
        font-size: {small - 2.0:.1f}pt; font-weight: 600;
    }}
    button.dc-tiny:hover {{ color: {fg_bright}; border-color: {accent}; }}
    button.dc-tiny.selected {{ background: {accent}; color: {bg_dark}; border-color: {accent}; }}
    button.dc-tiny:disabled {{ color: {muted}; opacity: 0.4; }}

    .dc-pill {{
        border-radius: 5px; padding: 1px 7px; font-weight: 700;
        font-size: {small - 2.0:.1f}pt;
    }}
    .dc-pill.call {{ background: {green}; color: {bg_dark}; }}
    .dc-pill.studio {{ background: {accent}; color: {bg_dark}; }}
    .dc-pill.off {{ background: {muted}; color: {bg_dark}; }}

    .dc-warn {{ color: {red}; font-size: {small - 1.5:.1f}pt; }}

    /* Compact sliders: no drawn value, the number lives in its own label. */
    scale {{ min-height: 16px; padding: 0; margin: 0; }}
    scale trough {{
        background: {bg_light}; border-radius: 999px;
        min-height: 3px; margin: 0;
    }}
    scale highlight {{ background: {accent}; border-radius: 999px; }}
    scale slider {{
        background: {fg}; border-radius: 999px;
        min-width: 11px; min-height: 11px; margin: -5px;
    }}
    scale:disabled slider {{ background: {muted}; }}
    scale:disabled highlight {{ background: {muted}; }}
    """
