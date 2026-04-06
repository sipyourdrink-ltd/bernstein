"""Tests for bernstein.core.claude_cost_tracking (CLAUDE-011)."""

from __future__ import annotations

import json

from bernstein.core.claude_cost_tracking import (
    CostTrackingAggregator,
    SessionCostData,
    parse_session_output,
)


class TestSessionCostData:
    def test_total_tokens(self) -> None:
        d = SessionCostData(input_tokens=100, output_tokens=50, cache_read_tokens=20)
        assert d.total_tokens == 170

    def test_to_dict(self) -> None:
        d = SessionCostData(session_id="s1", model="sonnet", input_tokens=100)
        result = d.to_dict()
        assert result["session_id"] == "s1"
        assert result["total_tokens"] == 100


class TestParseSessionOutput:
    def test_parse_stream_json_usage(self) -> None:
        output = json.dumps(
            {
                "type": "result",
                "usage": {"input_tokens": 1000, "output_tokens": 500},
                "cost_usd": 0.05,
            }
        )
        data = parse_session_output(output, session_id="s1", model="sonnet")
        assert data.input_tokens == 1000
        assert data.output_tokens == 500
        assert data.total_cost_usd == 0.05

    def test_parse_text_cost_line(self) -> None:
        output = "Some output\nTotal cost: $1.23\nDone."
        data = parse_session_output(output)
        assert data.total_cost_usd == 1.23

    def test_parse_text_token_line(self) -> None:
        output = "Input tokens: 12,345, Output tokens: 6,789"
        data = parse_session_output(output)
        assert data.input_tokens == 12345
        assert data.output_tokens == 6789

    def test_parse_cache_tokens(self) -> None:
        output = json.dumps(
            {
                "type": "result",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 200,
                    "cache_creation_input_tokens": 80,
                },
            }
        )
        data = parse_session_output(output)
        assert data.cache_read_tokens == 200
        assert data.cache_write_tokens == 80

    def test_parse_model_from_json(self) -> None:
        output = json.dumps({"type": "result", "model": "claude-sonnet-4-6"})
        data = parse_session_output(output)
        assert data.model == "claude-sonnet-4-6"

    def test_parse_duration(self) -> None:
        output = json.dumps({"type": "result", "duration_ms": 5000})
        data = parse_session_output(output)
        assert data.duration_s == 5.0

    def test_parse_empty_output(self) -> None:
        data = parse_session_output("")
        assert data.input_tokens == 0
        assert data.total_cost_usd == 0.0

    def test_parse_multi_line(self) -> None:
        output = "\n".join(
            [
                json.dumps({"type": "assistant", "message": "hello"}),
                json.dumps({"type": "assistant", "message": "world"}),
                json.dumps(
                    {
                        "type": "result",
                        "usage": {"input_tokens": 500, "output_tokens": 200},
                    }
                ),
            ]
        )
        data = parse_session_output(output)
        assert data.input_tokens == 500
        assert data.turns == 2

    def test_takes_max_values(self) -> None:
        output = "\n".join(
            [
                json.dumps({"usage": {"input_tokens": 100, "output_tokens": 50}}),
                json.dumps({"usage": {"input_tokens": 200, "output_tokens": 100}}),
            ]
        )
        data = parse_session_output(output)
        assert data.input_tokens == 200
        assert data.output_tokens == 100


class TestCostTrackingAggregator:
    def test_record_and_total(self) -> None:
        agg = CostTrackingAggregator()
        agg.record_session(SessionCostData(session_id="s1", total_cost_usd=1.0))
        agg.record_session(SessionCostData(session_id="s2", total_cost_usd=2.0))
        assert agg.total_cost_usd() == 3.0

    def test_total_tokens(self) -> None:
        agg = CostTrackingAggregator()
        agg.record_session(SessionCostData(session_id="s1", input_tokens=100, output_tokens=50))
        assert agg.total_tokens() == 150

    def test_summary_by_model(self) -> None:
        agg = CostTrackingAggregator()
        agg.record_session(SessionCostData(session_id="s1", model="sonnet", total_cost_usd=1.0))
        agg.record_session(SessionCostData(session_id="s2", model="opus", total_cost_usd=5.0))
        summary = agg.summary()
        assert summary["total_sessions"] == 2
        assert "sonnet" in summary["by_model"]
        assert "opus" in summary["by_model"]

    def test_empty_aggregator(self) -> None:
        agg = CostTrackingAggregator()
        assert agg.total_cost_usd() == 0.0
        assert agg.summary()["total_sessions"] == 0
