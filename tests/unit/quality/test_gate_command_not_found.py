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

The runner returns one shape, always ``(ok, detail, exit_code)``. Handing back
two values for an ordinary failure and three for a 127 made every caller
re-derive the shape before it could read anything, and the dead-code gate did
not - it unpacked two and would have raised on the very exit code this change
exists to report. One shape is what makes the signal safe to consume.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from bernstein.core.quality.quality_gates import NO_EXIT_CODE, _run_command


def test_missing_command_returns_its_exit_code(tmp_path: Path) -> None:
    """127 has to survive the return, or no caller can act on it."""
    ok, _detail, exit_code = _run_command("definitely-not-a-real-binary-4548", tmp_path, 30)
    assert ok is False
    assert exit_code == 127


def test_an_ordinary_failure_reports_its_own_exit_code(tmp_path: Path) -> None:
    """A non-zero exit that is not 127 must be distinguishable from one that is."""
    ok, _detail, exit_code = _run_command("exit 3", tmp_path, 30)
    assert ok is False
    assert exit_code == 3, "an ordinary failure must carry its own code, not 127 and not a placeholder"


def test_a_command_that_succeeds_reports_zero(tmp_path: Path) -> None:
    ok, _detail, exit_code = _run_command("true", tmp_path, 30)
    assert ok is True
    assert exit_code == 0


def test_every_return_path_carries_an_exit_code(tmp_path: Path) -> None:
    """Including the paths where no process ever reported one.

    A timeout kill and an OS-level spawn failure have no exit code of their own.
    They still return three values, because a caller that has to ask how many it
    got is a caller that will one day forget to ask - which is exactly how the
    dead-code gate ended up unpacking two.
    """
    ok, detail, exit_code = _run_command("sleep 5", tmp_path, 1)
    assert ok is False
    assert detail.startswith("Timed out")
    assert exit_code == NO_EXIT_CODE


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
