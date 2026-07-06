"""Spine-backed phase-gate boundary lineage (main-red regression guard).

``build_phased_runner_with_gate_lineage`` must route the per-boundary
lineage write through the canonical :class:`LineageSpine` write boundary
instead of the deprecated v1 ``LineageWriter`` (issue #2292 AC4). These
tests drive a real gate boundary through the returned runner and assert:

* the boundary produces a spine entry that ``LineageSpine.verify()``
  reports ``ok``, tagged with actor ``phase_gate:<phase>`` and the
  repo-relative phase-artifact path;
* two runs with a pinned timestamp produce a byte-identical spine entry
  for the same boundary + results, independent of ``PYTHONHASHSEED``.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

from bernstein.core.lineage.spine import LineageSpine
from bernstein.core.orchestration.phase_gate_lineage import (
    build_phased_runner_with_gate_lineage,
)
from bernstein.core.orchestration.phase_pipeline import (
    ArtifactStore,
    Phase,
    PhaseArtifact,
    PhasedRunner,
    PhaseSpec,
)
from bernstein.core.tasks.models import Complexity, Scope, Task, TaskStatus, TaskType

_KEY = b"k" * 32
_RUN_ID = "run-spine"

_SRC = Path(__file__).resolve().parents[2] / "src" / "bernstein"


def _task() -> Task:
    return Task(
        id="t-spine-1",
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
    return {
        Phase.RESEARCH: _research(),
        Phase.PLAN: _plan(),
        Phase.IMPLEMENT: _implement(),
    }[spec.phase]


def _run(sdd: Path, tmp_path: Path) -> list[object]:
    runner = build_phased_runner_with_gate_lineage(
        executor=_executor,
        sdd_dir=sdd,
        hmac_key=_KEY,
        run_id=_RUN_ID,
        store=ArtifactStore(root=tmp_path / "artifacts"),
    )
    assert isinstance(runner, PhasedRunner)
    return runner.run(_task())


def test_no_v1_writer_construction_in_builder_module() -> None:
    """The builder module constructs no deprecated v1 writer (guard mirror)."""
    module = _SRC / "core" / "orchestration" / "phase_gate_lineage.py"
    tree = ast.parse(module.read_text(encoding="utf-8"))
    forbidden = {"LineageRecorder", "LineageWriter"}
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in forbidden:
            hits.append(f"{func.id}(...) @ line {node.lineno}")
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in forbidden
            and func.attr in {"for_run", "create"}
        ):
            hits.append(f"{func.value.id}.{func.attr}(...) @ line {node.lineno}")
    assert hits == [], f"v1 writer construction leaked back into the builder: {hits}"


def test_boundary_write_lands_on_spine_and_verifies(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    results = _run(sdd, tmp_path)
    assert len(results) == 3

    spine = LineageSpine(sdd / "lineage", run_id=_RUN_ID, hmac_key=_KEY)
    verify = spine.verify()
    assert verify.ok, verify.errors

    entries = list(spine.iter_entries())
    # A phase-gate lineage entry per fired boundary (plus adjudication anchors).
    gate_entries = [e for e in entries if e.actor.startswith("phase_gate:")]
    assert gate_entries, "expected phase-gate boundary entries on the spine"

    for entry in gate_entries:
        # actor is phase_gate:<phase>, matching the v1 hook contract.
        assert entry.actor.startswith("phase_gate:")
        phase_value = entry.actor.split(":", 1)[1]
        # artifact_path is the repo-relative POSIX phase-artifact path.
        assert entry.artifact_path == f".sdd/runtime/phase_artifacts/{_task().id}/{phase_value}.json"
        assert not entry.artifact_path.startswith("/")
        assert ".." not in entry.artifact_path.split("/")
        # step_id is the boundary tick (from->to), model is the phase.
        assert "->" in entry.step_id
        assert entry.model == phase_value


def test_boundary_actor_and_step_id_match_v1_record(tmp_path: Path) -> None:
    """The plan boundary keeps actor=phase_gate:plan, step_id=research->plan."""
    sdd = tmp_path / ".sdd"
    _run(sdd, tmp_path)
    spine = LineageSpine(sdd / "lineage", run_id=_RUN_ID, hmac_key=_KEY)
    plan_entries = [e for e in spine.iter_entries() if e.actor == "phase_gate:plan"]
    assert plan_entries, "expected a phase_gate:plan boundary entry"
    assert plan_entries[0].step_id == "research->plan"
    assert plan_entries[0].model == "plan"


def _plan_entry_row(sdd: Path) -> bytes:
    spine = LineageSpine(sdd / "lineage", run_id=_RUN_ID, hmac_key=_KEY)
    for entry in spine.iter_entries():
        if entry.actor == "phase_gate:plan":
            return entry.to_row()
    raise AssertionError("no phase_gate:plan entry found")


def test_boundary_content_hash_is_deterministic(tmp_path: Path) -> None:
    """Same boundary + results => identical content_hash (no wall-clock in content)."""
    sdd_a = tmp_path / "a" / ".sdd"
    sdd_b = tmp_path / "b" / ".sdd"
    _run(sdd_a, tmp_path / "a")
    _run(sdd_b, tmp_path / "b")

    spine_a = LineageSpine(sdd_a / "lineage", run_id=_RUN_ID, hmac_key=_KEY)
    spine_b = LineageSpine(sdd_b / "lineage", run_id=_RUN_ID, hmac_key=_KEY)
    plan_a = next(e for e in spine_a.iter_entries() if e.actor == "phase_gate:plan")
    plan_b = next(e for e in spine_b.iter_entries() if e.actor == "phase_gate:plan")
    assert plan_a.content_hash == plan_b.content_hash
    # The full derived entry (hash + hmac) is byte-identical too.
    assert plan_a.to_row() == plan_b.to_row()


_BYTE_IDENTITY_SNIPPET = """
import sys
from pathlib import Path

sdd = Path(sys.argv[1]) / ".sdd"

from bernstein.core.lineage.spine import LineageSpine
from bernstein.core.orchestration.phase_gate_lineage import (
    build_phased_runner_with_gate_lineage,
)
from bernstein.core.orchestration.phase_pipeline import (
    ArtifactStore,
    Phase,
    PhaseArtifact,
    PhaseSpec,
)
from bernstein.core.tasks.models import Complexity, Scope, Task, TaskStatus, TaskType

KEY = b"k" * 32
RUN_ID = "run-spine"


def task():
    return Task(
        id="t-spine-1",
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


def executor(t, spec, prior):
    arts = {
        Phase.RESEARCH: PhaseArtifact(
            summary="research summary long enough to satisfy the strict schema",
            decisions=["use existing TaskStore <id:taskstore>"],
            constraints=["python 3.12", "pyright strict"],
            open_questions=[],
        ),
        Phase.PLAN: PhaseArtifact(
            summary="plan derived from research summary cleanly",
            decisions=["adopt <id:taskstore>"],
            constraints=["python 3.12", "pyright strict"],
            open_questions=[],
            extras={"dependencies": ["step1->step2"]},
        ),
        Phase.IMPLEMENT: PhaseArtifact(
            summary="implement follows the plan without dropping constraints",
            decisions=["adopt <id:taskstore>"],
            constraints=["python 3.12", "pyright strict"],
            open_questions=[],
            extras={
                "files_changed": ["src/foo.py"],
                "tests_added": ["tests/unit/test_foo.py"],
                "tests_passing": ["tests/unit/test_foo.py::test_smoke"],
            },
        ),
    }
    return arts[spec.phase]


runner = build_phased_runner_with_gate_lineage(
    executor=executor,
    sdd_dir=sdd,
    hmac_key=KEY,
    run_id=RUN_ID,
    store=ArtifactStore(root=Path(sys.argv[1]) / "artifacts"),
)
runner.run(task())
spine = LineageSpine(sdd / "lineage", run_id=RUN_ID, hmac_key=KEY)
row = next(e for e in spine.iter_entries() if e.actor == "phase_gate:plan").to_row()
sys.stdout.buffer.write(row)
"""


def _run_in_subprocess(work: Path, hashseed: str) -> bytes:
    work.mkdir(parents=True, exist_ok=True)
    script = work / "driver.py"
    script.write_text(_BYTE_IDENTITY_SNIPPET, encoding="utf-8")
    env = {"PYTHONHASHSEED": hashseed}
    import os

    full_env = {**os.environ, **env}
    proc = subprocess.run(
        [sys.executable, str(script), str(work)],
        capture_output=True,
        check=True,
        env=full_env,
    )
    return proc.stdout


def test_byte_identical_across_pythonhashseed(tmp_path: Path) -> None:
    """PYTHONHASHSEED=0 and =999 produce the byte-identical spine row."""
    row_0 = _run_in_subprocess(tmp_path / "seed0", "0")
    row_999 = _run_in_subprocess(tmp_path / "seed999", "999")
    assert row_0 == row_999
    assert row_0, "expected a non-empty phase_gate:plan spine row"
