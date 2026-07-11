"""The ``svid_reference`` field on AgentIdentityCard (issue #2363, AC 3).

Adding the SVID reference is additive: cards default to an empty reference, the
legacy ``card_hash`` stays bit-stable so existing HMAC anchors keep validating,
and the field round-trips through ``save``/``load``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from bernstein.core.security.agent_identity import (
    AgentIdentityCard,
    issue_identity_card,
    load_identity_card,
    save_identity_card,
)


def test_default_reference_empty() -> None:
    card = issue_identity_card("a1", "backend", "claude", "opus")
    assert card.svid_reference == ""


def test_legacy_hash_unaffected_by_reference() -> None:
    card = AgentIdentityCard(agent_id="a1", role="backend", adapter="claude", model="opus")
    before = card.card_hash
    card.svid_reference = "spiffe://ex.org/bernstein/deadbeefdeadbeef/a1"
    # Legacy hash (flag off) covers the pre-v1.0 field set only.
    assert card.card_hash == before


def test_reference_round_trips_through_disk(tmp_path: Path) -> None:
    card = issue_identity_card("a1", "backend", "claude", "opus")
    card.svid_reference = "spiffe://ex.org/bernstein/deadbeefdeadbeef/a1"
    runtime = tmp_path / "runtime"
    save_identity_card(card, runtime)
    loaded = load_identity_card("a1", runtime)
    assert loaded is not None
    assert loaded.svid_reference == card.svid_reference


def test_old_card_json_without_reference_loads(tmp_path: Path) -> None:
    """A pre-#2363 identity.json (no svid_reference key) still loads."""
    runtime = tmp_path / "runtime"
    agent_dir = runtime / "agents" / "a1"
    agent_dir.mkdir(parents=True)
    legacy = {
        "agent_id": "a1",
        "role": "backend",
        "adapter": "claude",
        "model": "opus",
        "capabilities": [],
        "denied_capabilities": [],
        "scope": [],
        "max_budget_usd": 10.0,
        "max_tokens": 64000,
        "max_steps": 30,
        "budget_mode": "graceful-finish-on-low",
        "extensions": {},
        "created_at": 1.0,
        "expires_at": 0.0,
    }
    (agent_dir / "identity.json").write_text(json.dumps(legacy))
    loaded = load_identity_card("a1", runtime)
    assert loaded is not None
    assert loaded.svid_reference == ""


def test_reference_covered_by_v1_hash(monkeypatch) -> None:
    monkeypatch.setenv("BERNSTEIN_AGENT_CARD_V1_0_HASH", "1")
    card = AgentIdentityCard(agent_id="a1", role="backend", adapter="claude", model="opus")
    without = card.card_hash
    card.svid_reference = "spiffe://ex.org/bernstein/deadbeefdeadbeef/a1"
    with_ref = card.card_hash
    # The v1.0 hash covers the full surface including the SVID reference.
    assert without != with_ref
    expected = hashlib.sha256(json.dumps(card.to_v1_dict(), sort_keys=True).encode()).hexdigest()[:16]
    assert with_ref == expected
