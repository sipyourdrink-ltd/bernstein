"""Every marker a conftest applies by name must be registered in pyproject.

``addopts`` carries ``--strict-markers``, so ``item.add_marker("name")`` with a
name absent from the ``markers`` list is not a warning -- pytest raises during
collection and the whole run ends in INTERNALERROR with exit code 3, before a
single test executes.

That is a worse failure than it looks, because of where it can hide. A
conftest under a directory no routine lane collects will never raise it: the
default runner walks ``tests/unit`` and the affected-test gate walks
``tests/unit`` and ``tests/integration`` (``scripts/test_impact.TEST_DIRS``),
so a suite outside both is exercised only by whatever workflow names it
directly. ``tests/protocol/`` is one such suite -- its only caller is the
publish workflow's release gate, which runs once per release, after the tag
exists and before anything is published. An unregistered marker there is a
release-time outage discovered at release time.

This test collects the marker names out of every conftest in the tree by
reading the source, so it needs none of those suites to be collected to fail.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _registered_markers() -> set[str]:
    """The marker names declared in ``[tool.pytest.ini_options] markers``."""
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    entries = config["tool"]["pytest"]["ini_options"]["markers"]
    # Each entry is "name: description"; the name is everything before the colon.
    return {entry.split(":", 1)[0].strip() for entry in entries}


def _markers_applied_by_name(source: str) -> set[str]:
    """Literal names passed to ``add_marker`` anywhere in *source*.

    Only string literals are collected. A computed name cannot be checked
    statically, and pretending otherwise would make this test lie rather than
    fail.
    """
    applied: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "add_marker":
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                applied.add(arg.value)
    return applied


def _conftests() -> list[Path]:
    return sorted(p for p in (REPO_ROOT / "tests").rglob("conftest.py") if p.is_file())


def test_every_conftest_applied_marker_is_registered_in_pyproject() -> None:
    registered = _registered_markers()
    offenders: list[str] = []

    for conftest in _conftests():
        for name in sorted(_markers_applied_by_name(conftest.read_text(encoding="utf-8"))):
            if name not in registered:
                offenders.append(f"{conftest.relative_to(REPO_ROOT)} applies {name!r}")

    assert not offenders, (
        "these conftests apply a marker that `[tool.pytest.ini_options] markers` "
        f"does not register: {offenders}. With `--strict-markers` this is not a "
        "warning -- pytest raises during collection and the run ends in "
        "INTERNALERROR before any test executes. Register the name in "
        "pyproject.toml rather than dropping --strict-markers; the strictness is "
        "what turns a typo'd marker into a failure instead of a silent no-op."
    )


def test_the_conftest_scan_actually_finds_something() -> None:
    """A scan that silently matched nothing would pass the test above forever."""
    conftests = _conftests()
    assert conftests, "no conftest.py found under tests/ -- the scan is looking in the wrong place"

    applied = set()
    for conftest in conftests:
        applied |= _markers_applied_by_name(conftest.read_text(encoding="utf-8"))

    assert applied, (
        "no conftest applies a marker by name, so the registration check above "
        "cannot fail on anything. If every conftest genuinely stopped calling "
        "add_marker with a literal, delete both tests rather than keeping a "
        "check that guards nothing."
    )


def test_a_marker_name_is_read_out_of_add_marker_source() -> None:
    """Pin the extractor itself: it reads the argument, not the method name."""
    source = "def f(item):\n    item.add_marker('protocol')\n    item.add_marker(computed)\n"
    assert _markers_applied_by_name(source) == {"protocol"}
