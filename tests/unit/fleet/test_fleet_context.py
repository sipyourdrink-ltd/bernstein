"""Tests for named operating contexts (#2550).

A context atomically pins server URL, store DSN, adapter defaults, and a
budget-envelope name as one named unit. Activation inserts one layer into
the home.py precedence chain between project and global, and is itself an
audit event. The run receipt embeds a canonical effective-settings hash;
replay refuses or flags on hash divergence, naming the diverging keys. With
no context active, the four-layer precedence is unchanged.
"""

from __future__ import annotations

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
