"""Tests for bernstein.core.claude_tool_result_injection (CLAUDE-013)."""

from __future__ import annotations

import json

from bernstein.core.claude_tool_result_injection import (
    GateResult,
    InjectionPayload,
    ToolResultInjector,
)


class TestGateResult:
    def test_to_dict(self) -> None:
        r = GateResult(
            gate_name="lint",
            passed=False,
            output="ruff error",
            errors=("E501 line too long",),
            command="ruff check .",
            exit_code=1,
        )
        d = r.to_dict()
        assert d["gate_name"] == "lint"
        assert not d["passed"]
        assert len(d["errors"]) == 1


class TestInjectionPayload:
    def test_to_context_text_passed(self) -> None:
        payload = InjectionPayload(
            gate_results=[GateResult(gate_name="lint", passed=True)],
            summary="1 gates passed",
            action_required=False,
        )
        text = payload.to_context_text()
        assert "PASSED" in text
        assert "Action Required" not in text

    def test_to_context_text_failed(self) -> None:
        payload = InjectionPayload(
            gate_results=[
                GateResult(
                    gate_name="lint",
                    passed=False,
                    errors=("E501 line too long", "W291 trailing whitespace"),
                )
            ],
            summary="1 gates failed",
            action_required=True,
        )
        text = payload.to_context_text()
        assert "FAILED" in text
        assert "E501" in text
        assert "Action Required" in text

    def test_to_json(self) -> None:
        payload = InjectionPayload(
            gate_results=[GateResult(gate_name="tests", passed=True)],
            summary="ok",
            action_required=False,
        )
        result = json.loads(payload.to_json())
        assert result["action_required"] is False

    def test_output_truncation(self) -> None:
        long_output = "\n".join(f"line {i}" for i in range(50))
        payload = InjectionPayload(
            gate_results=[GateResult(gate_name="lint", passed=False, output=long_output)],
            summary="failed",
            action_required=True,
        )
        text = payload.to_context_text()
        assert "truncated" in text


class TestToolResultInjector:
    def test_add_result(self) -> None:
        inj = ToolResultInjector()
        inj.add_result(GateResult(gate_name="lint", passed=True))
        assert inj.gate_count == 1

    def test_add_gate_output(self) -> None:
        inj = ToolResultInjector()
        result = inj.add_gate_output(
            "tests",
            passed=False,
            output="FAILED test_foo.py",
            errors=["AssertionError in test_bar"],
            command="pytest tests/ -x",
            exit_code=1,
        )
        assert result.gate_name == "tests"
        assert not result.passed

    def test_build_payload_mixed(self) -> None:
        inj = ToolResultInjector()
        inj.add_result(GateResult(gate_name="lint", passed=True))
        inj.add_result(GateResult(gate_name="tests", passed=False))
        payload = inj.build_payload()
        assert payload.action_required
        assert "1 gates passed" in payload.summary
        assert "1 gates failed" in payload.summary

    def test_build_payload_all_passed(self) -> None:
        inj = ToolResultInjector()
        inj.add_result(GateResult(gate_name="lint", passed=True))
        inj.add_result(GateResult(gate_name="tests", passed=True))
        payload = inj.build_payload()
        assert not payload.action_required

    def test_has_failures(self) -> None:
        inj = ToolResultInjector()
        inj.add_result(GateResult(gate_name="lint", passed=True))
        assert not inj.has_failures
        inj.add_result(GateResult(gate_name="tests", passed=False))
        assert inj.has_failures

    def test_clear(self) -> None:
        inj = ToolResultInjector()
        inj.add_result(GateResult(gate_name="lint", passed=True))
        inj.clear()
        assert inj.gate_count == 0

    def test_output_truncation(self) -> None:
        inj = ToolResultInjector(max_output_chars=100)
        result = inj.add_gate_output("lint", passed=False, output="x" * 200)
        assert len(result.output) < 200
        assert "truncated" in result.output

    def test_build_payload_json_format(self) -> None:
        inj = ToolResultInjector()
        inj.add_result(GateResult(gate_name="lint", passed=True))
        payload = inj.build_payload(fmt="json")
        assert payload.format == "json"

    def test_empty_injector(self) -> None:
        inj = ToolResultInjector()
        payload = inj.build_payload()
        assert not payload.action_required
        assert "No gates executed" in payload.summary
