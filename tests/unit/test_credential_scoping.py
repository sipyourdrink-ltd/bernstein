"""Tests for bernstein.core.credential_scoping.

Covers credential creation, scope validation, revocation, role defaults,
the CredentialScopeManager lifecycle, and the audit-051 per-agent
environment credential policy.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bernstein.core.credential_scoping import (
    AgentCredentialPolicy,
    AgentNotScopedError,
    CredentialScope,
    CredentialScopeManager,
    ScopedCredential,
    UnknownCredentialKeyError,
    create_scoped_credential,
    get_default_policy,
    get_scope_for_role,
    load_policy_from_file,
    scoped_credential_keys,
    set_default_policy,
    validate_request_against_scope,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def backend_scope() -> CredentialScope:
    """Create a backend-style scope."""
    return CredentialScope(
        allowed_operations=("code_gen", "file_read", "file_write"),
        allowed_models=("gpt-4",),
        max_tokens_per_request=8192,
        rate_limit_rpm=60,
    )


@pytest.fixture
def manager() -> CredentialScopeManager:
    """Create a fresh CredentialScopeManager."""
    return CredentialScopeManager()


# ---------------------------------------------------------------------------
# CredentialScope
# ---------------------------------------------------------------------------


class TestCredentialScope:
    """Tests for the CredentialScope dataclass."""

    def test_frozen(self) -> None:
        scope = CredentialScope(allowed_operations=("code_gen",))
        with pytest.raises(AttributeError):
            scope.allowed_operations = ("web_search",)  # type: ignore[misc]

    def test_defaults(self) -> None:
        scope = CredentialScope(allowed_operations=("code_gen",))
        assert scope.allowed_models is None
        assert scope.max_tokens_per_request is None
        assert scope.rate_limit_rpm is None

    def test_all_fields(self) -> None:
        scope = CredentialScope(
            allowed_operations=("code_gen",),
            allowed_models=("gpt-4", "claude-sonnet-4-20250514"),
            max_tokens_per_request=4096,
            rate_limit_rpm=30,
        )
        assert len(scope.allowed_operations) == 1
        assert len(scope.allowed_models) == 2  # type: ignore[arg-type]
        assert scope.max_tokens_per_request == 4096
        assert scope.rate_limit_rpm == 30


# ---------------------------------------------------------------------------
# create_scoped_credential
# ---------------------------------------------------------------------------


class TestCreateScopedCredential:
    """Tests for the create_scoped_credential helper."""

    def test_creates_credential(self, backend_scope: CredentialScope) -> None:
        cred = create_scoped_credential("agent-1", backend_scope)
        assert isinstance(cred, ScopedCredential)
        assert cred.agent_id == "agent-1"
        assert cred.scope is backend_scope

    def test_key_id_format(self, backend_scope: CredentialScope) -> None:
        cred = create_scoped_credential("agent-1", backend_scope)
        assert cred.key_id.startswith("sk-")
        assert len(cred.key_id) > 5

    def test_unique_key_ids(self, backend_scope: CredentialScope) -> None:
        cred_a = create_scoped_credential("agent-1", backend_scope)
        cred_b = create_scoped_credential("agent-1", backend_scope)
        assert cred_a.key_id != cred_b.key_id

    def test_expiry_default_ttl(self, backend_scope: CredentialScope) -> None:
        cred = create_scoped_credential("agent-1", backend_scope)
        delta = cred.expires_at - cred.created_at
        assert delta == timedelta(hours=24)

    def test_expiry_custom_ttl(self, backend_scope: CredentialScope) -> None:
        cred = create_scoped_credential("agent-1", backend_scope, ttl_hours=1)
        delta = cred.expires_at - cred.created_at
        assert delta == timedelta(hours=1)

    def test_created_at_is_utc(self, backend_scope: CredentialScope) -> None:
        cred = create_scoped_credential("agent-1", backend_scope)
        assert cred.created_at.tzinfo is not None


# ---------------------------------------------------------------------------
# validate_request_against_scope
# ---------------------------------------------------------------------------


class TestValidateRequestAgainstScope:
    """Tests for request-scope validation."""

    def test_valid_operation(self, backend_scope: CredentialScope) -> None:
        assert validate_request_against_scope({"operation": "code_gen"}, backend_scope)

    def test_invalid_operation(self, backend_scope: CredentialScope) -> None:
        assert not validate_request_against_scope({"operation": "web_search"}, backend_scope)

    def test_valid_model(self, backend_scope: CredentialScope) -> None:
        assert validate_request_against_scope({"model": "gpt-4"}, backend_scope)

    def test_invalid_model(self, backend_scope: CredentialScope) -> None:
        assert not validate_request_against_scope({"model": "llama-3"}, backend_scope)

    def test_no_model_restriction(self) -> None:
        scope = CredentialScope(allowed_operations=("code_gen",))
        assert validate_request_against_scope({"model": "anything"}, scope)

    def test_tokens_within_budget(self, backend_scope: CredentialScope) -> None:
        assert validate_request_against_scope({"tokens": 100}, backend_scope)

    def test_tokens_exceeds_budget(self, backend_scope: CredentialScope) -> None:
        assert not validate_request_against_scope({"tokens": 10000}, backend_scope)

    def test_empty_request(self, backend_scope: CredentialScope) -> None:
        assert validate_request_against_scope({}, backend_scope)

    def test_combined_fields(self, backend_scope: CredentialScope) -> None:
        assert validate_request_against_scope(
            {"operation": "code_gen", "model": "gpt-4", "tokens": 100},
            backend_scope,
        )

    def test_combined_one_invalid(self, backend_scope: CredentialScope) -> None:
        assert not validate_request_against_scope(
            {"operation": "code_gen", "model": "llama-3", "tokens": 100},
            backend_scope,
        )


# ---------------------------------------------------------------------------
# get_scope_for_role
# ---------------------------------------------------------------------------


class TestGetScopeForRole:
    """Tests for role-to-scope mapping."""

    def test_backend_role(self) -> None:
        scope = get_scope_for_role("backend")
        assert "code_gen" in scope.allowed_operations
        assert scope.allowed_models is not None

    def test_researcher_role(self) -> None:
        scope = get_scope_for_role("researcher")
        assert "web_search" in scope.allowed_operations
        assert "code_gen" not in scope.allowed_operations

    def test_admin_role(self) -> None:
        scope = get_scope_for_role("admin")
        assert "system_admin" in scope.allowed_operations

    def test_unknown_role_gets_minimal(self) -> None:
        scope = get_scope_for_role("nonexistent")
        assert scope.allowed_operations == ("file_read",)
        assert scope.max_tokens_per_request == 1024

    def test_frontend_role(self) -> None:
        scope = get_scope_for_role("frontend")
        assert "code_gen" in scope.allowed_operations
        assert "web_search" not in scope.allowed_operations


# ---------------------------------------------------------------------------
# CredentialScopeManager
# ---------------------------------------------------------------------------


class TestCredentialScopeManager:
    """Tests for the CredentialScopeManager."""

    def test_create_and_get(self, manager: CredentialScopeManager) -> None:
        cred = manager.create("agent-1", get_scope_for_role("backend"))
        fetched = manager.get(cred.key_id)
        assert fetched is cred

    def test_is_valid_fresh(self, manager: CredentialScopeManager) -> None:
        cred = manager.create("agent-1", get_scope_for_role("backend"))
        assert manager.is_valid(cred.key_id)

    def test_is_valid_unknown_key(self, manager: CredentialScopeManager) -> None:
        assert not manager.is_valid("sk-nonexistent")

    def test_revoke(self, manager: CredentialScopeManager) -> None:
        cred = manager.create("agent-1", get_scope_for_role("backend"))
        manager.revoke(cred.key_id)
        assert not manager.is_valid(cred.key_id)

    def test_revoke_all_for_agent(self, manager: CredentialScopeManager) -> None:
        manager.create("agent-1", get_scope_for_role("backend"))
        manager.create("agent-1", get_scope_for_role("frontend"))
        manager.create("agent-2", get_scope_for_role("backend"))
        count = manager.revoke_all_for_agent("agent-1")
        assert count == 2

    def test_list_for_agent(self, manager: CredentialScopeManager) -> None:
        manager.create("agent-1", get_scope_for_role("backend"))
        manager.create("agent-1", get_scope_for_role("frontend"))
        manager.create("agent-2", get_scope_for_role("backend"))
        creds = manager.list_for_agent("agent-1")
        assert len(creds) == 2

    def test_list_excludes_revoked(self, manager: CredentialScopeManager) -> None:
        cred = manager.create("agent-1", get_scope_for_role("backend"))
        manager.revoke(cred.key_id)
        assert len(manager.list_for_agent("agent-1")) == 0

    def test_validate_request(self, manager: CredentialScopeManager) -> None:
        cred = manager.create("agent-1", get_scope_for_role("backend"))
        assert manager.validate_request(cred.key_id, {"operation": "code_gen", "model": "gpt-4"})

    def test_validate_request_revoked(self, manager: CredentialScopeManager) -> None:
        cred = manager.create("agent-1", get_scope_for_role("backend"))
        manager.revoke(cred.key_id)
        assert not manager.validate_request(cred.key_id, {"operation": "code_gen"})

    def test_validate_request_out_of_scope(self, manager: CredentialScopeManager) -> None:
        cred = manager.create("agent-1", get_scope_for_role("backend"))
        assert not manager.validate_request(cred.key_id, {"operation": "web_search"})

    def test_cleanup_expired(self, manager: CredentialScopeManager) -> None:
        # Create a credential that expires immediately
        cred = manager.create("agent-1", get_scope_for_role("backend"), ttl_hours=0)
        # Manually set expiration in the past
        expired_cred = ScopedCredential(
            key_id=cred.key_id,
            agent_id=cred.agent_id,
            scope=cred.scope,
            created_at=cred.created_at,
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        manager._credentials[cred.key_id] = expired_cred
        removed = manager.cleanup_expired()
        assert removed == 1
        assert manager.get(cred.key_id) is None


# ---------------------------------------------------------------------------
# AgentCredentialPolicy (audit-051)
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_policy() -> AgentCredentialPolicy:
    """A representative enabled policy covering a few agents/roles."""
    return AgentCredentialPolicy(
        enabled=True,
        known_keys=frozenset({"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_ORG_ID"}),
        agent_rules={
            "backend-001": frozenset({"ANTHROPIC_API_KEY"}),
            "researcher-*": frozenset({"OPENAI_API_KEY"}),
        },
        role_rules={
            "analyst": frozenset({"OPENAI_API_KEY", "OPENAI_ORG_ID"}),
        },
    )


class TestAgentCredentialPolicyConstruction:
    """Tests for policy construction and validation invariants."""

    def test_disabled_by_default(self) -> None:
        policy = AgentCredentialPolicy()
        assert policy.enabled is False
        assert policy.known_keys == frozenset()

    def test_rules_referencing_undeclared_key_rejected(self) -> None:
        with pytest.raises(UnknownCredentialKeyError) as excinfo:
            AgentCredentialPolicy(
                enabled=True,
                known_keys=frozenset({"ANTHROPIC_API_KEY"}),
                agent_rules={"a-1": frozenset({"GOOGLE_API_KEY"})},
            )
        assert "GOOGLE_API_KEY" in str(excinfo.value)

    def test_role_rules_also_validated(self) -> None:
        with pytest.raises(UnknownCredentialKeyError):
            AgentCredentialPolicy(
                enabled=True,
                known_keys=frozenset({"A"}),
                role_rules={"backend": frozenset({"B"})},
            )


class TestAllowedFor:
    """Tests for AgentCredentialPolicy.allowed_for."""

    def test_exact_match(self, sample_policy: AgentCredentialPolicy) -> None:
        assert sample_policy.allowed_for("backend-001") == frozenset({"ANTHROPIC_API_KEY"})

    def test_glob_match(self, sample_policy: AgentCredentialPolicy) -> None:
        assert sample_policy.allowed_for("researcher-42") == frozenset({"OPENAI_API_KEY"})

    def test_role_fallback(self, sample_policy: AgentCredentialPolicy) -> None:
        assert sample_policy.allowed_for("unknown-agent", role="analyst") == frozenset(
            {"OPENAI_API_KEY", "OPENAI_ORG_ID"}
        )

    def test_unlisted_agent_fails_closed(self, sample_policy: AgentCredentialPolicy) -> None:
        with pytest.raises(AgentNotScopedError):
            sample_policy.allowed_for("stranger-1")

    def test_unlisted_agent_with_unknown_role_fails_closed(self, sample_policy: AgentCredentialPolicy) -> None:
        with pytest.raises(AgentNotScopedError):
            sample_policy.allowed_for("stranger-1", role="ghostbuster")

    def test_disabled_policy_returns_all_known(self) -> None:
        policy = AgentCredentialPolicy(
            enabled=False,
            known_keys=frozenset({"ANTHROPIC_API_KEY", "OPENAI_API_KEY"}),
        )
        # Disabled policy must not fail-close — it is a no-op.
        assert policy.allowed_for("any-agent") == policy.known_keys


class TestFilterKeys:
    """Tests for AgentCredentialPolicy.filter_keys — the hot-path."""

    def test_scoped_agent_sees_only_its_subset(self, sample_policy: AgentCredentialPolicy) -> None:
        got = sample_policy.filter_keys(
            "backend-001",
            {"ANTHROPIC_API_KEY", "OPENAI_API_KEY"},
        )
        assert got == ("ANTHROPIC_API_KEY",)

    def test_unscoped_agent_fails_closed(self, sample_policy: AgentCredentialPolicy) -> None:
        with pytest.raises(AgentNotScopedError):
            sample_policy.filter_keys("ghost-1", {"ANTHROPIC_API_KEY"})

    def test_unknown_requested_key_raises(self, sample_policy: AgentCredentialPolicy) -> None:
        with pytest.raises(UnknownCredentialKeyError) as excinfo:
            sample_policy.filter_keys("backend-001", {"COHERE_API_KEY"})
        assert "COHERE_API_KEY" in str(excinfo.value)

    def test_unknown_key_raised_even_when_disabled(self) -> None:
        policy = AgentCredentialPolicy(
            enabled=False,
            known_keys=frozenset({"ANTHROPIC_API_KEY"}),
        )
        with pytest.raises(UnknownCredentialKeyError):
            policy.filter_keys("any", {"MYSTERY_KEY"})

    def test_empty_known_keys_means_no_typo_check(self) -> None:
        # Legacy callers that predate the policy: policy with no
        # known_keys is fully inert, passes requested keys through.
        policy = AgentCredentialPolicy()
        assert policy.filter_keys("whatever", {"ANTHROPIC_API_KEY"}) == ("ANTHROPIC_API_KEY",)

    def test_role_fallback_filters_intersection(self, sample_policy: AgentCredentialPolicy) -> None:
        got = sample_policy.filter_keys(
            "nameless",
            {"OPENAI_API_KEY", "ANTHROPIC_API_KEY"},
            role="analyst",
        )
        # analyst is allowed OPENAI_API_KEY + OPENAI_ORG_ID; requested
        # set intersects to just OPENAI_API_KEY.
        assert got == ("OPENAI_API_KEY",)

    def test_returns_sorted_for_determinism(self, sample_policy: AgentCredentialPolicy) -> None:
        got = sample_policy.filter_keys(
            "nameless",
            {"OPENAI_ORG_ID", "OPENAI_API_KEY"},
            role="analyst",
        )
        assert got == ("OPENAI_API_KEY", "OPENAI_ORG_ID")


class TestScopedCredentialKeysHelper:
    """Tests for the module-level convenience wrapper."""

    def test_uses_default_policy(self, sample_policy: AgentCredentialPolicy) -> None:
        prior = get_default_policy()
        set_default_policy(sample_policy)
        try:
            got = scoped_credential_keys("backend-001", {"ANTHROPIC_API_KEY"})
            assert got == ("ANTHROPIC_API_KEY",)
        finally:
            set_default_policy(prior)

    def test_explicit_policy_overrides_default(self, sample_policy: AgentCredentialPolicy) -> None:
        # Default stays disabled; pass an explicit enforcing policy.
        with pytest.raises(AgentNotScopedError):
            scoped_credential_keys("ghost", {"ANTHROPIC_API_KEY"}, policy=sample_policy)


class TestLoadPolicyFromFile:
    """Tests for config-file loading."""

    def test_load_yaml(self, tmp_path: Path) -> None:
        path = tmp_path / "policy.yaml"
        path.write_text(
            """
enabled: true
known_keys:
  - ANTHROPIC_API_KEY
  - OPENAI_API_KEY
agents:
  backend-001:
    - ANTHROPIC_API_KEY
roles:
  researcher:
    - OPENAI_API_KEY
""".strip(),
            encoding="utf-8",
        )
        policy = load_policy_from_file(path)
        assert policy.enabled is True
        assert policy.known_keys == frozenset({"ANTHROPIC_API_KEY", "OPENAI_API_KEY"})
        assert policy.agent_rules["backend-001"] == frozenset({"ANTHROPIC_API_KEY"})
        assert policy.role_rules["researcher"] == frozenset({"OPENAI_API_KEY"})

    def test_load_json(self, tmp_path: Path) -> None:
        path = tmp_path / "policy.json"
        path.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "known_keys": ["ANTHROPIC_API_KEY"],
                    "agents": {"a-1": ["ANTHROPIC_API_KEY"]},
                }
            ),
            encoding="utf-8",
        )
        policy = load_policy_from_file(path)
        assert policy.enabled is True
        assert policy.agent_rules["a-1"] == frozenset({"ANTHROPIC_API_KEY"})

    def test_empty_file_ok(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.yaml"
        path.write_text("", encoding="utf-8")
        policy = load_policy_from_file(path)
        assert policy.enabled is False

    def test_load_rejects_undeclared_key(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text(
            "enabled: true\nknown_keys: [A]\nagents: {a-1: [B]}\n",
            encoding="utf-8",
        )
        with pytest.raises(UnknownCredentialKeyError):
            load_policy_from_file(path)


class TestBuildFilteredEnvIntegration:
    """End-to-end test: build_filtered_env honours the policy."""

    def test_scoped_agent_sees_only_allowed_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sample_policy: AgentCredentialPolicy,
    ) -> None:
        from bernstein.adapters.env_isolation import build_filtered_env

        monkeypatch.setenv("ANTHROPIC_API_KEY", "anth")
        monkeypatch.setenv("OPENAI_API_KEY", "oai")

        env = build_filtered_env(
            ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"],
            agent_id="backend-001",
            credential_policy=sample_policy,
        )
        assert env.get("ANTHROPIC_API_KEY") == "anth"
        assert "OPENAI_API_KEY" not in env

    def test_unscoped_agent_build_env_fails_closed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sample_policy: AgentCredentialPolicy,
    ) -> None:
        from bernstein.adapters.env_isolation import build_filtered_env

        monkeypatch.setenv("ANTHROPIC_API_KEY", "anth")
        with pytest.raises(AgentNotScopedError):
            build_filtered_env(
                ["ANTHROPIC_API_KEY"],
                agent_id="stranger-1",
                credential_policy=sample_policy,
            )

    def test_unknown_key_raises_during_build(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sample_policy: AgentCredentialPolicy,
    ) -> None:
        from bernstein.adapters.env_isolation import build_filtered_env

        monkeypatch.setenv("MYSTERY_KEY", "x")
        with pytest.raises(UnknownCredentialKeyError):
            build_filtered_env(
                ["MYSTERY_KEY"],
                agent_id="backend-001",
                credential_policy=sample_policy,
            )

    def test_no_agent_id_is_backward_compatible(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Legacy call sites without agent_id keep working unchanged."""
        from bernstein.adapters.env_isolation import build_filtered_env

        monkeypatch.setenv("ANTHROPIC_API_KEY", "anth")
        env = build_filtered_env(["ANTHROPIC_API_KEY"])
        assert env.get("ANTHROPIC_API_KEY") == "anth"
