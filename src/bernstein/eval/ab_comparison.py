"""Three-arm profile A/B comparison artifact (issue #2247).

``bernstein eval ab --suite`` runs one eval suite under two response
profiles and emits a single deterministic artifact carrying both the
cost delta and the quality delta. Two disciplines keep the artifact
honest:

* **Minimal-control arm.** Comparing a profile against an unconstrained
  baseline conflates the profile's content with the generic instruction
  to be brief. The three-arm plan runs ``baseline`` (profile unset),
  ``control`` (a minimal built-in terse addendum), and ``candidate``
  (the named profile). The honest delta is candidate vs control;
  candidate vs baseline is reported but labelled ``conflated``.
* **Ledger-anchored cost.** Every cost figure is a sum over concrete
  spend-ledger rows, referenced by the SHA-256 of their raw JSONL line
  bytes. A verifier holding the ledger resolves each reference and
  recomputes every aggregate; nothing is estimated.

The artifact invariants mirror ``cost/profile_report.py``: canonical
JSON (sorted keys, compact separators, ASCII escapes), no timestamps in
the hashed payload, content-addressed on-disk envelope, and one audit
chain event per emission (``eval.ab_comparison``). Aggregation uses
medians. A winner is declared only when both cost and quality are
measured for both honest arms; a run missing either emits
``incomparable``, never a partial winner. What the harness does not
measure is declared as structured fields (:data:`NOT_MEASURED`), not
prose.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import median
from typing import TYPE_CHECKING, Any

from bernstein.core.agents.response_style import addendum_sha256, render_style_addendum
from bernstein.core.cost.profile_report import canonical_json_bytes, read_ledger_window
from bernstein.core.cost.spend_ledger import CallTags
from bernstein.eval.ab_runner import (
    VARIANT_ADDENDUM_KEY,
    VARIANT_PROFILE_KEY,
    Task,
    Variant,
    spawn_executor,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from pathlib import Path

    from bernstein.core.cost.spend_ledger import SpendLedger

logger = logging.getLogger(__name__)

#: Discriminator embedded in every artifact payload.
ARTIFACT_KIND = "eval_ab_comparison"

#: Payload schema version. Bump when the content shape changes; the hash
#: covers the version so v1 and v2 artifacts never collide.
ARTIFACT_VERSION = 1

#: Reserved arm names in the three-arm plan.
ARM_BASELINE = "baseline"
ARM_CONTROL = "control"
ARM_CANDIDATE = "candidate"

#: Winner value emitted when cost or quality is missing on either honest
#: arm. Never accompanied by a partial per-axis winner.
INCOMPARABLE = "incomparable"

#: The minimal-control addendum: the generic brevity instruction with
#: none of a named profile's content. Constant by design - its SHA-256
#: is pinned into every three-arm artifact, so two operators running the
#: same bernstein version compare against the byte-identical control.
CONTROL_ADDENDUM = "## Response style: control\n\nKeep responses brief. Prefer short answers over long explanations."

#: Axes this harness deliberately does not measure, declared as stable
#: identifiers inside the artifact (sorted; structured, not prose).
NOT_MEASURED: tuple[str, ...] = (
    "cross_model_generalization",
    "fidelity_beyond_suite_verdicts",
    "latency",
)

#: Verdict vocabulary for one run.
VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_NOT_MEASURED = "not_measured"

#: Pass-rate / median-score tolerance band for tie-calling (mirrors
#: ``ab_runner.run_ab``).
DEFAULT_TOLERANCE = 0.05

#: Append-only pair index next to the artifacts, consumed by
#: ``bernstein cost profile-report`` to link cross-profile claims to the
#: latest comparison evidence.
INDEX_FILENAME = "index.jsonl"

#: USD figures are rounded to this many decimals everywhere in the
#: artifact; a verifier recomputing from the ledger applies the same
#: rounding.
_USD_DECIMALS = 6

#: Ratio figures (pass rates, scores) are rounded to this many decimals.
_RATE_DECIMALS = 4


# ---------------------------------------------------------------------------
# Arms
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Arm:
    """One comparison arm.

    Attributes:
        name: Arm label used in rows, aggregates, and winner fields.
        profile: Named response profile the arm runs under; empty for
            the ``baseline`` (profile unset) and ``control`` (built-in
            addendum) arms.
        addendum_sha256: SHA-256 of the exact addendum text the arm's
            runs carry - this hash pins what was compared.
    """

    name: str
    profile: str
    addendum_sha256: str


@dataclass(frozen=True)
class ArmPlan:
    """The resolved set of arms plus the pairs the artifact reports on.

    Attributes:
        arms: Arms in execution order.
        honest_pair: ``(a, b)`` arm names of the like-for-like
            comparison the winner is declared over.
        conflated_pair: ``(a, b)`` arm names of the shown-but-conflated
            comparison (three-arm plans only), else ``None``.
        profile_pair: Sorted named-profile pair used by the pair index
            (``baseline`` normalises to the unset-profile name).
    """

    arms: tuple[Arm, ...]
    honest_pair: tuple[str, str]
    conflated_pair: tuple[str, str] | None
    profile_pair: tuple[str, str]


#: Aliases accepted for the unset-profile baseline arm in three-arm mode.
_BASELINE_ALIASES = frozenset({ARM_BASELINE, "balanced"})


def build_arms(arm_a: str, arm_b: str, *, arms: int = 2, workdir: Path | None = None) -> ArmPlan:
    """Resolve CLI arm profiles into an executable :class:`ArmPlan`.

    Two-arm plans compare the named profiles directly; arm names are the
    profile names. Three-arm plans pin arm A to the unset-profile
    baseline (``baseline`` or ``balanced``), inject the built-in
    minimal-control arm, and treat arm B as the candidate.

    Args:
        arm_a: Profile for arm A. In three-arm mode this must be
            ``baseline`` or ``balanced`` (both mean "profile unset").
        arm_b: Profile for arm B (the candidate in three-arm mode).
        arms: 2 or 3.
        workdir: Project root used to resolve profile templates.

    Returns:
        The resolved :class:`ArmPlan`.

    Raises:
        ValueError: On an unknown profile, an invalid arm count, two
            identical two-arm profiles, a three-arm plan whose arm A is
            not the unset baseline, or a three-arm candidate that
            renders an empty addendum.
    """
    if arms == 2:
        if arm_a == arm_b:
            msg = f"arm profiles must differ; got {arm_a!r} twice"
            raise ValueError(msg)
        arm_objs = tuple(
            Arm(
                name=profile,
                profile=profile,
                addendum_sha256=addendum_sha256(render_style_addendum(profile, workdir=workdir)),
            )
            for profile in (arm_a, arm_b)
        )
        return ArmPlan(
            arms=arm_objs,
            honest_pair=(arm_a, arm_b),
            conflated_pair=None,
            profile_pair=_sorted_pair(arm_a, arm_b),
        )

    if arms == 3:
        if arm_a not in _BASELINE_ALIASES:
            msg = (
                f"three-arm plans pin arm A to the unset-profile baseline; "
                f"pass --arm-a baseline (or balanced), got {arm_a!r}"
            )
            raise ValueError(msg)
        candidate_addendum = render_style_addendum(arm_b, workdir=workdir)
        if not candidate_addendum:
            msg = (
                f"three-arm candidate profile {arm_b!r} renders an empty addendum "
                "and would be indistinguishable from the baseline arm"
            )
            raise ValueError(msg)
        arm_objs = (
            Arm(name=ARM_BASELINE, profile="", addendum_sha256=addendum_sha256("")),
            Arm(name=ARM_CONTROL, profile="", addendum_sha256=addendum_sha256(CONTROL_ADDENDUM)),
            Arm(name=ARM_CANDIDATE, profile=arm_b, addendum_sha256=addendum_sha256(candidate_addendum)),
        )
        return ArmPlan(
            arms=arm_objs,
            honest_pair=(ARM_CONTROL, ARM_CANDIDATE),
            conflated_pair=(ARM_BASELINE, ARM_CANDIDATE),
            profile_pair=_sorted_pair("balanced", arm_b),
        )

    msg = f"arms must be 2 or 3, got {arms}"
    raise ValueError(msg)


def _sorted_pair(a: str, b: str) -> tuple[str, str]:
    first, second = sorted((a, b))
    return (first, second)


# ---------------------------------------------------------------------------
# Run rows and executors
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArmRunRow:
    """One executed (arm, task, trial) run.

    Attributes:
        task_id: Suite task id.
        arm: Arm name the run was executed under.
        trial: Zero-based trial index.
        verdict: ``pass`` / ``fail`` / ``not_measured``.
        score: Normalised score in ``[0.0, 1.0]``.
        ledger_task_id: Join key into the spend ledger (the ``task_id``
            the run's LLM calls were recorded under).
    """

    task_id: str
    arm: str
    trial: int
    verdict: str
    score: float
    ledger_task_id: str


if TYPE_CHECKING:
    ArmExecutor = Callable[[Arm, Task, int], ArmRunRow]
    """Executes one (arm, task, trial) run and returns its row."""


def run_arms(
    plan: ArmPlan,
    tasks: Iterable[Task],
    *,
    executor: ArmExecutor,
    trials: int = 1,
) -> tuple[ArmRunRow, ...]:
    """Execute every (task, arm, trial) combination in deterministic order.

    Args:
        plan: The resolved arm plan.
        tasks: Suite tasks, consumed once; execution follows input order.
        executor: Callable producing one :class:`ArmRunRow` per run.
        trials: Number of trials per (arm, task) pair.

    Returns:
        Rows in execution order: task-major, then arm, then trial.
    """
    rows: list[ArmRunRow] = []
    for task in tasks:
        for arm in plan.arms:
            rows.extend(executor(arm, task, trial) for trial in range(trials))
    return tuple(rows)


def synthetic_arm_executor(ledger: SpendLedger, *, run_token: str | None = None) -> ArmExecutor:
    """Return the zero-network executor backed by a real spend ledger.

    Each run deterministically derives its output, token counts, and
    cost from the SHA-256 of ``(arm addendum hash, task id, trial)`` and
    records one genuine ledger row, so the full artifact flow - ledger
    references included - runs in CI without a network or an adapter.
    The output is ``"<arm name>::<task input>"``; the verdict is an
    exact match against ``task.expected`` (``not_measured`` when the
    suite provides no expectation).

    Args:
        ledger: Spend ledger the synthetic rows are recorded into. Point
            this at a dedicated file (never the production ledger) - the
            figures are synthetic.
        run_token: Uniqueness token folded into every ledger task id so
            repeated runs against the same ledger never alias. Defaults
            to a fresh token per executor.

    Returns:
        An executor producing one :class:`ArmRunRow` per run.
    """
    token = run_token if run_token is not None else f"{time.time_ns():x}"

    def _run(arm: Arm, task: Task, trial: int) -> ArmRunRow:
        digest = hashlib.sha256(f"{arm.addendum_sha256}:{task.task_id}:{trial}".encode()).digest()
        input_tokens = 200 + digest[0]
        output_tokens = 100 + digest[1]
        cost_usd = round((input_tokens * 3 + output_tokens * 15) / 1_000_000, 8)

        ledger_task_id = f"eval-ab:{token}:{arm.name}:{task.task_id}:{trial}"
        extra: dict[str, str] = {}
        if arm.profile:
            extra = {"response_profile": arm.profile, "profile_content_sha256": arm.addendum_sha256}
        ledger.record(
            tags=CallTags(
                task_id=ledger_task_id,
                agent_id=f"eval-ab-{arm.name}",
                role="eval",
                feature_label="eval_ab",
                extra=extra,
            ),
            model="synthetic",
            cost_usd=cost_usd,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

        output = f"{arm.name}::{task.input}"
        if task.expected is None:
            verdict, score = VERDICT_NOT_MEASURED, 0.0
        elif str(output) == str(task.expected):
            verdict, score = VERDICT_PASS, 1.0
        else:
            verdict, score = VERDICT_FAIL, 0.0
        return ArmRunRow(
            task_id=task.task_id,
            arm=arm.name,
            trial=trial,
            verdict=verdict,
            score=score,
            ledger_task_id=ledger_task_id,
        )

    return _run


def spawn_arm_executor(
    server_url: str,
    *,
    role: str = "backend",
    scope: str = "small",
    model: str | None = None,
    timeout_seconds: int = 1800,
    poll_interval_seconds: float = 5.0,
    transport: Any = None,
) -> ArmExecutor:
    """Return the real executor: every run is a normally spawned task.

    Wraps :func:`bernstein.eval.ab_runner.spawn_executor`, so each run
    goes through the task server's spawn path in its own isolated
    worktree. Named-profile arms are spawned with ``metadata['mode']``
    set to the profile (the spawn path stamps ``response_profile`` into
    the run's ledger rows); the control arm carries
    :data:`CONTROL_ADDENDUM` appended to the task description; the
    baseline arm is spawned untouched.

    Args:
        server_url: Base URL of the running task server.
        role: Agent role assigned to every spawned task.
        scope: Task scope for every spawned task.
        model: Optional model pin applied to every arm.
        timeout_seconds: Max seconds to wait for one task.
        poll_interval_seconds: Seconds between status polls.
        transport: Optional httpx transport override (tests only).

    Returns:
        An executor producing one :class:`ArmRunRow` per run; the row's
        ``ledger_task_id`` is the server-assigned task id.
    """
    executor = spawn_executor(
        server_url,
        role=role,
        scope=scope,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        transport=transport,
    )

    def _run(arm: Arm, task: Task, trial: int) -> ArmRunRow:
        metadata: dict[str, Any] = {}
        if arm.profile:
            metadata[VARIANT_PROFILE_KEY] = arm.profile
        if arm.name == ARM_CONTROL:
            metadata[VARIANT_ADDENDUM_KEY] = CONTROL_ADDENDUM
        variant = Variant(name=arm.name, prompt="", model=model, metadata=metadata)
        result = executor(variant, Task(task_id=f"{task.task_id}:{trial}", input=task.input, expected=task.expected))

        status = str(result.output.get("status", "")) if isinstance(result.output, dict) else ""
        measured_verdict = VERDICT_PASS if result.passed else VERDICT_FAIL
        verdict = VERDICT_NOT_MEASURED if status == "timeout" else measured_verdict
        return ArmRunRow(
            task_id=task.task_id,
            arm=arm.name,
            trial=trial,
            verdict=verdict,
            score=result.score,
            ledger_task_id=result.ledger_task_id,
        )

    return _run


# ---------------------------------------------------------------------------
# Artifact
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AbComparisonArtifact:
    """A built comparison artifact: hashed content plus its address."""

    content: dict[str, Any]
    sha256: str

    def artifact_bytes(self) -> bytes:
        """Canonical bytes of the on-disk artifact envelope."""
        return canonical_json_bytes({"artifact_sha256": self.sha256, "content": self.content})

    @property
    def artifact_name(self) -> str:
        """Content-addressed filename of the artifact."""
        return f"{self.sha256}.json"


def suite_file_sha256(path: Path) -> str:
    """SHA-256 hex digest of the suite file's raw bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_comparison_artifact(
    *,
    plan: ArmPlan,
    rows: Sequence[ArmRunRow],
    ledger_path: Path,
    suite_sha256: str,
    suite_name: str,
    adapter_versions: Mapping[str, str],
    trials: int,
    model: str | None = None,
    tolerance: float = DEFAULT_TOLERANCE,
) -> AbComparisonArtifact:
    """Build the deterministic comparison artifact over recorded runs.

    The content is a pure function of the recorded run set: the rows,
    the ledger lines they reference, and the pinned suite / profile
    hashes. Rebuilding over the same inputs re-serialises
    byte-identically; no wall-clock context enters the hashed payload.

    Args:
        plan: The arm plan the rows were executed under.
        rows: Recorded run rows, in execution order.
        ledger_path: Spend ledger the runs recorded their calls into.
        suite_sha256: SHA-256 of the suite file bytes.
        suite_name: Suite file basename (informational).
        adapter_versions: Adapter / harness versions recorded verbatim.
        trials: Trials per (arm, task) pair.
        model: Model label. ``None`` derives the label from the models
            seen on the referenced ledger rows (sorted, comma-joined).
        tolerance: Pass-rate / median-score tolerance band for the
            winner decision.

    Returns:
        The built :class:`AbComparisonArtifact`.
    """
    entries, raw_lines = read_ledger_window(ledger_path)
    by_ledger_task: dict[str, list[tuple[Any, str]]] = {}
    for entry, line in zip(entries, raw_lines, strict=True):
        line_sha = hashlib.sha256(line.encode("utf-8")).hexdigest()
        by_ledger_task.setdefault(entry.task_id, []).append((entry, line_sha))

    per_task: list[dict[str, Any]] = []
    referenced_models: set[str] = set()
    for row in rows:
        matched = by_ledger_task.get(row.ledger_task_id, []) if row.ledger_task_id else []
        refs = [sha for _entry, sha in matched]
        tokens: int | None = None
        usd: float | None = None
        if matched:
            tokens = sum(e.input_tokens + e.output_tokens for e, _sha in matched)
            usd = round(sum(e.cost_usd for e, _sha in matched), _USD_DECIMALS)
            referenced_models.update(e.model for e, _sha in matched if e.model)
        per_task.append(
            {
                "task_id": row.task_id,
                "arm": row.arm,
                "trial": row.trial,
                "tokens": tokens,
                "usd": usd,
                "verdict": row.verdict,
                "ledger_ref": refs,
            }
        )

    aggregates = {arm.name: _aggregate_arm(arm.name, per_task) for arm in plan.arms}
    winner = _decide_winner(plan.honest_pair, aggregates, tolerance=tolerance)
    deltas = _build_deltas(plan, aggregates)

    model_label = model if model else ",".join(sorted(referenced_models)) or "unknown"
    content: dict[str, Any] = {
        "kind": ARTIFACT_KIND,
        "version": ARTIFACT_VERSION,
        "suite_sha256": suite_sha256,
        "suite_name": suite_name,
        "trials": trials,
        "model": model_label,
        "adapter_versions": dict(sorted(adapter_versions.items())),
        "arms": {arm.name: {"profile": arm.profile, "addendum_sha256": arm.addendum_sha256} for arm in plan.arms},
        "profile_a_sha256": _arm_sha(plan, plan.honest_pair[0]),
        "profile_b_sha256": _arm_sha(plan, plan.honest_pair[1]),
        "per_task": per_task,
        "aggregates": aggregates,
        "deltas": deltas,
        "winner": winner,
        "not_measured": list(NOT_MEASURED),
    }
    return AbComparisonArtifact(content=content, sha256=hashlib.sha256(canonical_json_bytes(content)).hexdigest())


def _arm_sha(plan: ArmPlan, arm_name: str) -> str:
    for arm in plan.arms:
        if arm.name == arm_name:
            return arm.addendum_sha256
    return ""


def _aggregate_arm(arm_name: str, per_task: list[dict[str, Any]]) -> dict[str, Any]:
    """Median-based aggregates for one arm's rows.

    ``cost_measured`` / ``quality_measured`` are strict: every row of
    the arm must carry a ledger reference (cost) and a pass/fail verdict
    (quality). Partial figures are still reported over the rows that
    have data, but the strict flags are what gate the winner.
    """
    arm_rows = [r for r in per_task if r["arm"] == arm_name]
    costed = [r for r in arm_rows if r["ledger_ref"]]
    judged = [r for r in arm_rows if r["verdict"] in (VERDICT_PASS, VERDICT_FAIL)]

    usd_values = [float(r["usd"]) for r in costed]
    token_values = [int(r["tokens"]) for r in costed]
    verdict_scores = [1.0 if r["verdict"] == VERDICT_PASS else 0.0 for r in judged]
    return {
        "runs": len(arm_rows),
        "tasks": len({r["task_id"] for r in arm_rows}),
        "cost_measured": bool(arm_rows) and len(costed) == len(arm_rows),
        "quality_measured": bool(arm_rows) and len(judged) == len(arm_rows),
        "pass_rate": round(sum(verdict_scores) / len(judged), _RATE_DECIMALS) if judged else None,
        "median_score": round(median(verdict_scores), _RATE_DECIMALS) if judged else None,
        "median_tokens": median(token_values) if token_values else None,
        "median_usd": round(median(usd_values), _USD_DECIMALS) if usd_values else None,
        "total_tokens": sum(token_values) if token_values else None,
        "total_usd": round(sum(usd_values), _USD_DECIMALS) if usd_values else None,
    }


def _decide_winner(
    honest_pair: tuple[str, str],
    aggregates: Mapping[str, Mapping[str, Any]],
    *,
    tolerance: float,
) -> dict[str, Any]:
    """Winner over the honest pair; incomparable unless fully measured."""
    arm_a, arm_b = honest_pair
    agg_a, agg_b = aggregates[arm_a], aggregates[arm_b]

    missing: list[str] = []
    for name, agg in ((arm_a, agg_a), (arm_b, agg_b)):
        if not agg["cost_measured"]:
            missing.append(f"cost:{name}")
        if not agg["quality_measured"]:
            missing.append(f"quality:{name}")
    if missing:
        return {
            "arm": INCOMPARABLE,
            "pair": [arm_a, arm_b],
            "missing": sorted(missing),
            "reason": "winner requires cost and quality measured for both arms",
        }

    base = {"pair": [arm_a, arm_b], "missing": []}
    pass_a, pass_b = float(agg_a["pass_rate"]), float(agg_b["pass_rate"])
    if abs(pass_b - pass_a) > tolerance:
        arm, hi, lo = (arm_b, pass_b, pass_a) if pass_b > pass_a else (arm_a, pass_a, pass_b)
        return base | {"arm": arm, "reason": f"pass_rate {hi:.4f} beat {lo:.4f}"}

    score_a, score_b = float(agg_a["median_score"]), float(agg_b["median_score"])
    if abs(score_b - score_a) > tolerance:
        arm, hi, lo = (arm_b, score_b, score_a) if score_b > score_a else (arm_a, score_a, score_b)
        return base | {"arm": arm, "reason": f"median_score {hi:.4f} beat {lo:.4f}"}

    usd_a, usd_b = float(agg_a["median_usd"]), float(agg_b["median_usd"])
    if usd_a != usd_b:
        arm, lo, hi = (arm_b, usd_b, usd_a) if usd_b < usd_a else (arm_a, usd_a, usd_b)
        return base | {"arm": arm, "reason": f"quality within tolerance; median_usd {lo:.6f} beat {hi:.6f}"}

    return base | {"arm": "tie", "reason": f"arms within {tolerance:.0%} tolerance and equal median_usd"}


def _build_deltas(plan: ArmPlan, aggregates: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Per-pair deltas (b minus a); the conflated pair is labelled."""
    pairs: list[tuple[tuple[str, str], bool]] = [(plan.honest_pair, False)]
    if plan.conflated_pair is not None:
        pairs.append((plan.conflated_pair, True))

    deltas: dict[str, Any] = {}
    for (arm_a, arm_b), conflated in pairs:
        agg_a, agg_b = aggregates[arm_a], aggregates[arm_b]
        deltas[f"{arm_b}_vs_{arm_a}"] = {
            "pair": [arm_a, arm_b],
            "conflated": conflated,
            "pass_rate_delta": _delta(agg_a["pass_rate"], agg_b["pass_rate"], _RATE_DECIMALS),
            "median_score_delta": _delta(agg_a["median_score"], agg_b["median_score"], _RATE_DECIMALS),
            "median_tokens_delta": _delta(agg_a["median_tokens"], agg_b["median_tokens"], _RATE_DECIMALS),
            "median_usd_delta": _delta(agg_a["median_usd"], agg_b["median_usd"], _USD_DECIMALS),
        }
    return deltas


def _delta(a: Any, b: Any, decimals: int) -> float | None:
    if a is None or b is None:
        return None
    return round(float(b) - float(a), decimals)


# ---------------------------------------------------------------------------
# Artifact IO and the pair index
# ---------------------------------------------------------------------------


def write_comparison_artifact(artifact: AbComparisonArtifact, reports_dir: Path) -> Path:
    """Write the content-addressed artifact and return its path.

    The filename is the content hash, so re-emitting the same recorded
    run set overwrites the identical file - idempotent by construction.
    """
    reports_dir.mkdir(parents=True, exist_ok=True)
    out = reports_dir / artifact.artifact_name
    out.write_bytes(artifact.artifact_bytes())
    return out


def append_comparison_index(
    reports_dir: Path,
    *,
    profile_pair: tuple[str, str],
    artifact: AbComparisonArtifact,
    ts: float | None = None,
) -> None:
    """Append one pair-index row next to the artifacts.

    The index is presentation metadata (it lets ``cost profile-report``
    find the latest evidence for a profile pair); it lives outside the
    hashed artifact payload, so it may carry a timestamp. Best-effort
    append-only JSONL: a failed write is logged, never raised.
    """
    now = ts if ts is not None else time.time()
    first, second = _sorted_pair(*profile_pair)
    row = {
        "ts": now,
        "ts_iso": datetime.fromtimestamp(now, tz=UTC).isoformat(timespec="seconds"),
        "profile_a": first,
        "profile_b": second,
        "artifact_name": artifact.artifact_name,
        "artifact_sha256": artifact.sha256,
    }
    try:
        reports_dir.mkdir(parents=True, exist_ok=True)
        with (reports_dir / INDEX_FILENAME).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=False, separators=(",", ":")))
            fh.write("\n")
    except OSError as exc:  # pragma: no cover - IO failure path
        logger.warning("ab_comparison: failed to append pair index: %s", exc)


def latest_comparison_for_pair(reports_dir: Path, profile_a: str, profile_b: str) -> dict[str, Any] | None:
    """Return the newest index row for a profile pair, or ``None``.

    Lookup is order-insensitive; "newest" is the last matching row in
    file order (the index is append-only). Malformed lines are skipped.
    """
    index_path = reports_dir / INDEX_FILENAME
    if not index_path.exists():
        return None
    first, second = _sorted_pair(profile_a, profile_b)
    found: dict[str, Any] | None = None
    try:
        with index_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except ValueError:
                    continue
                if isinstance(parsed, dict) and parsed.get("profile_a") == first and parsed.get("profile_b") == second:
                    found = parsed
    except OSError as exc:  # pragma: no cover - IO failure path
        logger.warning("ab_comparison: failed to read pair index: %s", exc)
    return found


__all__ = [
    "ARM_BASELINE",
    "ARM_CANDIDATE",
    "ARM_CONTROL",
    "ARTIFACT_KIND",
    "ARTIFACT_VERSION",
    "CONTROL_ADDENDUM",
    "DEFAULT_TOLERANCE",
    "INCOMPARABLE",
    "INDEX_FILENAME",
    "NOT_MEASURED",
    "VERDICT_FAIL",
    "VERDICT_NOT_MEASURED",
    "VERDICT_PASS",
    "AbComparisonArtifact",
    "Arm",
    "ArmPlan",
    "ArmRunRow",
    "append_comparison_index",
    "build_arms",
    "build_comparison_artifact",
    "latest_comparison_for_pair",
    "run_arms",
    "spawn_arm_executor",
    "suite_file_sha256",
    "synthetic_arm_executor",
    "write_comparison_artifact",
]
