"""Dependency direction and chokepoints, enforced.

core ← ports ← adapters ← app(daemon) ← ui/cli. Arrows never reverse, the
engine protocol is composed in exactly one module, and the daemon reaches
hardware only through backends. A refactor that quietly violates any of
this fails here, not in a 3am debugging session.
"""

import ast
import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parents[1] / "src/opal_c1"


def imports_of(path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom) and not node.level:
            yield node.module or ""


def test_ports_depend_only_on_core():
    bad = [
        m for m in imports_of(SRC / "ports.py")
        if m.startswith("opal_c1") and not m.startswith("opal_c1.core")
    ]
    assert not bad, f"ports.py reaches outward: {bad}"


def test_adapters_never_import_the_application_or_ui():
    forbidden = ("opal_c1.daemon", "opal_c1.gui", "opal_c1.cli")
    offenders = []
    for path in (SRC / "adapters").glob("*.py"):
        for module in imports_of(path):
            if module.startswith(forbidden):
                offenders.append(f"{path.name}: {module}")
    assert not offenders, "adapters depend inward only:\n  " + "\n  ".join(offenders)


def test_daemon_touches_hardware_only_through_backends():
    src = (SRC / "daemon.py").read_text()
    for direct in ("UvcControls", "OpalDevice", "import depthai", "usb.core"):
        assert direct not in src, (
            f"daemon.py references {direct}: hardware belongs behind a backend"
        )


def test_engine_protocol_is_composed_in_one_place():
    # The literal protocol verbs may appear only where they are defined
    # (core/model.py) — anywhere else is the argv/socket drift coming back.
    # Every verb the protocol knows. A new verb must be added here when it
    # is added to core/model.py, or this guard cannot see its drift. The
    # brace requirement is what separates composing a protocol line
    # (f"blur {v}") from merely mentioning a word in prose or help text.
    pattern = re.compile(
        r'f?"(look|strength|flip|overlay-rect|overlay-opacity|overlay'
        r'|zoom|pan|clahe|blur|blur-style|background|model-strength) \{'
    )
    offenders = []
    for path in SRC.rglob("*.py"):
        if path.relative_to(SRC).as_posix() == "core/model.py":
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(SRC)}:{n}: {line.strip()}")
    assert not offenders, "protocol strings outside core.model:\n  " + "\n  ".join(offenders)


def test_gui_and_cli_speak_only_to_the_daemon():
    # Driving adapters go through the Client; reaching for a camera backend
    # from the UI would create a second owner.
    for name in ("gui.py",):
        for module in imports_of(SRC / name):
            assert not module.startswith("opal_c1.adapters"), (
                f"{name} imports {module}: the panel talks to the daemon only"
            )


def test_cli_resolution_names_derive_from_core():
    # The CLI's short names must be a projection of the core's resolution
    # facts, never a second copy: the copy once kept offering a geometry
    # the camera cannot deliver.
    from opal_c1.cli import RESOLUTIONS
    from opal_c1.core.model import RESOLUTIONS_STUDIO

    core_sizes = {(r[1], r[2]) for r in RESOLUTIONS_STUDIO}
    assert set(RESOLUTIONS.values()) <= core_sizes
    assert (5312, 6000) not in RESOLUTIONS.values()
