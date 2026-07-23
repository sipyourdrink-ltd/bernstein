"""Unit tests for CLI-adapter token-usage capture (issue #2797).

Plain CLI adapters (qwen and friends) write no ``.tokens`` sidecar during a
run, so ``bernstein cost`` reported ``Tokens In 0`` / ``Tokens Out 0`` for
runs that made real model calls. These tests pin the parse-and-capture path
that gives the CLI-adapter route its own usage source, plus the downstream
recovery that turns the captured sidecar into nonzero cost-table token
counts. No network calls: the qwen usage payloads are representative
captures of ``qwen --output-format stream-json`` output.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from bernstein.core.cost.cli_adapter_usage import (
    capture_cli_adapter_usage,
    parse_stream_json_usage,
)

if TYPE_CHECKING:
    from pathlib import Path

# A representative ``qwen --output-format stream-json`` session log for a
# free route. Line-delimited JSON: a session_start, one assistant message
# with per-call usage, and a terminal result carrying the authoritative
# cumulative ``stats.models[<route>].tokens`` breakdown.
_QWEN_STREAM_JSON_LOG = "\n".join(
    [
        json.dumps(
            {
                "type": "system",
                "subtype": "session_start",
                "session_id": "s1",
                "model": "cohere/north-mini-code:free",
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "session_id": "s1",
                "message": {
                    "role": "assistant",
                    "model": "cohere/north-mini-code:free",
                    "content": [{"type": "text", "text": "Editing files..."}],
                    "usage": {"input_tokens": 1200, "output_tokens": 340},
                },
            }
        ),
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "session_id": "s1",
                "is_error": False,
                "duration_ms": 1234,
                "result": "done",
                "usage": {"input_tokens": 5120, "output_tokens": 880},
                "stats": {
                    "models": {
                        "cohere/north-mini-code:free": {
                            "api": {"total_requests": 3, "total_errors": 0},
                            "tokens": {
                                "prompt": 5120,
                                "completion": 880,
                                "total": 6000,
                                "cached": 1000,
                                "thoughts": 0,
                            },
                        }
                    }
                },
            }
        ),
    ]
)


class TestParseStreamJsonUsage:
    """parse_stream_json_usage extracts real in/out tokens and model."""

    def test_prefers_authoritative_stats_tokens(self) -> None:
        tokens_in, tokens_out, model = parse_stream_json_usage(_QWEN_STREAM_JSON_LOG)
        assert tokens_in == 5120
        assert tokens_out == 880
        assert model == "cohere/north-mini-code:free"

    def test_falls_back_to_result_usage_without_stats(self) -> None:
        log = "\n".join(
            [
                json.dumps({"type": "system", "subtype": "session_start", "model": "qwen3-coder-plus"}),
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "usage": {"input_tokens": 900, "output_tokens": 120},
                    }
                ),
            ]
        )
        tokens_in, tokens_out, model = parse_stream_json_usage(log)
        assert tokens_in == 900
        assert tokens_out == 120
        assert model == "qwen3-coder-plus"

    def test_sums_assistant_usage_without_result_or_stats(self) -> None:
        log = "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"model": "qwen3-coder-plus", "usage": {"input_tokens": 100, "output_tokens": 20}},
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"model": "qwen3-coder-plus", "usage": {"input_tokens": 300, "output_tokens": 40}},
                    }
                ),
            ]
        )
        tokens_in, tokens_out, model = parse_stream_json_usage(log)
        assert tokens_in == 400
        assert tokens_out == 60
        assert model == "qwen3-coder-plus"

    def test_single_object_output_format(self) -> None:
        # ``--output-format json`` returns one buffered object with stats.
        blob = json.dumps(
            {
                "response": "done",
                "stats": {"models": {"qwen3-coder-plus": {"tokens": {"prompt": 42, "completion": 7}}}},
            }
        )
        tokens_in, tokens_out, model = parse_stream_json_usage(blob)
        assert tokens_in == 42
        assert tokens_out == 7
        assert model == "qwen3-coder-plus"

    def test_empty_and_nonjson_log_returns_zeros(self) -> None:
        assert parse_stream_json_usage("") == (0, 0, "")
        assert parse_stream_json_usage("not json at all\njust text\n") == (0, 0, "")


class TestCaptureCliAdapterUsage:
    """capture_cli_adapter_usage materialises the recovery sidecar."""

    def _write_log(self, workdir: Path, session_id: str, text: str) -> Path:
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(text, encoding="utf-8")
        return log_path

    def test_writes_sidecar_from_qwen_log(self, tmp_path: Path) -> None:
        self._write_log(tmp_path, "s1", _QWEN_STREAM_JSON_LOG)
        tokens_in, tokens_out, model = capture_cli_adapter_usage(tmp_path, "s1")
        assert (tokens_in, tokens_out, model) == (5120, 880, "cohere/north-mini-code:free")

        sidecar = tmp_path / ".sdd" / "runtime" / "s1.tokens"
        assert sidecar.exists()
        rec = json.loads(sidecar.read_text(encoding="utf-8").strip())
        assert rec["in"] == 5120
        assert rec["out"] == 880

    def test_noop_when_sidecar_already_present(self, tmp_path: Path) -> None:
        # openai_agents / Claude wrapper already wrote a sidecar during the
        # run - capture must not double-count on top of it.
        self._write_log(tmp_path, "s1", _QWEN_STREAM_JSON_LOG)
        sidecar = tmp_path / ".sdd" / "runtime" / "s1.tokens"
        sidecar.write_text(json.dumps({"ts": 1.0, "in": 10, "out": 2}) + "\n", encoding="utf-8")

        result = capture_cli_adapter_usage(tmp_path, "s1")
        assert result == (0, 0, "")
        # Sidecar left untouched (single record).
        assert len(sidecar.read_text(encoding="utf-8").strip().splitlines()) == 1

    def test_noop_when_log_missing(self, tmp_path: Path) -> None:
        assert capture_cli_adapter_usage(tmp_path, "missing") == (0, 0, "")
        assert not (tmp_path / ".sdd" / "runtime" / "missing.tokens").exists()

    def test_explicit_log_path_wins(self, tmp_path: Path) -> None:
        log_path = tmp_path / "custom" / "qwen.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(_QWEN_STREAM_JSON_LOG, encoding="utf-8")
        tokens_in, tokens_out, _ = capture_cli_adapter_usage(tmp_path, "s1", log_path)
        assert tokens_in == 5120
        assert tokens_out == 880


class TestSidecarFeedsCostRecovery:
    """The captured sidecar becomes nonzero cost-table token counts."""

    def test_recovery_reads_nonzero_tokens_zero_usd_for_free_route(self, tmp_path: Path) -> None:
        from types import SimpleNamespace

        from bernstein.core.agents.agent_lifecycle import _read_runner_cost_usd

        log_path = tmp_path / ".sdd" / "runtime" / "s1.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(_QWEN_STREAM_JSON_LOG, encoding="utf-8")
        capture_cli_adapter_usage(tmp_path, "s1")

        session = SimpleNamespace(
            id="s1",
            model_config=SimpleNamespace(model="cohere/north-mini-code:free"),
        )
        cost_usd, tokens_in, tokens_out = _read_runner_cost_usd(tmp_path, session, "task-1")
        # Free route: dollars legitimately zero, tokens must be real.
        assert cost_usd == 0.0
        assert tokens_in == 5120
        assert tokens_out == 880
