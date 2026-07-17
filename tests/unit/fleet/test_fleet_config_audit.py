"""Semantic verification of fleet config-plane chain events (#2550)."""

from __future__ import annotations

from pathlib import Path

from bernstein.core.fleet.config_audit import verify_fleet_config_events
from bernstein.core.fleet.connection import ConnectionDocumentStore, create_document
from bernstein.core.fleet.variables import FleetVariableStore
from bernstein.core.security.audit_chain import (
    AuditChainStore,
    record_fleet_conn_resolve,
    record_fleet_var_set,
)

_KEY = b"k" * 32


def _chain(tmp_path: Path) -> AuditChainStore:
    return AuditChainStore(tmp_path / "audit", key=_KEY)


def test_empty_chain_passes(tmp_path: Path) -> None:
    ok, errors = verify_fleet_config_events(_chain(tmp_path))
    assert ok
    assert errors == []


def test_valid_variable_and_connection_history_passes(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    store = FleetVariableStore(tmp_path / "vars", chain=chain)
    store.set("k", 1)
    store.set("k", 2)
    store.set("other", "x")

    conn_store = ConnectionDocumentStore(tmp_path / "conns")
    create_document(
        name="team-slack",
        secret_name="slack",
        scope="chat:write",
        connector_defaults={},
        identity_dir=tmp_path / "id",
        chain=chain,
        store=conn_store,
    )
    doc = conn_store.get("team-slack")
    record_fleet_conn_resolve(
        chain=chain, name="team-slack", document_hash=doc.document_hash(), task_id="t1", token_id="tok1"
    )

    ok, errors = verify_fleet_config_events(chain)
    assert ok, errors


def test_spliced_variable_ordinal_fails(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    # Directly record a write with a non-contiguous ordinal (as a splice
    # would leave behind) - the semantic pillar catches it even though each
    # record is individually HMAC-valid.
    record_fleet_var_set(chain=chain, name="k", old_value_hash="", new_value_hash="sha256:a", chain_position=0)
    record_fleet_var_set(chain=chain, name="k", old_value_hash="sha256:a", new_value_hash="sha256:b", chain_position=2)
    ok, errors = verify_fleet_config_events(chain)
    assert not ok
    assert any("out of sequence" in e for e in errors)


def test_broken_value_hash_lineage_fails(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    record_fleet_var_set(chain=chain, name="k", old_value_hash="", new_value_hash="sha256:a", chain_position=0)
    record_fleet_var_set(
        chain=chain, name="k", old_value_hash="sha256:WRONG", new_value_hash="sha256:b", chain_position=1
    )
    ok, errors = verify_fleet_config_events(chain)
    assert not ok
    assert any("does not chain" in e for e in errors)


def test_malformed_chain_position_is_reported_not_crashed(tmp_path: Path) -> None:
    # A non-integer chain_position must be reported as an error, never crash
    # verification with a ValueError.
    chain = _chain(tmp_path)
    chain.log_with_prev_digest(
        event_type="fleet.var_set",
        actor="x",
        resource_type="fleet_variable",
        resource_id="k",
        details={
            "name": "k",
            "old_value_hash": "",
            "new_value_hash": "sha256:a",
            "chain_position": "not-an-int",
        },
    )
    ok, errors = verify_fleet_config_events(chain)
    assert not ok
    assert any("malformed chain_position" in e for e in errors)


def test_reordered_history_is_caught_without_normalization(tmp_path: Path) -> None:
    # Positions appended out of order in the chain must fail (the verifier
    # never re-sorts to hide it).
    chain = _chain(tmp_path)
    record_fleet_var_set(chain=chain, name="k", old_value_hash="", new_value_hash="sha256:a", chain_position=0)
    record_fleet_var_set(chain=chain, name="k", old_value_hash="sha256:a", new_value_hash="sha256:b", chain_position=2)
    record_fleet_var_set(chain=chain, name="k", old_value_hash="sha256:b", new_value_hash="sha256:c", chain_position=1)
    ok, errors = verify_fleet_config_events(chain)
    assert not ok
    assert any("out of sequence" in e for e in errors)


def test_resolution_without_create_fails(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    record_fleet_conn_resolve(chain=chain, name="ghost", document_hash="sha256:z", task_id="t1", token_id="tok1")
    ok, errors = verify_fleet_config_events(chain)
    assert not ok
    assert any("unknown document" in e for e in errors)
