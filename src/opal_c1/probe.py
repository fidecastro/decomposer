"""Map an Opal C1's UVC Extension Unit: what controls exist and what they accept.

The C1 advertises 80 vendor controls behind a placeholder GUID with no public
documentation, so the only way to learn the layout is to ask the device.

The default path issues read-only GET requests. `run_write_sweep` is the
explicit SET_CUR diagnostic: it restores every selector it touches, and it
exists so a human can watch the live preview for focus / white-balance motion.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

from opal_c1.usbdesc import ExtensionUnit, find_extension_units
from opal_c1.uvcxu import (
    ERRNO_HINTS,
    ControlProbe,
    get_cur,
    probe_unit,
    set_cur,
)


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


@dataclass
class WriteStep:
    selector: int
    written: bytes
    readback: bytes | None = None
    error: int | None = None
    restored: bool = False
    restore_error: int | None = None

    def to_dict(self) -> dict:
        return {
            "selector": self.selector,
            "written": self.written.hex(),
            "readback": None if self.readback is None else self.readback.hex(),
            "error": self.error,
            "restored": self.restored,
            "restore_error": self.restore_error,
        }


@dataclass
class WriteSweepReport:
    device: dict
    unit_id: int
    guid: str
    probes: list[ControlProbe]
    steps: list[WriteStep] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "device": self.device,
            "unit": {"id": self.unit_id, "guid": self.guid},
            "probes": [p.to_dict() for p in self.probes],
            "steps": [s.to_dict() for s in self.steps],
            "skipped": self.skipped,
        }


def _resolve_unit(dev_path: str, unit: int | None) -> tuple[dict, ExtensionUnit]:
    usb, xus = find_extension_units(dev_path)
    if not xus:
        raise SystemExit(f"{dev_path}: no UVC extension units in the descriptors")

    xu: ExtensionUnit | None = (
        next((u for u in xus if u.unit_id == unit), None) if unit is not None else xus[0]
    )
    if xu is None:
        ids = ", ".join(str(u.unit_id) for u in xus)
        raise SystemExit(f"no extension unit with id {unit} (found: {ids})")
    return usb.describe(), xu


def run(dev_path: str, unit: int | None, selectors: range, quiet: bool = False):
    device, xu = _resolve_unit(dev_path, unit)

    def progress(p: ControlProbe) -> None:
        if not quiet:
            mark = "." if not p.supported else "#"
            print(mark, end="", flush=True)

    probes = probe_unit(dev_path, xu.unit_id, selectors, on_progress=progress)
    if not quiet:
        print()
    return ProbeReport(
        device=device,
        unit_id=xu.unit_id,
        guid=xu.guid,
        num_controls=xu.num_controls,
        probes=probes,
    )


def candidate_values(probe: ControlProbe, extra: list[int] | None = None) -> list[bytes]:
    """Build a short SET_CUR sequence for one selector, always length-correct.

    Prefers the device's own min/def/max when they fit, then a mid-point snapped
    to `res`, then any caller extras. Duplicates and out-of-range values drop.
    """
    if not probe.supported or probe.length is None:
        return []
    length = probe.length
    lo = probe.as_int("min")
    hi = probe.as_int("max")
    nxt = probe.as_int("res") or 1
    if nxt <= 0:
        nxt = 1
    default = probe.as_int("def")
    current = probe.as_int("cur")

    ints: list[int] = []
    for v in (lo, default, hi):
        if v is not None:
            ints.append(v)
    if lo is not None and hi is not None and hi >= lo:
        mid = lo + ((hi - lo) // 2)
        mid = lo + ((mid - lo) // nxt) * nxt
        ints.append(mid)
        # A couple of spaced samples so a human can see motion if any.
        span = hi - lo
        if span >= 4 * nxt:
            ints.append(lo + ((span // 4) // nxt) * nxt)
            ints.append(lo + ((3 * span // 4) // nxt) * nxt)
    if extra:
        ints.extend(extra)

    out: list[bytes] = []
    seen: set[bytes] = set()
    for value in ints:
        if lo is not None and value < lo:
            continue
        if hi is not None and value > hi:
            continue
        # Skip the resting value — watching "set to what it already is" wastes dwell.
        if current is not None and value == current:
            continue
        try:
            raw = int(value).to_bytes(length, "little")
        except OverflowError:
            continue
        if raw in seen:
            continue
        seen.add(raw)
        out.append(raw)
    return out


def run_write_sweep(
    dev_path: str,
    unit: int | None,
    selectors: range,
    *,
    values: list[int] | None = None,
    dwell: float = 1.5,
    prompt: bool = False,
    dry_run: bool = False,
    on_step=None,
) -> WriteSweepReport:
    """SET_CUR each writable selector, verify readback, always restore CUR.

    Intended for Call mode with a live preview open: the operator watches for
    lens or white-balance motion while this walks selectors 1-8. A power-cycle
    is the recovery path if the firmware wedges.
    """
    device, xu = _resolve_unit(dev_path, unit)
    probes = probe_unit(dev_path, xu.unit_id, selectors)
    report = WriteSweepReport(
        device=device, unit_id=xu.unit_id, guid=xu.guid, probes=probes
    )

    targets = [p for p in probes if p.writable]
    report.skipped = [p.selector for p in probes if p.supported and not p.writable]

    fd = None if dry_run else os.open(dev_path, os.O_RDWR)
    try:
        for probe in targets:
            assert probe.length is not None
            original = probe.values.get("cur")
            if original is None:
                report.skipped.append(probe.selector)
                continue
            planned = candidate_values(probe, extra=values)
            if not planned:
                report.skipped.append(probe.selector)
                continue

            for raw in planned:
                step = WriteStep(selector=probe.selector, written=raw)
                if dry_run:
                    report.steps.append(step)
                    if on_step:
                        on_step(probe, step, original, restoring=False)
                    continue
                assert fd is not None
                try:
                    set_cur(fd, xu.unit_id, probe.selector, raw)
                    try:
                        step.readback = get_cur(
                            fd, xu.unit_id, probe.selector, probe.length
                        )
                    except OSError as e:
                        step.error = e.errno
                except OSError as e:
                    step.error = e.errno
                report.steps.append(step)
                if on_step:
                    on_step(probe, step, original, restoring=False)
                if dwell > 0 and not prompt:
                    time.sleep(dwell)
                if prompt:
                    try:
                        input("  [Enter] next value  ")
                    except EOFError:
                        pass

            if dry_run:
                continue
            assert fd is not None
            # Restore even when individual sets failed — best-effort return home.
            restore = WriteStep(selector=probe.selector, written=original)
            try:
                set_cur(fd, xu.unit_id, probe.selector, original)
                try:
                    restore.readback = get_cur(
                        fd, xu.unit_id, probe.selector, probe.length
                    )
                    restore.restored = restore.readback == original
                except OSError as e:
                    restore.restore_error = e.errno
            except OSError as e:
                restore.restore_error = e.errno
            if report.steps and report.steps[-1].selector == probe.selector:
                report.steps[-1].restored = restore.restored
                report.steps[-1].restore_error = restore.restore_error
            if on_step:
                on_step(probe, restore, original, restoring=True)
    finally:
        if fd is not None:
            os.close(fd)
    return report


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


def _fmt_bytes(raw: bytes | None) -> str:
    if raw is None:
        return "-"
    if len(raw) <= 8:
        return str(int.from_bytes(raw, "little"))
    return raw.hex()


def format_write_report(r: WriteSweepReport) -> str:
    lines: list[str] = []
    d = r.device
    lines.append(
        f"{d.get('manufacturer')} {d.get('product')}  "
        f"{d.get('idVendor')}:{d.get('idProduct')}  "
        f"fw {d.get('bcdDevice')}"
    )
    lines.append(f"Extension Unit {r.unit_id}  write sweep  {len(r.steps)} SET_CUR steps")
    lines.append("")
    if not r.steps:
        lines.append("No writable selectors were exercised.")
    else:
        hdr = f"{'sel':>4}  {'wrote':>8}  {'read':>8}  result"
        lines.append(hdr)
        lines.append("-" * len(hdr))
        for s in r.steps:
            if s.error is not None:
                result = f"errno {s.error} ({ERRNO_HINTS.get(s.error, 'unknown')})"
            elif s.readback is None:
                result = "dry-run"
            elif s.readback == s.written:
                result = "ok"
            else:
                result = "readback mismatch"
            lines.append(
                f"{s.selector:>4}  {_fmt_bytes(s.written):>8}  "
                f"{_fmt_bytes(s.readback):>8}  {result}"
            )
            if s.restore_error is not None:
                lines.append(
                    f"      restore failed: errno {s.restore_error} "
                    f"({ERRNO_HINTS.get(s.restore_error, 'unknown')})"
                )
            elif s.restored:
                lines.append("      restored")
    if r.skipped:
        lines.append("")
        lines.append(f"skipped selectors: {_ranges(sorted(set(r.skipped)))}")
    return "\n".join(lines)


def write_json(r: ProbeReport | WriteSweepReport, path: str) -> None:
    with open(path, "w") as f:
        json.dump(r.to_dict(), f, indent=2)
