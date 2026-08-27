"""USB hotplug events: a replug is noticed in seconds, not on the next poll.

Listens on the kernel uevent netlink socket for add events carrying the C1's
vendor id and fires a callback, debounced — flaky jacks can bounce a
connection several times in a row, and each bounce must not trigger its own
recovery attempt. If the socket is unavailable the watcher simply does not
run; the polling paths still exist and behave exactly as before.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from typing import Callable, Optional

NETLINK_KOBJECT_UEVENT = 15


def watch(
    vid_hex: str,
    callback: Callable[[], None],
    debounce: float = 3.0,
    stop_event: Optional[threading.Event] = None,
) -> threading.Thread:
    """Start the watcher thread. Fires callback on debounced add events."""
    # uevent PRODUCT lines drop leading zeros: "PRODUCT=3e7/f63d/410".
    needle = f"PRODUCT={vid_hex.lstrip('0')}/".encode()

    def run() -> None:
        try:
            sock = socket.socket(
                socket.AF_NETLINK, socket.SOCK_DGRAM, NETLINK_KOBJECT_UEVENT
            )
            # Group 1: raw kernel uevents, plain text, no libudev framing.
            sock.bind((os.getpid(), 1))
        except OSError as e:
            print(f"usb hotplug watch unavailable ({e}); polling only")
            return
        sock.settimeout(1.0)
        last = 0.0
        while stop_event is None or not stop_event.is_set():
            try:
                data = sock.recv(8192)
            except socket.timeout:
                continue
            except OSError:
                return
            if b"ACTION=add" not in data or needle not in data:
                continue
            now = time.monotonic()
            if now - last < debounce:
                continue
            last = now
            try:
                callback()
            except Exception:
                pass

    thread = threading.Thread(target=run, daemon=True, name="usb-hotplug")
    thread.start()
    return thread
