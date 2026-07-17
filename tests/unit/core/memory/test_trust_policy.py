"""Tests for :mod:`bernstein.core.memory.trust_policy`.

``MemoryTrustPolicy`` is the primitive that enforces provenance on the
spawned-agent prompt path (see ``spawner_core._load_persistent_memory``).
These tests cover the policy in isolation: which rows it trusts by
attribute, how it filters a candidate list, and how the environment-derived
default is built.
"""

from __future__ import annotations

import pytest

from bernstein.core.memory.sqlite_store import MemoryEntry
from bernstein.core.memory.trust_policy import (
    TRUST_POLICY_ENABLED_ENV_VAR,
    TRUSTED_ADAPTERS_ENV_VAR,
    MemoryTrustPolicy,
    active_trust_policy,
)


def _entry(source_adapter: str | None, content: str = "x") -> MemoryEntry:
    return MemoryEntry(
        id=1,
        type="learning",
        content=content,
        tags=[],
        created_at=0.0,
        source_adapter=source_adapter,
    )


# ---------------------------------------------------------------------------
# is_trusted
# ---------------------------------------------------------------------------


class TestIsTrusted:
    def test_untagged_row_trusted_by_default(self) -> None:
        policy = MemoryTrustPolicy()
        assert policy.is_trusted(_entry(None)) is True

    def test_foreign_adapter_row_untrusted_by_default(self) -> None:
        policy = MemoryTrustPolicy()
        assert policy.is_trusted(_entry("some-other-adapter")) is False

    def test_explicitly_allow_listed_adapter_is_trusted(self) -> None:
        policy = MemoryTrustPolicy(trusted_adapters=frozenset({"claude-code"}))
        assert policy.is_trusted(_entry("claude-code")) is True
        assert policy.is_trusted(_entry("codex")) is False

    def test_disabled_policy_trusts_everything(self) -> None:
        policy = MemoryTrustPolicy(enabled=False)
        assert policy.is_trusted(_entry("anything")) is True
        assert policy.is_trusted(_entry(None)) is True

    def test_trust_untagged_false_distrusts_untagged_rows(self) -> None:
        policy = MemoryTrustPolicy(trust_untagged=False)
        assert policy.is_trusted(_entry(None)) is False


# ---------------------------------------------------------------------------
# filter_entries
# ---------------------------------------------------------------------------


class TestFilterEntries:
    def test_filter_preserves_order_and_drops_untrusted(self) -> None:
        policy = MemoryTrustPolicy(trusted_adapters=frozenset({"claude-code"}))
        entries = [
            _entry(None, "a"),
            _entry("attacker", "poison"),
            _entry("claude-code", "b"),
        ]
        kept = policy.filter_entries(entries)
        assert [e.content for e in kept] == ["a", "b"]

    def test_filter_empty_input_returns_empty(self) -> None:
        assert MemoryTrustPolicy().filter_entries([]) == []


# ---------------------------------------------------------------------------
# active_trust_policy - environment wiring
# ---------------------------------------------------------------------------


class TestActiveTrustPolicy:
    def test_default_env_yields_enabled_policy_with_no_extra_adapters(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(TRUST_POLICY_ENABLED_ENV_VAR, raising=False)
        monkeypatch.delenv(TRUSTED_ADAPTERS_ENV_VAR, raising=False)
        policy = active_trust_policy()
        assert policy.enabled is True
        assert policy.trusted_adapters == frozenset()
        assert policy.trust_untagged is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "FALSE", "Off"])
    def test_falsy_env_disables_policy(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv(TRUST_POLICY_ENABLED_ENV_VAR, value)
        assert active_trust_policy().enabled is False

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
    def test_truthy_env_keeps_policy_enabled(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv(TRUST_POLICY_ENABLED_ENV_VAR, value)
        assert active_trust_policy().enabled is True

    def test_trusted_adapters_env_var_is_parsed_as_comma_separated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(TRUSTED_ADAPTERS_ENV_VAR, "claude-code, codex ,gemini-cli")
        policy = active_trust_policy()
        assert policy.trusted_adapters == frozenset({"claude-code", "codex", "gemini-cli"})
