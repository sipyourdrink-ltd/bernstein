"""Unit tests for OpenClawAdapter."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from bernstein.core.models import ModelConfig, Task
from bernstein.core.spawner import AgentSpawner

from bernstein.adapters.base import SpawnError, SpawnResult
from bernstein.adapters.openclaw import API_KEY_ENV, BASE_URL_ENV, MODEL_ENV, PROVIDER_ID, OpenClawAdapter
from bernstein.core.sandbox import DockerSandbox
from bernstein.core.security.network_policy import NetworkPolicyDenied
from tests.unit._adapter_test_helpers import inner_cmd, make_popen_mock

pytestmark = pytest.mark.usefixtures("no_watchdog_threads")

GATEWAY = {BASE_URL_ENV: "http://gw.test/v1", API_KEY_ENV: "sk-openclaw-secret"}


def _spawn(tmp_path: Path, env: dict[str, str], model: str = "openai/test", **kwargs: object) -> list[str]:
    with (
        patch.dict("os.environ", env, clear=False),
        patch("bernstein.adapters.openclaw.subprocess.Popen", return_value=make_popen_mock(pid=900)) as popen,
    ):
        OpenClawAdapter().spawn(
            prompt="fix the bug",
            workdir=tmp_path,
            model_config=ModelConfig(model=model, effort="high"),
            session_id="openclaw-s1",
            **kwargs,  # type: ignore[arg-type]
        )
    return inner_cmd(popen.call_args.args[0])


@pytest.fixture(autouse=True)
def _no_ambient_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (BASE_URL_ENV, API_KEY_ENV, MODEL_ENV, "BERNSTEIN_NETWORK_POLICY", "BERNSTEIN_PROFILE_MODE"):
        monkeypatch.delenv(name, raising=False)


class TestOpenClawAdapterSpawn:
    def test_runs_one_headless_turn_in_the_worktree(self, tmp_path: Path) -> None:
        cmd = _spawn(tmp_path, {}, model="anthropic/claude-x", timeout_seconds=900, system_addendum="be brief")
        prompt_file = tmp_path / ".sdd" / "runtime" / "openclaw-s1.prompt.md"
        assert cmd == [
            "openclaw",
            "agent",
            "exec",
            "--message-file",
            str(prompt_file),
            "--cwd",
            str(tmp_path),
            "--json",
            "--auth-env-only",
            "--model",
            "anthropic/claude-x",
            "--thinking",
            "high",
            "--timeout",
            "900",
        ]
        assert prompt_file.read_text() == "fix the bug\n\nbe brief"
        if os.name == "posix":
            assert stat.S_IMODE(prompt_file.stat().st_mode) == 0o600
            assert stat.S_IMODE(prompt_file.with_name("openclaw-s1.log").stat().st_mode) == 0o600

    def test_auto_model_is_refused_before_writing(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="explicit task model"):
            _spawn(tmp_path, {}, model="auto")
        assert not (tmp_path / ".sdd").exists()

    def test_model_override_cannot_change_the_admitted_model(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match=MODEL_ENV):
            _spawn(tmp_path, {**GATEWAY, MODEL_ENV: "alternate"}, model="admitted")

    def test_endpoint_cannot_change_after_admission(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="changed after admission"):
            _spawn(tmp_path, GATEWAY, model="admitted", mcp_config={"base_url": "https://other.test/v1"})

    def test_gateway_runs_against_a_generated_config_only(self, tmp_path: Path) -> None:
        cmd = _spawn(tmp_path, {**GATEWAY, MODEL_ENV: "gw-model"}, model="gw-model")
        config_path = tmp_path / ".sdd" / "runtime" / "openclaw-openclaw-s1.json"
        assert cmd[cmd.index("--config") + 1] == str(config_path)
        assert "--auth-env-only" not in cmd
        assert cmd[cmd.index("--model") + 1] == f"{PROVIDER_ID}/gw-model"
        text = config_path.read_text()
        assert "sk-openclaw-secret" not in text
        provider = json.loads(text)["models"]["providers"][PROVIDER_ID]
        assert provider["baseUrl"] == "http://gw.test/v1"
        assert provider["apiKey"] == "${OPENAI_API_KEY}"
        assert provider["api"] == "openai-completions"
        assert provider["models"] == [{"id": "gw-model", "name": "gw-model"}]

    def test_gateway_model_defaults_to_the_task_model(self, tmp_path: Path) -> None:
        cmd = _spawn(tmp_path, GATEWAY, model="task-model")
        assert cmd[cmd.index("--model") + 1] == f"{PROVIDER_ID}/task-model"

    def test_the_key_reaches_openclaw_only_through_the_env(self, tmp_path: Path) -> None:
        with (
            patch.dict("os.environ", {**GATEWAY, "UNRELATED_SECRET": "x"}, clear=False),
            patch("bernstein.adapters.openclaw.subprocess.Popen", return_value=make_popen_mock(pid=901)) as popen,
        ):
            OpenClawAdapter().spawn(
                prompt="p", workdir=tmp_path, model_config=ModelConfig(model="m", effort="high"), session_id="oc-s2"
            )
        env = popen.call_args.kwargs["env"]
        assert env["OPENAI_API_KEY"] == "sk-openclaw-secret"
        assert API_KEY_ENV not in env
        assert env["OPENCLAW_HOME"] == env["OPENCLAW_STATE_DIR"] == env["HOME"]
        assert tmp_path.resolve() in Path(env["HOME"]).parents
        assert popen.call_args.kwargs["cwd"] == env["HOME"]
        argv = popen.call_args.args[0]
        assert argv[argv.index("--model") + 1] == f"{PROVIDER_ID}/m"
        assert "UNRELATED_SECRET" not in env
        assert "sk-openclaw-secret" not in " ".join(popen.call_args.args[0])

    @pytest.mark.parametrize(
        ("env", "model", "missing"),
        [
            ({BASE_URL_ENV: "http://gw.test/v1"}, "m", API_KEY_ENV),
            ({API_KEY_ENV: "k"}, "m", BASE_URL_ENV),
        ],
    )
    def test_incomplete_gateway_settings_name_what_is_missing(
        self, tmp_path: Path, env: dict[str, str], model: str, missing: str
    ) -> None:
        with pytest.raises(ValueError, match=missing):
            _spawn(tmp_path, env, model=model)

        assert not (tmp_path / ".sdd").exists()

    @pytest.mark.parametrize("timeout", [0, -1])
    def test_nonpositive_timeout_disables_the_deadline(self, tmp_path: Path, timeout: int) -> None:
        cmd = _spawn(tmp_path, {}, timeout_seconds=timeout)
        assert cmd[cmd.index("--timeout") + 1] == "0"

    def test_denied_gateway_refuses_before_writing(self, tmp_path: Path) -> None:
        with pytest.raises(NetworkPolicyDenied):
            _spawn(tmp_path, {**GATEWAY, "BERNSTEIN_NETWORK_POLICY": "none"}, model="m")
        assert not (tmp_path / ".sdd").exists()

    def test_restricted_policy_refuses_unknown_provider_endpoints(self, tmp_path: Path) -> None:
        with pytest.raises(NetworkPolicyDenied):
            _spawn(tmp_path, {"BERNSTEIN_NETWORK_POLICY": "example.test"})
        assert not (tmp_path / ".sdd").exists()

    def test_gateway_url_cannot_embed_credentials(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match=BASE_URL_ENV):
            _spawn(tmp_path, {**GATEWAY, BASE_URL_ENV: "https://user:secret@example.test/v1"}, model="m")
        assert not (tmp_path / ".sdd").exists()

    def test_no_gateway_keeps_config_out_of_the_launch(self, tmp_path: Path) -> None:
        cmd = _spawn(tmp_path, {}, model="openai/test")
        assert "--auth-env-only" in cmd
        assert "--config" not in cmd
        assert not list(tmp_path.rglob("*.json"))

    def test_missing_binary_is_reported_by_the_worker(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATH", str(tmp_path))
        result = OpenClawAdapter().spawn(
            prompt="p",
            workdir=tmp_path,
            model_config=ModelConfig(model="openai/test", effort="high"),
            session_id="oc-s3",
            timeout_seconds=0,
        )
        assert result.proc is not None
        assert cast("subprocess.Popen[bytes]", result.proc).wait(timeout=10) == 127
        assert "openclaw" in result.log_path.read_text()


def test_spawner_admits_and_records_the_gateway_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in GATEWAY.items():
        monkeypatch.setenv(key, value)
    spawner = AgentSpawner(
        OpenClawAdapter(), tmp_path, tmp_path, use_worktrees=False, default_model="task-model", adapter_pinned=True
    )
    task = Task(id="t1", title="test OpenClaw dispatch", description="synthetic", role="backend")
    with (
        patch.object(spawner, "_enforce_admission_policy") as admit,
        patch.object(spawner, "_preflight_adapter_admission"),
        patch.object(OpenClawAdapter, "spawn", autospec=True) as spawn,
    ):
        spawn.return_value = SpawnResult(pid=900, log_path=tmp_path / "agent.log")
        session = spawner.spawn_for_tasks([task])
    assert spawn.call_args.kwargs["mcp_config"]["base_url"] == GATEWAY[BASE_URL_ENV]
    assert admit.call_count == 2
    assert all(call.kwargs["subject"].endpoint == GATEWAY[BASE_URL_ENV] for call in admit.call_args_list)
    assert all(call.kwargs["subject"].adapter == "openclaw" for call in admit.call_args_list)
    assert session.endpoint_base_url == GATEWAY[BASE_URL_ENV]
    assert session.endpoint_model == "task-model"


def test_spawner_refuses_openclaw_in_a_container(tmp_path: Path) -> None:
    spawner = AgentSpawner(
        OpenClawAdapter(),
        tmp_path,
        tmp_path,
        use_worktrees=False,
        default_model="openai/test",
        adapter_pinned=True,
        sandbox=DockerSandbox(enabled=True),
    )
    task = Task(id="t1", title="test OpenClaw dispatch", description="synthetic", role="backend")
    with patch.object(OpenClawAdapter, "spawn") as spawn, pytest.raises(SpawnError, match="direct subprocess"):
        spawner.spawn_for_tasks([task])
    spawn.assert_not_called()


def test_name() -> None:
    assert OpenClawAdapter().name() == "OpenClaw"
