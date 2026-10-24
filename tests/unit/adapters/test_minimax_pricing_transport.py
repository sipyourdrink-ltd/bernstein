"""Exercise compatible-provider pricing through the real SDK's HTTP boundary."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from bernstein.adapters import openai_agents_builtins as builtins
from bernstein.adapters import openai_agents_runner as runner


@pytest.mark.parametrize(
    ("model", "base_url", "expected_cost"),
    [
        ("MiniMax-M3.1-Flash", "https://api.minimax.io/v1", 0.003),
        ("MiniMax-M3", "https://api.minimax.io/v1", 0.012),
        ("MiniMax-M2.7", "https://api.minimax.io/v1", 0.006),
        ("saved-custom-model", "https://custom.example/v1", 0.0),
        ("MiniMax-M3.1-Flash", "https://custom.example/v1", 0.003),
        ("minimax/minimax-m3", "https://openrouter.ai/api/v1", 0.012),
        ("gpt-5-mini", "https://custom.example/v1", 0.012),
    ],
)
def test_runner_prices_real_sdk_tool_roundtrip(
    model: str,
    base_url: str,
    expected_cost: float,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Keep explicit IDs, tools and history intact while metering Flash separately."""
    agents = pytest.importorskip("agents")
    openai = pytest.importorskip("openai")
    requests: list[dict[str, Any]] = []
    calls: list[str] = []

    @agents.function_tool
    def echo(value: str) -> str:
        """Echo a value back to the caller."""
        calls.append(value)
        return value

    def respond(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"{base_url}/chat/completions"
        body = json.loads(request.content)
        requests.append(body)
        message: dict[str, Any] = {"role": "assistant", "content": "done"}
        if len(requests) == 1:
            message = {
                "role": "assistant",
                "content": "I will echo the value.",
                "reasoning_content": "Preserve this reasoning across the tool round trip.",
                "tool_calls": [
                    {
                        "id": "call_echo",
                        "type": "function",
                        "function": {"name": "echo", "arguments": '{"value":"retained"}'},
                    }
                ],
            }
        return httpx.Response(
            200,
            json={
                "id": f"chatcmpl-{len(requests)}",
                "object": "chat.completion",
                "created": 1,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls" if len(requests) == 1 else "stop",
                    }
                ],
                "usage": {"prompt_tokens": 2_000, "completion_tokens": 2_000, "total_tokens": 4_000},
            },
        )

    real_client = openai.AsyncOpenAI

    def offline_client(**kwargs: Any) -> Any:
        return real_client(**kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))

    monkeypatch.setattr(openai, "AsyncOpenAI", offline_client)
    monkeypatch.setattr(builtins, "build_builtin_tools", lambda *args, **kwargs: [echo])
    monkeypatch.setenv("MINIMAX_API_KEY", "offline-placeholder")
    manifest = runner.RunnerManifest(
        session_id="pricing-transport",
        model=model,
        prompt="Echo retained, then finish.",
        workdir=str(tmp_path),
        heartbeat_dir=str(tmp_path / "heartbeats"),
        base_url=base_url,
        api_key_env="MINIMAX_API_KEY",
        tool_source="builtin",
        max_turns=3,
    )
    assert runner.run(manifest) == runner.EXIT_OK
    assert calls == ["retained"]
    assert len(requests) == 2
    for body in requests:
        assert body["model"] == model
        assert body["tools"][0]["function"]["name"] == "echo"
        for unsupported in ("thinking", "reasoning_effort", "reasoning_split", "service_tier"):
            assert unsupported not in body
    history = requests[1]["messages"]
    assert any(item.get("tool_calls", [{}])[0].get("id") == "call_echo" for item in history)
    assert any(item.get("tool_call_id") == "call_echo" and item["content"] == "retained" for item in history)
    assert any(item.get("content") == "I will echo the value." for item in history)
    reasoning_replayed = any(
        item.get("reasoning_content") == "Preserve this reasoning across the tool round trip." for item in history
    )
    assert reasoning_replayed is (model.startswith("MiniMax-") and base_url == "https://api.minimax.io/v1")
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    usage = next(event for event in events if event["type"] == "usage")
    assert usage["model"] == model
    assert usage["input_tokens"] == 4_000
    assert usage["output_tokens"] == 4_000
    assert usage["cost_usd"] == pytest.approx(expected_cost)
    assert usage["priced"] is (model != "saved-custom-model")


@pytest.mark.parametrize("origin", [None, "MiniMax-M3", "different-model"])
def test_minimax_reasoning_from_another_model_is_not_replayed(origin: str | None) -> None:
    context = SimpleNamespace(model="MiniMax-M3.1-Flash", reasoning=SimpleNamespace(origin_model=origin))
    assert runner._should_replay_minimax_reasoning(context) is False
