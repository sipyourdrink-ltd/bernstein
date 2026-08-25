"""A gate whose command is not installed must not read as failing evidence.

`_run_command` used to collapse every non-zero exit into `(False, output)`, so a
gate configured with a tool that is not on PATH reported the same way a gate
that ran and found real problems does: a lint failure. The two call for opposite
responses -- one is a defect in the diff, the other is a defect in the machine --
and an operator reading the run has no way to tell them apart.

Exit code 127 is the shell's answer for "command not found", so it is the one
signal that separates them. These tests pin both halves: that the code survives
the return path at all, and that the gate runner turns it into a distinct status
rather than a failure.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from bernstein.core.quality.quality_gates import _run_command


def test_missing_command_returns_its_exit_code(tmp_path: Path) -> None:
    """127 has to survive the return, or no caller can act on it."""
    result = _run_command("definitely-not-a-real-binary-4548", tmp_path, 30)
    assert len(result) == 3, "a missing command must carry its exit code back to the caller"
    ok, _detail, exit_code = result
    assert ok is False
    assert exit_code == 127


def test_a_command_that_runs_and_fails_keeps_the_two_tuple(tmp_path: Path) -> None:
    """The wider return shape must not disturb callers that unpack two values.

    Every existing caller writes `ok, detail = run_command_sync(...)`. If an
    ordinary failure started returning three values, all of them would break on
    the next release -- so the third element is reserved for 127 alone.
    """
    result = _run_command("exit 3", tmp_path, 30)
    assert len(result) == 2
    ok, _detail = result
    assert ok is False


def test_a_command_that_succeeds_keeps_the_two_tuple(tmp_path: Path) -> None:
    result = _run_command("true", tmp_path, 30)
    assert len(result) == 2
    assert result[0] is True


def test_gate_runner_reports_a_missing_command_as_command_not_found(tmp_path: Path) -> None:
    """The user-visible half: status is `command_not_found`, not `fail`.

    Driven through the real `_run_command_gate` rather than a stub, because the
    defect was in how the runner interpreted the return value -- a test against
    a fake would have passed the whole time the bug was shipping.
    """
    from bernstein.core.quality.gate_pipeline import GatePipelineStep
    from bernstein.core.quality.gate_runner import GateRunner
    from bernstein.core.quality.quality_gates import QualityGatesConfig

    runner = GateRunner(QualityGatesConfig(), tmp_path)
    step = GatePipelineStep(name="lint", required=True)

    result = asyncio.run(runner._run_command_gate(step, "definitely-not-a-real-binary-4548", tmp_path, 30))

    assert result.status == "command_not_found", (
        f"a gate whose tool is not installed reported {result.status!r}; "
        "that is indistinguishable from the tool running and finding problems"
    )
    assert "Command not found" in result.details
    assert result.blocked is True, "a required gate that could not run must still block"
