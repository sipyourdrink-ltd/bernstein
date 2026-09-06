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
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

SRC = Path(__file__).resolve().parents[2] / "src" / "bernstein"

#: The module that is supposed to own the rename.
CANONICAL = SRC / "core" / "persistence" / "atomic_write.py"


#: Names that publish a file by moving another one onto it.
_PUBLISHING = {"replace", "rename"}

#: The canonical helpers. A body that *calls* one of these delegates.
_DELEGATES = {"write_atomic_bytes", "write_atomic_text", "write_atomic_json", "promote_atomic"}


def _called_names(node: ast.AST) -> set[str]:
    """Return the bare and attribute names this body calls."""
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Attribute):
            names.add(func.attr)
        elif isinstance(func, ast.Name):
            names.add(func.id)
    return names


def _atomic_helpers() -> list[tuple[str, ast.AST]]:
    """Return ``(label, node)`` for every local ``*atomic*`` write helper.

    A helper is in scope when it either publishes by its own rename *or*
    delegates to the canonical module. Scoping on the rename alone would
    drop every converted helper out of the scan, so a later regression to a
    plain non-atomic ``write_bytes`` in any of them would be invisible.
    """
    found: list[tuple[str, ast.AST]] = []
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
            called = _called_names(node)
            if not (called & _PUBLISHING) and not (called & _DELEGATES):
                continue
            found.append((f"{path.relative_to(SRC).as_posix()}::{node.name}", node))
    return found


def test_the_guard_can_see_the_helpers_it_is_guarding() -> None:
    """A scan that finds nothing would pass both guards for the wrong reason."""
    assert len(_atomic_helpers()) >= 5


def test_no_atomic_helper_publishes_with_rename() -> None:
    """``os.replace`` overwrites atomically; ``Path.rename`` refuses on Windows."""
    offenders = [label for label, node in _atomic_helpers() if "rename" in _called_names(node)]
    assert offenders == [], (
        "these helpers publish with rename, which raises FileExistsError on "
        "Windows when the destination exists: " + ", ".join(offenders)
    )


def test_every_atomic_helper_fsyncs_or_delegates() -> None:
    """A rename that outlives its own bytes is not a crash-safe write.

    Delegation is decided on the *call*, not on a substring of the source.
    Matching text would let any helper named ``*write_atomic*`` exempt
    itself, because the extracted segment includes its own ``def`` line --
    which is how ``plugin_pin_manifest._write_atomic``, a genuine offender,
    scored clean against an earlier draft of this guard.
    """
    offenders = []
    for label, node in _atomic_helpers():
        called = _called_names(node)
        if called & _DELEGATES or "fsync" in called:
            continue
        offenders.append(label)
    assert offenders == [], (
        "these helpers publish without fsync and without delegating to "
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
        (
            "tokens.template_compression.store_backup",
            lambda: template_compression.store_backup(payload, backup_root=tmp_path / "backups"),
        ),
        ("tunnels.registry", lambda: registry._atomic_write(tmp_path / "tunnels.json", "{}")),
        ("plugins_core.plugin_pin_manifest", lambda: plugin_pin_manifest._write_atomic(tmp_path / "pins.json", b"{}")),
        ("evolution.applicator", lambda: executor._atomic_write(tmp_path / "proposal.yaml", {"id": "p-1"})),
    ]


#: Labels of the helpers this change routed through the canonical path, so
#: each is its own test case. Looping inside one test stops at the first
#: failure and leaves the other six unreported.
_CONVERTED_LABELS = [
    "mcp_catalog.user_config",
    "review_responder.dedup",
    "tokens.template_compression",
    "tokens.template_compression.store_backup",
    "tunnels.registry",
    "plugins_core.plugin_pin_manifest",
    "evolution.applicator",
]


@pytest.mark.parametrize("label", _CONVERTED_LABELS)
def test_every_converted_writer_fsyncs(label: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A rename that outlives its own bytes is the failure these helpers hide."""
    write = dict(_converted_writers(tmp_path))[label]
    seen = _fsync_spy(monkeypatch)
    write()
    assert seen, f"{label} published without fsyncing"


def test_the_parametrisation_covers_every_converted_writer(tmp_path: Path) -> None:
    """A label dropped from the list above would silently stop being tested."""
    assert sorted(label for label, _ in _converted_writers(tmp_path)) == sorted(_CONVERTED_LABELS)


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


def test_a_proposal_round_trips_through_write_and_read(tmp_path: Path) -> None:
    """Both sides name UTF-8, so a value survives a host whose locale does not.

    An earlier draft asserted only that the bytes decoded, which
    ``yaml.dump``'s ASCII escaping made true either way. What matters is
    that the writer and ``_read_yaml`` agree: hard-coding UTF-8 on one side
    alone turns a self-consistent pair into a mismatched one.
    """
    from bernstein.evolution.applicator import FileUpgradeExecutor

    executor = FileUpgradeExecutor(tmp_path / "evolution")
    proposal = tmp_path / "proposal.yaml"
    executor._atomic_write(proposal, {"title": "café ☃"})
    assert executor._read_yaml(proposal) == {"title": "café ☃"}


def test_a_catalog_round_trips_through_write_and_read(tmp_path: Path) -> None:
    """``user_config`` writes UTF-8 and now reads it back the same way."""
    from bernstein.core.protocols.mcp_catalog import user_config

    catalog = tmp_path / "catalog.json"
    user_config._atomic_write(catalog, {"name": "café ☃"})
    assert user_config._load_raw(catalog) == {"name": "café ☃"}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes only")
def test_runtime_state_is_owner_only_but_templates_stay_readable(tmp_path: Path) -> None:
    """The helper's default narrows to 0600; role templates opt back out.

    ``resolve_roles_dir`` can land on the templates directory inside the
    installed package, so a system-wide install written as one account and
    run as another must not lose read access to its own templates.
    """
    from bernstein.core.tokens import template_compression
    from bernstein.core.tunnels import registry

    runtime = tmp_path / "tunnels.json"
    registry._atomic_write(runtime, "{}")
    assert (runtime.stat().st_mode & 0o777) == 0o600

    template = tmp_path / "role.md"
    payload = b"# role"
    template_compression._atomic_write(template, payload, expected_sha256=hashlib.sha256(payload).hexdigest())
    assert (template.stat().st_mode & 0o777) == 0o644


def test_a_converted_writer_still_round_trips_its_payload(tmp_path: Path) -> None:
    """Delegating must not change what lands on disk."""
    import json

    from bernstein.core.protocols.mcp_catalog import user_config

    catalog = tmp_path / "catalog.json"
    user_config._atomic_write(catalog, {"b": 2, "a": 1})
    assert json.loads(catalog.read_text(encoding="utf-8")) == {"a": 1, "b": 2}
