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
    # Several themes use dark_foreground/muted for non-text decoration.  On a
    # dark panel those values can be below readable text contrast (Nord's
    # muted colour is a concrete example), so secondary *text* starts from the
    # theme's light foreground instead.
    fg_dim = t.color("light_foreground", t.color("dark_foreground", fg))
    fg_bright = t.color("bright_foreground", fg)
    accent = t.color("accent")
    sel = t.color("selection")
    muted = t.color("light_foreground", fg_dim)
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

    .dc-header {{ padding: 6px 10px; }}
    .dc-drag {{
        color: {fg_dim}; padding: 4px 2px 0 2px; min-width: 12px;
        font-size: {small + 1.0:.1f}pt;
    }}
    .dc-drag:hover {{ color: {accent}; }}
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
        border: 1px solid {sel}; border-radius: 6px;
        padding: 0 6px; margin: 0; min-height: 0; min-width: 0;
        font-size: {small - 1.0:.1f}pt; font-weight: 600;
    }}
    button.dc-tiny:hover {{ color: {fg_bright}; border-color: {accent}; }}
    button.dc-tiny.selected {{ background: {accent}; color: {bg_dark}; border-color: {accent}; }}
    button.dc-tiny:disabled {{ color: {muted}; opacity: 0.4; }}

    /* Popovers sit over a dimmed parent, so their own text and actions need a
       fully opaque contrast treatment.  This class is shared by confirmations,
       the model chooser, and preset naming. */
    popover.dc-popover > contents {{
        background: {bg_light}; color: {fg_bright};
        border: 1px solid {accent}; border-radius: 9px;
    }}
    popover.dc-popover label,
    popover.dc-popover .dc-hint,
    popover.dc-popover .dc-label {{ color: {fg_bright}; opacity: 1; }}
    popover.dc-popover button.dc-chip {{
        background: {sel}; color: {fg_bright};
        border-color: alpha({accent}, 0.65); opacity: 1;
    }}
    popover.dc-popover button.dc-chip:hover {{
        background: {accent}; color: {bg_dark};
    }}

    .dc-warn {{ color: {red}; font-size: {small - 1.5:.1f}pt; }}

    /* Click and type affordances: the discrete mouseover. */
    .dc-clickable {{
        border-bottom: 1px dotted alpha({muted}, 0.8);
    }}
    .dc-clickable:hover {{ color: {accent}; }}

    /* Capture: countdown numeral, REC badge, and the shutter flash. */
    button.dc-cap {{
        font-size: {small + 2.0:.1f}pt;
        padding: 0px 10px;
        min-height: 26px;
        border-radius: 6px;
        color: {fg_dim};
        background: alpha({fg_dim}, 0.08);
        border: 1px solid alpha({muted}, 0.4);
    }}
    button.dc-cap:hover {{ color: {red}; border-color: alpha({red}, 0.6); }}

    switch {{
        min-height: 22px; min-width: 40px; padding: 0;
        border-radius: 999px;
    }}
    switch slider {{
        background: {fg_bright}; border: 0; border-radius: 999px;
        min-height: 18px; min-width: 18px; margin: 2px; padding: 0;
        box-shadow: none;
    }}
    switch:checked {{ background: {green}; }}

    .dc-count {{
        color: {fg_bright}; font-weight: 800; font-size: 34pt;
        text-shadow: 0 0 8px alpha(black, 0.8);
    }}
    .dc-rec {{
        color: {red}; font-weight: 800; font-size: {small:.1f}pt;
        text-shadow: 0 0 6px alpha(black, 0.8);
    }}
    .dc-flash {{ background: white; }}
    entry.dc-entry:hover {{
        border-color: alpha({accent}, 0.55);
    }}

    .dc-vsep {{
        min-width: 1px;
        background: alpha({muted}, 0.25);
        margin-top: 4px; margin-bottom: 10px;
    }}

    .dc-status {{
        border-radius: 6px; padding: 0 7px; font-weight: 700;
        font-size: {small - 2.0:.1f}pt;
        border: 1px solid {muted}; min-height: 20px;
    }}
    .dc-status.live {{ color: {green}; border-color: {green}; }}
    .dc-status.dead {{ color: {muted}; opacity: 0.7; }}

    /* Value boxes: a quiet box says "type here" without shouting. */
    entry.dc-entry {{
        background: alpha({fg_dim}, 0.07);
        border: 1px solid alpha({muted}, 0.35);
        border-radius: 4px;
        box-shadow: none;
        color: {fg_dim}; font-size: {small - 1.0:.1f}pt;
        padding: 0px 4px; margin: 0; min-height: 0;
        caret-color: {accent};
    }}
    entry.dc-entry:focus {{
        color: {fg_bright};
        border-color: {accent};
    }}

    /* Compact sliders: no drawn value, the number lives in its own entry. */
    scale {{ min-height: 22px; padding: 0; margin: 0; }}
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

    /* One vertical rhythm throughout the panel.  Styles still communicate
       hierarchy, but button, dropdown, entry, and switch boxes align. */
    .dc-control-row {{ min-height: 24px; }}
    .dc-root button,
    .dc-root menubutton > button,
    .dc-root dropdown > button,
    .dc-root entry.dc-entry {{
        min-height: 22px; padding-top: 0; padding-bottom: 0;
    }}
    .dc-root switch {{ min-height: 22px; }}
    .dc-root button.dc-clear {{
        min-width: 22px; padding-left: 0; padding-right: 0;
    }}
    .dc-root button.dc-clear:disabled {{ opacity: 0.35; }}
    entry.dc-value-entry {{ min-width: 54px; }}
    """
