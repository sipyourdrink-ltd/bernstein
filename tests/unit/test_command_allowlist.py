"""Tests for SEC-015: Command allowlist per task scope."""

from __future__ import annotations

from bernstein.core.command_allowlist import (
    ScopeAllowlist,
    ScopeAllowlistConfig,
    check_command,
)


class TestScopeAllowlist:
    def test_frozen(self) -> None:
        al = ScopeAllowlist(scope="small")
        assert al.scope == "small"


class TestScopeAllowlistConfig:
    def test_defaults_have_small(self) -> None:
        config = ScopeAllowlistConfig()
        al = config.get_allowlist("small")
        assert al is not None
        assert al.scope == "small"

    def test_defaults_have_medium(self) -> None:
        config = ScopeAllowlistConfig()
        al = config.get_allowlist("medium")
        assert al is not None

    def test_defaults_have_large(self) -> None:
        config = ScopeAllowlistConfig()
        al = config.get_allowlist("large")
        assert al is not None

    def test_unknown_scope_returns_none(self) -> None:
        config = ScopeAllowlistConfig()
        assert config.get_allowlist("custom") is None

    def test_custom_scope_overrides(self) -> None:
        custom = ScopeAllowlist(scope="small", allowed_commands=("echo",))
        config = ScopeAllowlistConfig(scope_lists={"small": custom})
        al = config.get_allowlist("small")
        assert al is not None
        assert al.allowed_commands == ("echo",)


class TestCheckCommand:
    def test_small_scope_allows_ls(self) -> None:
        result = check_command("ls -la", scope="small")
        assert result.allowed

    def test_small_scope_allows_cat(self) -> None:
        result = check_command("cat src/main.py", scope="small")
        assert result.allowed

    def test_small_scope_denies_rm_rf(self) -> None:
        result = check_command("rm -rf /tmp/data", scope="small")
        assert not result.allowed

    def test_small_scope_denies_docker(self) -> None:
        result = check_command("docker run ubuntu", scope="small")
        assert not result.allowed

    def test_small_scope_allows_pytest(self) -> None:
        result = check_command("uv run pytest tests/", scope="small")
        assert result.allowed

    def test_small_scope_allows_git_status(self) -> None:
        result = check_command("git status", scope="small")
        assert result.allowed

    def test_small_scope_denies_git_push_force(self) -> None:
        result = check_command("git push --force origin main", scope="small")
        assert not result.allowed

    def test_medium_scope_allows_git(self) -> None:
        result = check_command("git commit -m 'fix'", scope="medium")
        assert result.allowed

    def test_medium_scope_allows_mkdir(self) -> None:
        result = check_command("mkdir -p src/new_module", scope="medium")
        assert result.allowed

    def test_medium_scope_denies_sudo(self) -> None:
        result = check_command("sudo rm -rf /", scope="medium")
        assert not result.allowed

    def test_large_scope_allows_most(self) -> None:
        result = check_command("docker build .", scope="large")
        assert result.allowed

    def test_large_scope_denies_rm_rf_root(self) -> None:
        result = check_command("rm -rf /", scope="large")
        assert not result.allowed

    def test_disabled_config_allows_all(self) -> None:
        config = ScopeAllowlistConfig(enabled=False)
        result = check_command("rm -rf /", scope="small", config=config)
        assert result.allowed

    def test_unknown_scope_allows_all(self) -> None:
        result = check_command("anything", scope="custom")
        assert result.allowed

    def test_not_in_allowlist_denied(self) -> None:
        result = check_command("kubectl delete pods", scope="small")
        assert not result.allowed

    def test_default_config_used(self) -> None:
        result = check_command("echo hello", scope="small")
        assert result.allowed

    def test_verdict_has_scope(self) -> None:
        result = check_command("ls", scope="small")
        assert result.scope == "small"

    def test_verdict_has_command(self) -> None:
        result = check_command("ls -la", scope="small")
        assert result.command == "ls -la"
