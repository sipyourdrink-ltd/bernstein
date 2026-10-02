"""Unit tests for OpenClawAdapter."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.openclaw import API_KEY_ENV, BASE_URL_ENV, MODEL_ENV, PROVIDER_ID, OpenClawAdapter
from tests.unit._adapter_test_helpers import inner_cmd, make_popen_mock

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.usefixtures("no_watchdog_threads")

GATEWAY = {BASE_URL_ENV: "http://gw.test/v1", API_KEY_ENV: "sk-openclaw-secret"}


def _spawn(tmp_path: Path, env: dict[str, str], model: str = "auto", **kwargs: object) -> list[str]:
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
    for name in (BASE_URL_ENV, API_KEY_ENV, MODEL_ENV):
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
            "--model",
            "anthropic/claude-x",
            "--timeout",
            "900",
        ]
        assert prompt_file.read_text() == "fix the bug\n\nbe brief"

    def test_auto_model_is_left_to_openclaw(self, tmp_path: Path) -> None:
        assert "--model" not in _spawn(tmp_path, {})

    def test_gateway_runs_against_a_generated_config_only(self, tmp_path: Path) -> None:
        cmd = _spawn(tmp_path, {**GATEWAY, MODEL_ENV: "gw-model"}, model="ignored")
        config_path = tmp_path / ".sdd" / "runtime" / "openclaw-openclaw-s1.json"
        assert cmd[cmd.index("--config") + 1] == str(config_path)
        assert "--auth-env-only" in cmd
        assert cmd[cmd.index("--model") + 1] == f"{PROVIDER_ID}/gw-model"
        text = config_path.read_text()
        assert "sk-openclaw-secret" not in text
        provider = json.loads(text)["models"]["providers"][PROVIDER_ID]
        assert provider["baseUrl"] == "http://gw.test/v1"
        assert provider["apiKey"] == "${BERNSTEIN_OPENCLAW_OPENAI_API_KEY}"
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
        assert env[API_KEY_ENV] == "sk-openclaw-secret"
        assert "UNRELATED_SECRET" not in env
        assert "sk-openclaw-secret" not in " ".join(popen.call_args.args[0])

    @pytest.mark.parametrize(
        ("env", "model", "missing"),
        [
            ({BASE_URL_ENV: "http://gw.test/v1"}, "m", API_KEY_ENV),
            ({API_KEY_ENV: "k"}, "m", BASE_URL_ENV),
            (GATEWAY, "auto", MODEL_ENV),
        ],
    )
    def test_incomplete_gateway_settings_name_what_is_missing(
        self, tmp_path: Path, env: dict[str, str], model: str, missing: str
    ) -> None:
        with pytest.raises(RuntimeError, match=missing):
            _spawn(tmp_path, env, model=model)

    def test_translates_missing_cli(self, tmp_path: Path) -> None:
        with (
            patch("bernstein.adapters.openclaw.subprocess.Popen", side_effect=FileNotFoundError("nope")),
            pytest.raises(RuntimeError, match="openclaw not found") as excinfo,
        ):
            OpenClawAdapter().spawn(
                prompt="p", workdir=tmp_path, model_config=ModelConfig(model="m", effort="high"), session_id="oc-s3"
            )
        assert "npm install -g openclaw" in str(excinfo.value)


def test_name() -> None:
    assert OpenClawAdapter().name() == "OpenClaw"
