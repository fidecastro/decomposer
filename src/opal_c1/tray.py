"""StatusNotifierItem, so decomposer gets a button in the Omarchy bar.

The bar is Quickshell and runs a StatusNotifierWatcher, and its widget ids are
all built in — there is no "custom widget" slot to fill. A tray item is the
supported way in, and it needs no new dependencies: Gio speaks D-Bus.

The icon is sent as a pixmap rather than an icon name so the mark travels with
the process and does not depend on an icon theme being installed or refreshed.
"""

from __future__ import annotations

import os
import threading
from typing import Callable, Optional

from gi.repository import Gio, GLib

from opal_c1 import logo

def _icon_dir() -> "os.PathLike":
    from pathlib import Path

    return Path.home() / ".local/share/icons/hicolor/scalable/apps"


def _icon_installed() -> bool:
    from pathlib import Path

    return (Path(_icon_dir()) / "decomposer.svg").is_file()


WATCHER_NAME = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
ITEM_PATH = "/StatusNotifierItem"

INTERFACE_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="Activate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="SecondaryActivate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="ContextMenu">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="Scroll">
      <arg name="delta" type="i" direction="in"/>
      <arg name="orientation" type="s" direction="in"/>
    </method>
    <signal name="NewIcon"/>
    <signal name="NewStatus"><arg name="status" type="s"/></signal>
    <signal name="NewToolTip"/>
  </interface>
</node>
"""


class Tray:
    """One tray item. `on_activate` runs when the bar button is clicked."""

    def __init__(
        self,
        on_activate: Callable[[], None],
        on_secondary: Optional[Callable[[], None]] = None,
        tooltip: str = "decomposer",
    ):
        self.on_activate = on_activate
        self.on_secondary = on_secondary or on_activate
        self.tooltip = tooltip
        self.bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self._conn: Optional[Gio.DBusConnection] = None
        self._reg_id = 0
        self._pixmap = logo.argb_pixmap(22)
        self._owner_id = Gio.bus_own_name(
            Gio.BusType.SESSION,
            self.bus_name,
            Gio.BusNameOwnerFlags.NONE,
            self._on_bus_acquired,
            self._on_name_acquired,
            self._on_name_lost,
        )

    # -- exported object -------------------------------------------------

    def _on_bus_acquired(self, conn: Gio.DBusConnection, _name: str) -> None:
        self._conn = conn
        node = Gio.DBusNodeInfo.new_for_xml(INTERFACE_XML)
        self._reg_id = conn.register_object(
            ITEM_PATH,
            node.interfaces[0],
            self._on_method,
            self._on_get_property,
            None,
        )

    def _on_name_acquired(self, conn: Gio.DBusConnection, name: str) -> None:
        # The watcher wants the well-known name we just took.
        conn.call(
            WATCHER_NAME,
            WATCHER_PATH,
            WATCHER_NAME,
            "RegisterStatusNotifierItem",
            GLib.Variant("(s)", (name,)),
            None,
            Gio.DBusCallFlags.NONE,
            5000,
            None,
            self._on_registered,
        )

    def _on_registered(self, conn, result) -> None:
        try:
            conn.call_finish(result)
        except GLib.Error as e:
            # No watcher (bar not running, or a desktop without a tray) is not
            # fatal: everything else still works, there is just no button.
            print(f"tray: no StatusNotifierWatcher ({e.message})")

    def _on_name_lost(self, _conn, _name) -> None:
        pass

    def _on_method(
        self, _conn, _sender, _path, _iface, method, params, invocation
    ) -> None:
        if method == "Activate":
            self.on_activate()
        elif method in ("SecondaryActivate", "ContextMenu"):
            self.on_secondary()
        elif method == "Scroll":
            pass
        invocation.return_value(None)

    def _on_get_property(self, _conn, _sender, _path, _iface, prop):
        w, h, data = self._pixmap
        if prop == "Category":
            return GLib.Variant("s", "Hardware")
        if prop == "Id":
            return GLib.Variant("s", "decomposer")
        if prop == "Title":
            return GLib.Variant("s", "decomposer")
        if prop == "Status":
            return GLib.Variant("s", "Active")
        if prop == "IconName":
            # Some hosts render only named icons and ignore IconPixmap, so
            # advertise the installed theme icon when it is actually there and
            # leave the pixmap as the fallback for hosts that prefer it.
            return GLib.Variant("s", "decomposer" if _icon_installed() else "")
        if prop in ("AttentionIconName", "OverlayIconName"):
            return GLib.Variant("s", "")
        if prop == "IconThemePath":
            return GLib.Variant("s", str(_icon_dir()))
        if prop == "IconPixmap":
            return GLib.Variant("a(iiay)", [(w, h, data)])
        if prop == "ToolTip":
            return GLib.Variant(
                "(sa(iiay)ss)", ("", [(w, h, data)], "decomposer", self.tooltip)
            )
        if prop == "ItemIsMenu":
            return GLib.Variant("b", False)
        if prop == "Menu":
            return GLib.Variant("o", "/NO_DBUSMENU")
        return None

    def set_tooltip(self, text: str) -> None:
        self.tooltip = text
        if self._conn is not None:
            with_suppress = (GLib.Error, TypeError)
            try:
                self._conn.emit_signal(
                    None, ITEM_PATH, "org.kde.StatusNotifierItem", "NewToolTip", None
                )
            except with_suppress:
                pass


def run_in_thread(on_activate: Callable[[], None]) -> threading.Thread:
    """Host the tray on its own GLib main loop.

    The daemon's own loop is a blocking accept() on a Unix socket, and D-Bus
    needs a GLib context, so the tray gets its own thread and context.
    """

    def run() -> None:
        context = GLib.MainContext.new()
        context.push_thread_default()
        loop = GLib.MainLoop.new(context, False)
        Tray(on_activate)  # kept alive by the loop
        loop.run()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread
