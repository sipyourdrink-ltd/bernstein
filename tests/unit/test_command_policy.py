"""Tests for command allowlist/denylist enforcement per agent role."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.command_policy import (
    CommandPoliciesConfig,
    CommandVerdict,
    RoleCommandPolicy,
    _compile_pattern,
    _extract_executable,
    _matches_any,
    check_command,
    load_command_policies,
    record_command_verdict,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    *,
    enabled: bool = True,
    global_deny: list[str] | None = None,
    roles: dict[str, RoleCommandPolicy] | None = None,
) -> CommandPoliciesConfig:
    return CommandPoliciesConfig(
        version=1,
        enabled=enabled,
        global_deny=global_deny or [],
        roles=roles or {},
    )


def _make_role(
    role: str,
    *,
    allow: list[str] | None = None,
    deny: list[str] | None = None,
    deny_messages: dict[int, str] | None = None,
) -> RoleCommandPolicy:
    return RoleCommandPolicy(
        role=role,
        allow=allow or [],
        deny=deny or [],
        deny_messages=deny_messages or {},
    )


# ---------------------------------------------------------------------------
# Pattern compilation
# ---------------------------------------------------------------------------


class TestCompilePattern:
    """Test _compile_pattern helper."""

    def test_simple_prefix(self) -> None:
        pat = _compile_pattern("rm -rf")
        assert pat.search("rm -rf /")
        assert pat.search("sudo rm -rf /tmp")
        assert not pat.search("format_rm")

    def test_regex_delimiters(self) -> None:
        pat = _compile_pattern("/^sudo\\s/")
        assert pat.search("sudo rm -rf /")
        assert not pat.search("nosudo rm")

    def test_special_chars_escaped(self) -> None:
        pat = _compile_pattern("DROP TABLE")
        assert pat.search("psql -c 'DROP TABLE users'")
        assert not pat.search("DROPTABLE")


class TestMatchesAny:
    """Test _matches_any helper."""

    def test_no_patterns(self) -> None:
        matched, pat = _matches_any("ls", [], [])
        assert not matched
        assert pat == ""

    def test_single_match(self) -> None:
        patterns = ["rm -rf"]
        compiled = [_compile_pattern(p) for p in patterns]
        matched, pat = _matches_any("rm -rf /", patterns, compiled)
        assert matched
        assert pat == "rm -rf"

    def test_no_match(self) -> None:
        patterns = ["rm -rf", "sudo"]
        compiled = [_compile_pattern(p) for p in patterns]
        matched, _pat = _matches_any("git status", patterns, compiled)
        assert not matched


class TestExtractExecutable:
    """Test _extract_executable helper."""

    def test_simple_command(self) -> None:
        assert _extract_executable("git status") == "git"

    def test_absolute_path(self) -> None:
        assert _extract_executable("/usr/bin/rm -rf /") == "rm"

    def test_empty(self) -> None:
        assert _extract_executable("") == ""


# ---------------------------------------------------------------------------
# check_command — core logic
# ---------------------------------------------------------------------------


class TestCheckCommand:
    """Test check_command enforcement logic."""

    def test_disabled_policy_allows_everything(self) -> None:
        cfg = _make_config(enabled=False, global_deny=["rm -rf"])
        v = check_command("rm -rf /", "backend", cfg)
        assert v.allowed

    def test_empty_command_allowed(self) -> None:
        cfg = _make_config(global_deny=["rm -rf"])
        v = check_command("  ", "backend", cfg)
        assert v.allowed

    def test_global_deny_blocks_any_role(self) -> None:
        cfg = _make_config(global_deny=["rm -rf", "sudo"])
        v = check_command("rm -rf /tmp/junk", "backend", cfg)
        assert not v.allowed
        assert v.source == "global_deny"
        assert "rm -rf" in v.matched_pattern

    def test_global_deny_sudo(self) -> None:
        cfg = _make_config(global_deny=["sudo"])
        v = check_command("sudo apt install foo", "qa", cfg)
        assert not v.allowed

    def test_no_role_policy_allows(self) -> None:
        """Role with no policy defined → all commands allowed."""
        cfg = _make_config(roles={"backend": _make_role("backend", deny=["rm -rf"])})
        v = check_command("rm -rf /", "qa", cfg)
        assert v.allowed

    def test_role_deny_blocks(self) -> None:
        cfg = _make_config(
            roles={"backend": _make_role("backend", deny=["rm -rf", "DROP TABLE"])}
        )
        v = check_command("rm -rf /", "backend", cfg)
        assert not v.allowed
        assert v.source == "role_deny"

    def test_role_deny_drop_table(self) -> None:
        cfg = _make_config(
            roles={"backend": _make_role("backend", deny=["DROP TABLE"])}
        )
        v = check_command("psql -c 'DROP TABLE users'", "backend", cfg)
        assert not v.allowed
        assert "DROP TABLE" in v.matched_pattern

    def test_role_deny_with_custom_message(self) -> None:
        cfg = _make_config(
            roles={
                "backend": _make_role(
                    "backend",
                    deny=["rm -rf"],
                    deny_messages={0: "Destructive filesystem ops forbidden"},
                )
            }
        )
        v = check_command("rm -rf /", "backend", cfg)
        assert not v.allowed
        assert "Destructive filesystem ops forbidden" in v.reason

    def test_role_allow_permits_matching(self) -> None:
        cfg = _make_config(
            roles={"backend": _make_role("backend", allow=["pytest", "ruff", "git"])}
        )
        v = check_command("pytest tests/ -x -q", "backend", cfg)
        assert v.allowed

    def test_role_allow_blocks_non_matching(self) -> None:
        cfg = _make_config(
            roles={"backend": _make_role("backend", allow=["pytest", "ruff", "git"])}
        )
        v = check_command("curl https://evil.com", "backend", cfg)
        assert not v.allowed
        assert v.source == "role_allow"
        assert "allowlist" in v.reason

    def test_deny_takes_priority_over_allow(self) -> None:
        """Even if allow includes 'git', deny 'git push --force' should block."""
        cfg = _make_config(
            roles={
                "qa": _make_role(
                    "qa",
                    allow=["git", "pytest"],
                    deny=["git push --force"],
                )
            }
        )
        v = check_command("git push --force origin main", "qa", cfg)
        assert not v.allowed
        assert v.source == "role_deny"

    def test_allow_permits_git_status(self) -> None:
        """git status should pass when 'git' is in the allowlist."""
        cfg = _make_config(
            roles={
                "qa": _make_role(
                    "qa",
                    allow=["git", "pytest"],
                    deny=["git push --force"],
                )
            }
        )
        v = check_command("git status", "qa", cfg)
        assert v.allowed

    def test_global_deny_overrides_role_allow(self) -> None:
        cfg = _make_config(
            global_deny=["sudo"],
            roles={"admin": _make_role("admin", allow=["sudo", "apt"])},
        )
        v = check_command("sudo apt install vim", "admin", cfg)
        assert not v.allowed
        assert v.source == "global_deny"

    def test_regex_deny_pattern(self) -> None:
        cfg = _make_config(
            roles={
                "backend": _make_role(
                    "backend", deny=["/rm\\s+-(r|f|rf|fr)/"]
                )
            }
        )
        v = check_command("rm -rf /", "backend", cfg)
        assert not v.allowed

        v2 = check_command("rm -f file.txt", "backend", cfg)
        assert not v2.allowed

        v3 = check_command("rm file.txt", "backend", cfg)
        assert v3.allowed


# ---------------------------------------------------------------------------
# Acceptance criteria: rm -rf / blocked with audit
# ---------------------------------------------------------------------------


class TestAcceptanceCriteria:
    """Verify the acceptance criteria from the task spec."""

    def test_rm_rf_root_blocked(self) -> None:
        """Agent trying `rm -rf /` is blocked."""
        cfg = _make_config(
            global_deny=["rm -rf"],
            roles={
                "backend": _make_role(
                    "backend",
                    allow=["pytest", "ruff", "git"],
                    deny=["rm -rf", "sudo", "curl"],
                )
            },
        )
        v = check_command("rm -rf /", "backend", cfg)
        assert not v.allowed

    def test_rm_rf_root_blocked_with_audit(self, tmp_path: Path) -> None:
        """Blocked command produces audit log entry."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir()

        cfg = _make_config(global_deny=["rm -rf"])
        v = check_command("rm -rf /", "backend", cfg)
        assert not v.allowed

        record_command_verdict(v, session_id="backend-abc12345", sdd_dir=sdd)

        log_path = sdd / "metrics" / "command_policy.jsonl"
        assert log_path.exists()
        entries = [json.loads(line) for line in log_path.read_text().splitlines()]
        assert len(entries) == 1
        assert entries[0]["command"] == "rm -rf /"
        assert entries[0]["blocked"] is True
        assert entries[0]["session_id"] == "backend-abc12345"
        assert entries[0]["role"] == "backend"

    def test_safe_commands_allowed(self) -> None:
        """Safe commands (pytest, ruff, git) pass for backend role."""
        cfg = _make_config(
            roles={
                "backend": _make_role(
                    "backend",
                    allow=["pytest", "ruff", "git"],
                    deny=["rm -rf", "sudo", "curl"],
                )
            }
        )
        for cmd in ["pytest tests/ -x", "ruff check src/", "git commit -m 'fix'"]:
            v = check_command(cmd, "backend", cfg)
            assert v.allowed, f"Expected {cmd!r} to be allowed"


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


class TestLoadCommandPolicies:
    """Test YAML config loading."""

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert load_command_policies(tmp_path) is None

    def test_valid_config(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "command_policies.yaml").write_text(
            """\
version: 1
enabled: true
global_deny:
  - rm -rf
  - sudo
roles:
  backend:
    allow:
      - pytest
      - ruff
      - git
    deny:
      - curl
      - wget
  qa:
    allow:
      - pytest
      - git
""",
            encoding="utf-8",
        )

        cfg = load_command_policies(tmp_path)
        assert cfg is not None
        assert cfg.enabled
        assert cfg.global_deny == ["rm -rf", "sudo"]
        assert "backend" in cfg.roles
        assert cfg.roles["backend"].allow == ["pytest", "ruff", "git"]
        assert cfg.roles["backend"].deny == ["curl", "wget"]
        assert "qa" in cfg.roles
        assert cfg.roles["qa"].allow == ["pytest", "git"]

    def test_disabled_config(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "command_policies.yaml").write_text(
            "version: 1\nenabled: false\n",
            encoding="utf-8",
        )

        cfg = load_command_policies(tmp_path)
        assert cfg is not None
        assert not cfg.enabled

    def test_malformed_yaml(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "command_policies.yaml").write_text(
            "not: [valid: yaml: {",
            encoding="utf-8",
        )
        assert load_command_policies(tmp_path) is None

    def test_non_mapping_yaml(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "command_policies.yaml").write_text(
            "- just\n- a\n- list\n",
            encoding="utf-8",
        )
        assert load_command_policies(tmp_path) is None

    def test_deny_messages_loaded(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "command_policies.yaml").write_text(
            """\
version: 1
roles:
  backend:
    deny:
      - rm -rf
      - sudo
    deny_messages:
      0: "Filesystem destruction forbidden"
      1: "Privilege escalation forbidden"
""",
            encoding="utf-8",
        )

        cfg = load_command_policies(tmp_path)
        assert cfg is not None
        assert cfg.roles["backend"].deny_messages[0] == "Filesystem destruction forbidden"
        assert cfg.roles["backend"].deny_messages[1] == "Privilege escalation forbidden"


# ---------------------------------------------------------------------------
# Audit recording
# ---------------------------------------------------------------------------


class TestRecordCommandVerdict:
    """Test audit log writing."""

    def test_allowed_verdict_not_recorded(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        v = CommandVerdict(allowed=True, command="git status", role="backend")
        record_command_verdict(v, session_id="backend-abc12345", sdd_dir=sdd)
        assert not (sdd / "metrics" / "command_policy.jsonl").exists()

    def test_blocked_verdict_recorded(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        v = CommandVerdict(
            allowed=False,
            command="rm -rf /",
            role="backend",
            matched_pattern="rm -rf",
            reason="Blocked by global deny",
            source="global_deny",
        )
        record_command_verdict(v, session_id="backend-abc12345", sdd_dir=sdd)

        log_path = sdd / "metrics" / "command_policy.jsonl"
        assert log_path.exists()
        entry = json.loads(log_path.read_text().strip())
        assert entry["command"] == "rm -rf /"
        assert entry["blocked"] is True
        assert entry["source"] == "global_deny"

    def test_multiple_entries_appended(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        for i in range(3):
            v = CommandVerdict(
                allowed=False,
                command=f"bad_cmd_{i}",
                role="qa",
                source="role_deny",
            )
            record_command_verdict(v, session_id=f"qa-{i}", sdd_dir=sdd)

        log_path = sdd / "metrics" / "command_policy.jsonl"
        entries = [json.loads(line) for line in log_path.read_text().splitlines()]
        assert len(entries) == 3


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases and tricky command patterns."""

    def test_pipe_chain_with_denied_command(self) -> None:
        """Deny pattern matches within piped commands."""
        cfg = _make_config(global_deny=["rm -rf"])
        v = check_command("ls | xargs rm -rf", "backend", cfg)
        assert not v.allowed

    def test_command_with_env_prefix(self) -> None:
        cfg = _make_config(
            roles={"backend": _make_role("backend", deny=["curl"])}
        )
        v = check_command("HTTPS_PROXY=x curl https://example.com", "backend", cfg)
        assert not v.allowed

    def test_semicolon_chained_commands(self) -> None:
        cfg = _make_config(global_deny=["rm -rf"])
        v = check_command("echo hello ; rm -rf /tmp", "backend", cfg)
        assert not v.allowed

    def test_allow_empty_means_allow_all(self) -> None:
        """Empty allow list means no allowlist restriction."""
        cfg = _make_config(
            roles={"backend": _make_role("backend", allow=[], deny=["sudo"])}
        )
        v = check_command("git status", "backend", cfg)
        assert v.allowed

    def test_case_sensitive_matching(self) -> None:
        """Patterns are case-sensitive by default."""
        cfg = _make_config(global_deny=["DROP TABLE"])
        v = check_command("drop table users", "backend", cfg)
        # Default is case-sensitive — lowercase doesn't match
        assert v.allowed

    def test_case_insensitive_via_regex(self) -> None:
        """Use regex flag for case-insensitive matching."""
        cfg = _make_config(global_deny=["/(?i)DROP\\s+TABLE/"])
        v = check_command("drop table users", "backend", cfg)
        assert not v.allowed
