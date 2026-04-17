"""Tests for agent identity cards."""

from __future__ import annotations

from pathlib import Path

from bernstein.core.security.agent_identity import (
    AgentIdentityCard,
    check_capability,
    issue_identity_card,
    load_identity_card,
    save_identity_card,
)


class TestIssueIdentityCard:
    def test_backend_role_gets_full_capabilities(self) -> None:
        card = issue_identity_card("agent-1", "backend", "claude", "sonnet")
        assert "write_files" in card.capabilities
        assert "run_tests" in card.capabilities
        assert card.role == "backend"

    def test_reviewer_role_cannot_write(self) -> None:
        card = issue_identity_card("agent-2", "reviewer", "claude", "opus")
        assert "write_files" not in card.capabilities
        assert "write_files" in card.denied_capabilities

    def test_unknown_role_gets_minimal_capabilities(self) -> None:
        card = issue_identity_card("agent-3", "unknown", "claude", "haiku")
        assert card.capabilities == ["read_files"]

    def test_card_has_stable_hash(self) -> None:
        card = issue_identity_card("agent-4", "qa", "claude", "haiku")
        h1 = card.card_hash
        h2 = card.card_hash
        assert h1 == h2
        assert len(h1) == 16

    def test_ttl_sets_expiry(self) -> None:
        card = issue_identity_card("agent-5", "qa", "claude", "haiku", ttl_seconds=60)
        assert card.expires_at > card.created_at
        assert not card.is_expired()


class TestSaveLoadIdentityCard:
    def test_roundtrip(self, tmp_path: Path) -> None:
        card = issue_identity_card("agent-6", "backend", "claude", "sonnet")
        save_identity_card(card, tmp_path)
        loaded = load_identity_card("agent-6", tmp_path)
        assert loaded is not None
        assert loaded.agent_id == card.agent_id
        assert loaded.capabilities == card.capabilities

    def test_missing_returns_none(self, tmp_path: Path) -> None:
        assert load_identity_card("nonexistent", tmp_path) is None


class TestCheckCapability:
    def test_allowed_capability(self) -> None:
        card = issue_identity_card("a", "backend", "claude", "sonnet")
        ok, _ = check_capability(card, "write_files")
        assert ok

    def test_denied_capability(self) -> None:
        card = issue_identity_card("a", "reviewer", "claude", "opus")
        ok, reason = check_capability(card, "write_files")
        assert not ok
        assert "denied" in reason

    def test_ungranted_capability(self) -> None:
        card = issue_identity_card("a", "qa", "claude", "haiku")
        ok, reason = check_capability(card, "network_access")
        assert not ok
        assert "not granted" in reason

    def test_expired_card(self) -> None:
        card = AgentIdentityCard(
            agent_id="a",
            role="backend",
            adapter="claude",
            model="sonnet",
            capabilities=["write_files"],
            expires_at=1.0,  # long ago
        )
        ok, reason = check_capability(card, "write_files")
        assert not ok
        assert "expired" in reason


class TestInScope:
    def test_empty_scope_allows_all(self) -> None:
        card = issue_identity_card("a", "backend", "claude", "sonnet")
        assert card.in_scope("/any/path")

    def test_scope_restricts(self) -> None:
        card = issue_identity_card("a", "backend", "claude", "sonnet", scope=["/src/api/"])
        assert card.in_scope("/src/api/users.py")
        assert not card.in_scope("/src/auth/")
