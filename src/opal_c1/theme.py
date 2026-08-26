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
    """GTK CSS built from the theme palette."""
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
    window.decomposer {{ background: {bg}; color: {fg}; }}
    .dc-header {{ background: {bg_dark}; color: {fg_bright};
                  border-bottom: 1px solid {sel}; }}
    .dc-title {{ font-weight: 700; letter-spacing: 0.5px; color: {fg_bright}; }}
    .dc-section {{ color: {fg_dim}; font-size: 0.82rem; font-weight: 700;
                   letter-spacing: 1.2px; text-transform: uppercase; }}
    .dc-card {{ background: {bg_dark}; border: 1px solid {sel};
                border-radius: 10px; padding: 14px; }}
    .dc-hint {{ color: {muted}; font-size: 0.85rem; }}
    .dc-value {{ color: {fg_dim}; font-size: 0.85rem; }}

    button.dc-chip {{
        background: {bg_light}; color: {fg}; border: 1px solid {sel};
        border-radius: 999px; padding: 6px 14px; font-weight: 600;
    }}
    button.dc-chip:hover {{ background: {sel}; color: {fg_bright}; }}
    button.dc-chip.selected {{
        background: {accent}; color: {bg_dark}; border-color: {accent};
    }}
    button.dc-chip:disabled {{ color: {muted}; background: {bg_dark}; }}

    .dc-pill {{ border-radius: 999px; padding: 3px 12px; font-weight: 700;
                font-size: 0.82rem; }}
    .dc-pill.call {{ background: {green}; color: {bg_dark}; }}
    .dc-pill.studio {{ background: {accent}; color: {bg_dark}; }}
    .dc-pill.off {{ background: {muted}; color: {bg_dark}; }}

    .dc-warn {{ color: {red}; font-size: 0.85rem; }}
    .dc-ok {{ color: {green}; font-size: 0.85rem; }}

    /* No explicit slider min-width/height: GTK derives the handle size from
       the trough, and forcing both makes it compute a negative and warn. */
    scale {{ min-height: 26px; }}
    scale trough {{ background: {bg_light}; border-radius: 999px; min-height: 6px; }}
    scale highlight {{ background: {accent}; border-radius: 999px; }}
    scale slider {{ background: {fg}; border-radius: 999px; }}
    """
