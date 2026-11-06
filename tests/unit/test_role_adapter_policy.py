"""Tests for the per-role adapter deny-list policy.

Coverage:

* Empty policy is back-compat - every role/adapter pair allowed.
* Non-empty allow-list rejects any adapter not on the list.
* Deny emits a structured ``role.adapter.denied`` event into the audit log.
* Policy roundtrips through JSON disk persistence.
* CLI ``show`` / ``set`` / ``test`` produce expected output and exit codes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.role_adapter_policy_cmd import security_group
from bernstein.core.security.audit import AuditLog
from bernstein.core.security.role_adapter_policy import (
    ADAPTER_DENY_EVENT_TYPE,
    DEFAULT_POLICY_PATH,
    PolicyFileCorrupt,
    RoleAdapterDenied,
    RolePolicy,
    check,
    enforce,
    get_policy,
    load_policy_file,
    reset_policy,
    save_policy_file,
    set_policy,
)


@pytest.fixture(autouse=True)
def _reset_policy_each_test():
    """Reset the global policy after every test to avoid cross-test bleed."""
    yield
    reset_policy()


# ---------------------------------------------------------------------------
# Default semantics - back-compat
# ---------------------------------------------------------------------------


class TestEmptyPolicyBackCompat:
    """Empty allow-list = unrestricted (no behaviour change for existing users)."""

    def test_empty_policy_allows_everything(self) -> None:
        # No global state mutation.
        assert check("backend", "claude") is True
        assert check("security", "claude_routine") is True
        assert check("docs", "anything") is True

    def test_role_with_empty_tuple_is_unrestricted(self) -> None:
        policy = RolePolicy(per_role_allowlists={"backend": ()})
        assert policy.is_allowed("backend", "anything")

    def test_unknown_role_is_unrestricted(self) -> None:
        policy = RolePolicy(per_role_allowlists={"security": ("claude",)})
        # ``backend`` was not configured; allow everything.
        assert policy.is_allowed("backend", "claude_routine")


# ---------------------------------------------------------------------------
# Allow-list enforcement
# ---------------------------------------------------------------------------


class TestAllowList:
    """Configured allow-list strictly limits adapter spawn."""

    def test_allowed_adapter_passes(self) -> None:
        policy = RolePolicy(per_role_allowlists={"security": ("claude", "aider")})
        assert policy.is_allowed("security", "claude")
        assert policy.is_allowed("security", "aider")

    def test_denied_adapter_blocked(self) -> None:
        policy = RolePolicy(per_role_allowlists={"security": ("claude",)})
        assert not policy.is_allowed("security", "claude_routine")

    def test_enforce_raises_on_deny(self) -> None:
        policy = RolePolicy(per_role_allowlists={"security": ("claude",)})
        with pytest.raises(RoleAdapterDenied) as excinfo:
            enforce("security", "claude_routine", policy=policy)
        assert excinfo.value.role == "security"
        assert excinfo.value.adapter == "claude_routine"
        assert excinfo.value.allowed == ("claude",)

    def test_enforce_silent_on_allow(self) -> None:
        policy = RolePolicy(per_role_allowlists={"security": ("claude",)})
        # No exception expected.
        enforce("security", "claude", policy=policy)


# ---------------------------------------------------------------------------
# Audit-event emission
# ---------------------------------------------------------------------------


class TestAuditEmission:
    """Deny path writes a ``role.adapter.denied`` event into the chain."""

    def test_deny_emits_audit_event(self, tmp_path: Path) -> None:
        audit_dir = tmp_path / ".sdd" / "audit"
        audit_dir.mkdir(parents=True)
        log = AuditLog(audit_dir, key=b"a" * 32)

        policy = RolePolicy(per_role_allowlists={"security": ("claude",)})
        with pytest.raises(RoleAdapterDenied):
            enforce("security", "claude_routine", audit_log=log, policy=policy)

        # The audit dir has a single jsonl file with one event.
        files = sorted(audit_dir.glob("*.jsonl"))
        assert len(files) == 1
        events = [json.loads(line) for line in files[0].read_text().splitlines() if line.strip()]
        assert len(events) == 1
        ev = events[0]
        assert ev["event_type"] == ADAPTER_DENY_EVENT_TYPE
        assert ev["resource_type"] == "adapter"
        assert ev["resource_id"] == "claude_routine"
        assert ev["details"]["role"] == "security"
        assert ev["details"]["adapter"] == "claude_routine"
        assert ev["details"]["allowed_adapters"] == ["claude"]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TestPersistence:
    """Disk format roundtrips through ``load`` / ``save``."""

    def test_save_and_load(self, tmp_path: Path) -> None:
        path = tmp_path / "policy.json"
        original = RolePolicy(per_role_allowlists={"security": ("claude", "aider"), "docs": ("mock",)})
        save_policy_file(original, path)
        loaded = load_policy_file(path)
        assert loaded.allowed_for("security") == ("aider", "claude")  # sorted
        assert loaded.allowed_for("docs") == ("mock",)

    def test_missing_file_yields_empty_policy(self, tmp_path: Path) -> None:
        loaded = load_policy_file(tmp_path / "does-not-exist.json")
        assert loaded.per_role_allowlists == {}

    def test_malformed_file_raises_policy_file_corrupt(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("{not valid json")
        with pytest.raises(PolicyFileCorrupt):
            load_policy_file(path)

    def test_from_dict_normalises_lists(self) -> None:
        policy = RolePolicy.from_dict({"security": ["claude", "aider", "claude"]})
        # Dedup + sort.
        assert policy.allowed_for("security") == ("aider", "claude")

    def test_from_dict_handles_non_list(self) -> None:
        policy = RolePolicy.from_dict({"security": "claude"})  # type: ignore[dict-item]
        # Treated as no-op for the role.
        assert policy.allowed_for("security") == ()


# ---------------------------------------------------------------------------
# Global accessor
# ---------------------------------------------------------------------------


class TestGlobalAccessor:
    """get_policy / set_policy / reset_policy lifecycle."""

    def test_default_policy_is_empty(self) -> None:
        assert get_policy().per_role_allowlists == {}

    def test_set_returns_previous(self) -> None:
        new_policy = RolePolicy(per_role_allowlists={"x": ("y",)})
        previous = set_policy(new_policy)
        assert previous.per_role_allowlists == {}
        assert get_policy() is new_policy

    def test_reset(self) -> None:
        set_policy(RolePolicy(per_role_allowlists={"x": ("y",)}))
        reset_policy()
        assert get_policy().per_role_allowlists == {}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCLI:
    """End-to-end click runner round-trip."""

    def test_show_empty(self, tmp_path: Path) -> None:
        runner = CliRunner()
        path = tmp_path / "policy.json"
        result = runner.invoke(security_group, ["role-adapter-policy", "show", "--policy-file", str(path)])
        assert result.exit_code == 0, result.output
        assert "empty" in result.output

    def test_set_then_show(self, tmp_path: Path) -> None:
        runner = CliRunner()
        path = tmp_path / "policy.json"

        result = runner.invoke(
            security_group,
            [
                "role-adapter-policy",
                "set",
                "--role",
                "security",
                "--allow",
                "claude",
                "--allow",
                "aider",
                "--policy-file",
                str(path),
            ],
        )
        assert result.exit_code == 0, result.output

        result = runner.invoke(security_group, ["role-adapter-policy", "show", "--policy-file", str(path)])
        assert result.exit_code == 0
        assert "security" in result.output
        assert "claude" in result.output
        assert "aider" in result.output

    def test_set_clear(self, tmp_path: Path) -> None:
        """``set --role X`` (no --allow) clears the allow-list."""
        runner = CliRunner()
        path = tmp_path / "policy.json"

        # Seed.
        runner.invoke(
            security_group,
            ["role-adapter-policy", "set", "--role", "security", "--allow", "claude", "--policy-file", str(path)],
        )
        # Clear.
        result = runner.invoke(
            security_group,
            ["role-adapter-policy", "set", "--role", "security", "--policy-file", str(path)],
        )
        assert result.exit_code == 0
        assert "cleared" in result.output

        loaded = load_policy_file(path)
        assert loaded.allowed_for("security") == ()

    def test_test_allow_path(self, tmp_path: Path) -> None:
        runner = CliRunner()
        path = tmp_path / "policy.json"
        runner.invoke(
            security_group,
            ["role-adapter-policy", "set", "--role", "security", "--allow", "claude", "--policy-file", str(path)],
        )
        result = runner.invoke(
            security_group,
            ["role-adapter-policy", "test", "--role", "security", "--adapter", "claude", "--policy-file", str(path)],
        )
        assert result.exit_code == 0
        assert "ALLOW" in result.output

    def test_test_deny_path(self, tmp_path: Path) -> None:
        runner = CliRunner()
        path = tmp_path / "policy.json"
        runner.invoke(
            security_group,
            ["role-adapter-policy", "set", "--role", "security", "--allow", "claude", "--policy-file", str(path)],
        )
        result = runner.invoke(
            security_group,
            [
                "role-adapter-policy",
                "test",
                "--role",
                "security",
                "--adapter",
                "claude_routine",
                "--policy-file",
                str(path),
            ],
        )
        assert result.exit_code == 1
        assert "DENY" in result.output


# ---------------------------------------------------------------------------
# Default-path constant invariant
# ---------------------------------------------------------------------------


def test_default_policy_path_under_sdd() -> None:
    """The default path must live under .sdd/security/ to match RBAC siblings."""
    assert str(DEFAULT_POLICY_PATH).startswith(".sdd/")
    assert "security" in str(DEFAULT_POLICY_PATH)
