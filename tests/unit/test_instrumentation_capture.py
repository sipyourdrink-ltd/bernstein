"""Unit tests for RunInstrumenter content/result capture and batched-task fan-out.

Covers the instrumentation-module half of the batched-task instrumentation
fix: the runner (``openai_agents_runner.run``) passes ``extra_dirs`` to
``init_instrumenter`` and ``content``/``result`` to ``log_message``/
``log_tool_call``; these tests pin the module-side contract those calls
depend on.
"""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.instrumentation import (
    _MESSAGE_CONTENT_TRUNCATE_CHARS,
    RunInstrumenter,
    init_instrumenter,
)


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestLogMessageContent:
    def test_content_recorded(self, tmp_path: Path) -> None:
        inst = RunInstrumenter(run_id="r1", task_id="t1", agent_id="a1", base_dir=tmp_path / "agent")
        inst.log_message(idx=0, role="user", content_length=5, content="hello")
        records = _read_jsonl(tmp_path / "agent" / "conversation.jsonl")
        assert records[0]["content"] == "hello"
        assert records[0]["content_length"] == 5

    def test_content_none_keeps_shape_only_record(self, tmp_path: Path) -> None:
        inst = RunInstrumenter(run_id="r1", task_id="t1", agent_id="a1", base_dir=tmp_path / "agent")
        inst.log_message(idx=0, role="user", content_length=5)
        records = _read_jsonl(tmp_path / "agent" / "conversation.jsonl")
        assert "content" not in records[0]

    def test_content_truncated_but_length_true(self, tmp_path: Path) -> None:
        inst = RunInstrumenter(run_id="r1", task_id="t1", agent_id="a1", base_dir=tmp_path / "agent")
        huge = "x" * (_MESSAGE_CONTENT_TRUNCATE_CHARS + 100)
        inst.log_message(idx=0, role="assistant", content_length=len(huge), content=huge)
        records = _read_jsonl(tmp_path / "agent" / "conversation.jsonl")
        content = records[0]["content"]
        assert isinstance(content, str)
        assert len(content) < len(huge)
        assert content.endswith("...[truncated]")
        assert records[0]["content_length"] == len(huge)


class TestLogToolCallResult:
    def test_result_recorded(self, tmp_path: Path) -> None:
        inst = RunInstrumenter(run_id="r1", task_id="t1", agent_id="a1", base_dir=tmp_path / "agent")
        inst.log_tool_call(
            call_id="c1",
            ts_start="2026-01-01T00:00:00+00:00",
            ts_end="2026-01-01T00:00:01+00:00",
            tool="read_file",
            success=True,
            result={"bytes": 42, "body": "abc"},
        )
        records = _read_jsonl(tmp_path / "agent" / "tool-calls.jsonl")
        assert records[0]["result"] == {"bytes": 42, "body": "abc"}

    def test_result_none_serialized_as_null(self, tmp_path: Path) -> None:
        inst = RunInstrumenter(run_id="r1", task_id="t1", agent_id="a1", base_dir=tmp_path / "agent")
        inst.log_tool_call(
            call_id="c1",
            ts_start="2026-01-01T00:00:00+00:00",
            ts_end="2026-01-01T00:00:01+00:00",
            tool="read_file",
            success=False,
            error="Boom: failed",
        )
        records = _read_jsonl(tmp_path / "agent" / "tool-calls.jsonl")
        assert records[0]["result"] is None

    def test_huge_string_result_truncated(self, tmp_path: Path) -> None:
        inst = RunInstrumenter(run_id="r1", task_id="t1", agent_id="a1", base_dir=tmp_path / "agent")
        inst.log_tool_call(
            call_id="c1",
            ts_start="2026-01-01T00:00:00+00:00",
            ts_end="2026-01-01T00:00:01+00:00",
            tool="read_file",
            success=True,
            result="y" * 10_000,
        )
        records = _read_jsonl(tmp_path / "agent" / "tool-calls.jsonl")
        result = records[0]["result"]
        assert isinstance(result, str)
        assert len(result) < 10_000
        assert result.endswith("...[truncated]")


class TestExtraDirsFanOut:
    def test_writes_mirrored_to_every_extra_dir(self, tmp_path: Path) -> None:
        base = tmp_path / "t1" / "agents" / "a1"
        extra_a = tmp_path / "t2" / "agents" / "a1"
        extra_b = tmp_path / "t3" / "agents" / "a1"
        inst = init_instrumenter(run_id="r1", task_id="t1", agent_id="a1", base_dir=base, extra_dirs=[extra_a, extra_b])
        inst.log_message(idx=0, role="user", content_length=2, content="hi")
        inst.log_tool_call(
            call_id="c1",
            ts_start="2026-01-01T00:00:00+00:00",
            ts_end="2026-01-01T00:00:01+00:00",
            tool="ls",
            success=True,
            result="ok",
        )
        for root in (base, extra_a, extra_b):
            conv = _read_jsonl(root / "conversation.jsonl")
            assert conv[0]["content"] == "hi"
            tools = _read_jsonl(root / "tool-calls.jsonl")
            assert tools[0]["result"] == "ok"

    def test_no_extra_dirs_matches_prior_behaviour(self, tmp_path: Path) -> None:
        inst = init_instrumenter(run_id="r1", task_id="t1", agent_id="a1", base_dir=tmp_path / "agent")
        inst.log_message(idx=0, role="user", content_length=2, content="hi")
        assert (tmp_path / "agent" / "conversation.jsonl").exists()

    def test_unwritable_extra_dir_does_not_break_primary(self, tmp_path: Path) -> None:
        base = tmp_path / "t1" / "agents" / "a1"
        # A regular FILE at the extra-dir path makes mkdir raise OSError.
        blocked = tmp_path / "blocked"
        blocked.write_text("not a dir", encoding="utf-8")
        inst = RunInstrumenter(run_id="r1", task_id="t1", agent_id="a1", base_dir=base, extra_dirs=[blocked])
        inst.log_message(idx=0, role="user", content_length=2, content="hi")
        records = _read_jsonl(base / "conversation.jsonl")
        assert records[0]["content"] == "hi"
        assert blocked.read_text(encoding="utf-8") == "not a dir"


class TestLogToolCallCoverage:
    def test_coverage_record_round_trips_with_call_id(self, tmp_path: Path) -> None:
        inst = RunInstrumenter(run_id="r1", task_id="t1", agent_id="a1", base_dir=tmp_path / "agent")
        coverage = {
            "file_count": 5,
            "corpus_digest": "sha256:1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
            "coverage": "complete",
            "truncated": False,
            "truncation_reason": None,
            "exit_status": 0,
            "exit_checked": True,
        }
        inst.log_tool_call(
            call_id="c-cov-1",
            ts_start="2026-01-01T00:00:00+00:00",
            ts_end="2026-01-01T00:00:01+00:00",
            tool="list_dir",
            success=True,
            result=["a.py", "b.py"],
            coverage=coverage,
        )
        records = _read_jsonl(tmp_path / "agent" / "tool-calls.jsonl")
        assert len(records) == 1
        assert records[0]["call_id"] == "c-cov-1"
        assert records[0]["coverage"] == coverage

    def test_coverage_record_not_truncated_by_result_cap(self, tmp_path: Path) -> None:
        inst = RunInstrumenter(run_id="r1", task_id="t1", agent_id="a1", base_dir=tmp_path / "agent")
        long_digest = "sha256:" + "a" * 600
        coverage = {
            "file_count": 100,
            "corpus_digest": long_digest,
            "coverage": "complete",
            "truncated": False,
            "truncation_reason": None,
            "exit_status": 0,
            "exit_checked": True,
        }
        inst.log_tool_call(
            call_id="c-cov-2",
            ts_start="2026-01-01T00:00:00+00:00",
            ts_end="2026-01-01T00:00:01+00:00",
            tool="search_files",
            success=True,
            coverage=coverage,
        )
        records = _read_jsonl(tmp_path / "agent" / "tool-calls.jsonl")
        recorded_cov = records[0].get("coverage")
        assert isinstance(recorded_cov, dict)
        assert recorded_cov["corpus_digest"] == long_digest
        assert not recorded_cov["corpus_digest"].endswith("...[truncated]")

    def test_truncated_walk_coverage_reflects_truncation(self, tmp_path: Path) -> None:
        inst = RunInstrumenter(run_id="r1", task_id="t1", agent_id="a1", base_dir=tmp_path / "agent")
        coverage = {
            "file_count": 50,
            "corpus_digest": "sha256:fedcba",
            "coverage": "partial",
            "truncated": True,
            "truncation_reason": "timeout",
            "exit_status": "timeout",
            "exit_checked": True,
        }
        inst.log_tool_call(
            call_id="c-cov-3",
            ts_start="2026-01-01T00:00:00+00:00",
            ts_end="2026-01-01T00:00:05+00:00",
            tool="list_dir",
            success=False,
            coverage=coverage,
        )
        records = _read_jsonl(tmp_path / "agent" / "tool-calls.jsonl")
        rec = records[0]
        assert rec["coverage"]["truncated"] is True
        assert rec["coverage"]["truncation_reason"] == "timeout"
        assert rec["coverage"]["coverage"] == "partial"

    def test_absent_coverage_differs_from_present_empty_coverage(self, tmp_path: Path) -> None:
        inst = RunInstrumenter(run_id="r1", task_id="t1", agent_id="a1", base_dir=tmp_path / "agent")
        inst.log_tool_call(
            call_id="c-nocov",
            ts_start="2026-01-01T00:00:00+00:00",
            ts_end="2026-01-01T00:00:01+00:00",
            tool="list_dir",
            success=True,
        )
        empty_cov = {
            "file_count": 0,
            "corpus_digest": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "coverage": "complete",
            "truncated": False,
            "truncation_reason": None,
            "exit_status": 0,
            "exit_checked": True,
        }
        inst.log_tool_call(
            call_id="c-emptycov",
            ts_start="2026-01-01T00:00:00+00:00",
            ts_end="2026-01-01T00:00:01+00:00",
            tool="list_dir",
            success=True,
            coverage=empty_cov,
        )
        records = _read_jsonl(tmp_path / "agent" / "tool-calls.jsonl")
        assert "coverage" not in records[0] or records[0]["coverage"] is None
        assert records[1]["coverage"] == empty_cov
