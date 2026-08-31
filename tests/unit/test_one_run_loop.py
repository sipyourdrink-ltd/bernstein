"""There is exactly one orchestrator run loop, and it paces through one seam.

ORCH-009 opened ``orchestrator_run.py`` to hold the run loop, with
``Orchestrator`` as the public facade. The extraction landed and the delegation
did not, so two loops existed and only one ran. The dead one then drifted: it
hardcoded the failure ceiling the live loop reads from config, never recorded
``RunClosureOutcome.FAILED``, and kept a bare ``time.sleep`` after #4872 gave
the live loop its ``_pace`` seam - so the repository held both a bug and its
fix, with only the bug's fix unreachable.

Deleting the duplicate is not self-enforcing: the next decomposition can
recreate it, and nothing would notice for as long as nothing called it. These
tests pin the two properties that made the drift expensive - one loop, and one
pacing seam on it.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

from bernstein.core.orchestration import orchestrator_run
from bernstein.core.orchestration.orchestrator import Orchestrator

ORCHESTRATION_DIR = Path(orchestrator_run.__file__).resolve().parent


def test_the_run_loop_module_holds_no_second_loop() -> None:
    """``orchestrator_run`` must not reacquire a run loop or its parts.

    Named individually rather than as "no public functions" because the module
    legitimately keeps the dependency-scan helpers; the loop is the part that
    must not come back.
    """
    resurrected = [
        name
        for name in ("run", "_run_loop", "_adaptive_sleep", "_run_startup", "_run_shutdown")
        if hasattr(orchestrator_run, name)
    ]
    assert not resurrected, (
        f"orchestrator_run has regrown run-loop functions {resurrected}. The loop lives in "
        "Orchestrator.run(); a second copy drifts silently because nothing calls it."
    )


def test_orchestrator_run_paces_through_the_seam_and_never_sleeps_directly() -> None:
    """``Orchestrator.run`` must reach ``time.sleep`` only via ``_pace``.

    ``time.sleep`` is one object shared by the whole process, so a test that
    patches it sees every sleep taken while ``run()`` is on the stack. #4872
    routed the loop's own pacing through ``_pace`` so the schedule could be
    observed on its own; a direct call added later silently reopens that.
    """
    # dedent: getsource on a method returns it at class indentation, which ast rejects
    tree = ast.parse(textwrap.dedent(inspect.getsource(Orchestrator.run)))
    direct = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "sleep"
    ]
    assert not direct, (
        f"Orchestrator.run calls time.sleep directly at offset(s) {direct}; pace through "
        "self._pace() so a test can observe the loop's own schedule and nothing else."
    )


def test_no_module_in_orchestration_defines_a_rival_adaptive_sleep() -> None:
    """The adaptive-backoff schedule must have one definition.

    Two copies is how the ceiling and the closure outcome drifted apart in the
    first place: each fix landed in whichever copy its author was reading.
    """
    definitions: list[str] = []
    for path in sorted(ORCHESTRATION_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_adaptive_sleep":
                definitions.append(f"{path.name}:{node.lineno}")
    assert not definitions, f"rival adaptive-sleep definition(s): {definitions}"
