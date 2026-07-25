"""Guard: deprecated v1 lineage writers are unreachable from src/.

Issue #2292 AC4 established that v1 ``LineageRecorder`` and persistence
``LineageWriter`` must have zero construction sites in ``src/``. New
artifact-provenance writes go through
:class:`bernstein.core.lineage.spine.LineageSpine` at the single adapter write
boundary.

Issue #2960 closes the bypass that a constructor-only check leaves open. A
constructor check is satisfied by moving the deprecated body into a module-level
helper and calling that instead, so a signed write could still ride the
deprecated module without tripping anything. The guard therefore now enforces
four properties over the shipped source tree (not tests, not docstrings):

1. No construction of a deprecated v1 writer.
2. No *import* of a deprecated v1 writer at all - including under
   ``TYPE_CHECKING`` - so no ``src/`` entrypoint can be typed against one.
3. The signed-append substrate (``LineageStore.append(entry, jws=...)``) is
   reached only from the one supported module,
   :mod:`bernstein.core.lineage.signed_write`. Any other call site is a signed
   write that skipped the supported path.
4. The deprecated recorder module does not re-export the sealing primitive, so
   ``from ...lineage.recorder import seal_write`` cannot become the new bypass.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

_SRC = Path(__file__).resolve().parents[3] / "src" / "bernstein"

_FORBIDDEN_CTORS = {"LineageRecorder", "LineageWriter"}

#: Deprecated modules whose members must not be imported anywhere in ``src/``.
_DEPRECATED_RECORDER_MODULE = "bernstein.core.lineage.recorder"

#: The single module allowed to perform a signed append against ``LineageStore``.
_SIGNED_WRITE_MODULE = _SRC / "core" / "lineage" / "signed_write.py"

#: Sealing primitives that must only ever be imported from the supported module.
_SEALING_NAMES = {"seal_write", "SignedLineageLog"}


def _iter_src_modules() -> list[tuple[Path, ast.Module]]:
    out: list[tuple[Path, ast.Module]] = []
    for py in sorted(_SRC.rglob("*.py")):
        try:
            out.append((py, ast.parse(py.read_text(encoding="utf-8"))))
        except (SyntaxError, UnicodeDecodeError):
            continue
    return out


def _construction_sites() -> list[str]:
    hits: list[str] = []
    for py, tree in _iter_src_modules():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # Direct call: LineageWriter(...) / LineageRecorder(...)
            if isinstance(func, ast.Name) and func.id in _FORBIDDEN_CTORS:
                hits.append(f"{py}:{node.lineno}: {func.id}(...)")
            # Factory call: LineageWriter.for_run(...)
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id in _FORBIDDEN_CTORS
                and func.attr in {"for_run", "create"}
            ):
                hits.append(f"{py}:{node.lineno}: {func.value.id}.{func.attr}(...)")
    return hits


def _import_sites() -> list[str]:
    """Return every ``src/`` import of a deprecated v1 writer name."""
    hits: list[str] = []
    for py, tree in _iter_src_modules():
        if py.name == "recorder.py" and py.parent.name == "lineage":
            # The deprecated module is allowed to define its own class.
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in _FORBIDDEN_CTORS:
                        hits.append(f"{py}:{node.lineno}: from {node.module} import {alias.name}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname in _FORBIDDEN_CTORS:
                        hits.append(f"{py}:{node.lineno}: import {alias.name} as {alias.asname}")
    return hits


def _signed_append_sites() -> list[str]:
    """Return every ``.append(..., jws=...)`` call outside the supported module.

    ``LineageStore.append`` is the only way v1 signed bytes land on disk; it is
    identified by its ``jws`` keyword, which no other ``append`` in the tree
    takes.
    """
    hits: list[str] = []
    for py, tree in _iter_src_modules():
        if py == _SIGNED_WRITE_MODULE:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "append"):
                continue
            if any(kw.arg == "jws" for kw in node.keywords):
                hits.append(f"{py}:{node.lineno}: signed append outside signed_write.py")
    return hits


def _sealing_import_bypasses() -> list[str]:
    """Return ``src/`` imports of a sealing primitive from the deprecated module."""
    hits: list[str] = []
    for py, tree in _iter_src_modules():
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module != _DEPRECATED_RECORDER_MODULE:
                continue
            for alias in node.names:
                if alias.name in _SEALING_NAMES:
                    hits.append(f"{py}:{node.lineno}: from {node.module} import {alias.name}")
    return hits


def test_no_v1_writer_construction_in_src() -> None:
    sites = _construction_sites()
    assert sites == [], (
        "deprecated v1 lineage writers must not be constructed in src/; "
        "route provenance writes through LineageSpine, or signed writes through "
        "bernstein.core.lineage.signed_write:\n" + "\n".join(sites)
    )


def test_no_v1_writer_import_in_src() -> None:
    """A v1-typed entrypoint is a construction site waiting to happen."""
    sites = _import_sites()
    assert sites == [], (
        "deprecated v1 lineage writers must not be imported in src/ (not even "
        "under TYPE_CHECKING); type signed-write entrypoints against "
        "bernstein.core.lineage.signed_write.SignedLineageLog:\n" + "\n".join(sites)
    )


def test_signed_append_only_from_the_supported_module() -> None:
    """A signed write cannot silently regress onto a deprecated substrate."""
    sites = _signed_append_sites()
    assert sites == [], (
        "LineageStore.append(entry, jws=...) is the signed-write substrate and may "
        "only be called from bernstein/core/lineage/signed_write.py; call seal_write "
        "instead:\n" + "\n".join(sites)
    )


def test_deprecated_recorder_does_not_re_export_the_sealing_primitive() -> None:
    """The deprecated module must not become the new signed-write door."""
    recorder = importlib.import_module(_DEPRECATED_RECORDER_MODULE)
    assert recorder.__all__ == ["LineageRecorder"], (
        f"{_DEPRECATED_RECORDER_MODULE} must export only the deprecated shim, got {recorder.__all__}"
    )
    # ``SignedLineageLog`` is necessarily bound here - the shim subclasses it -
    # but the sealing *function* must not be reachable through this module.
    assert not hasattr(recorder, "seal_write"), (
        f"{_DEPRECATED_RECORDER_MODULE}.seal_write re-opens the bypass issue #2960 closed"
    )
    bypasses = _sealing_import_bypasses()
    assert bypasses == [], "sealing primitives must be imported from signed_write:\n" + "\n".join(bypasses)


def test_deprecated_recorder_is_a_shim_over_the_supported_path() -> None:
    """``LineageRecorder`` carries no substrate of its own any more."""
    from bernstein.core.lineage.recorder import LineageRecorder
    from bernstein.core.lineage.signed_write import SignedLineageLog

    assert issubclass(LineageRecorder, SignedLineageLog)
    # The shim adds a deprecation warning and nothing else: no override of the
    # write path, so it cannot diverge from the supported bytes.
    assert "record_write" not in vars(LineageRecorder)
