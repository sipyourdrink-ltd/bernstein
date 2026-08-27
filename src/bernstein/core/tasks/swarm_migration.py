"""Swarm migration: deterministic file-level fanout for high-cardinality changes.

Framework migrations, lint-rule rollouts, and API renames want
deterministic, file-level fanout with many subagents in parallel rather
than the manager-LLM 2-5 split implemented by
:mod:`bernstein.core.tasks.task_splitter`.

The flow is:

    plan = MigrationPlan(glob="src/**/*.py", transform_prompt="...", ...)
    targets = enumerate_targets(plan, repo_root)
    chunks = chunk_targets(targets, plan.chunk_size)
    task_ids = spawn_swarm(plan, store, repo_root)            # one task per chunk
    # ... agents execute, then orchestrator collects per-task results ...
    report = reduce_swarm(plan_id, child_results)             # pass/fail aggregate

Idempotency is provided via a checkpoint file under
``<repo_root>/.sdd/runtime/swarm/{plan_id}.json`` - re-running with the
same ``plan.id`` skips chunks whose hash already appears in the
checkpoint's ``completed_chunks`` set (verified done) and reuses the task
of any chunk still in flight (issue #4624); a chunk recorded in
``failed_chunks`` after its reopen budget was exhausted, or one whose task
is gone, gets a fresh task on the next call instead of returning its old
task id from memory. The orchestrator (``task_lifecycle.py``) calls
:func:`mark_chunk_complete` / :func:`mark_chunk_failed` /
:func:`maybe_reduce_swarm` as chunk tasks reach a terminal state; once every
chunk has resolved, the aggregate :class:`SwarmReport` is written into the
same checkpoint file under ``report``.

The actual code-transformation logic is delegated to spawned agents
exactly like any other task; this module only owns enumeration,
chunking, fanout, and reduction.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_CHUNK_SIZE = 5
_DEFAULT_MAX_PARALLEL = 20
_HARD_PARALLEL_CEILING = 20

_SWARM_TAG = "[swarm-migration]"


class _SwarmTaskStore(Protocol):
    """Minimal task-store surface used by :func:`spawn_swarm`.

    Designed so the production :class:`bernstein.core.tasks.task_store.TaskStore`
    and lightweight in-memory test doubles satisfy the same contract
    without dragging the full async server into unit tests.
    """

    def create_sync(self, body: dict[str, Any]) -> str:
        """Create a task and return its server-assigned id."""
        ...

    def is_task_active(self, task_id: str) -> bool:
        """Report whether *task_id* still exists and may still be working.

        "Active" means any non-terminal state - the task could still be
        editing its ``owned_files`` right now. :func:`spawn_swarm` consults
        this before respawning an unfinished chunk so a mid-swarm re-run of
        ``bernstein migrate`` reuses the in-flight task instead of handing a
        second task the same files (issue #4624). A store that cannot answer
        - unreachable server, unknown id - returns ``False`` so the chunk
        respawns exactly as it did before this method existed, preserving the
        #4541 guarantee that a dead task id is never returned forever.
        """
        ...


@dataclass(frozen=True)
class MigrationPlan:
    """Description of one swarm-migration run.

    Attributes:
        id: Stable identifier used for checkpointing and idempotency.
        glob: Repo-relative glob pattern enumerating migration targets
            (e.g. ``"src/**/*.py"``).
        transform_prompt: Free-text instruction for the spawned agents,
            describing the code transformation to apply.
        chunk_size: Number of files handed to a single subagent.
        max_parallel: Upper bound on concurrent swarm tasks.  The actual
            cap is the minimum of this value, the chunk count, and
            ``_HARD_PARALLEL_CEILING``.
        role: Role assigned to spawned tasks.
        title_prefix: Human-readable prefix for spawned task titles.
        excludes: Optional repo-relative glob patterns to exclude after
            primary enumeration (e.g. ``("**/__pycache__/**",)``).
    """

    id: str
    glob: str
    transform_prompt: str
    chunk_size: int = _DEFAULT_CHUNK_SIZE
    max_parallel: int = _DEFAULT_MAX_PARALLEL
    role: str = "backend"
    title_prefix: str = "swarm-migrate"
    excludes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.chunk_size < 1:
            raise ValueError(f"chunk_size must be >= 1, got {self.chunk_size}")
        if self.max_parallel < 1:
            raise ValueError(f"max_parallel must be >= 1, got {self.max_parallel}")
        if not self.id.strip():
            raise ValueError("plan id must be non-empty")
        if not self.glob.strip():
            raise ValueError("glob must be non-empty")


@dataclass(frozen=True)
class SwarmChunkResult:
    """Outcome reported for a single swarm chunk."""

    task_id: str
    chunk_hash: str
    files: tuple[str, ...]
    passed: bool
    summary: str = ""


@dataclass(frozen=True)
class SwarmReport:
    """Aggregated report posted to the bulletin board after reduction."""

    plan_id: str
    total_chunks: int
    passed_chunks: int
    failed_chunks: int
    skipped_chunks: int
    failed_files: tuple[str, ...]
    timestamp: float = field(default_factory=time.time)

    def to_bulletin_content(self) -> str:
        """Render a one-line bulletin summary."""
        return (
            f"{_SWARM_TAG} plan={self.plan_id} "
            f"chunks={self.passed_chunks}/{self.total_chunks} "
            f"failed={self.failed_chunks} skipped={self.skipped_chunks}"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict (frozen dataclasses ship via asdict)."""
        return asdict(self)


def enumerate_targets(plan: MigrationPlan, repo_root: Path) -> list[Path]:
    """Resolve ``plan.glob`` against ``repo_root`` and return matching files.

    Hidden directories (anything whose name starts with ``.``) are
    skipped to avoid descending into ``.git`` / ``.sdd`` / venvs by
    accident.  Symlinks are not followed for the same reason.
    Results are sorted for deterministic chunking.
    """
    if not repo_root.exists():
        raise FileNotFoundError(f"repo_root does not exist: {repo_root}")

    matched = {p.resolve() for p in repo_root.glob(plan.glob) if p.is_file()}
    excluded: set[Path] = set()
    for pattern in plan.excludes:
        excluded.update(p.resolve() for p in repo_root.glob(pattern) if p.is_file())

    def _is_hidden(p: Path) -> bool:
        try:
            rel_parts = p.relative_to(repo_root.resolve()).parts
        except ValueError:
            return False
        return any(part.startswith(".") for part in rel_parts)

    final = [p for p in sorted(matched - excluded) if not _is_hidden(p)]
    logger.info(
        "swarm_migration: enumerated %d target(s) for plan=%s glob=%s",
        len(final),
        plan.id,
        plan.glob,
    )
    return final


def chunk_targets(targets: list[Path], chunk_size: int) -> list[list[Path]]:
    """Split *targets* into fixed-size chunks, preserving order."""
    if chunk_size < 1:
        raise ValueError(f"chunk_size must be >= 1, got {chunk_size}")
    return [targets[i : i + chunk_size] for i in range(0, len(targets), chunk_size)]


def _chunk_hash(plan_id: str, files: list[Path]) -> str:
    """Stable chunk hash derived from plan id and the chunk's file paths."""
    h = hashlib.sha256(plan_id.encode("utf-8"))
    h.update(b"\x00")
    for f in files:
        h.update(str(f).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


def _checkpoint_path(repo_root: Path, plan_id: str) -> Path:
    safe_id = plan_id.replace("/", "_").replace(" ", "_")
    return repo_root / ".sdd" / "runtime" / "swarm" / f"{safe_id}.json"


def _empty_checkpoint() -> dict[str, Any]:
    return {"completed_chunks": [], "failed_chunks": {}, "task_ids": {}}


def _load_checkpoint(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty_checkpoint()
    try:
        loaded: Any = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise TypeError("checkpoint root must be an object")
        loaded.setdefault("completed_chunks", [])
        loaded.setdefault("failed_chunks", {})
        loaded.setdefault("task_ids", {})
        return loaded
    except (OSError, ValueError, TypeError):
        logger.warning("swarm_migration: corrupt checkpoint at %s; ignoring", path)
        return _empty_checkpoint()


def _write_checkpoint(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def effective_max_parallel(plan: MigrationPlan, chunk_count: int, adaptive_cap: int | None = None) -> int:
    """Compute the effective concurrent-spawn cap for a swarm.

    The cap is ``min(plan.max_parallel, chunk_count, _HARD_PARALLEL_CEILING)``
    intersected with the adaptive-parallelism cap when supplied.
    """
    cap = min(plan.max_parallel, max(chunk_count, 1), _HARD_PARALLEL_CEILING)
    if adaptive_cap is not None and adaptive_cap > 0:
        cap = min(cap, adaptive_cap)
    return max(cap, 1)


def _build_task_body(
    plan: MigrationPlan,
    chunk: list[Path],
    chunk_hash: str,
    repo_root: Path,
    chunk_index: int,
    total_chunks: int,
) -> dict[str, Any]:
    rel_files = [str(p.relative_to(repo_root.resolve())) for p in chunk]
    description = (
        f"{_SWARM_TAG} plan={plan.id} chunk={chunk_index + 1}/{total_chunks} hash={chunk_hash}\n\n"
        f"Apply the following transformation to each file in the chunk:\n\n"
        f"{plan.transform_prompt}\n\n"
        f"Files:\n" + "\n".join(f"- {p}" for p in rel_files)
    )
    return {
        "title": f"{plan.title_prefix}: {plan.id} [{chunk_index + 1}/{total_chunks}]",
        "description": description,
        "role": plan.role,
        "priority": 2,
        "scope": "small",
        "complexity": "medium",
        "owned_files": rel_files,
        "metadata": {
            "swarm_plan_id": plan.id,
            "swarm_chunk_hash": chunk_hash,
            "swarm_chunk_index": chunk_index,
            "swarm_total_chunks": total_chunks,
        },
    }


def spawn_swarm(
    plan: MigrationPlan,
    store: _SwarmTaskStore,
    repo_root: Path,
    *,
    adaptive_cap: int | None = None,
) -> list[str]:
    """Enumerate, chunk, and fan out one task per chunk.

    Idempotent on *verified completion only* (issue #4541): a chunk is
    skipped and its task id reused once :func:`mark_chunk_complete` has
    recorded it as done. A chunk that was *verified failed* via
    :func:`mark_chunk_failed` once its reopen budget was exhausted, or that
    never ran at all, gets a fresh task id, so a restart can always make
    progress past a chunk that permanently failed instead of returning the
    same dead id forever.

    An unfinished chunk that still has a *live* task from an earlier call is
    the one case that is **not** respawned (issue #4624): re-running
    ``bernstein migrate`` while a swarm is still in flight would otherwise
    create a second task owning the same files, and two agents editing the
    same paths corrupt each other's work. Such a chunk is detected via
    :meth:`_SwarmTaskStore.is_task_active` on its recorded id and its
    existing task is reused; only when that id is gone or terminal (a crash
    that never reached :func:`mark_chunk_failed`, an out-of-band cancel) does
    the chunk respawn, so the #4541 guarantee still holds. The task-id memory
    in the checkpoint (``task_ids``) is bookkeeping for the reduce step, not a
    second idempotency key.

    Args:
        plan: Migration plan describing the glob and transform.
        store: Anything satisfying :class:`_SwarmTaskStore` (production
            store or test double).
        repo_root: Repo root used to resolve ``plan.glob`` and the
            checkpoint location under ``.sdd/runtime/swarm/``.
        adaptive_cap: Optional cap injected by the caller (typically
            :class:`bernstein.core.orchestration.adaptive_parallelism.AdaptiveParallelism`).
            A non-positive value is treated as "no cap".

    Returns:
        List of task ids that exist for the plan after this call -
        previously-recorded ids first (in chunk order), then any newly
        spawned ones.
    """
    targets = enumerate_targets(plan, repo_root)
    chunks = chunk_targets(targets, plan.chunk_size)
    cap = effective_max_parallel(plan, len(chunks), adaptive_cap)
    logger.info(
        "swarm_migration: plan=%s chunks=%d effective_max_parallel=%d",
        plan.id,
        len(chunks),
        cap,
    )

    cp_path = _checkpoint_path(repo_root, plan.id)
    checkpoint = _load_checkpoint(cp_path)
    completed: set[str] = set(checkpoint["completed_chunks"])
    failed: dict[str, Any] = dict(checkpoint["failed_chunks"])
    known: dict[str, str] = dict(checkpoint["task_ids"])

    respawned_a_failure = False
    ordered_ids: list[str] = []
    for idx, chunk in enumerate(chunks):
        chunk_hash = _chunk_hash(plan.id, chunk)
        if chunk_hash in completed:
            ordered_ids.append(known[chunk_hash])
            continue
        prior_id = known.get(chunk_hash)
        if prior_id is not None and chunk_hash not in failed and store.is_task_active(prior_id):
            # Unfinished, but its earlier task is still in flight (issue #4624):
            # reuse it rather than spawn a second owner of the same files. A
            # verified failure (in ``failed``) is deliberately excluded - it is
            # meant to respawn - as is a task the store no longer reports active.
            ordered_ids.append(prior_id)
            continue
        if chunk_hash in failed:
            respawned_a_failure = True
        body = _build_task_body(plan, chunk, chunk_hash, repo_root, idx, len(chunks))
        task_id = store.create_sync(body)
        known[chunk_hash] = task_id
        failed.pop(chunk_hash, None)
        ordered_ids.append(task_id)

    checkpoint["task_ids"] = known
    checkpoint["completed_chunks"] = sorted(completed)
    checkpoint["failed_chunks"] = failed
    checkpoint["plan_id"] = plan.id
    checkpoint["chunk_count"] = len(chunks)
    checkpoint["effective_max_parallel"] = cap
    checkpoint["updated_at"] = time.time()
    if respawned_a_failure:
        # A stale report no longer describes the plan; let it reduce again.
        checkpoint.pop("reduced", None)
        checkpoint.pop("report", None)
    _write_checkpoint(cp_path, checkpoint)
    return ordered_ids


def mark_chunk_complete(plan_id: str, chunk_hash: str, repo_root: Path) -> None:
    """Record *chunk_hash* as completed for *plan_id*.

    Called by the orchestrator (or tests) after a swarm task finishes
    successfully so a re-run of the same plan skips the chunk.
    """
    cp_path = _checkpoint_path(repo_root, plan_id)
    checkpoint = _load_checkpoint(cp_path)
    completed: set[str] = set(checkpoint["completed_chunks"])
    completed.add(chunk_hash)
    failed: dict[str, Any] = dict(checkpoint["failed_chunks"])
    failed.pop(chunk_hash, None)
    checkpoint["completed_chunks"] = sorted(completed)
    checkpoint["failed_chunks"] = failed
    checkpoint["plan_id"] = plan_id
    checkpoint["updated_at"] = time.time()
    _write_checkpoint(cp_path, checkpoint)


def mark_chunk_failed(
    plan_id: str,
    chunk_hash: str,
    repo_root: Path,
    *,
    files: tuple[str, ...] = (),
    reason: str = "",
) -> None:
    """Record *chunk_hash* as permanently failed for *plan_id*.

    Called by the orchestrator once a swarm task's reopen budget is
    exhausted, so the chunk is distinguishable in the checkpoint from one
    that never ran (issue #4541) and :func:`spawn_swarm` respawns it under a
    fresh task id on the plan's next invocation instead of returning the
    same dead id forever.
    """
    cp_path = _checkpoint_path(repo_root, plan_id)
    checkpoint = _load_checkpoint(cp_path)
    completed: set[str] = set(checkpoint["completed_chunks"])
    completed.discard(chunk_hash)
    failed: dict[str, Any] = dict(checkpoint["failed_chunks"])
    failed[chunk_hash] = {"reason": reason, "files": list(files)}
    checkpoint["completed_chunks"] = sorted(completed)
    checkpoint["failed_chunks"] = failed
    checkpoint["plan_id"] = plan_id
    checkpoint["updated_at"] = time.time()
    _write_checkpoint(cp_path, checkpoint)


def maybe_reduce_swarm(plan_id: str, repo_root: Path) -> SwarmReport | None:
    """Aggregate and durably record *plan_id*'s report once every chunk resolves.

    Safe to call after every chunk completion or failure: a no-op until
    ``completed_chunks`` and ``failed_chunks`` together account for every
    chunk the plan was spawned with, and a no-op again afterwards - the
    checkpoint's own ``reduced`` flag is checked and set in the same
    load/write round trip used by every other checkpoint mutation in this
    module, so two chunks landing back to back in the same orchestrator
    tick cannot both trigger a second reduce. The report is written into the
    same checkpoint file the chunk completions already live in (rather than
    a separate run artifact) so a plan's full outcome - which chunks passed,
    which failed and why - stays in the one durable place idempotency
    already depends on.
    """
    cp_path = _checkpoint_path(repo_root, plan_id)
    checkpoint = _load_checkpoint(cp_path)
    chunk_count = checkpoint.get("chunk_count")
    if not chunk_count or checkpoint.get("reduced"):
        return None
    completed: set[str] = set(checkpoint["completed_chunks"])
    failed: dict[str, Any] = checkpoint["failed_chunks"]
    if len(completed) + len(failed) < chunk_count:
        return None

    known: dict[str, str] = checkpoint["task_ids"]
    child_results = [
        SwarmChunkResult(task_id=known.get(h, ""), chunk_hash=h, files=(), passed=True) for h in sorted(completed)
    ] + [
        SwarmChunkResult(
            task_id=known.get(h, ""),
            chunk_hash=h,
            files=tuple(info.get("files", ())),
            passed=False,
            summary=info.get("reason", ""),
        )
        for h, info in sorted(failed.items())
    ]
    report = reduce_swarm(plan_id, child_results)
    checkpoint["reduced"] = True
    checkpoint["report"] = report.to_dict()
    checkpoint["updated_at"] = time.time()
    _write_checkpoint(cp_path, checkpoint)
    return report


def reduce_swarm(plan_id: str, child_results: list[SwarmChunkResult]) -> SwarmReport:
    """Aggregate per-chunk outcomes into a single :class:`SwarmReport`.

    Failed files are collected from every non-passing chunk so the
    operator can re-target them with a follow-up plan.
    """
    total = len(child_results)
    passed = sum(1 for r in child_results if r.passed)
    failed_files: list[str] = []
    for r in child_results:
        if not r.passed:
            failed_files.extend(r.files)
    report = SwarmReport(
        plan_id=plan_id,
        total_chunks=total,
        passed_chunks=passed,
        failed_chunks=total - passed,
        skipped_chunks=0,
        failed_files=tuple(failed_files),
    )
    logger.info(
        "swarm_migration: plan=%s reduced %d/%d chunks passed (%d failed files)",
        plan_id,
        report.passed_chunks,
        report.total_chunks,
        len(report.failed_files),
    )
    return report
