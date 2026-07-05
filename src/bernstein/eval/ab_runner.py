"""A/B runner primitive - deterministic prompt-vs-prompt comparison.

This is the *primitive* layer for the eval harness + A/B. It runs two
prompt variants over the same task set, scores each output, and produces a
deterministic comparison artefact (JSON-serialisable). Synthetic / dummy
executors are first-class so this slice has zero LLM-cost test path.

Design notes:
    * Pure functions; no I/O coupling beyond explicit ``executor`` /
      ``scorer`` callables passed in.
    * Deterministic ordering: tasks iterate in input order; comparison
      output uses ``sort_keys`` JSON dump for stable diffs.
    * Companion to ``bernstein.core.quality.ab_test`` (model-vs-model on a
      single live task via httpx). This module covers prompt-vs-prompt
      offline / synthetic eval.
    * Real runs dispatch through the normal spawn path: the task server
      creates each run as an ordinary task executed in its own isolated
      worktree (:func:`spawn_executor`). The spend ledger rows those runs
      produce are the only source of cost figures - :class:`ArmCost`
      references them by ledger line hash, never by re-estimation.
    * Benchmark loaders (SWE-bench Pro, Terminal-Bench) are intentionally
      out of scope - see ``feat-swe-bench-pro-terminal-bench-nightly``.
"""

from __future__ import annotations

import json
import logging
import statistics
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.security.sanitize import sanitize_log

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Variant:
    """A prompt or model variant under test.

    Attributes:
        name: Human-readable variant id (e.g. ``"reviewer-v1"``).
        prompt: Prompt template / system prompt body.
        model: Optional model hint (``"haiku"`` etc.); not interpreted by
            the runner - surfaced for downstream executors.
        metadata: Free-form extra fields persisted into the comparison.
    """

    name: str
    prompt: str
    model: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Task:
    """A single evaluation task.

    Attributes:
        task_id: Stable identifier (used for grouping in the comparison).
        input: Task input - typed as ``Any`` so YAML / synthetic / real
            payloads all flow through.
        expected: Optional reference answer used by deterministic scorers.
    """

    task_id: str
    input: Any
    expected: Any = None


@dataclass(frozen=True)
class RunResult:
    """One executor invocation's outcome for a (variant, task) pair.

    Attributes:
        variant: Variant name that produced ``output``.
        task_id: Task that was executed.
        output: Raw executor output (string, dict, …).
        score: Normalised score in ``[0.0, 1.0]``.
        duration_ms: Wall-clock duration in milliseconds.
        passed: Whether the task is considered successful (score >= 0.5
            by default; can be overridden by the scorer).
        ledger_task_id: Join key into the spend ledger - the ``task_id``
            the run's LLM calls were recorded under. Empty for executors
            that produce no ledger rows (e.g. :func:`echo_executor`).
    """

    variant: str
    task_id: str
    output: Any
    score: float
    duration_ms: float = 0.0
    passed: bool = False
    ledger_task_id: str = ""


@dataclass(frozen=True)
class ArmCost:
    """Ledger-sourced cost figures for one comparison arm.

    Every figure is a sum over concrete spend-ledger rows; the rows are
    referenced by the SHA-256 of their raw JSONL line bytes so a
    verifier holding the ledger can resolve each reference and recompute
    the sums. Nothing here is estimated.

    Attributes:
        arm: Arm (variant) name the figures belong to.
        entries: Number of ledger rows referenced.
        input_tokens: Sum of ``input_tokens`` over the referenced rows.
        output_tokens: Sum of ``output_tokens`` over the referenced rows.
        cost_usd: Sum of ``cost_usd`` over the referenced rows, rounded
            to 6 decimals.
        ledger_refs: SHA-256 hex digests of the referenced raw ledger
            lines, in ledger file order.
    """

    arm: str
    entries: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    ledger_refs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a deterministic dict suitable for JSON serialisation."""
        return {
            "arm": self.arm,
            "entries": self.entries,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
            "ledger_refs": list(self.ledger_refs),
        }


@dataclass(frozen=True)
class VariantStats:
    """Aggregate stats for one variant across the task set.

    Attributes:
        name: Variant name.
        n: Total task count for this variant.
        pass_count: Number of tasks where ``passed`` is True.
        pass_rate: ``pass_count / n`` (0.0 when n=0).
        mean_score: Arithmetic mean of per-task scores.
        mean_duration_ms: Arithmetic mean of per-task durations.
    """

    name: str
    n: int
    pass_count: int
    pass_rate: float
    mean_score: float
    mean_duration_ms: float


@dataclass(frozen=True)
class TaskDelta:
    """Per-task score delta between A and B (B - A).

    Attributes:
        task_id: Task identifier.
        score_a: Variant A's score (NaN-safe: 0.0 if missing).
        score_b: Variant B's score.
        delta: ``score_b - score_a``; positive => B beats A.
    """

    task_id: str
    score_a: float
    score_b: float
    delta: float


@dataclass(frozen=True)
class Comparison:
    """A/B comparison artefact - the deliverable of this primitive.

    Attributes:
        variant_a: Stats for the A side.
        variant_b: Stats for the B side.
        per_task: Per-task deltas, in input order.
        winner: ``"a"``, ``"b"``, or ``"tie"`` based on pass-rate then
            mean-score (5% tolerance band).
        reason: Human-readable explanation.
        cost_a: Ledger-sourced cost figures for the A side, or ``None``
            when the runs produced no ledger rows (synthetic executors).
        cost_b: Ledger-sourced cost figures for the B side.
    """

    variant_a: VariantStats
    variant_b: VariantStats
    per_task: tuple[TaskDelta, ...]
    winner: str
    reason: str
    cost_a: ArmCost | None = None
    cost_b: ArmCost | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a deterministic dict suitable for JSON serialisation.

        The ``cost_a`` / ``cost_b`` keys appear only when cost figures
        were attached, so pre-cost consumers keep seeing the original
        shape byte-for-byte.
        """
        out: dict[str, Any] = {
            "variant_a": _stats_to_dict(self.variant_a),
            "variant_b": _stats_to_dict(self.variant_b),
            "per_task": [_delta_to_dict(d) for d in self.per_task],
            "winner": self.winner,
            "reason": self.reason,
        }
        if self.cost_a is not None:
            out["cost_a"] = self.cost_a.to_dict()
        if self.cost_b is not None:
            out["cost_b"] = self.cost_b.to_dict()
        return out

    def to_json(self, *, indent: int = 2) -> str:
        """Render as a deterministic JSON string."""
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)


# ---------------------------------------------------------------------------
# Executor / scorer protocols (typed callables, no Protocol class needed)
# ---------------------------------------------------------------------------


Executor = Callable[[Variant, Task], RunResult]
"""Synchronous executor: run one variant on one task, return RunResult.

Implementations should set ``score`` and ``passed`` themselves *or* leave
them at default (0.0 / False) and rely on the scorer.
"""

Scorer = Callable[[Variant, Task, Any], tuple[float, bool]]
"""Optional scorer: ``(variant, task, raw_output) -> (score, passed)``.

When supplied to :func:`run_ab`, the scorer runs after the executor and
overwrites the executor's score / passed. Use this for deterministic
post-hoc grading.
"""


# ---------------------------------------------------------------------------
# Built-in scorers (synthetic-friendly)
# ---------------------------------------------------------------------------


def exact_match_scorer(_variant: Variant, task: Task, output: Any) -> tuple[float, bool]:
    """Score 1.0 iff ``str(output) == str(task.expected)``.

    Args:
        _variant: Unused (kept for protocol compatibility).
        task: The task being scored - uses ``task.expected``.
        output: Raw executor output.

    Returns:
        ``(1.0, True)`` on exact-string match, else ``(0.0, False)``.
    """
    matched = str(output) == str(task.expected)
    return (1.0 if matched else 0.0, matched)


# ---------------------------------------------------------------------------
# Built-in executor for tests / dry-runs
# ---------------------------------------------------------------------------


def echo_executor(variant: Variant, task: Task) -> RunResult:
    """Deterministic dummy executor - returns ``f"{prompt}::{input}"``.

    Used by the test fixtures and as a smoke-test default. Score is left
    at 0.0; pair with a scorer (e.g. :func:`exact_match_scorer`) to make
    a meaningful comparison.

    Args:
        variant: Variant whose prompt is echoed.
        task: Task whose input is echoed.

    Returns:
        ``RunResult`` with deterministic ``output`` and zero duration.
    """
    output = f"{variant.prompt}::{task.input}"
    return RunResult(
        variant=variant.name,
        task_id=task.task_id,
        output=output,
        score=0.0,
        duration_ms=0.0,
        passed=False,
    )


# ---------------------------------------------------------------------------
# Real executor - dispatch through the normal spawn path
# ---------------------------------------------------------------------------

#: Variant.metadata key naming the response-style profile a run is spawned
#: under. The spawn path resolves it via ``Task.metadata['mode']`` and
#: stamps ``response_profile`` into every ledger row the run produces.
VARIANT_PROFILE_KEY = "response_profile"

#: Variant.metadata key carrying a plain-text addendum appended to the
#: task description (used by the minimal-control arm, which is not a
#: named profile).
VARIANT_ADDENDUM_KEY = "prompt_addendum"

_SPAWN_TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled", "closed"})
_SPAWN_PASS_STATUSES = frozenset({"done", "closed"})


def spawn_executor(
    server_url: str,
    *,
    role: str = "backend",
    scope: str = "small",
    timeout_seconds: int = 1800,
    poll_interval_seconds: float = 5.0,
    transport: Any = None,
) -> Executor:
    """Return an executor that runs each (variant, task) as a real task.

    Each invocation POSTs one task to the task server, so the run goes
    through the normal spawn path: role resolution, model policy, and an
    isolated per-task worktree. A variant whose metadata carries
    :data:`VARIANT_PROFILE_KEY` is spawned with ``metadata['mode']`` set
    to that profile, so the spawn path renders the profile addendum and
    stamps ``response_profile`` / ``profile_content_sha256`` into the
    ledger rows the run produces. A variant carrying
    :data:`VARIANT_ADDENDUM_KEY` gets that text appended to the task
    description instead (the minimal-control arm).

    Args:
        server_url: Base URL of the running task server.
        role: Agent role assigned to every spawned task.
        scope: Task scope (``small`` / ``medium`` / ``large``).
        timeout_seconds: Max seconds to wait for one task to finish.
        poll_interval_seconds: Seconds between status polls.
        transport: Optional httpx transport override (tests inject an
            ``httpx.MockTransport`` here for a zero-network path).

    Returns:
        An :data:`Executor` whose :class:`RunResult.ledger_task_id` is
        the server-assigned task id - the join key into the spend
        ledger.
    """
    import httpx  # local import: only the real path needs it

    def _run(variant: Variant, task: Task) -> RunResult:
        description = str(task.input)
        addendum = str(variant.metadata.get(VARIANT_ADDENDUM_KEY, "") or "")
        if addendum:
            description = f"{description}\n\n{addendum}"

        metadata: dict[str, Any] = {
            "eval_ab": True,
            "eval_arm": variant.name,
            "eval_task_id": task.task_id,
        }
        profile = str(variant.metadata.get(VARIANT_PROFILE_KEY, "") or "")
        if profile:
            metadata["mode"] = profile
        payload: dict[str, Any] = {
            "title": f"[eval-ab:{variant.name}] {description[:80]}",
            "description": description,
            "role": role,
            "scope": scope,
            "metadata": metadata,
        }
        if variant.model:
            payload["model"] = variant.model
            metadata["pinned_model"] = True

        start = time.monotonic()
        with httpx.Client(transport=transport) as client:
            resp = client.post(f"{server_url}/tasks", json=payload, timeout=10.0)
            resp.raise_for_status()
            created: dict[str, Any] = resp.json()
            server_task_id = str(created.get("id") or "")
            if not server_task_id:
                msg = f"task server did not return a task id: {created}"
                raise RuntimeError(msg)
            logger.info(
                "eval ab: spawned arm=%s task=%s as server task %s",
                sanitize_log(variant.name),
                sanitize_log(task.task_id),
                sanitize_log(server_task_id),
            )
            task_data = _poll_spawned_task(
                client,
                server_url,
                server_task_id,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
            )
        duration_ms = (time.monotonic() - start) * 1000.0

        status = str(task_data.get("status", "unknown"))
        passed = status in _SPAWN_PASS_STATUSES
        meta: dict[str, Any] = task_data.get("metadata", {}) or {}
        quality_passed = bool(meta.get("quality_passed", task_data.get("quality_passed", passed)))
        score = 1.0 if (passed and quality_passed) else 0.0
        return RunResult(
            variant=variant.name,
            task_id=task.task_id,
            output=task_data,
            score=score,
            duration_ms=duration_ms,
            passed=passed and quality_passed,
            ledger_task_id=server_task_id,
        )

    return _run


def _poll_spawned_task(
    client: Any,
    server_url: str,
    server_task_id: str,
    *,
    timeout_seconds: int,
    poll_interval_seconds: float,
) -> dict[str, Any]:
    """Poll one spawned task until it is terminal or the timeout passes.

    Returns:
        The final task dict, or ``{"status": "timeout"}`` when the task
        did not reach a terminal status in time (the caller records the
        run as not-passed rather than raising, so one stuck arm cannot
        abort the whole comparison).
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        resp = client.get(f"{server_url}/tasks/{server_task_id}", timeout=10.0)
        resp.raise_for_status()
        task_data: dict[str, Any] = resp.json()
        if str(task_data.get("status", "")) in _SPAWN_TERMINAL_STATUSES:
            return task_data
        time.sleep(poll_interval_seconds)
    logger.warning("eval ab: server task %s timed out after %ss", sanitize_log(server_task_id), timeout_seconds)
    return {"status": "timeout"}


# ---------------------------------------------------------------------------
# Core runner
# ---------------------------------------------------------------------------


def run_ab(
    variant_a: Variant,
    variant_b: Variant,
    tasks: Iterable[Task],
    *,
    executor: Executor = echo_executor,
    scorer: Scorer | None = None,
    tolerance: float = 0.05,
) -> Comparison:
    """Run two variants over the same task set and build a comparison.

    Tasks are iterated once; each task is executed for variant A then
    variant B. Results are aggregated into :class:`VariantStats` and
    per-task deltas are computed in input order.

    Args:
        variant_a: First variant (the baseline / control).
        variant_b: Second variant (the candidate / treatment).
        tasks: Iterable of :class:`Task`. Consumed once.
        executor: Callable that runs one variant on one task. Defaults to
            :func:`echo_executor` for synthetic test paths.
        scorer: Optional callable that overrides executor scoring with a
            deterministic post-hoc grade.
        tolerance: Pass-rate / mean-score tolerance band for tie-calling.
            Default 5% (``0.05``).

    Returns:
        A populated :class:`Comparison`.

    Raises:
        ValueError: If ``variant_a.name == variant_b.name`` (would cause
            ambiguous deltas).
    """
    if variant_a.name == variant_b.name:
        msg = f"variant names must differ; got {variant_a.name!r} twice"
        raise ValueError(msg)

    task_list = list(tasks)
    results_a: list[RunResult] = []
    results_b: list[RunResult] = []

    for task in task_list:
        results_a.append(_score_one(variant_a, task, executor, scorer))
        results_b.append(_score_one(variant_b, task, executor, scorer))

    stats_a = _aggregate(variant_a.name, results_a)
    stats_b = _aggregate(variant_b.name, results_b)

    deltas: list[TaskDelta] = []
    by_task_a = {r.task_id: r for r in results_a}
    by_task_b = {r.task_id: r for r in results_b}
    for task in task_list:
        ra = by_task_a.get(task.task_id)
        rb = by_task_b.get(task.task_id)
        sa = ra.score if ra else 0.0
        sb = rb.score if rb else 0.0
        deltas.append(TaskDelta(task_id=task.task_id, score_a=sa, score_b=sb, delta=sb - sa))

    winner, reason = _decide_winner(stats_a, stats_b, tolerance=tolerance)

    return Comparison(
        variant_a=stats_a,
        variant_b=stats_b,
        per_task=tuple(deltas),
        winner=winner,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# YAML / JSON I/O helpers (thin wrappers - keep deps light)
# ---------------------------------------------------------------------------


def load_variant_yaml(path: Path) -> Variant:
    """Load a Variant from a YAML file with keys ``name``, ``prompt``.

    Args:
        path: Path to the YAML file.

    Returns:
        Parsed :class:`Variant`.

    Raises:
        ValueError: If required keys are missing.
    """
    import yaml  # local import: only needed for CLI / file path

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if "name" not in raw or "prompt" not in raw:
        msg = f"variant YAML {path} missing required keys 'name'/'prompt'"
        raise ValueError(msg)
    return Variant(
        name=str(raw["name"]),
        prompt=str(raw["prompt"]),
        model=raw.get("model"),
        metadata=raw.get("metadata", {}) or {},
    )


def load_tasks_yaml(path: Path) -> list[Task]:
    """Load a list of Tasks from a YAML file with a top-level ``tasks`` key.

    Expected schema::

        tasks:
          - id: t1
            input: "hello"
            expected: "world"

    Args:
        path: Path to the YAML file.

    Returns:
        List of :class:`Task`, in YAML order.

    Raises:
        ValueError: If the file lacks a ``tasks`` list.
    """
    import yaml  # local import

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    items = raw.get("tasks")
    if not isinstance(items, list):
        msg = f"tasks YAML {path} must have top-level 'tasks: [...]' list"
        raise ValueError(msg)
    return [
        Task(
            task_id=str(item.get("id", f"t{idx}")),
            input=item.get("input"),
            expected=item.get("expected"),
        )
        for idx, item in enumerate(items)
    ]


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _score_one(
    variant: Variant,
    task: Task,
    executor: Executor,
    scorer: Scorer | None,
) -> RunResult:
    """Run executor and (optionally) override score with scorer."""
    res = executor(variant, task)
    if scorer is None:
        return res
    score, passed = scorer(variant, task, res.output)
    return RunResult(
        variant=res.variant,
        task_id=res.task_id,
        output=res.output,
        score=score,
        duration_ms=res.duration_ms,
        passed=passed,
    )


def _aggregate(name: str, results: list[RunResult]) -> VariantStats:
    """Compute :class:`VariantStats` from a list of run results."""
    n = len(results)
    if n == 0:
        return VariantStats(name=name, n=0, pass_count=0, pass_rate=0.0, mean_score=0.0, mean_duration_ms=0.0)
    pass_count = sum(1 for r in results if r.passed)
    return VariantStats(
        name=name,
        n=n,
        pass_count=pass_count,
        pass_rate=pass_count / n,
        mean_score=statistics.fmean(r.score for r in results),
        mean_duration_ms=statistics.fmean(r.duration_ms for r in results),
    )


def _decide_winner(
    a: VariantStats,
    b: VariantStats,
    *,
    tolerance: float,
) -> tuple[str, str]:
    """Pick winner from pass-rate (primary) then mean-score (secondary)."""
    pr_diff = b.pass_rate - a.pass_rate
    if abs(pr_diff) > tolerance:
        if pr_diff > 0:
            return "b", f"{b.name} pass_rate {b.pass_rate:.2%} beat {a.name} {a.pass_rate:.2%}"
        return "a", f"{a.name} pass_rate {a.pass_rate:.2%} beat {b.name} {b.pass_rate:.2%}"

    score_diff = b.mean_score - a.mean_score
    if abs(score_diff) > tolerance:
        if score_diff > 0:
            return "b", f"{b.name} mean_score {b.mean_score:.3f} beat {a.name} {a.mean_score:.3f}"
        return "a", f"{a.name} mean_score {a.mean_score:.3f} beat {b.name} {b.mean_score:.3f}"

    return "tie", f"variants within {tolerance:.0%} tolerance on pass-rate and mean-score"


def _stats_to_dict(s: VariantStats) -> dict[str, Any]:
    return {
        "name": s.name,
        "n": s.n,
        "pass_count": s.pass_count,
        "pass_rate": s.pass_rate,
        "mean_score": s.mean_score,
        "mean_duration_ms": s.mean_duration_ms,
    }


def _delta_to_dict(d: TaskDelta) -> dict[str, Any]:
    return {
        "task_id": d.task_id,
        "score_a": d.score_a,
        "score_b": d.score_b,
        "delta": d.delta,
    }


__all__ = [
    "VARIANT_ADDENDUM_KEY",
    "VARIANT_PROFILE_KEY",
    "ArmCost",
    "Comparison",
    "Executor",
    "RunResult",
    "Scorer",
    "Task",
    "TaskDelta",
    "Variant",
    "VariantStats",
    "echo_executor",
    "exact_match_scorer",
    "load_tasks_yaml",
    "load_variant_yaml",
    "run_ab",
    "spawn_executor",
]
