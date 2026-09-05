"""Every ``_atomic_write`` helper publishes durably, and none uses ``rename``.

``core/persistence/atomic_write.py`` exists so the temp-and-rename dance is
written once: a per-writer temporary name, ``fsync`` of the file, ``fsync`` of
the containing directory, and owner-only permissions on runtime state.

Helpers that reimplemented it locally each dropped a different part, and the
parts are not interchangeable:

* a missing ``fsync`` makes the rename durable while the bytes behind it are
  not, which is the empty-file-after-a-crash case the pattern exists to avoid;
* ``Path.rename`` raises ``FileExistsError`` on Windows when the destination
  exists, so a helper using it cannot overwrite there at all.

The two guards below are the cheap way to keep the next copy from
reintroducing either. They are deliberately narrower than "nobody may rename":
a helper that fsyncs and uses ``os.replace`` is safe, and several predate the
canonical module without being wrong.
"""

from __future__ import annotations

import ast
import hashlib
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

SRC = Path(__file__).resolve().parents[2] / "src" / "bernstein"

#: The module that is supposed to own the rename.
CANONICAL = SRC / "core" / "persistence" / "atomic_write.py"


def _atomic_helpers() -> list[tuple[str, str]]:
    """Return ``(label, source)`` for every local ``*atomic*`` write helper."""
    found: list[tuple[str, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        if path == CANONICAL:
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover - unreadable file
            continue
        if "atomic" not in source:
            continue
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if "atomic" not in node.name.lower():
                continue
            body = ast.get_source_segment(source, node) or ""
            if ".replace(" not in body and ".rename(" not in body:
                continue
            found.append((f"{path.relative_to(SRC).as_posix()}::{node.name}", body))
    return found


def test_the_guard_can_see_the_helpers_it_is_guarding() -> None:
    """A scan that finds nothing would pass both guards for the wrong reason."""
    assert len(_atomic_helpers()) >= 5


def test_no_atomic_helper_publishes_with_rename() -> None:
    """``os.replace`` overwrites atomically; ``Path.rename`` refuses on Windows."""
    offenders = [label for label, body in _atomic_helpers() if ".rename(" in body]
    assert offenders == [], (
        "these helpers publish with rename, which raises FileExistsError on "
        "Windows when the destination exists: " + ", ".join(offenders)
    )


def test_every_atomic_helper_fsyncs_or_delegates() -> None:
    """A rename that outlives its own bytes is not a crash-safe write."""
    offenders = [label for label, body in _atomic_helpers() if "fsync" not in body and "write_atomic" not in body]
    assert offenders == [], (
        "these helpers rename without fsync and without delegating to "
        "core.persistence.atomic_write: " + ", ".join(offenders)
    )


def _fsync_spy(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record every ``os.fsync`` the write path performs."""
    seen: list[int] = []
    real = os.fsync

    def spy(fd: int) -> None:
        seen.append(fd)
        real(fd)

    monkeypatch.setattr(os, "fsync", spy)
    return seen


def _converted_writers(tmp_path: Path) -> list[tuple[str, Callable[[], Any]]]:
    """Return ``(label, thunk)`` for each helper this change routed through the canonical path."""
    from bernstein.core.plugins_core import plugin_pin_manifest
    from bernstein.core.protocols.mcp_catalog import user_config
    from bernstein.core.review_responder import dedup
    from bernstein.core.tokens import template_compression
    from bernstein.core.tunnels import registry
    from bernstein.evolution.applicator import FileUpgradeExecutor

    payload = b"backup bytes"
    executor = FileUpgradeExecutor(tmp_path / "evolution")

    return [
        ("mcp_catalog.user_config", lambda: user_config._atomic_write(tmp_path / "catalog.json", {"a": 1})),
        ("review_responder.dedup", lambda: dedup._atomic_write(tmp_path / "dedup.json", "{}")),
        (
            "tokens.template_compression",
            lambda: template_compression._atomic_write(
                tmp_path / "backup.bin",
                payload,
                expected_sha256=hashlib.sha256(payload).hexdigest(),
            ),
        ),
        ("tunnels.registry", lambda: registry._atomic_write(tmp_path / "tunnels.json", "{}")),
        ("plugins_core.plugin_pin_manifest", lambda: plugin_pin_manifest._write_atomic(tmp_path / "pins.json", b"{}")),
        ("evolution.applicator", lambda: executor._atomic_write(tmp_path / "proposal.yaml", {"id": "p-1"})),
    ]


def test_every_converted_writer_fsyncs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for label, write in _converted_writers(tmp_path):
        seen = _fsync_spy(monkeypatch)
        write()
        assert seen, f"{label} published without fsyncing"
        monkeypatch.undo()


def test_every_converted_writer_leaves_no_temporary_behind(tmp_path: Path) -> None:
    for _label, write in _converted_writers(tmp_path):
        write()
    stray = sorted(p.name for p in tmp_path.iterdir() if ".tmp" in p.name)
    assert stray == []


def test_rewriting_an_existing_file_replaces_it(tmp_path: Path) -> None:
    """``Path.rename`` refuses an existing destination on Windows.

    ``FileUpgradeExecutor._atomic_write`` used it, so rewriting a proposal
    raised ``FileExistsError`` there rather than replacing the file. It is
    the reason the guard above checks for ``rename`` and not only for
    ``fsync``.
    """
    from bernstein.evolution.applicator import FileUpgradeExecutor

    executor = FileUpgradeExecutor(tmp_path / "evolution")
    target = tmp_path / "proposal.yaml"
    executor._atomic_write(target, {"id": "first"})
    executor._atomic_write(target, {"id": "second"})
    assert "second" in target.read_text(encoding="utf-8")


def test_yaml_is_written_as_utf8_regardless_of_host_locale(tmp_path: Path) -> None:
    """``Path.open("w")`` falls back to the locale encoding.

    ``FileUpgradeExecutor._atomic_write`` used it, and PyYAML emits non-ASCII
    as-is by default, so a proposal carrying one was encoded in whatever the
    writing host happened to use and had to be read back on the same one.
    """
    from bernstein.evolution.applicator import FileUpgradeExecutor

    executor = FileUpgradeExecutor(tmp_path / "evolution")
    proposal = tmp_path / "proposal.yaml"
    executor._atomic_write(proposal, {"title": "café"})
    assert proposal.read_bytes().decode("utf-8")


def test_a_converted_writer_still_round_trips_its_payload(tmp_path: Path) -> None:
    """Delegating must not change what lands on disk."""
    import json

    from bernstein.core.protocols.mcp_catalog import user_config

    catalog = tmp_path / "catalog.json"
    user_config._atomic_write(catalog, {"b": 2, "a": 1})
    assert json.loads(catalog.read_text(encoding="utf-8")) == {"a": 1, "b": 2}
