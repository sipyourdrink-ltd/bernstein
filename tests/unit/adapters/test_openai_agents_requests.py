"""Offline request-body regressions using the real optional OpenAI Agents SDK."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bernstein.adapters.openai_agents import OpenAIAgentsAdapter
from bernstein.adapters.openai_agents_runner import EXIT_OK, RunnerManifest, run
from bernstein.core.agents.spawner_core import AgentSpawner
from bernstein.core.protocols.mcp.mcp_manager import MCPManager
from bernstein.core.routing.mode_profile import ModeProfile
from bernstein.core.tasks.models import ModelConfig, Task

type RequestEndpoint = tuple[str, list[tuple[str, dict[str, Any]]]]


@pytest.fixture
def request_endpoint(monkeypatch: pytest.MonkeyPatch) -> Iterator[RequestEndpoint]:
    """Record serialized SDK requests without credentials or external services."""
    sdk = pytest.importorskip("agents")
    openai = pytest.importorskip("openai")
    requests: list[tuple[str, dict[str, Any]]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, body))
            if self.path == "/v1/chat/completions":
                response = {
                    "id": "chatcmpl-offline",
                    "object": "chat.completion",
                    "created": 0,
                    "model": body["model"],
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
            else:
                response = {
                    "id": "resp_offline",
                    "object": "response",
                    "created_at": 0,
                    "model": body["model"],
                    "status": "completed",
                    "output": [
                        {
                            "id": "msg_offline",
                            "type": "message",
                            "status": "completed",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "ok", "annotations": []}],
                        }
                    ],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                }
            payload = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}/v1"
    real_client = openai.AsyncOpenAI

    def make_client(**kwargs: Any) -> Any:
        kwargs.setdefault("base_url", base_url)
        kwargs.setdefault("api_key", "offline-test-key")
        kwargs["max_retries"] = 0
        return real_client(**kwargs)

    monkeypatch.setattr(openai, "AsyncOpenAI", make_client)
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    sdk.set_tracing_disabled(True)
    # A preceding custom-endpoint case must not change the next case's route.
    sdk.set_default_openai_api("responses")
    try:
        yield base_url, requests
    finally:
        sdk.set_default_openai_api("responses")
        sdk.set_tracing_disabled(False)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _manifest(
    tmp_path: Path,
    model: str,
    base_url: str | None,
    *,
    settings: dict[str, Any] | None = None,
    role_policy: dict[str, Any] | None = None,
    with_mode_profile: bool = False,
) -> RunnerManifest:
    """Use Bernstein's manifest builder and, when requested, its sampling fold."""
    config = ModelConfig(model=model, effort="high")
    adapter = OpenAIAgentsAdapter()
    values: dict[str, Any] = {"api_key_env": "OPENAI_API_KEY", **(settings or {})}
    if base_url is not None:
        values["base_url"] = base_url
    if with_mode_profile:
        templates = tmp_path / "templates"
        templates.mkdir()
        spawner = AgentSpawner(adapter, templates, tmp_path, use_worktrees=False)
        values = (
            spawner._apply_sampling_overrides(
                values,
                role_policy=role_policy or {},
                model_config=config,
                tasks=[Task(id="offline-request", title="Reply ok", description="", role="backend")],
                provider_name="openai_agents",
            )
            or {}
        )
    values = MCPManager([]).build_mcp_config_for_task(task_mcp_servers=[], base_config=values) or {}
    return RunnerManifest.from_dict(
        adapter._build_manifest(
            prompt="Reply ok",
            workdir=tmp_path,
            model_config=config,
            session_id="offline-request",
            mcp_config=values,
            timeout_seconds=30,
            task_scope="small",
            budget_multiplier=1.0,
            system_addendum="",
        )
    )


@pytest.mark.parametrize("model", ["gpt-5-mini", "gpt-5-mini-2025-08-07"])
def test_custom_gpt5_mini_uses_completion_token_field(
    tmp_path: Path,
    request_endpoint: RequestEndpoint,
    model: str,
) -> None:
    base_url, requests = request_endpoint
    manifest = _manifest(tmp_path, model, base_url, settings={"max_tokens": 4096})
    assert run(manifest) == EXIT_OK
    assert len(requests) == 1
    path, body = requests[0]
    assert path == "/v1/chat/completions"
    assert body.get("max_completion_tokens") == 4096
    assert "max_tokens" not in body


@pytest.mark.parametrize("base_url_override", [False, True])
def test_compatible_model_retains_explicit_settings(
    tmp_path: Path,
    request_endpoint: RequestEndpoint,
    base_url_override: bool,
) -> None:
    base_url, requests = request_endpoint
    manifest = _manifest(
        tmp_path,
        "gpt-4.1-mini",
        base_url if base_url_override else None,
        settings={"max_tokens": 2048, "temperature": 0.7, "top_p": 0.9},
        with_mode_profile=True,
    )
    assert run(manifest) == EXIT_OK
    assert len(requests) == 1
    path, body = requests[0]
    assert path == ("/v1/chat/completions" if base_url_override else "/v1/responses")
    assert body["temperature"] == 0.7
    assert body["top_p"] == 0.9
    assert body["max_tokens" if base_url_override else "max_output_tokens"] == 2048
    assert "max_completion_tokens" not in body


def test_standard_gpt5_mini_preserves_responses_fix(
    tmp_path: Path,
    request_endpoint: RequestEndpoint,
) -> None:
    _, requests = request_endpoint
    manifest = _manifest(
        tmp_path,
        "gpt-5-mini",
        None,
        settings={"max_tokens": 4096, "temperature": 0.3, "top_p": 0.8},
    )
    assert run(manifest) == EXIT_OK
    assert len(requests) == 1
    path, body = requests[0]
    assert path == "/v1/responses"
    assert body["max_output_tokens"] == 4096
    assert "temperature" not in body
    assert "top_p" not in body
    assert "max_completion_tokens" not in body


@pytest.mark.parametrize("model", ["gpt-5-mini", "gpt-5-mini-2025-08-07", "kimi-k2.5"])
def test_custom_endpoint_omits_incompatible_profile_temperature(
    tmp_path: Path,
    request_endpoint: RequestEndpoint,
    model: str,
) -> None:
    base_url, requests = request_endpoint
    manifest = _manifest(tmp_path, model, base_url, with_mode_profile=True)
    assert run(manifest) == EXIT_OK
    assert len(requests) == 1
    assert "temperature" not in requests[0][1]


@pytest.mark.parametrize("custom_endpoint", [False, True])
def test_unconfigured_output_cap_is_omitted(
    tmp_path: Path,
    request_endpoint: RequestEndpoint,
    custom_endpoint: bool,
) -> None:
    base_url, requests = request_endpoint
    manifest = _manifest(tmp_path, "gpt-5-mini", base_url if custom_endpoint else None)
    assert run(manifest) == EXIT_OK
    assert len(requests) == 1
    assert not {"max_tokens", "max_completion_tokens", "max_output_tokens"}.intersection(requests[0][1])


@pytest.mark.parametrize("origin", ["mcp", "role"])
@pytest.mark.parametrize("model,temperature", [("gpt-5-mini", 0.3), ("kimi-k2.5", 1.0)])
def test_custom_endpoint_preserves_explicit_sampling_and_large_cap(
    tmp_path: Path,
    request_endpoint: RequestEndpoint,
    origin: str,
    model: str,
    temperature: float,
) -> None:
    # Equality with the implicit GPT-5 mini temperature must not turn an
    # operator choice into a default. Explicit-invalid policy remains unchanged.
    base_url, requests = request_endpoint
    explicit = {"temperature": temperature, "max_tokens": 200_000}
    manifest = _manifest(
        tmp_path,
        model,
        base_url,
        settings=explicit if origin == "mcp" else None,
        role_policy=explicit if origin == "role" else None,
        with_mode_profile=True,
    )
    assert run(manifest) == EXIT_OK
    assert len(requests) == 1
    body = requests[0][1]
    assert body["temperature"] == temperature
    assert body["max_completion_tokens" if model == "gpt-5-mini" else "max_tokens"] == 200_000


def test_profile_top_p_is_omitted_but_profile_token_cap_is_preserved(
    tmp_path: Path,
    request_endpoint: RequestEndpoint,
) -> None:
    base_url, requests = request_endpoint
    profile = ModeProfile(name="deep", system_prompt_preamble="", temperature=0.3, top_p=0.8, max_tokens=2048)
    with patch("bernstein.core.agents.spawner_prompt.select_mode", return_value=profile):
        manifest = _manifest(tmp_path, "gpt-5-mini", base_url, with_mode_profile=True)
    assert run(manifest) == EXIT_OK
    assert len(requests) == 1
    body = requests[0][1]
    assert "temperature" not in body
    assert "top_p" not in body
    assert body["max_completion_tokens"] == 2048


@pytest.mark.parametrize("model,temperature", [("compatible-model", 0.2), ("vendor/gpt-5-mini", 0.3)])
def test_other_model_keeps_its_profile_defaults(
    tmp_path: Path,
    request_endpoint: RequestEndpoint,
    model: str,
    temperature: float,
) -> None:
    base_url, requests = request_endpoint
    manifest = _manifest(tmp_path, model, base_url, with_mode_profile=True)
    assert run(manifest) == EXIT_OK
    assert len(requests) == 1
    assert requests[0][1]["temperature"] == temperature
