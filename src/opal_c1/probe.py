"""Map an Opal C1's UVC Extension Unit: what controls exist and what they accept.

The C1 advertises 80 vendor controls behind a placeholder GUID with no public
documentation, so the only way to learn the layout is to ask the device. This
module issues read-only GET requests and reports what came back.

Nothing here writes to the camera.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from opal_c1.usbdesc import ExtensionUnit, UsbDevice, find_extension_units
from opal_c1.uvcxu import ERRNO_HINTS, ControlProbe, probe_unit


@dataclass
class ProbeReport:
    device: dict
    unit_id: int
    guid: str
    num_controls: int
    probes: list[ControlProbe]

    @property
    def live(self) -> list[ControlProbe]:
        return [p for p in self.probes if p.supported]

    def to_dict(self) -> dict:
        return {
            "device": self.device,
            "unit": {
                "id": self.unit_id,
                "guid": self.guid,
                "num_controls": self.num_controls,
            },
            "probes": [p.to_dict() for p in self.probes],
        }


def run(dev_path: str, unit: int | None, selectors: range, quiet: bool = False):
    usb, xus = find_extension_units(dev_path)
    if not xus:
        raise SystemExit(f"{dev_path}: no UVC extension units in the descriptors")

    xu: ExtensionUnit = (
        next((u for u in xus if u.unit_id == unit), None) if unit is not None else xus[0]
    )
    if xu is None:
        ids = ", ".join(str(u.unit_id) for u in xus)
        raise SystemExit(f"no extension unit with id {unit} (found: {ids})")

    def progress(p: ControlProbe) -> None:
        if not quiet:
            mark = "." if not p.supported else "#"
            print(mark, end="", flush=True)

    probes = probe_unit(dev_path, xu.unit_id, selectors, on_progress=progress)
    if not quiet:
        print()
    return ProbeReport(
        device=usb.describe(),
        unit_id=xu.unit_id,
        guid=xu.guid,
        num_controls=xu.num_controls,
        probes=probes,
    )


def _ranges(nums: list[int]) -> str:
    """Collapse [1,2,3,7,8] into '1-3,7-8' for compact reporting."""
    if not nums:
        return ""
    out, start, prev = [], nums[0], nums[0]
    for n in nums[1:] + [None]:
        if n != prev + 1:
            out.append(str(start) if start == prev else f"{start}-{prev}")
            start = n
        prev = n
    return ",".join(out)


def _fmt_value(p: ControlProbe, which: str) -> str:
    raw = p.values.get(which)
    if raw is None:
        err = p.errors.get(which)
        return f"!{err}" if err else "-"
    if len(raw) <= 8:
        return str(int.from_bytes(raw, "little"))
    return raw.hex()


def format_report(r: ProbeReport) -> str:
    lines: list[str] = []
    d = r.device
    lines.append(
        f"{d.get('manufacturer')} {d.get('product')}  "
        f"{d.get('idVendor')}:{d.get('idProduct')}  "
        f"fw {d.get('bcdDevice')}  serial {d.get('serial')}"
    )
    lines.append(f"Extension Unit {r.unit_id}  guid {r.guid}  advertises {r.num_controls} controls")
    lines.append("")

    live = r.live
    if not live:
        lines.append("No selector answered GET_LEN.")
        errs = {}
        for p in r.probes:
            e = p.errors.get("len")
            if e:
                errs[e] = errs.get(e, 0) + 1
        for e, n in sorted(errs.items()):
            lines.append(f"  {n:3d} x errno {e}: {ERRNO_HINTS.get(e, 'unknown')}")
        return "\n".join(lines)

    lines.append(f"{len(live)} of {len(r.probes)} selectors answered:")
    lines.append("")
    hdr = f"{'sel':>4}  {'len':>3}  {'cur':>12}  {'min':>12}  {'max':>12}  {'res':>10}  {'def':>12}  caps"
    lines.append(hdr)
    lines.append("-" * len(hdr))
    for p in live:
        lines.append(
            f"{p.selector:>4}  {p.length:>3}  "
            f"{_fmt_value(p,'cur'):>12}  {_fmt_value(p,'min'):>12}  "
            f"{_fmt_value(p,'max'):>12}  {_fmt_value(p,'res'):>10}  "
            f"{_fmt_value(p,'def'):>12}  {','.join(p.caps)}"
        )

    # Distinguish "the device answered GET_LEN with 0" (declared but not
    # implemented) from "the request never completed" (stalled / rejected).
    empty = [p for p in r.probes if p.length == 0]
    failed = [p for p in r.probes if p.length is None]
    if empty:
        lines.append("")
        lines.append(
            f"{len(empty)} selectors answered GET_LEN with length 0 "
            f"(declared in bmControls, not implemented): "
            f"{_ranges([p.selector for p in empty])}"
        )
    if failed:
        counts: dict[int, int] = {}
        for p in failed:
            e = p.errors.get("len", -1)
            counts[e] = counts.get(e, 0) + 1
        for e, n in sorted(counts.items()):
            lines.append(
                f"{n} selectors failed: errno {e} — {ERRNO_HINTS.get(e, 'unknown')} "
                f"({_ranges([p.selector for p in failed if p.errors.get('len') == e])})"
            )
    return "\n".join(lines)


def write_json(r: ProbeReport, path: str) -> None:
    with open(path, "w") as f:
        json.dump(r.to_dict(), f, indent=2)
