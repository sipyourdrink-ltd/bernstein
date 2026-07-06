"""Live-caller wiring for the phase-gate lineage hook (issue #2294, AC1).

Historically ``make_lineage_hook`` had zero live callers, so a gate decision
left no attestable record. This wires it into the phased runner via a factory
that binds the hook AND emits a signed adjudication record per boundary. The
factory is the live caller AC1 requires.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.orchestration.phase_gate_lineage import (
    build_phased_runner_with_gate_lineage,
)
from bernstein.core.orchestration.phase_pipeline import (
    Phase,
    PhaseArtifact,
    PhasedRunner,
    PhaseSpec,
)
from bernstein.core.tasks.models import Complexity, Scope, Task, TaskStatus, TaskType

_KEY = b"k" * 32


def _task() -> Task:
    return Task(
        id="t-wire-1",
        title="title",
        description="desc",
        role="backend",
        priority=2,
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        status=TaskStatus.OPEN,
        task_type=TaskType.STANDARD,
        metadata={"phases": ["research", "plan", "implement"]},
    )


def _research() -> PhaseArtifact:
    return PhaseArtifact(
        summary="research summary long enough to satisfy the strict schema",
        decisions=["use existing TaskStore <id:taskstore>"],
        constraints=["python 3.12", "pyright strict"],
        open_questions=[],
    )


def _plan() -> PhaseArtifact:
    return PhaseArtifact(
        summary="plan derived from research summary cleanly",
        decisions=["adopt <id:taskstore>"],
        constraints=["python 3.12", "pyright strict"],
        open_questions=[],
        extras={"dependencies": ["step1->step2"]},
    )


def _implement() -> PhaseArtifact:
    return PhaseArtifact(
        summary="implement follows the plan without dropping constraints",
        decisions=["adopt <id:taskstore>"],
        constraints=["python 3.12", "pyright strict"],
        open_questions=[],
        extras={
            "files_changed": ["src/foo.py"],
            "tests_added": ["tests/unit/test_foo.py"],
            "tests_passing": ["tests/unit/test_foo.py::test_smoke"],
        },
    )


def _executor(task: Task, spec: PhaseSpec, prior: PhaseArtifact | None) -> PhaseArtifact:
    return {Phase.RESEARCH: _research(), Phase.PLAN: _plan(), Phase.IMPLEMENT: _implement()}[spec.phase]


def test_factory_returns_runner_with_live_hook(tmp_path: Path) -> None:
    runner = build_phased_runner_with_gate_lineage(
        executor=_executor,
        sdd_dir=tmp_path / ".sdd",
        hmac_key=_KEY,
    )
    assert isinstance(runner, PhasedRunner)
    assert runner.gate_lineage_hook is not None


def test_wired_runner_writes_lineage_and_adjudication_records(tmp_path: Path) -> None:
    from bernstein.core.orchestration.phase_pipeline import ArtifactStore
    from bernstein.core.persistence.lineage import LineageReader

    sdd = tmp_path / ".sdd"
    runner = build_phased_runner_with_gate_lineage(
        executor=_executor,
        sdd_dir=sdd,
        hmac_key=_KEY,
        run_id="run-wire",
        store=ArtifactStore(root=tmp_path / "artifacts"),
    )
    results = runner.run(_task())
    assert len(results) == 3

    # A lineage record per boundary (research entry + plan + implement).
    reader = LineageReader(sdd)
    records = list(reader.iter_records())
    assert records, "expected phase-gate lineage records"
    assert all(r.regulatory_class == "phase_gate" for r in records)

    # The adjudication spine has at least one signed record anchored.
    from bernstein.core.lineage.spine import LineageSpine

    spine = LineageSpine(sdd / "lineage", run_id="run-wire", hmac_key=_KEY)
    result = spine.verify()
    assert result.ok, result.errors
