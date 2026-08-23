"""Audit-chain recording of capability deltas and authorizations (#3768).

When a :class:`~bernstein.core.security.capability_delta.GrantDelta` is
computed for an agent role, the delta and any subsequent authorization are
mirrored into the HMAC-chained audit log as tamper-evident entries. This
module pins the recording contract:

* ``record_capability_delta`` appends an event whose ``details`` carries the
  run, role, delta hash, widening flag, and canonical changes JSON.
* ``record_capability_authorization`` appends an event with a deterministic,
  self-authenticating ``authorization_hash``.
* Both events chain onto the audit log (``prev_chain_digest`` is set).
* The entries are queryable and correlate by ``delta_hash`` / ``resource_id``.
* A byte-flip in a recorded details payload breaks ``chain.verify()``.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.security.audit_chain import (
    EVENT_CAPABILITY_AUTHORIZATION,
    EVENT_CAPABILITY_DELTA,
    AuditChainStore,
    CapabilityAuthorizationDetails,
    CapabilityDeltaDetails,
    record_capability_authorization,
    record_capability_delta,
)

KEY = b"k" * 32

_DELTA_HASH = "sha256:" + "a" * 64
_CHANGES_JSON = '[{"path":"/tmp","direction":"WIDENING","axis":"allowed","old_value":null,"new_value":"/tmp"}]'


def _create_chain(tmp_path: Path) -> AuditChainStore:
    """Return a fresh ``AuditChainStore`` over an isolated temp dir."""
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    return AuditChainStore(audit_dir, key=KEY)


# ---------------------------------------------------------------------------
# Event 1: capability.delta_recorded
# ---------------------------------------------------------------------------


def test_record_capability_delta_appends_event(tmp_path: Path) -> None:
    """``record_capability_delta`` appends an event with the right payload."""
    chain = _create_chain(tmp_path)

    event = record_capability_delta(
        chain=chain,
        run_id="run-1",
        role="backend",
        delta_hash=_DELTA_HASH,
        is_widening=True,
        changes_json=_CHANGES_JSON,
    )

    assert event.event_type == EVENT_CAPABILITY_DELTA
    assert event.actor == "backend"
    assert event.resource_type == "capability_delta"
    assert event.resource_id == _DELTA_HASH

    details = event.details
    assert details["run_id"] == "run-1"
    assert details["role"] == "backend"
    assert details["delta_hash"] == _DELTA_HASH
    assert details["is_widening"] is True
    assert details["changes_json"] == _CHANGES_JSON
    assert "prev_chain_digest" in details


def test_record_capability_delta_is_queryable(tmp_path: Path) -> None:
    """The delta event is retrievable via ``chain.query``."""
    chain = _create_chain(tmp_path)
    record_capability_delta(
        chain=chain,
        run_id="run-1",
        role="backend",
        delta_hash=_DELTA_HASH,
        is_widening=True,
        changes_json=_CHANGES_JSON,
    )

    events = chain.query(event_type=EVENT_CAPABILITY_DELTA)
    assert len(events) == 1
    assert events[0].resource_id == _DELTA_HASH


# ---------------------------------------------------------------------------
# Event 2: capability.authorization
# ---------------------------------------------------------------------------


def test_record_capability_authorization_appends_event(tmp_path: Path) -> None:
    """``record_capability_authorization`` appends an event with the right payload."""
    chain = _create_chain(tmp_path)

    event = record_capability_authorization(
        chain=chain,
        run_id="run-1",
        delta_hash=_DELTA_HASH,
        authorizer="operator",
        authorized_at_ns=1_000_000,
    )

    assert event.event_type == EVENT_CAPABILITY_AUTHORIZATION
    assert event.actor == "operator"
    assert event.resource_type == "capability_authorization"
    assert event.resource_id == _DELTA_HASH

    details = event.details
    assert details["run_id"] == "run-1"
    assert details["delta_hash"] == _DELTA_HASH
    assert details["authorizer"] == "operator"
    assert details["authorized_at_ns"] == 1_000_000
    assert details["authorization_hash"].startswith("sha256:")
    assert len(details["authorization_hash"]) == len("sha256:") + 64
    assert "prev_chain_digest" in details


def test_record_capability_authorization_is_queryable(tmp_path: Path) -> None:
    """The authorization event is retrievable via ``chain.query``."""
    chain = _create_chain(tmp_path)
    record_capability_authorization(
        chain=chain,
        run_id="run-1",
        delta_hash=_DELTA_HASH,
        authorizer="operator",
        authorized_at_ns=1_000_000,
    )

    events = chain.query(event_type=EVENT_CAPABILITY_AUTHORIZATION)
    assert len(events) == 1
    assert events[0].resource_id == _DELTA_HASH


def test_authorization_hash_is_deterministic(tmp_path: Path) -> None:
    """Same inputs produce the same ``authorization_hash``."""
    chain_a = _create_chain(tmp_path / "a")
    chain_b = _create_chain(tmp_path / "b")

    event_a = record_capability_authorization(
        chain=chain_a,
        run_id="run-1",
        delta_hash=_DELTA_HASH,
        authorizer="operator",
        authorized_at_ns=1_000_000,
    )
    event_b = record_capability_authorization(
        chain=chain_b,
        run_id="run-1",
        delta_hash=_DELTA_HASH,
        authorizer="operator",
        authorized_at_ns=1_000_000,
    )

    assert event_a.details["authorization_hash"] == event_b.details["authorization_hash"]


def test_authorization_hash_changes_with_inputs(tmp_path: Path) -> None:
    """Different inputs produce a different ``authorization_hash``."""
    chain = _create_chain(tmp_path)

    event_a = record_capability_authorization(
        chain=chain,
        run_id="run-1",
        delta_hash=_DELTA_HASH,
        authorizer="operator",
        authorized_at_ns=1_000_000,
    )
    event_b = record_capability_authorization(
        chain=chain,
        run_id="run-1",
        delta_hash=_DELTA_HASH,
        authorizer="steward:agent-1",
        authorized_at_ns=1_000_000,
    )

    assert event_a.details["authorization_hash"] != event_b.details["authorization_hash"]


# ---------------------------------------------------------------------------
# Chain linkage
# ---------------------------------------------------------------------------


def test_prev_chain_digest_is_set(tmp_path: Path) -> None:
    """``prev_chain_digest`` chains events onto each other."""
    chain = _create_chain(tmp_path)

    event1 = record_capability_delta(
        chain=chain,
        run_id="run-1",
        role="backend",
        delta_hash=_DELTA_HASH,
        is_widening=True,
        changes_json=_CHANGES_JSON,
    )
    event2 = record_capability_authorization(
        chain=chain,
        run_id="run-1",
        delta_hash=_DELTA_HASH,
        authorizer="operator",
        authorized_at_ns=1_000_000,
    )

    assert event1.details["prev_chain_digest"] != ""
    assert event2.details["prev_chain_digest"] != ""
    # The second event's prev_chain_digest should match the first event's hmac.
    assert event2.details["prev_chain_digest"] == event1.hmac


def test_correlate_delta_and_authorization_by_delta_hash(tmp_path: Path) -> None:
    """A delta event and its authorization correlate by ``delta_hash``."""
    chain = _create_chain(tmp_path)
    record_capability_delta(
        chain=chain,
        run_id="run-1",
        role="backend",
        delta_hash=_DELTA_HASH,
        is_widening=True,
        changes_json=_CHANGES_JSON,
    )
    record_capability_authorization(
        chain=chain,
        run_id="run-1",
        delta_hash=_DELTA_HASH,
        authorizer="operator",
        authorized_at_ns=1_000_000,
    )

    delta_events = chain.query(event_type=EVENT_CAPABILITY_DELTA, resource_id=_DELTA_HASH)
    auth_events = chain.query(event_type=EVENT_CAPABILITY_AUTHORIZATION, resource_id=_DELTA_HASH)

    assert len(delta_events) == 1
    assert len(auth_events) == 1
    assert delta_events[0].resource_id == auth_events[0].resource_id == _DELTA_HASH


# ---------------------------------------------------------------------------
# Tamper-evidence
# ---------------------------------------------------------------------------


def test_tamper_evidence_byte_flip_breaks_verification(tmp_path: Path) -> None:
    """A byte-flip in a recorded details payload must break ``chain.verify()``."""
    chain = _create_chain(tmp_path)
    record_capability_delta(
        chain=chain,
        run_id="run-1",
        role="backend",
        delta_hash=_DELTA_HASH,
        is_widening=True,
        changes_json=_CHANGES_JSON,
    )

    target = sorted(chain._log._audit_dir.glob("*.jsonl"))[0]  # pyright: ignore[reportPrivateUsage]
    raw = target.read_bytes()

    # Flip a byte inside the details payload (not the HMAC or structure).
    # ``json.dumps(sort_keys=True)`` emits ``": "`` separators, so search for
    # the space-separated form.
    needle = b'"run_id": "run-1"'
    offset = raw.find(needle)
    assert offset > 0, "test setup: could not locate run_id in the record bytes"

    mutated = bytearray(raw)
    # Flip '1' -> '0' in "run-1" (xor with 0x01).
    char_offset = offset + len(b'"run_id": "run-')
    mutated[char_offset] ^= 0x01
    target.write_bytes(bytes(mutated))

    ok, errors = chain.verify()
    assert not ok, "a byte-flip in the details payload must break verification"
    assert errors, "verify() returned invalid=True with empty errors list"


# ---------------------------------------------------------------------------
# Details dataclasses
# ---------------------------------------------------------------------------


def test_capability_delta_details_to_dict() -> None:
    """``CapabilityDeltaDetails.to_dict`` round-trips all fields."""
    details = CapabilityDeltaDetails(
        run_id="run-1",
        role="backend",
        delta_hash=_DELTA_HASH,
        is_widening=True,
        changes_json=_CHANGES_JSON,
    )
    d = details.to_dict()
    assert d == {
        "run_id": "run-1",
        "role": "backend",
        "delta_hash": _DELTA_HASH,
        "is_widening": True,
        "changes_json": _CHANGES_JSON,
    }


def test_capability_authorization_details_to_dict() -> None:
    """``CapabilityAuthorizationDetails.to_dict`` round-trips all fields."""
    auth_hash = "sha256:" + "f" * 64
    details = CapabilityAuthorizationDetails(
        run_id="run-1",
        delta_hash=_DELTA_HASH,
        authorizer="operator",
        authorized_at_ns=1_000_000,
        authorization_hash=auth_hash,
    )
    d = details.to_dict()
    assert d == {
        "run_id": "run-1",
        "delta_hash": _DELTA_HASH,
        "authorizer": "operator",
        "authorized_at_ns": 1_000_000,
        "authorization_hash": auth_hash,
    }


def test_clean_chain_verifies(tmp_path: Path) -> None:
    """A clean chain with both event types must verify."""
    chain = _create_chain(tmp_path)
    record_capability_delta(
        chain=chain,
        run_id="run-1",
        role="backend",
        delta_hash=_DELTA_HASH,
        is_widening=True,
        changes_json=_CHANGES_JSON,
    )
    record_capability_authorization(
        chain=chain,
        run_id="run-1",
        delta_hash=_DELTA_HASH,
        authorizer="operator",
        authorized_at_ns=1_000_000,
    )

    ok, errors = chain.verify()
    assert ok, errors
    assert errors == []
