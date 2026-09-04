"""Tests for the AgentIdentityCard v1.0 dataclass migration.

Covers the new ``protocol_version`` / ``supported_interfaces`` /
``security_schemes`` / ``signatures`` fields, the ``to_legacy_dict``
back-compat shim, and the feature-flagged hash migration.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from bernstein.core.identity.agent_card import (
    A2A_PROTOCOL_VERSION_V1_0,
    AGENT_CARD_V1_0_HASH_ENV,
    AgentIdentityCard,
    InterfaceSpec,
    SecurityScheme,
    Signature,
    issue_identity_card,
    load_identity_card,
    save_identity_card,
)


def _legacy_card() -> AgentIdentityCard:
    """A pre-v1.0-shaped card (no v1.0 fields populated)."""
    return issue_identity_card(
        agent_id="claude-backend-v1mig",
        role="backend",
        adapter="claude-cli",
        model="claude-opus-4-7",
        scope=["src/"],
        max_budget_usd=4.0,
        ttl_seconds=600,
    )


def _v1_card() -> AgentIdentityCard:
    """A v1.0-shaped card with all new fields populated."""
    card = _legacy_card()
    card.protocol_version = A2A_PROTOCOL_VERSION_V1_0
    card.supported_interfaces = [InterfaceSpec(name="HTTP+JSON", version="1.0")]
    card.security_schemes = [
        SecurityScheme(id="bearer-jwt", type="http", scheme="Bearer", required=True),
    ]
    card.signatures = [Signature(kid="agent-claude-backend-v1mig", jws="abc..xyz")]
    return card


# ---------------------------------------------------------------------------
# v1.0 fields exist on the dataclass
# ---------------------------------------------------------------------------


class TestV1Fields:
    def test_default_card_has_empty_v1_fields(self) -> None:
        card = _legacy_card()
        assert card.protocol_version == ""
        assert card.supported_interfaces == []
        assert card.security_schemes == []
        assert card.signatures == []

    def test_v1_fields_round_trip_through_to_json(self) -> None:
        card = _v1_card()
        data = json.loads(card.to_json())
        assert data["protocol_version"] == "1.0"
        assert data["supported_interfaces"] == [{"name": "HTTP+JSON", "version": "1.0", "description": ""}]
        assert data["security_schemes"][0]["id"] == "bearer-jwt"
        assert data["signatures"][0]["typ"] == "agent-card+jws"

    def test_load_identity_card_rebuilds_v1_dataclasses(self, tmp_path: Path) -> None:
        """Loading a saved card must coerce nested dicts back to dataclass instances."""
        runtime = tmp_path / "runtime"
        save_identity_card(_v1_card(), runtime)
        loaded = load_identity_card("claude-backend-v1mig", runtime)
        assert loaded is not None
        assert isinstance(loaded.supported_interfaces[0], InterfaceSpec)
        assert isinstance(loaded.security_schemes[0], SecurityScheme)
        assert isinstance(loaded.signatures[0], Signature)
        assert loaded.protocol_version == "1.0"

    def test_load_legacy_card_still_works(self, tmp_path: Path) -> None:
        """Pre-v1.0 ``identity.json`` on disk must keep loading post-migration."""
        runtime = tmp_path / "runtime"
        save_identity_card(_legacy_card(), runtime)
        # Strip the v1.0 keys to mimic an older Bernstein release's output.
        path = runtime / "agents" / "claude-backend-v1mig" / "identity.json"
        legacy_data = {
            k: v
            for k, v in json.loads(path.read_text()).items()
            if k not in {"protocol_version", "supported_interfaces", "security_schemes", "signatures"}
        }
        path.write_text(json.dumps(legacy_data))

        loaded = load_identity_card("claude-backend-v1mig", runtime)
        assert loaded is not None
        assert loaded.protocol_version == ""
        assert loaded.supported_interfaces == []


# ---------------------------------------------------------------------------
# to_legacy_dict - back-compat shim
# ---------------------------------------------------------------------------


class TestLegacyDict:
    def test_to_legacy_dict_strips_v1_fields(self) -> None:
        card = _v1_card()
        legacy = card.to_legacy_dict()
        for excluded in ("protocol_version", "supported_interfaces", "security_schemes", "signatures"):
            assert excluded not in legacy

    def test_to_legacy_dict_preserves_pre_v1_keys(self) -> None:
        card = _v1_card()
        legacy = card.to_legacy_dict()
        for required in (
            "agent_id",
            "role",
            "adapter",
            "model",
            "capabilities",
            "denied_capabilities",
            "scope",
            "max_budget_usd",
            "max_tokens",
            "max_steps",
            "budget_mode",
            "extensions",
            "created_at",
            "expires_at",
        ):
            assert required in legacy, required


# ---------------------------------------------------------------------------
# card_hash - feature-flagged migration
# ---------------------------------------------------------------------------


class TestCardHashMigration:
    def test_default_hash_is_legacy_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without the flag, ``card_hash`` is the pre-v1.0 SHA-256 prefix."""
        monkeypatch.delenv(AGENT_CARD_V1_0_HASH_ENV, raising=False)
        card = _v1_card()

        legacy_payload = json.dumps(card.to_legacy_dict(), sort_keys=True).encode()
        expected_legacy = hashlib.sha256(legacy_payload).hexdigest()[:16]
        assert card.card_hash == expected_legacy

    def test_v1_flag_switches_hash_to_full_surface(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With the flag, ``card_hash`` covers the v1.0 fields too."""
        monkeypatch.setenv(AGENT_CARD_V1_0_HASH_ENV, "1")
        card = _v1_card()

        v1_payload = json.dumps(card.to_v1_dict(), sort_keys=True).encode()
        expected_v1 = hashlib.sha256(v1_payload).hexdigest()[:16]
        assert card.card_hash == expected_v1

    def test_legacy_card_hash_stable_across_v1_field_addition(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A card that pre-dates the v1.0 fields must still hash the same.

        This is the migration safety guarantee - adding empty v1.0 fields
        to a previously-created card must not change ``card_hash`` while
        the flag is off.
        """
        monkeypatch.delenv(AGENT_CARD_V1_0_HASH_ENV, raising=False)
        legacy = _legacy_card()
        v1_blank = AgentIdentityCard(
            agent_id=legacy.agent_id,
            role=legacy.role,
            adapter=legacy.adapter,
            model=legacy.model,
            capabilities=legacy.capabilities,
            denied_capabilities=legacy.denied_capabilities,
            scope=legacy.scope,
            max_budget_usd=legacy.max_budget_usd,
            max_tokens=legacy.max_tokens,
            max_steps=legacy.max_steps,
            budget_mode=legacy.budget_mode,
            extensions=legacy.extensions,
            created_at=legacy.created_at,
            expires_at=legacy.expires_at,
            # v1.0 fields default to empty.
        )
        assert legacy.card_hash == v1_blank.card_hash

    def test_v1_flag_off_ignores_v1_fields_in_hash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Adding v1.0 fields to a card must not flip the legacy hash.

        Two cards that differ only in v1.0 fields must hash identically
        when the flag is off - that's how operators safely populate the
        new fields ahead of flipping the migration switch.
        """
        monkeypatch.delenv(AGENT_CARD_V1_0_HASH_ENV, raising=False)
        no_v1 = _legacy_card()
        with_v1 = _v1_card()
        # Force identical timestamps so only the v1.0 fields differ.
        with_v1.created_at = no_v1.created_at
        with_v1.expires_at = no_v1.expires_at
        assert no_v1.card_hash == with_v1.card_hash

    def test_v1_flag_on_distinguishes_v1_field_changes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Once the flag is on, mutating a v1.0 field must change the hash."""
        monkeypatch.setenv(AGENT_CARD_V1_0_HASH_ENV, "1")
        a = _v1_card()
        b = _v1_card()
        b.signatures = [Signature(kid="rotated-kid", jws="abc..xyz")]
        # Identical timestamps → only the signatures field differs.
        b.created_at = a.created_at
        b.expires_at = a.expires_at
        assert a.card_hash != b.card_hash
