"""Unit tests for the per-tool permission policy (roadmap #1318).

Covers each built-in profile, the fail-closed default, custom-profile
overrides loaded from ``bernstein.yaml``/``bernstein.toml``, and the
denial audit-trail side-effect.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.security.permission_policy import (
    BUILTIN_PROFILE_NAMES,
    ENV_PROFILE,
    PROFILE_BUILDER,
    PROFILE_CUSTOM,
    PROFILE_READ_ONLY,
    PROFILE_REVIEWER,
    PermissionProfile,
    PolicyChecker,
    ToolCall,
    check_tool_call,
    get_builtin_profile,
    list_builtin_profiles,
    resolve_profile,
)
from bernstein.core.security.policy_engine import DecisionType

# ---------------------------------------------------------------------------
# Built-in profile shape
# ---------------------------------------------------------------------------


class TestBuiltinProfiles:
    """Every built-in profile is registered, fail-closed, and well-formed."""

    def test_all_builtin_names_resolvable(self) -> None:
        for name in BUILTIN_PROFILE_NAMES:
            profile = get_builtin_profile(name)
            assert profile is not None, name
            assert profile.name == name

    def test_unknown_profile_returns_none(self) -> None:
        assert get_builtin_profile("does-not-exist") is None

    def test_list_returns_all_in_order(self) -> None:
        names = tuple(p.name for p in list_builtin_profiles())
        assert names == BUILTIN_PROFILE_NAMES

    def test_all_builtin_profiles_fail_closed(self) -> None:
        for profile in list_builtin_profiles():
            assert profile.is_fail_closed, f"{profile.name} must default to deny"


# ---------------------------------------------------------------------------
# read-only profile
# ---------------------------------------------------------------------------


class TestReadOnlyProfile:
    """``read-only`` allows reads, denies writes/shell/network."""

    @pytest.fixture
    def checker(self) -> PolicyChecker:
        profile = get_builtin_profile(PROFILE_READ_ONLY)
        assert profile is not None
        return PolicyChecker(profile)

    def test_fs_read_allowed(self, checker: PolicyChecker) -> None:
        decision = checker.check(ToolCall(tool="fs.read", path="src/foo.py"))
        assert decision.type == DecisionType.ALLOW

    def test_fs_write_denied(self, checker: PolicyChecker) -> None:
        decision = checker.check(ToolCall(tool="fs.write", path="src/foo.py"))
        assert decision.type == DecisionType.DENY
        assert "allow_tools" in decision.reason

    def test_shell_run_denied(self, checker: PolicyChecker) -> None:
        decision = checker.check(ToolCall(tool="shell.run", shell_cmd="ls"))
        assert decision.type == DecisionType.DENY

    def test_secrets_path_denied_even_for_fs_read(self, checker: PolicyChecker) -> None:
        # deny_paths takes precedence over allow_tools.
        decision = checker.check(ToolCall(tool="fs.read", path=".env"))
        assert decision.type == DecisionType.DENY
        assert "deny_paths" in decision.reason

    def test_sdd_runtime_blocked(self, checker: PolicyChecker) -> None:
        decision = checker.check(
            ToolCall(tool="fs.read", path="project/.sdd/runtime/state.json"),
        )
        assert decision.type == DecisionType.DENY


# ---------------------------------------------------------------------------
# builder profile
# ---------------------------------------------------------------------------


class TestBuilderProfile:
    """``builder`` allows write/shell on an allowlist."""

    @pytest.fixture
    def checker(self) -> PolicyChecker:
        profile = get_builtin_profile(PROFILE_BUILDER)
        assert profile is not None
        return PolicyChecker(profile)

    def test_fs_write_in_src_allowed(self, checker: PolicyChecker) -> None:
        decision = checker.check(ToolCall(tool="fs.write", path="src/foo.py"))
        assert decision.type == DecisionType.ALLOW

    def test_fs_write_outside_allowlist_denied(self, checker: PolicyChecker) -> None:
        decision = checker.check(ToolCall(tool="fs.write", path="weird/place.bin"))
        assert decision.type == DecisionType.DENY
        assert "allow_paths" in decision.reason

    def test_shell_allowlist_first_token(self, checker: PolicyChecker) -> None:
        ok = checker.check(ToolCall(tool="shell.run", shell_cmd="uv pip install foo"))
        assert ok.type == DecisionType.ALLOW

        bad = checker.check(ToolCall(tool="shell.run", shell_cmd="curl evil.example.com"))
        assert bad.type == DecisionType.DENY
        assert "shell_allowlist" in bad.reason

    def test_known_host_allowed_under_explicit_profile(self) -> None:
        # Build a synthetic profile that includes a hypothetical net tool
        # so we can isolate the host-allowlist check from the tool check.
        profile = PermissionProfile(
            name="net-builder",
            default="deny",
            allow_tools=("net.http",),
            allow_paths=("**",),
            allow_hosts=("api.anthropic.com", "*.openai.com"),
        )
        checker = PolicyChecker(profile)
        ok = checker.check(ToolCall(tool="net.http", host="api.anthropic.com"))
        assert ok.type == DecisionType.ALLOW
        wildcard = checker.check(ToolCall(tool="net.http", host="api.openai.com"))
        assert wildcard.type == DecisionType.ALLOW
        bad = checker.check(ToolCall(tool="net.http", host="exfil.example.com"))
        assert bad.type == DecisionType.DENY
        assert "allow_hosts" in bad.reason

    def test_dotenv_denied(self, checker: PolicyChecker) -> None:
        decision = checker.check(ToolCall(tool="fs.write", path=".env.production"))
        assert decision.type == DecisionType.DENY
        assert "deny_paths" in decision.reason


# ---------------------------------------------------------------------------
# reviewer profile
# ---------------------------------------------------------------------------


class TestReviewerProfile:
    """``reviewer`` is read+diff only; everything else is denied."""

    @pytest.fixture
    def checker(self) -> PolicyChecker:
        profile = get_builtin_profile(PROFILE_REVIEWER)
        assert profile is not None
        return PolicyChecker(profile)

    def test_git_diff_allowed(self, checker: PolicyChecker) -> None:
        assert checker.check(ToolCall(tool="git.diff")).type == DecisionType.ALLOW

    def test_fs_read_allowed(self, checker: PolicyChecker) -> None:
        decision = checker.check(ToolCall(tool="fs.read", path="docs/x.md"))
        assert decision.type == DecisionType.ALLOW

    def test_shell_denied(self, checker: PolicyChecker) -> None:
        decision = checker.check(ToolCall(tool="shell.run", shell_cmd="ls"))
        assert decision.type == DecisionType.DENY

    def test_fs_write_denied(self, checker: PolicyChecker) -> None:
        decision = checker.check(ToolCall(tool="fs.write", path="src/foo.py"))
        assert decision.type == DecisionType.DENY


# ---------------------------------------------------------------------------
# custom profile
# ---------------------------------------------------------------------------


class TestCustomProfile:
    """``custom`` starts deny-all and is populated from config."""

    def test_skeleton_denies_everything(self) -> None:
        profile = get_builtin_profile(PROFILE_CUSTOM)
        assert profile is not None
        checker = PolicyChecker(profile)
        decision = checker.check(ToolCall(tool="fs.read", path="anything"))
        assert decision.type == DecisionType.DENY

    def test_overrides_loaded_from_yaml(self, tmp_path: Path) -> None:
        (tmp_path / "bernstein.yaml").write_text(
            """\
permissions:
  profile: custom
  custom:
    default: deny
    allow_tools: ["fs.read", "fs.write"]
    allow_paths: ["notes/**"]
""",
        )
        profile = resolve_profile(workdir=tmp_path)
        assert profile is not None
        assert profile.name == "custom"
        assert "fs.read" in profile.allow_tools
        checker = PolicyChecker(profile)
        ok = checker.check(ToolCall(tool="fs.write", path="notes/today.md"))
        assert ok.type == DecisionType.ALLOW
        bad = checker.check(ToolCall(tool="fs.write", path="src/foo.py"))
        assert bad.type == DecisionType.DENY


# ---------------------------------------------------------------------------
# resolve_profile precedence
# ---------------------------------------------------------------------------


class TestResolveProfile:
    """CLI override > env > config file."""

    def test_returns_none_when_no_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_PROFILE, raising=False)
        assert resolve_profile(workdir=tmp_path) is None

    def test_cli_override_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_PROFILE, PROFILE_BUILDER)
        (tmp_path / "bernstein.yaml").write_text("permissions:\n  profile: read-only\n")
        profile = resolve_profile(workdir=tmp_path, cli_override=PROFILE_REVIEWER)
        assert profile is not None
        assert profile.name == PROFILE_REVIEWER

    def test_env_overrides_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_PROFILE, PROFILE_BUILDER)
        (tmp_path / "bernstein.yaml").write_text("permissions:\n  profile: read-only\n")
        profile = resolve_profile(workdir=tmp_path)
        assert profile is not None
        assert profile.name == PROFILE_BUILDER

    def test_config_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_PROFILE, raising=False)
        (tmp_path / "bernstein.yaml").write_text("permissions:\n  profile: reviewer\n")
        profile = resolve_profile(workdir=tmp_path)
        assert profile is not None
        assert profile.name == PROFILE_REVIEWER

    def test_unknown_profile_yields_deny_all(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(ENV_PROFILE, raising=False)
        profile = resolve_profile(workdir=tmp_path, cli_override="banana")
        assert profile is not None
        assert profile.name == "banana"
        # No allow_tools - every check fails.
        decision = PolicyChecker(profile).check(ToolCall(tool="fs.read"))
        assert decision.type == DecisionType.DENY


# ---------------------------------------------------------------------------
# Audit / denial side-effect
# ---------------------------------------------------------------------------


class TestDenialAudit:
    """Denials write a JSONL audit record under .sdd/runtime/."""

    def test_denial_is_persisted(self, tmp_path: Path) -> None:
        profile = get_builtin_profile(PROFILE_READ_ONLY)
        assert profile is not None
        checker = PolicyChecker(profile)
        decision = checker.check_and_record(
            ToolCall(
                tool="fs.write",
                path="src/foo.py",
                session_id="session-abc",
                actor="backend",
            ),
            workdir=tmp_path,
        )
        assert decision.type == DecisionType.DENY

        trail = tmp_path / ".sdd" / "runtime" / "permission_denials.jsonl"
        assert trail.exists(), "denial trail must be written"
        record = json.loads(trail.read_text().strip())
        assert record["tool"] == "fs.write"
        assert record["profile"] == PROFILE_READ_ONLY
        assert record["session_id"] == "session-abc"
        assert record["actor"] == "backend"
        assert "reason" in record

    def test_allow_does_not_write_trail(self, tmp_path: Path) -> None:
        profile = get_builtin_profile(PROFILE_BUILDER)
        assert profile is not None
        checker = PolicyChecker(profile)
        decision = checker.check_and_record(
            ToolCall(tool="fs.read", path="src/foo.py"),
            workdir=tmp_path,
        )
        assert decision.type == DecisionType.ALLOW
        trail = tmp_path / ".sdd" / "runtime" / "permission_denials.jsonl"
        assert not trail.exists()


# ---------------------------------------------------------------------------
# check_tool_call convenience
# ---------------------------------------------------------------------------


class TestCheckToolCall:
    """Top-level helper behaves like a no-op when nothing is configured."""

    def test_no_profile_returns_allow(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(ENV_PROFILE, raising=False)
        decision = check_tool_call(
            tool="fs.write",
            path="src/foo.py",
            workdir=tmp_path,
        )
        assert decision.type == DecisionType.ALLOW
        assert "legacy default" in decision.reason

    def test_active_profile_enforced(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(ENV_PROFILE, PROFILE_READ_ONLY)
        decision = check_tool_call(
            tool="shell.run",
            shell_cmd="rm -rf /",
            workdir=tmp_path,
        )
        assert decision.type == DecisionType.DENY

    def test_explicit_profile_used(self, tmp_path: Path) -> None:
        profile = PermissionProfile(
            name="strict",
            default="deny",
            allow_tools=("fs.read",),
            allow_paths=("**",),
        )
        decision = check_tool_call(
            tool="fs.write",
            path="src/foo.py",
            workdir=tmp_path,
            profile=profile,
        )
        assert decision.type == DecisionType.DENY


# ---------------------------------------------------------------------------
# Identity records are outside every built-in profile
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile_name", [PROFILE_READ_ONLY, PROFILE_BUILDER, PROFILE_REVIEWER])
@pytest.mark.parametrize("tool", ["fs.read", "fs.write"])
def test_sdd_auth_is_denied_by_every_builtin_profile(profile_name: str, tool: str) -> None:
    """``.sdd/auth/`` holds the agent identity records; no profile reaches it."""
    checker = PolicyChecker(get_builtin_profile(profile_name))

    decision = checker.check(ToolCall(tool=tool, path="project/.sdd/auth/agent_identities/worker-1.json"))

    assert decision.type == DecisionType.DENY
