"""Unit tests for CodexAdapter spawn/kill/is_alive."""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.models import ApiTier, ModelConfig, ProviderType

from bernstein.adapters._contract import STRATEGY_MATRIX, AdapterStrategy, DangerousModeStrategy
from bernstein.adapters.capability_profile import SandboxTier
from bernstein.adapters.codex import (
    _BYPASS_SANDBOX_FLAG,
    _DEFAULT_CODEX_MODEL,
    _SANDBOXED_ARGS,
    _TIERS_REPLACING_VENDOR_SANDBOX,
    _UNDECLARED_HOST_ISOLATION,
    CodexAdapter,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_popen_mock(pid: int) -> MagicMock:
    m = MagicMock(spec=subprocess.Popen)
    m.pid = pid
    m.wait.return_value = None
    return m


def _inner_cmd(full_cmd: list[str]) -> list[str]:
    """Extract the actual CLI command after the '--' worker separator."""
    sep = full_cmd.index("--")
    return full_cmd[sep + 1 :]


def _spawn_inner_cmd(adapter: CodexAdapter, tmp_path: Path, pid: int) -> list[str]:
    """Spawn with Popen mocked and return the codex argv the adapter built."""
    proc_mock = _make_popen_mock(pid=pid)
    with patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock) as popen:
        adapter.spawn(
            prompt="hello",
            workdir=tmp_path,
            model_config=ModelConfig(model="gpt-5.5", effort="high"),
            session_id=f"codex-sbx{pid}",
        )
    return _inner_cmd(popen.call_args.args[0])


# ---------------------------------------------------------------------------
# CodexAdapter.spawn() - command construction
# ---------------------------------------------------------------------------


class TestCodexAdapterSpawn:
    """CodexAdapter.spawn() builds correct command."""

    def test_wrapped_with_worker(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=100)
        with patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="o3", effort="high"),
                session_id="codex-s1",
            )
        cmd = popen.call_args.args[0]
        assert cmd[0] == sys.executable
        assert cmd[1:3] == ["-m", "bernstein.core.orchestration.worker"]
        inner = _inner_cmd(cmd)
        assert inner[0] == "codex"

    def test_model_flag_passthrough(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=101)
        with patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="o3-mini", effort="high"),
                session_id="codex-s2",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "-m" in inner
        assert inner[inner.index("-m") + 1] == "o3-mini"

    def test_sandbox_flag_present(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=102)
        with patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="o3", effort="high"),
                session_id="codex-s3",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--sandbox" in inner
        assert inner[inner.index("--sandbox") + 1] == "workspace-write"
        # Upstream codex 0.130+ deprecated --full-auto; ensure we no longer emit it.
        assert "--full-auto" not in inner

    def test_json_output_flag_present(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=103)
        with patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="o3", effort="high"),
                session_id="codex-s4",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--json" in inner

    def test_output_file_flag_present(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=109)
        with patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="o3", effort="high"),
                session_id="codex-s9",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "-o" in inner
        assert inner[inner.index("-o") + 1].endswith("codex-s9.last-message.txt")

    def test_prompt_appended_last(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=104)
        with patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="my-unique-prompt",
                workdir=tmp_path,
                model_config=ModelConfig(model="o3", effort="high"),
                session_id="codex-s5",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert inner[-1] == "my-unique-prompt"

    def test_creates_log_dir(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=105)
        with patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="o3", effort="high"),
                session_id="codex-s6",
            )
        assert (tmp_path / ".sdd" / "runtime").is_dir()

    def test_spawn_result_pid(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=106)
        with patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock):
            result = adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="o3", effort="high"),
                session_id="codex-s7",
            )
        assert result.pid == 106

    def test_log_path_uses_session_id(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=107)
        with patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock):
            result = adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="o3", effort="high"),
                session_id="my-codex-session",
            )
        assert result.log_path.name == "my-codex-session.log"

    def test_start_new_session_enabled(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=108)
        with patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="o3", effort="high"),
                session_id="codex-s8",
            )
        kwargs = popen.call_args.kwargs
        assert kwargs.get("start_new_session") is True

    def test_codex_prompt_carries_the_completion_protocol(self, tmp_path: Path) -> None:
        """Codex has no system-prompt flag; a non-empty addendum must still
        reach the agent by riding on the positional prompt argument (issue
        #5325), or the completion / heartbeat / signal-check protocol never
        reaches a ``--cli codex`` run.
        """
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=110)
        with patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="o3", effort="high"),
                session_id="codex-s10",
                system_addendum="When done, POST /complete. Heartbeat every 30s.",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "When done, POST /complete. Heartbeat every 30s." in inner[-1]

    def test_codex_addendum_appended_after_task_brief(self, tmp_path: Path) -> None:
        """A truncated prompt must lose the addendum, never the task brief."""
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=111)
        with patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="primary task brief",
                workdir=tmp_path,
                model_config=ModelConfig(model="o3", effort="high"),
                session_id="codex-s11",
                system_addendum="HEARTBEAT every 30s",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        full_prompt = inner[-1]
        assert full_prompt.index("primary task brief") < full_prompt.index("HEARTBEAT every 30s")

    def test_codex_empty_addendum_leaves_prompt_untouched(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=112)
        with patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="just the task",
                workdir=tmp_path,
                model_config=ModelConfig(model="o3", effort="high"),
                session_id="codex-s12",
                system_addendum="",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert inner[-1] == "just the task"


# ---------------------------------------------------------------------------
# spawn() - env isolation
# ---------------------------------------------------------------------------


class TestCodexEnvIsolation:
    """spawn() passes only OPENAI-specific keys to subprocess."""

    def test_env_contains_openai_keys(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=200)
        with (
            patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock) as popen,
            patch.dict(
                "os.environ",
                {"OPENAI_API_KEY": "sk-test", "OPENAI_ORG_ID": "org-123", "PATH": "/usr/bin"},
                clear=True,
            ),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="o3", effort="high"),
                session_id="codex-env1",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert "OPENAI_API_KEY" in env
        assert env["OPENAI_API_KEY"] == "sk-test"

    def test_env_excludes_unrelated_keys(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=201)
        with (
            patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock) as popen,
            patch.dict(
                "os.environ",
                {
                    "OPENAI_API_KEY": "sk-test",
                    "ANTHROPIC_API_KEY": "ant-secret",
                    "DATABASE_URL": "postgres://x",
                    "PATH": "/usr/bin",
                },
                clear=True,
            ),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="o3", effort="high"),
                session_id="codex-env2",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert "ANTHROPIC_API_KEY" not in env
        assert "DATABASE_URL" not in env

    def test_env_includes_path(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=202)
        with (
            patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock) as popen,
            patch.dict("os.environ", {"PATH": "/usr/bin", "OPENAI_API_KEY": "sk-x"}, clear=True),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="o3", effort="high"),
                session_id="codex-env3",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert "PATH" in env


# ---------------------------------------------------------------------------
# CodexAdapter.name()
# ---------------------------------------------------------------------------


class TestCodexAdapterName:
    def test_name(self) -> None:
        assert CodexAdapter().name() == "Codex"


# ---------------------------------------------------------------------------
# Missing binary / PermissionError
# ---------------------------------------------------------------------------


class TestCodexSpawnMissingBinary:
    def test_file_not_found_raises_runtime_error(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        with (
            patch(
                "bernstein.adapters.codex.subprocess.Popen",
                side_effect=FileNotFoundError("No such file"),
            ),
            pytest.raises(RuntimeError, match="not found in PATH"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="o3", effort="high"),
                session_id="missing",
            )

    def test_permission_error_raises_runtime_error(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        with (
            patch(
                "bernstein.adapters.codex.subprocess.Popen",
                side_effect=PermissionError("Permission denied"),
            ),
            pytest.raises(RuntimeError, match="[Pp]ermission"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="o3", effort="high"),
                session_id="perm-denied",
            )


class TestCodexWarningsAndFastExit:
    def test_warns_when_no_key_and_no_oauth(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=301)
        missing_auth = tmp_path / "no-codex" / "auth.json"
        with (
            patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock),
            patch("bernstein.adapters.codex._CODEX_AUTH_FILE", missing_auth),
            patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True),
            caplog.at_level("WARNING"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="o3", effort="high"),
                session_id="warn-missing-key",
            )
        assert "no OPENAI_API_KEY and no Codex OAuth session" in caplog.text

    def test_no_auth_warning_with_oauth_session(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """A valid ChatGPT OAuth session (~/.codex/auth.json) must not warn (issue #2075)."""
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=303)
        auth_file = tmp_path / "auth.json"
        auth_file.write_text("{}")
        with (
            patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock),
            patch("bernstein.adapters.codex._CODEX_AUTH_FILE", auth_file),
            patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True),
            caplog.at_level("WARNING"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="o3", effort="high"),
                session_id="oauth-session",
            )
        assert not any("OPENAI_API_KEY" in r.message or "OAuth session" in r.message for r in caplog.records)

    def test_claude_tier_model_mapped_to_codex_default(self, tmp_path: Path) -> None:
        """A Claude tier name reaching the adapter must not become `codex exec -m opus` (issue #2075)."""
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=304)
        with patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="opus", effort="max"),
                session_id="codex-opus",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert inner[inner.index("-m") + 1] == _DEFAULT_CODEX_MODEL
        assert "opus" not in inner

    def test_default_model_is_one_upstream_still_serves(self) -> None:
        """The fallback pin must be a model Codex accepts, or it 400s on every use.

        ``gpt-5.4`` was the pin until 2026-09-02; the backend now rejects it on
        the ChatGPT-account auth path and it is absent from the account's model
        catalogue. Pinning the substitution to a retired identifier turns the
        Claude-tier safety net into a guaranteed failure.
        """
        assert _DEFAULT_CODEX_MODEL == "gpt-5.5"
        assert CodexAdapter.default_model == _DEFAULT_CODEX_MODEL

    def test_fast_exit_rate_limit_raises(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=302)
        proc_mock.wait.return_value = 1
        with (
            patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock),
            patch.object(CodexAdapter, "_read_last_lines", return_value=["429 rate limit exceeded"]),
        ):
            with pytest.raises(RuntimeError, match="rate-limited"):
                adapter.spawn(
                    prompt="hello",
                    workdir=tmp_path,
                    model_config=ModelConfig(model="o3", effort="high"),
                    session_id="codex-fast-exit",
                )


# ---------------------------------------------------------------------------
# is_alive() and kill() - inherited from CLIAdapter base
# ---------------------------------------------------------------------------


class TestCodexIsAlive:
    def test_true_when_process_exists(self) -> None:
        adapter = CodexAdapter()
        with patch("bernstein.adapters.base.process_alive", return_value=True) as mock_alive:
            assert adapter.is_alive(1234) is True
        mock_alive.assert_called_once_with(1234)

    def test_false_when_oserror(self) -> None:
        adapter = CodexAdapter()
        with patch("bernstein.adapters.base.process_alive", return_value=False):
            assert adapter.is_alive(9999) is False


class TestCodexKill:
    def test_calls_killpg(self) -> None:
        adapter = CodexAdapter()
        with patch("bernstein.adapters.base.reap_process_group") as mock_killpg:
            adapter.kill(555)
        mock_killpg.assert_called_once_with(555)

    def test_does_not_raise_on_oserror(self) -> None:
        adapter = CodexAdapter()
        with patch("bernstein.adapters.base.reap_process_group", return_value=False):
            adapter.kill(556)  # must not raise


# ---------------------------------------------------------------------------
# detect_tier()
# ---------------------------------------------------------------------------


class TestCodexDetectTier:
    def test_returns_none_without_api_key(self) -> None:
        adapter = CodexAdapter()
        with patch.dict("os.environ", {}, clear=True):
            assert adapter.detect_tier() is None

    def test_enterprise_with_org_id(self) -> None:
        adapter = CodexAdapter()
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test", "OPENAI_ORG_ID": "org-123"}, clear=True):
            info = adapter.detect_tier()
        assert info is not None
        assert info.tier == ApiTier.ENTERPRISE
        assert info.provider == ProviderType.CODEX

    def test_pro_with_sk_proj_key(self) -> None:
        adapter = CodexAdapter()
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-proj-abc123"}, clear=True):
            info = adapter.detect_tier()
        assert info is not None
        assert info.tier == ApiTier.PRO

    def test_plus_with_sk_key(self) -> None:
        adapter = CodexAdapter()
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-abc123"}, clear=True):
            info = adapter.detect_tier()
        assert info is not None
        assert info.tier == ApiTier.PLUS

    def test_free_with_unknown_key_format(self) -> None:
        adapter = CodexAdapter()
        with patch.dict("os.environ", {"OPENAI_API_KEY": "random-key"}, clear=True):
            info = adapter.detect_tier()
        assert info is not None
        assert info.tier == ApiTier.FREE


# ---------------------------------------------------------------------------
# Sandbox posture selection
# ---------------------------------------------------------------------------


class TestSandboxPosture:
    """The sandbox argv is chosen by the declared strategy, not hardcoded.

    ``--sandbox workspace-write`` is implemented with bubblewrap, which needs
    an unprivileged user namespace. A runner that already isolates the process
    denies exactly that, and the resulting failure is silent: every
    model-issued command fails inside the turn, the diff is empty, and
    ``codex exec`` still exits 0 with ``turn.completed``. So the posture has to
    be selectable, and the escalated form has to be the deliberate opt-in
    rather than the default a plain host inherits.
    """

    def _spawn_inner(self, adapter: CodexAdapter, tmp_path: Path, pid: int) -> list[str]:
        return _spawn_inner_cmd(adapter, tmp_path, pid)

    def test_escalated_strategy_bypasses_the_vendor_sandbox(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        adapter.strategy_override = AdapterStrategy(dangerous_mode=DangerousModeStrategy.ALWAYS_ON)

        inner = self._spawn_inner(adapter, tmp_path, pid=140)

        assert _BYPASS_SANDBOX_FLAG in inner
        assert "--sandbox" not in inner

    def test_default_strategy_keeps_the_vendor_sandbox(self, tmp_path: Path) -> None:
        """The shipped declaration is CLI_FLAG, so a plain spawn stays sandboxed."""
        adapter = CodexAdapter()

        inner = self._spawn_inner(adapter, tmp_path, pid=141)

        assert _BYPASS_SANDBOX_FLAG not in inner
        assert tuple(inner[inner.index("--sandbox") : inner.index("--sandbox") + 2]) == _SANDBOXED_ARGS

    @pytest.mark.parametrize(
        "declared",
        [
            DangerousModeStrategy.CLI_FLAG,
            DangerousModeStrategy.ENV_VAR,
            DangerousModeStrategy.UNSUPPORTED,
        ],
    )
    def test_only_always_on_escalates(self, tmp_path: Path, declared: DangerousModeStrategy) -> None:
        """Every other declared value keeps the sandbox; only ALWAYS_ON drops it."""
        adapter = CodexAdapter()
        adapter.strategy_override = AdapterStrategy(dangerous_mode=declared)

        inner = self._spawn_inner(adapter, tmp_path, pid=142)

        assert _BYPASS_SANDBOX_FLAG not in inner
        assert inner[inner.index("--sandbox") + 1] == "workspace-write"

    def test_shipped_declaration_is_not_escalated(self) -> None:
        """Guards the matrix row: flipping it to ALWAYS_ON would drop the sandbox repo-wide."""
        assert CodexAdapter()._dangerous_mode() is DangerousModeStrategy.CLI_FLAG

    def test_bypass_is_logged_so_the_escalation_is_visible(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        adapter = CodexAdapter()
        adapter.strategy_override = AdapterStrategy(dangerous_mode=DangerousModeStrategy.ALWAYS_ON)

        with caplog.at_level("WARNING", logger="bernstein.adapters.codex"):
            self._spawn_inner(adapter, tmp_path, pid=143)

        assert any(_BYPASS_SANDBOX_FLAG in record.getMessage() for record in caplog.records)


class TestHostIsolationDeclaration:
    """#5341 - an operator whose host already isolates the process says so.

    Before this, the only way to drop the vendor sandbox was the escalated
    dangerous-mode strategy, a blunt "bypass everything" switch that says
    nothing about *why* it is safe. The declaration is narrower and auditable:
    it names the isolation tier the host provides and the evidence for it, and
    only a tier at or above ``container`` -- a boundary that actually replaces
    what bubblewrap would have given -- drops the vendor sandbox.
    """

    def _adapter(self, tier: SandboxTier, evidence: str = "") -> CodexAdapter:
        adapter = CodexAdapter()
        adapter.host_isolation = tier
        adapter.host_isolation_evidence = evidence
        return adapter

    def test_the_tiers_that_drop_the_sandbox_are_real_tiers(self) -> None:
        """The adapter keys on tier names it cannot import; a rename must not pass."""
        assert set(_TIERS_REPLACING_VENDOR_SANDBOX) <= {tier.value for tier in SandboxTier}
        assert SandboxTier.NONE.value == _UNDECLARED_HOST_ISOLATION

    def test_undeclared_host_keeps_the_vendor_sandbox(self, tmp_path: Path) -> None:
        """The constructor default is the weakest tier, so nothing changes by accident."""
        adapter = CodexAdapter()

        assert adapter.host_isolation == SandboxTier.NONE
        inner = _spawn_inner_cmd(adapter, tmp_path, pid=160)

        assert _BYPASS_SANDBOX_FLAG not in inner
        assert tuple(inner[inner.index("--sandbox") : inner.index("--sandbox") + 2]) == _SANDBOXED_ARGS

    def test_process_tier_keeps_the_vendor_sandbox(self, tmp_path: Path) -> None:
        """Process-level confinement does not supply the namespace bubblewrap needs."""
        inner = _spawn_inner_cmd(self._adapter(SandboxTier.PROCESS, "seccomp profile"), tmp_path, pid=161)

        assert _BYPASS_SANDBOX_FLAG not in inner
        assert inner[inner.index("--sandbox") + 1] == "workspace-write"

    @pytest.mark.parametrize("tier", [SandboxTier.CONTAINER, SandboxTier.VM])
    def test_container_and_vm_tiers_drop_the_vendor_sandbox(self, tmp_path: Path, tier: SandboxTier) -> None:
        inner = _spawn_inner_cmd(self._adapter(tier, "read-only rootfs"), tmp_path, pid=162)

        assert _BYPASS_SANDBOX_FLAG in inner
        assert "--sandbox" not in inner

    def test_drop_names_the_tier_and_the_evidence(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        adapter = self._adapter(SandboxTier.CONTAINER, "read-only rootfs, cap-drop ALL")

        with caplog.at_level("WARNING", logger="bernstein.adapters.codex"):
            _spawn_inner_cmd(adapter, tmp_path, pid=163)

        messages = [record.getMessage() for record in caplog.records]
        declared = [m for m in messages if "host isolation declared" in m]
        assert len(declared) == 1
        assert "tier=container" in declared[0]
        assert "read-only rootfs, cap-drop ALL" in declared[0]

    def test_drop_without_evidence_says_so(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("WARNING", logger="bernstein.adapters.codex"):
            _spawn_inner_cmd(self._adapter(SandboxTier.VM), tmp_path, pid=164)

        declared = [m for m in (r.getMessage() for r in caplog.records) if "host isolation declared" in m]
        assert len(declared) == 1
        assert "evidence=none given" in declared[0]

    def test_the_warning_is_emitted_once_per_adapter_not_per_spawn(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A per-spawn warning would bury the one line an operator has to read."""
        adapter = self._adapter(SandboxTier.CONTAINER, "read-only rootfs")

        with caplog.at_level("WARNING", logger="bernstein.adapters.codex"):
            first = _spawn_inner_cmd(adapter, tmp_path, pid=165)
            second = _spawn_inner_cmd(adapter, tmp_path, pid=166)

        assert _BYPASS_SANDBOX_FLAG in first
        assert _BYPASS_SANDBOX_FLAG in second
        declared = [m for m in (r.getMessage() for r in caplog.records) if "host isolation declared" in m]
        assert len(declared) == 1

    def test_the_adapter_advertises_that_it_consumes_the_declaration(self) -> None:
        """The spawner injects only into adapters carrying this marker."""
        assert CodexAdapter.consumes_host_isolation is True

    def test_the_declared_strategy_is_unchanged(self) -> None:
        """The declaration is a second, narrower route -- it does not escalate the matrix."""
        assert STRATEGY_MATRIX["codex"].dangerous_mode is DangerousModeStrategy.CLI_FLAG


class TestSandboxFailureDetection:
    """#5314 - a run whose every shell call the sandbox refused still exits 0.

    ``_probe_fast_exit`` cannot catch it: that probe treats an early NON-ZERO
    exit as a spawn failure, and the exit code here is zero. The signal has to
    come from the event stream, and the reported run showed why it matters:
    16/16 commands refused, 0 files changed, ~194k tokens, ``turn.completed``.
    """

    BWRAP = (
        "bwrap: No permissions to create a new namespace, likely because the "
        "kernel does not allow non-privileged user namespaces."
    )

    def _event(self, exit_code, output=""):
        import json

        return json.dumps(
            {
                "item": {
                    "item_type": "command_execution",
                    "command": "pytest",
                    "exit_code": exit_code,
                    "aggregated_output": output,
                }
            }
        )

    def test_all_commands_refused_is_detected(self):
        from bernstein.adapters.codex import detect_sandbox_failure

        log = "\n".join(self._event(1, self.BWRAP) for _ in range(16))
        detected = detect_sandbox_failure(log)

        assert detected is not None
        detail, failed, total = detected
        assert (failed, total) == (16, 16)
        assert "every shell command was refused" in detail

    def test_a_successful_run_is_untouched(self):
        from bernstein.adapters.codex import detect_sandbox_failure

        log = "\n".join(self._event(0, "ok") for _ in range(5))
        assert detect_sandbox_failure(log) is None

    def test_ordinary_command_failures_are_not_a_sandbox_failure(self):
        """The control that matters. A failing test suite, a missing file, a
        bad flag - every command can legitimately exit non-zero without the
        sandbox being at fault. Reporting those as permission_denied would
        abort real runs, so the bwrap signature is required."""
        from bernstein.adapters.codex import detect_sandbox_failure

        log = "\n".join(self._event(1, "FAILED tests/test_x.py") for _ in range(9))
        assert detect_sandbox_failure(log) is None

    def test_a_partial_failure_is_not_reported(self):
        """One refused command among working ones is not this defect: the run
        did real work, and aborting it would lose that."""
        from bernstein.adapters.codex import detect_sandbox_failure

        log = "\n".join([self._event(0, "ok"), self._event(1, self.BWRAP)])
        assert detect_sandbox_failure(log) is None

    def test_a_run_with_no_shell_calls_is_not_reported(self):
        """Zero commands is not evidence of anything, even if the bwrap string
        appears somewhere else in the log."""
        from bernstein.adapters.codex import detect_sandbox_failure

        assert detect_sandbox_failure(f"some prose mentioning {self.BWRAP}") is None

    def test_empty_and_malformed_logs_do_not_raise(self):
        from bernstein.adapters.codex import detect_sandbox_failure

        for log in ("", "not json at all", "{broken", "{}\n[]\nnull"):
            assert detect_sandbox_failure(log) is None

    def test_the_flat_event_shape_is_also_understood(self):
        """codex has emitted both a nested ``item`` and a flat event; accept
        either rather than silently seeing zero commands."""
        import json

        from bernstein.adapters.codex import detect_sandbox_failure

        log = "\n".join(
            json.dumps(
                {
                    "type": "command_execution",
                    "exit_code": 1,
                    "output": self.BWRAP,
                }
            )
            for _ in range(3)
        )
        detected = detect_sandbox_failure(log)
        assert detected is not None and detected[1:] == (3, 3)
