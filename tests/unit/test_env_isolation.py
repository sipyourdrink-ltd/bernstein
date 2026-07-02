"""Unit tests for the env-var isolation utility."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from bernstein.adapters.env_isolation import _BASE_ALLOWLIST, build_filtered_env

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# build_filtered_env - unit tests
# ---------------------------------------------------------------------------


class TestBuildFilteredEnv:
    """build_filtered_env returns exactly the allowed variables."""

    def test_base_vars_always_included(self) -> None:
        """PATH, HOME, LANG are always passed when present in os.environ."""
        fake_env = {"PATH": "/usr/bin", "HOME": "/root", "LANG": "en_US.UTF-8"}
        with patch("bernstein.adapters.env_isolation.os.environ", fake_env):
            result = build_filtered_env()
        assert result["PATH"] == "/usr/bin"
        assert result["HOME"] == "/root"
        assert result["LANG"] == "en_US.UTF-8"

    def test_secrets_excluded(self) -> None:
        """Database credentials, CI tokens and unrelated keys are stripped."""
        sensitive = {
            "DATABASE_URL": "postgres://user:pass@host/db",
            "AWS_SECRET_ACCESS_KEY": "s3cr3t",
            "CI_TOKEN": "abc123",
            "SLACK_WEBHOOK": "https://hooks.slack.com/...",
            "PATH": "/usr/bin",
            "HOME": "/home/user",
        }
        with patch("bernstein.adapters.env_isolation.os.environ", sensitive):
            result = build_filtered_env()
        assert "DATABASE_URL" not in result
        assert "AWS_SECRET_ACCESS_KEY" not in result
        assert "CI_TOKEN" not in result
        assert "SLACK_WEBHOOK" not in result
        # safe vars still present
        assert "PATH" in result
        assert "HOME" in result

    def test_bernstein_auth_env_passes_through(self) -> None:
        """BERNSTEIN_AUTH_TOKEN / BERNSTEIN_HOOK_SECRET must reach the agent.

        The agent's Claude-Code hook runner signs ``/hooks/{session_id}``
        POSTs with ``SECRET="${BERNSTEIN_HOOK_SECRET:-$BERNSTEIN_AUTH_TOKEN}"``.
        If the allowlist strips these vars, openssl HMACs over the empty
        string and every hook event returns 401 - which silently kills
        token-tracking, context-util %, and completion markers.
        """
        env = {
            "PATH": "/bin",
            "HOME": "/home/u",
            "BERNSTEIN_AUTH_TOKEN": "ephemeral-token",
            "BERNSTEIN_HOOK_SECRET": "shared-secret",
            "BERNSTEIN_AUTH_DISABLED": "0",
        }
        with patch("bernstein.adapters.env_isolation.os.environ", env):
            result = build_filtered_env()
        assert result["BERNSTEIN_AUTH_TOKEN"] == "ephemeral-token"
        assert result["BERNSTEIN_HOOK_SECRET"] == "shared-secret"
        assert result["BERNSTEIN_AUTH_DISABLED"] == "0"

    def test_bernstein_server_url_passes_through(self) -> None:
        """BERNSTEIN_SERVER_URL must reach the agent.

        Remote workers export it before spawning so the agent's
        server-facing plumbing targets the central server instead of
        falling back to http://localhost:8052. It is a URL, not a
        credential, so the allowlist passes it through.
        """
        env = {
            "PATH": "/bin",
            "HOME": "/home/u",
            "BERNSTEIN_SERVER_URL": "http://central:8052",
        }
        with patch("bernstein.adapters.env_isolation.os.environ", env):
            result = build_filtered_env()
        assert result["BERNSTEIN_SERVER_URL"] == "http://central:8052"

    def test_extra_keys_included(self) -> None:
        """API key names passed via extra_keys are included."""
        env = {"PATH": "/bin", "ANTHROPIC_API_KEY": "sk-ant-abc", "UNRELATED_SECRET": "boom"}
        with patch("bernstein.adapters.env_isolation.os.environ", env):
            result = build_filtered_env(["ANTHROPIC_API_KEY"])
        assert result["ANTHROPIC_API_KEY"] == "sk-ant-abc"
        assert "UNRELATED_SECRET" not in result

    def test_missing_extra_key_silently_omitted(self) -> None:
        """If an extra key is not set in the env, it is simply absent from result."""
        env = {"PATH": "/bin"}
        with patch("bernstein.adapters.env_isolation.os.environ", env):
            result = build_filtered_env(["ANTHROPIC_API_KEY"])
        assert "ANTHROPIC_API_KEY" not in result

    def test_empty_environment(self) -> None:
        """Returns empty dict when os.environ is empty."""
        with (
            patch("bernstein.adapters.env_isolation.os.environ", {}),
            patch("sys.path", []),
        ):
            result = build_filtered_env(["ANTHROPIC_API_KEY"])
        assert result == {}

    def test_result_is_independent_copy(self) -> None:
        """Mutating the result does not affect os.environ or a subsequent call."""
        env = {"PATH": "/bin", "HOME": "/home/user"}
        with patch("bernstein.adapters.env_isolation.os.environ", env):
            r1 = build_filtered_env()
            r1["PATH"] = "mutated"
            r2 = build_filtered_env()
        assert r2["PATH"] == "/bin"

    def test_multiple_extra_keys(self) -> None:
        """Multiple extra keys are all included."""
        env = {
            "OPENAI_API_KEY": "sk-openai",
            "OPENAI_ORG_ID": "org-123",
            "OPENAI_BASE_URL": "https://api.openai.com/v1",
            "SECRET": "leakme",
            "PATH": "/bin",
        }
        with patch("bernstein.adapters.env_isolation.os.environ", env):
            result = build_filtered_env(["OPENAI_API_KEY", "OPENAI_ORG_ID", "OPENAI_BASE_URL"])
        assert result["OPENAI_API_KEY"] == "sk-openai"
        assert result["OPENAI_ORG_ID"] == "org-123"
        assert result["OPENAI_BASE_URL"] == "https://api.openai.com/v1"
        assert "SECRET" not in result

    def test_base_allowlist_includes_git_vars(self) -> None:
        """Git authoring vars are in the base allowlist for commit attribution."""
        assert "GIT_AUTHOR_NAME" in _BASE_ALLOWLIST
        assert "GIT_AUTHOR_EMAIL" in _BASE_ALLOWLIST
        assert "GIT_COMMITTER_NAME" in _BASE_ALLOWLIST
        assert "GIT_COMMITTER_EMAIL" in _BASE_ALLOWLIST

    def test_base_allowlist_includes_ssh_auth_sock(self) -> None:
        """SSH_AUTH_SOCK is in the base allowlist for git push over SSH."""
        assert "SSH_AUTH_SOCK" in _BASE_ALLOWLIST

    def test_git_vars_passed_when_set(self) -> None:
        """Git identity vars present in os.environ are forwarded to agents."""
        env = {
            "GIT_AUTHOR_NAME": "Alice",
            "GIT_AUTHOR_EMAIL": "alice@example.com",
            "UNRELATED": "nope",
            "PATH": "/bin",
        }
        with patch("bernstein.adapters.env_isolation.os.environ", env):
            result = build_filtered_env()
        assert result["GIT_AUTHOR_NAME"] == "Alice"
        assert result["GIT_AUTHOR_EMAIL"] == "alice@example.com"
        assert "UNRELATED" not in result


# ---------------------------------------------------------------------------
# Integration: adapters pass env= to subprocess.Popen
# ---------------------------------------------------------------------------


def _make_popen_mock(pid: int = 999) -> MagicMock:
    m = MagicMock(spec=subprocess.Popen)
    m.pid = pid
    return m


class TestAdaptersUseFilteredEnv:
    """Each adapter passes env= to subprocess.Popen, not None."""

    def test_codex_passes_env(self, tmp_path: Path) -> None:
        from bernstein.core.models import ModelConfig

        from bernstein.adapters.codex import CodexAdapter

        adapter = CodexAdapter()
        proc_mock = _make_popen_mock()
        with patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock) as popen_spy:
            adapter.spawn(
                prompt="do work",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5.4", effort="high"),
                session_id="sess-codex",
            )
        kwargs = popen_spy.call_args.kwargs
        assert "env" in kwargs, "env= must be passed to Popen"
        assert kwargs["env"] is not None
        assert isinstance(kwargs["env"], dict)
        # Secrets must not bleed through
        assert "DATABASE_URL" not in kwargs["env"]

    def test_gemini_passes_env(self, tmp_path: Path) -> None:
        from bernstein.core.models import ModelConfig

        from bernstein.adapters.gemini import GeminiAdapter

        adapter = GeminiAdapter()
        proc_mock = _make_popen_mock()
        with patch("bernstein.adapters.gemini.subprocess.Popen", return_value=proc_mock) as popen_spy:
            adapter.spawn(
                prompt="do work",
                workdir=tmp_path,
                model_config=ModelConfig(model="gemini-pro", effort="high"),
                session_id="sess-gemini",
            )
        kwargs = popen_spy.call_args.kwargs
        assert "env" in kwargs
        assert kwargs["env"] is not None

    def test_claude_passes_env_to_both_procs(self, tmp_path: Path) -> None:
        from bernstein.core.models import ModelConfig

        from bernstein.adapters.claude import ClaudeCodeAdapter

        adapter = ClaudeCodeAdapter()
        claude_mock = _make_popen_mock(pid=100)
        claude_mock.stdout = MagicMock()
        wrapper_mock = _make_popen_mock(pid=101)

        with patch(
            "bernstein.adapters.claude.subprocess.Popen",
            side_effect=[claude_mock, wrapper_mock],
        ) as popen_spy:
            adapter.spawn(
                prompt="do work",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="sess-claude",
            )

        assert popen_spy.call_count == 2, "Should spawn claude + wrapper"
        for call in popen_spy.call_args_list:
            kw = call.kwargs
            assert "env" in kw, "Both Popen calls must receive env="
            assert kw["env"] is not None

    def test_claude_env_includes_api_key(self, tmp_path: Path) -> None:
        from bernstein.core.models import ModelConfig

        from bernstein.adapters.claude import ClaudeCodeAdapter

        adapter = ClaudeCodeAdapter()
        claude_mock = _make_popen_mock(pid=100)
        claude_mock.stdout = MagicMock()
        wrapper_mock = _make_popen_mock(pid=101)

        fake_env = {
            "PATH": "/bin",
            "HOME": "/home/u",
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "SECRET_DB": "leak",
        }
        with (
            patch("bernstein.adapters.env_isolation.os.environ", fake_env),
            patch(
                "bernstein.adapters.claude.subprocess.Popen",
                side_effect=[claude_mock, wrapper_mock],
            ) as popen_spy,
        ):
            adapter.spawn(
                prompt="do work",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="sess-claude",
            )

        first_call_env = popen_spy.call_args_list[0].kwargs["env"]
        assert first_call_env["ANTHROPIC_API_KEY"] == "sk-ant-test"
        assert "SECRET_DB" not in first_call_env

    def test_aider_passes_env(self, tmp_path: Path) -> None:
        from bernstein.core.models import ModelConfig

        from bernstein.adapters.aider import AiderAdapter

        adapter = AiderAdapter()
        proc_mock = _make_popen_mock()
        with patch("bernstein.adapters.aider.subprocess.Popen", return_value=proc_mock) as popen_spy:
            adapter.spawn(
                prompt="do work",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="sess-aider",
            )
        kwargs = popen_spy.call_args.kwargs
        assert "env" in kwargs
        assert kwargs["env"] is not None

    def test_amp_passes_env(self, tmp_path: Path) -> None:
        from bernstein.core.models import ModelConfig

        from bernstein.adapters.amp import AmpAdapter

        adapter = AmpAdapter()
        proc_mock = _make_popen_mock()
        with patch("bernstein.adapters.amp.subprocess.Popen", return_value=proc_mock) as popen_spy:
            adapter.spawn(
                prompt="do work",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="sess-amp",
            )
        kwargs = popen_spy.call_args.kwargs
        assert "env" in kwargs
        assert kwargs["env"] is not None

    def test_generic_passes_env(self, tmp_path: Path) -> None:
        from bernstein.core.models import ModelConfig

        from bernstein.adapters.generic import GenericAdapter

        adapter = GenericAdapter(cli_command="mycli", display_name="MyCLI")
        proc_mock = _make_popen_mock()
        with patch("bernstein.adapters.generic.subprocess.Popen", return_value=proc_mock) as popen_spy:
            adapter.spawn(
                prompt="do work",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="sess-generic",
            )
        kwargs = popen_spy.call_args.kwargs
        assert "env" in kwargs
        assert kwargs["env"] is not None


# ---------------------------------------------------------------------------
# Embedded agent-team spawning gate - deny by default
# ---------------------------------------------------------------------------


class TestEmbeddedAgentTeamsDeny:
    """The embedded agent-team spawning gate is stripped by default.

    A spawned worker that inherits ``CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS``
    can launch its own teammate instances; those teammates inherit the
    lead's permission mode and run outside this worker's HMAC audit trail,
    breaking the one-worker-one-audit-trail invariant.  The gate must be
    denied even if it somehow reaches the allowlist.
    """

    def test_gate_stripped_by_default(self) -> None:
        fake_env = {
            "PATH": "/usr/bin",
            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
        }
        with patch("bernstein.adapters.env_isolation.os.environ", fake_env):
            result = build_filtered_env()
        assert "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS" not in result
        assert result["PATH"] == "/usr/bin"

    def test_gate_stripped_even_when_in_extra_keys(self) -> None:
        """An operator-widened allowlist cannot re-open the hole."""
        fake_env = {
            "PATH": "/usr/bin",
            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
        }
        with patch("bernstein.adapters.env_isolation.os.environ", fake_env):
            result = build_filtered_env(["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"])
        assert "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS" not in result

    def test_forward_looking_suffixes_stripped(self) -> None:
        """A future ``*_AGENT_TEAMS`` gate is denied without a code change."""
        fake_env = {
            "PATH": "/usr/bin",
            "SOMEVENDOR_EXPERIMENTAL_AGENT_TEAMS": "1",
            "OTHER_AGENT_TEAMS": "yes",
        }
        with patch("bernstein.adapters.env_isolation.os.environ", fake_env):
            result = build_filtered_env(["SOMEVENDOR_EXPERIMENTAL_AGENT_TEAMS", "OTHER_AGENT_TEAMS"])
        assert "SOMEVENDOR_EXPERIMENTAL_AGENT_TEAMS" not in result
        assert "OTHER_AGENT_TEAMS" not in result

    def test_opt_in_repermits_gate(self) -> None:
        """An explicit host opt-in re-permits the gate."""
        fake_env = {
            "PATH": "/usr/bin",
            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
            "BERNSTEIN_ALLOW_EMBEDDED_AGENT_TEAMS": "1",
        }
        with patch("bernstein.adapters.env_isolation.os.environ", fake_env):
            result = build_filtered_env(["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"])
        assert result["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] == "1"

    def test_opt_in_falsey_value_still_denies(self) -> None:
        """A stray empty/``0`` opt-in value keeps deny-by-default."""
        fake_env = {
            "PATH": "/usr/bin",
            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
            "BERNSTEIN_ALLOW_EMBEDDED_AGENT_TEAMS": "0",
        }
        with patch("bernstein.adapters.env_isolation.os.environ", fake_env):
            result = build_filtered_env(["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"])
        assert "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS" not in result

    def test_opt_in_emits_audit_event(self, tmp_path: Path) -> None:
        """record_embedded_teams_opt_in appends an attestation to the chain."""
        from bernstein.adapters.env_isolation import record_embedded_teams_opt_in
        from bernstein.core.security.audit import AuditLog

        audit_dir = tmp_path / ".sdd" / "audit"
        record_embedded_teams_opt_in(
            adapter="claude",
            session_id="qa-abc12345",
            audit_dir=audit_dir,
        )
        events = AuditLog(audit_dir=audit_dir).query(event_type="adapter.embedded_agent_teams_enabled")
        assert len(events) == 1
        assert events[0].actor == "claude"
        assert events[0].resource_id == "qa-abc12345"
        assert events[0].details["adapter"] == "claude"

    def test_secrets_overlay_cannot_reintroduce_gate(self) -> None:
        """A secrets provider returning the gate is re-stripped before return.

        The embedded-team strip runs both before and after the secrets
        overlay.  If an external provider returned the agent-team gate, the
        post-overlay strip must remove it again so the deny-by-default posture
        holds right up to the return.
        """
        from bernstein.core.security.secrets import SecretsConfig

        cfg = SecretsConfig(provider="vault", path="secret/test")
        fake_env = {"PATH": "/usr/bin"}
        provider_secrets = {
            "SOME_API_KEY": "value",
            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
        }
        with (
            patch("bernstein.adapters.env_isolation.os.environ", fake_env),
            patch(
                "bernstein.core.security.secrets.load_secrets",
                return_value=provider_secrets,
            ),
        ):
            result = build_filtered_env(secrets_config=cfg)
        assert "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS" not in result
        assert result["SOME_API_KEY"] == "value"

    def test_secrets_overlay_gate_kept_when_opted_in(self) -> None:
        """When the operator opts in, a provider-returned gate survives."""
        from bernstein.core.security.secrets import SecretsConfig

        cfg = SecretsConfig(provider="vault", path="secret/test")
        fake_env = {
            "PATH": "/usr/bin",
            "BERNSTEIN_ALLOW_EMBEDDED_AGENT_TEAMS": "1",
        }
        provider_secrets = {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}
        with (
            patch("bernstein.adapters.env_isolation.os.environ", fake_env),
            patch(
                "bernstein.core.security.secrets.load_secrets",
                return_value=provider_secrets,
            ),
        ):
            result = build_filtered_env(secrets_config=cfg)
        assert result["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] == "1"
