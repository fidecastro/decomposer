"""Find a V4L2 node's USB device and parse its UVC descriptors.

Used to discover Extension Units (unit ID, GUID, control count) without
hardcoding anything, so the probe works on any C1 — or any other UVC camera.
Stdlib only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# USB descriptor types
DT_INTERFACE = 0x04
DT_CS_INTERFACE = 0x24

# UVC VideoControl interface descriptor subtypes (UVC 1.5 A.5)
VC_HEADER = 0x01
VC_INPUT_TERMINAL = 0x02
VC_OUTPUT_TERMINAL = 0x03
VC_SELECTOR_UNIT = 0x04
VC_PROCESSING_UNIT = 0x05
VC_EXTENSION_UNIT = 0x06

USB_CLASS_VIDEO = 0x0E
SC_VIDEOCONTROL = 0x01


@dataclass
class ExtensionUnit:
    unit_id: int
    guid: str
    num_controls: int
    source_ids: list[int]
    bmcontrols: bytes

    @property
    def flagged_selectors(self) -> list[int]:
        """Selectors whose bit is set in bmControls (1-based, LSB of byte 0)."""
        out = []
        for byte_i, byte in enumerate(self.bmcontrols):
            for bit in range(8):
                if byte & (1 << bit):
                    out.append(byte_i * 8 + bit + 1)
        return out


@dataclass
class UsbDevice:
    sysfs: Path

    def attr(self, name: str) -> str | None:
        f = self.sysfs / name
        try:
            return f.read_text().strip()
        except OSError:
            return None

    @property
    def descriptors(self) -> bytes:
        return (self.sysfs / "descriptors").read_bytes()

    def describe(self) -> dict:
        return {
            k: self.attr(k)
            for k in (
                "idVendor", "idProduct", "product", "manufacturer",
                "serial", "bcdDevice", "speed", "bMaxPower",
            )
        }


def usb_device_for_video_node(dev_path: str) -> UsbDevice:
    """Walk /sys from /dev/videoN up to the owning USB device directory."""
    name = os.path.basename(dev_path)
    link = Path("/sys/class/video4linux") / name / "device"
    if not link.exists():
        raise FileNotFoundError(f"{dev_path}: no sysfs entry at {link}")
    node = link.resolve()
    # The video node hangs off a USB *interface*; its parent is the device.
    for candidate in (node, *node.parents):
        if (candidate / "descriptors").exists() and (candidate / "idVendor").exists():
            return UsbDevice(sysfs=candidate)
    raise FileNotFoundError(f"{dev_path}: could not locate parent USB device")


def _format_guid(g: bytes) -> str:
    return "{%08x-%04x-%04x-%s-%s}" % (
        int.from_bytes(g[0:4], "little"),
        int.from_bytes(g[4:6], "little"),
        int.from_bytes(g[6:8], "little"),
        g[8:10].hex(),
        g[10:16].hex(),
    )


def parse_extension_units(blob: bytes) -> list[ExtensionUnit]:
    """Extract VC_EXTENSION_UNIT descriptors from a raw descriptor blob."""
    units: list[ExtensionUnit] = []
    in_videocontrol = False
    i = 0
    while i < len(blob):
        length = blob[i]
        if length == 0 or i + length > len(blob):
            break
        d = blob[i : i + length]
        dtype = d[1]

        if dtype == DT_INTERFACE and length >= 9:
            in_videocontrol = d[5] == USB_CLASS_VIDEO and d[6] == SC_VIDEOCONTROL
        elif dtype == DT_CS_INTERFACE and in_videocontrol and d[2] == VC_EXTENSION_UNIT:
            # bLength bDescriptorType bDescriptorSubtype bUnitID guid[16]
            # bNumControls bNrInPins baSourceID[p] bControlSize bmControls[n] iExtension
            num_pins = d[21]
            ctrl_size_at = 22 + num_pins
            if ctrl_size_at < length:
                ctrl_size = d[ctrl_size_at]
                units.append(
                    ExtensionUnit(
                        unit_id=d[3],
                        guid=_format_guid(d[4:20]),
                        num_controls=d[20],
                        source_ids=list(d[22 : 22 + num_pins]),
                        bmcontrols=bytes(
                            d[ctrl_size_at + 1 : ctrl_size_at + 1 + ctrl_size]
                        ),
                    )
                )
        i += length
    return units


def find_extension_units(dev_path: str) -> tuple[UsbDevice, list[ExtensionUnit]]:
    dev = usb_device_for_video_node(dev_path)
    return dev, parse_extension_units(dev.descriptors)
