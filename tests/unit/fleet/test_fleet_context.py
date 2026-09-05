"""Tests for named operating contexts (#2550).

A context atomically pins server URL, store DSN, adapter defaults, and a
budget-envelope name as one named unit. Activation inserts one layer into
the home.py precedence chain between project and global, and is itself an
audit event. The run receipt embeds a canonical effective-settings hash;
replay refuses or flags on hash divergence, naming the diverging keys. With
no context active, the four-layer precedence is unchanged.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bernstein.core.config.home import BernsteinHome, resolve_config
from bernstein.core.fleet.context import (
    ContextHashMismatch,
    ContextStore,
    OperatingContext,
)
from bernstein.core.security.audit_chain import (
    EVENT_FLEET_CONTEXT_ACTIVATE,
    AuditChainStore,
)

_KEY = b"k" * 32


def _chain(tmp_path: Path) -> AuditChainStore:
    return AuditChainStore(tmp_path / "audit", key=_KEY)


def _store(project_dir: Path, chain: AuditChainStore) -> ContextStore:
    return ContextStore(project_dir / ".sdd" / "fleet" / "contexts", chain=chain)


def test_context_hash_is_deterministic_across_installs(tmp_path: Path) -> None:
    ctx_a = OperatingContext(
        name="staging-fleet",
        server_url="https://staging.example",
        store_dsn="postgres://s/db",
        adapter_defaults={"model": "claude", "effort": "medium"},
        budget_envelope="staging-budget",
    )
    ctx_b = OperatingContext(
        name="staging-fleet",
        server_url="https://staging.example",
        store_dsn="postgres://s/db",
        adapter_defaults={"effort": "medium", "model": "claude"},  # different key order
        budget_envelope="staging-budget",
    )
    assert ctx_a.canonical_document() == ctx_b.canonical_document()
    assert ctx_a.settings_hash() == ctx_b.settings_hash()


def test_activation_is_atomic_audit_event_and_layer(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    chain = _chain(tmp_path)
    store = _store(project, chain)
    home = BernsteinHome(tmp_path / "home")

    store.create(
        OperatingContext(
            name="staging",
            server_url="https://staging",
            store_dsn="postgres://s",
            adapter_defaults={},
            budget_envelope="staging-budget",
            config_layer={"model": "claude", "budget": 42},
        )
    )
    receipt = store.activate("staging")

    # The activation is an audit event embedding the settings hash.
    events = chain.query(event_type=EVENT_FLEET_CONTEXT_ACTIVATE)
    assert len(events) == 1
    assert events[0].details["name"] == "staging"
    assert events[0].details["settings_hash"] == receipt.settings_hash
    ok, errs = chain.verify()
    assert ok, errs

    # The context now contributes a layer between project and global.
    resolved = resolve_config("budget", home=home, project_dir=project)
    assert resolved["value"] == 42
    assert resolved["source"] == "context"
    resolved_model = resolve_config("model", home=home, project_dir=project)
    assert resolved_model["source"] == "context"


def test_no_context_active_preserves_four_layer_precedence(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    home = BernsteinHome(tmp_path / "home")

    # No active.json written -> default resolution, no context layer.
    resolved = resolve_config("model", home=home, project_dir=project)
    sources = [layer["source"] for layer in resolved["source_chain"]]
    assert "context" not in sources
    assert resolved["source"] == "default"


def test_deactivate_removes_context_layer(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    chain = _chain(tmp_path)
    store = _store(project, chain)
    home = BernsteinHome(tmp_path / "home")
    store.create(
        OperatingContext(
            name="staging",
            server_url="https://staging",
            store_dsn="",
            adapter_defaults={},
            budget_envelope="b",
            config_layer={"budget": 7},
        )
    )
    store.activate("staging")
    assert resolve_config("budget", home=home, project_dir=project)["source"] == "context"

    store.deactivate()
    assert resolve_config("budget", home=home, project_dir=project)["source"] != "context"


def test_replay_flags_hash_divergence_naming_keys(tmp_path: Path) -> None:
    recorded = OperatingContext(
        name="prod",
        server_url="https://prod",
        store_dsn="postgres://p",
        adapter_defaults={"model": "claude"},
        budget_envelope="prod-budget",
    )
    receipt = recorded.run_receipt()

    # Same context -> no divergence.
    same = receipt.verify_against(recorded)
    assert same.ok
    assert same.diverging_keys == []

    # A drifted context: server_url and budget changed.
    drifted = OperatingContext(
        name="prod",
        server_url="https://prod-2",
        store_dsn="postgres://p",
        adapter_defaults={"model": "claude"},
        budget_envelope="prod-budget-v2",
    )
    result = receipt.verify_against(drifted)
    assert not result.ok
    assert result.diverging_keys == ["budget_envelope", "server_url"]
    assert result.recorded_hash != result.current_hash


def test_replay_strict_policy_refuses_on_divergence(tmp_path: Path) -> None:
    recorded = OperatingContext(name="prod", server_url="https://a")
    drifted = OperatingContext(name="prod", server_url="https://b")
    receipt = recorded.run_receipt()

    # Flag policy: returns divergence without raising.
    flagged = receipt.verify_against(drifted, strict=False)
    assert not flagged.ok

    # Strict policy: refuses (raises) on divergence.
    with pytest.raises(ContextHashMismatch):
        receipt.verify_against(drifted, strict=True)


def test_create_list_get(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    chain = _chain(tmp_path)
    store = _store(project, chain)
    store.create(OperatingContext(name="dev", server_url="http://localhost:8052"))
    store.create(OperatingContext(name="staging", server_url="https://staging"))
    assert store.list_names() == ["dev", "staging"]
    assert store.get("dev").server_url == "http://localhost:8052"


# ---------------------------------------------------------------------------
# Durability of the context files and the activation pointer
# ---------------------------------------------------------------------------


def _context(name: str = "staging-fleet") -> OperatingContext:
    return OperatingContext(
        name=name,
        server_url="https://staging.example",
        store_dsn="postgres://s/db",
        adapter_defaults={"model": "claude", "effort": "medium"},
        budget_envelope="staging-budget",
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


def test_the_activation_pointer_is_fsynced_before_it_is_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An audited activation must not survive as a pointer full of nothing.

    ``activate`` records the audit event first so a live pointer always has
    its record. Without an fsync the inverse happens instead: the rename is
    durable, the bytes are not, and ``_active_field`` answers the truncated
    pointer with ``None``. The fleet silently drops back to four-layer
    precedence while the chain still says the context was activated.
    """
    project = tmp_path / "proj"
    project.mkdir()
    store = _store(project, _chain(tmp_path))
    store.create(_context())
    seen = _fsync_spy(monkeypatch)
    store.activate("staging-fleet")
    assert seen, "the activation pointer was published without being fsynced"


def test_a_context_definition_is_fsynced_before_it_is_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    store = _store(project, _chain(tmp_path))
    seen = _fsync_spy(monkeypatch)
    store.create(_context())
    assert seen, "the context definition was published without being fsynced"


def test_writing_leaves_no_temporary_behind(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    store = _store(project, _chain(tmp_path))
    store.create(_context())
    store.activate("staging-fleet")
    root = project / ".sdd" / "fleet" / "contexts"
    assert [p.name for p in root.iterdir() if ".tmp" in p.name] == []


def test_concurrent_writers_do_not_share_one_temporary_slot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing here holds a lock, so the temporary name must be per-writer.

    ``path.with_suffix(".json.tmp")`` gave every writer of one target the
    same slot: two activations could write it at once and publish a torn
    mix of both.
    """
    project = tmp_path / "proj"
    project.mkdir()
    store = _store(project, _chain(tmp_path))
    store.create(_context())

    temporaries: list[str] = []
    from bernstein.core.persistence import atomic_write

    real = atomic_write._tmp_path_for

    def record(path: Path) -> Path:
        chosen = real(path)
        temporaries.append(chosen.name)
        return chosen

    monkeypatch.setattr(atomic_write, "_tmp_path_for", record)
    store.activate("staging-fleet")
    store.activate("staging-fleet")
    assert len(temporaries) == 2
    assert temporaries[0] != temporaries[1]


def test_a_failed_write_leaves_the_previous_pointer_intact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    store = _store(project, _chain(tmp_path))
    store.create(_context())
    store.create(_context("prod-fleet"))
    store.activate("staging-fleet")

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("bernstein.core.persistence.atomic_write.os.replace", boom)
    with pytest.raises(OSError):
        store.activate("prod-fleet")

    monkeypatch.undo()
    assert store.active_name() == "staging-fleet"
    root = project / ".sdd" / "fleet" / "contexts"
    assert [p.name for p in root.iterdir() if ".tmp" in p.name] == []
