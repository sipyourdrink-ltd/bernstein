"""Unit tests for the swarm-migration map-reduce helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.tasks.swarm_migration import (
    MigrationPlan,
    SwarmChunkResult,
    chunk_targets,
    effective_max_parallel,
    enumerate_targets,
    mark_chunk_complete,
    reduce_swarm,
    spawn_swarm,
)


class _RecordingStore:
    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []
        self._counter = 0
        # Ids this store considers still in flight. spawn_swarm consults it via
        # is_task_active before respawning an unfinished chunk (issue #4624).
        self.active_ids: set[str] = set()

    def create_sync(self, body: dict[str, Any]) -> str:
        self._counter += 1
        self.bodies.append(body)
        task_id = f"task-{self._counter:03d}"
        self.active_ids.add(task_id)
        return task_id

    def is_task_active(self, task_id: str) -> bool:
        return task_id in self.active_ids


def _make_repo(tmp_path: Path, files: list[str]) -> Path:
    for rel in files:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# placeholder\n", encoding="utf-8")
    return tmp_path


def test_migration_plan_validates_inputs() -> None:
    with pytest.raises(ValueError, match="chunk_size"):
        MigrationPlan(id="x", glob="*.py", transform_prompt="t", chunk_size=0)
    with pytest.raises(ValueError, match="max_parallel"):
        MigrationPlan(id="x", glob="*.py", transform_prompt="t", max_parallel=0)
    with pytest.raises(ValueError, match="plan id"):
        MigrationPlan(id="   ", glob="*.py", transform_prompt="t")
    with pytest.raises(ValueError, match="glob"):
        MigrationPlan(id="x", glob="", transform_prompt="t")


def test_enumerate_targets_filters_hidden_and_excludes(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        [
            "src/a.py",
            "src/b.py",
            "src/sub/c.py",
            "src/skip_me.py",
            ".sdd/runtime/state.py",
            ".git/hooks/pre-commit.py",
        ],
    )
    plan = MigrationPlan(
        id="t",
        glob="src/**/*.py",
        transform_prompt="convert",
        excludes=("src/skip_me.py",),
    )
    targets = enumerate_targets(plan, repo)
    rels = sorted(p.relative_to(repo).as_posix() for p in targets)
    assert rels == ["src/a.py", "src/b.py", "src/sub/c.py"]


def test_chunk_targets_preserves_order_and_size() -> None:
    paths = [Path(f"f{i}.py") for i in range(7)]
    chunks = chunk_targets(paths, chunk_size=3)
    assert [len(c) for c in chunks] == [3, 3, 1]
    assert chunks[0][0] == Path("f0.py")
    assert chunks[2][0] == Path("f6.py")
    with pytest.raises(ValueError):
        chunk_targets(paths, chunk_size=0)


def test_effective_max_parallel_caps_correctly() -> None:
    plan = MigrationPlan(id="t", glob="*.py", transform_prompt="t", max_parallel=50)
    assert effective_max_parallel(plan, chunk_count=3) == 3
    assert effective_max_parallel(plan, chunk_count=100) == 20  # hard ceiling
    assert effective_max_parallel(plan, chunk_count=10, adaptive_cap=4) == 4
    assert effective_max_parallel(plan, chunk_count=10, adaptive_cap=0) == 10
    assert effective_max_parallel(plan, chunk_count=0) == 1  # never zero


def test_spawn_swarm_emits_one_task_per_chunk(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, [f"src/m{i}.py" for i in range(7)])
    plan = MigrationPlan(id="big", glob="src/*.py", transform_prompt="convert", chunk_size=3)
    store = _RecordingStore()

    ids = spawn_swarm(plan, store, repo)

    assert len(ids) == 3
    assert len(store.bodies) == 3
    first = store.bodies[0]
    assert first["role"] == "backend"
    assert first["scope"] == "small"
    assert first["metadata"]["swarm_plan_id"] == "big"
    assert first["metadata"]["swarm_chunk_index"] == 0
    assert first["metadata"]["swarm_total_chunks"] == 3
    assert len(first["owned_files"]) == 3

    cp_path = repo / ".sdd" / "runtime" / "swarm" / "big.json"
    assert cp_path.exists()
    cp = json.loads(cp_path.read_text())
    assert cp["chunk_count"] == 3
    assert len(cp["task_ids"]) == 3


def test_spawn_swarm_idempotent_skips_completed_chunks(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, [f"src/m{i}.py" for i in range(6)])
    plan = MigrationPlan(id="dedup", glob="src/*.py", transform_prompt="convert", chunk_size=3)
    store = _RecordingStore()

    first_ids = spawn_swarm(plan, store, repo)
    assert len(first_ids) == 2

    # mark all chunks complete; a second run must spawn nothing new.
    cp_path = repo / ".sdd" / "runtime" / "swarm" / "dedup.json"
    cp = json.loads(cp_path.read_text())
    for chunk_hash in cp["task_ids"]:
        mark_chunk_complete(plan.id, chunk_hash, repo)

    second_store = _RecordingStore()
    second_ids = spawn_swarm(plan, second_store, repo)
    assert second_ids == first_ids
    assert second_store.bodies == []


def test_inflight_chunk_is_reused_not_respawned(tmp_path: Path) -> None:
    """A mid-swarm re-run must not hand a second task the same files (#4624).

    Re-running ``bernstein migrate`` while chunks are still executing hits the
    same persistent server, which still reports those tasks active. spawn_swarm
    must reuse them rather than spawn duplicate owners of the same paths.
    """
    repo = _make_repo(tmp_path, [f"src/m{i}.py" for i in range(4)])
    plan = MigrationPlan(id="inflight", glob="src/*.py", transform_prompt="convert", chunk_size=1)
    store = _RecordingStore()

    first_ids = spawn_swarm(plan, store, repo)
    assert len(first_ids) == 4

    # Nothing resolved: every task is still in flight on the same store.
    second_ids = spawn_swarm(plan, store, repo)

    assert second_ids == first_ids
    assert len(store.bodies) == 4  # no chunk was respawned


def test_dead_but_unmarked_chunk_respawns(tmp_path: Path) -> None:
    """A task that died without a recorded failure is still respawned (#4624/#4541).

    is_task_active gates the reuse: a chunk whose task the server no longer
    reports active - a crash or out-of-band cancel that never reached
    mark_chunk_failed - falls through to a fresh spawn, so #4541's "never a dead
    id forever" guarantee survives the in-flight-reuse change.
    """
    repo = _make_repo(tmp_path, ["src/a.py", "src/b.py"])
    plan = MigrationPlan(id="dead", glob="src/*.py", transform_prompt="convert", chunk_size=1)
    store = _RecordingStore()

    first_ids = spawn_swarm(plan, store, repo)
    assert len(first_ids) == 2

    # Chunk 0's task dies silently; chunk 1 stays in flight.
    store.active_ids.discard(first_ids[0])
    second_ids = spawn_swarm(plan, store, repo)

    assert second_ids[0] != first_ids[0]  # dead task respawned
    assert second_ids[1] == first_ids[1]  # live task reused
    assert len(store.bodies) == 3  # exactly one respawn


def test_reduce_swarm_aggregates_pass_fail() -> None:
    results = [
        SwarmChunkResult(task_id="t1", chunk_hash="h1", files=("a.py", "b.py"), passed=True),
        SwarmChunkResult(task_id="t2", chunk_hash="h2", files=("c.py", "d.py"), passed=False, summary="syntax"),
        SwarmChunkResult(task_id="t3", chunk_hash="h3", files=("e.py",), passed=True),
    ]
    report = reduce_swarm("plan-x", results)
    assert report.plan_id == "plan-x"
    assert report.total_chunks == 3
    assert report.passed_chunks == 2
    assert report.failed_chunks == 1
    assert report.failed_files == ("c.py", "d.py")
    bulletin = report.to_bulletin_content()
    assert "plan=plan-x" in bulletin
    assert "chunks=2/3" in bulletin


def test_reduce_swarm_handles_empty_input() -> None:
    report = reduce_swarm("empty", [])
    assert report.total_chunks == 0
    assert report.passed_chunks == 0
    assert report.failed_files == ()
