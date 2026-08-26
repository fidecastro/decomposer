"""The core stays pure, enforced.

Hexagonal only works if the hexagon has walls. A module in opal_c1.core that
imports IO — filesystem, sockets, subprocesses, hardware SDKs, clocks — has
quietly become an adapter, and everything that depends on the core being
testable without a camera breaks with it.
"""

import ast
import pathlib

CORE = pathlib.Path(__file__).resolve().parents[1] / "src/opal_c1/core"

ALLOWED_ROOTS = {
    "__future__",
    "dataclasses",
    "enum",
    "typing",
    "math",
    "collections",
    # The core may import itself.
    "opal_c1",
}
FORBIDDEN_OPAL = ("opal_c1.adapters", "opal_c1.daemon", "opal_c1.device",
                  "opal_c1.v4l2", "opal_c1.gui", "opal_c1.cli")


def iter_imports(path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import stays inside the package
                continue
            yield node.module or ""


def test_core_modules_import_no_io():
    assert CORE.is_dir(), f"missing {CORE}"
    offenders = []
    for path in sorted(CORE.glob("*.py")):
        for module in iter_imports(path):
            root = module.split(".")[0]
            if root not in ALLOWED_ROOTS:
                offenders.append(f"{path.name}: import {module}")
            if module.startswith(FORBIDDEN_OPAL):
                offenders.append(f"{path.name}: import {module} (outer layer)")
            if module.startswith("opal_c1") and not module.startswith("opal_c1.core"):
                offenders.append(f"{path.name}: import {module} (outside core)")
    assert not offenders, "core purity violated:\n  " + "\n  ".join(offenders)


def test_core_has_the_expected_modules():
    names = {p.stem for p in CORE.glob("*.py")}
    assert {"model", "transitions", "health", "presets"} <= names
