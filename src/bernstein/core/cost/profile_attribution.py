"""Per-profile cost attribution derived from the spend ledger.

``bernstein cost`` rolls up spend by agent, model, task, role, and
envelope; response-style profiles add one more dimension. The spawn
path stamps ``response_profile`` (and ``profile_content_sha256``) into
each ledger entry's tag block, so attribution here is strictly
per-entry: every figure is recomputable from recorded ledger rows and
figures that cannot be computed from the ledger are omitted.

Two rules keep the numbers trustworthy:

* **Transition exclusion.** A task started under profile A and
  re-spawned under profile B must not be credited to either profile.
  The spawn path records a :class:`ProfileTransition` event when it
  overwrites a previously stamped profile; every ledger entry of a
  transitioned task lands in the excluded bucket. Attribution is
  all-or-nothing per task - never split heuristically.
* **Honesty rule.** No cross-profile savings claim is produced unless
  both profiles have at least :data:`MIN_COMPARABLE_TASKS` tasks with
  the same role and model. Below that bar the comparison list is
  empty and renderers print "insufficient comparable runs".
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from bernstein.core.cost.spend_ledger import LedgerEntry

logger = logging.getLogger(__name__)

#: Honesty-rule threshold: a cross-profile savings claim requires both
#: profiles to have at least this many tasks sharing the same role and
#: model. Five is the smallest cohort where a per-task mean is not
#: dominated by a single outlier run; operators who want stricter or
#: looser comparisons pass ``min_tasks`` explicitly.
MIN_COMPARABLE_TASKS: int = 5

#: Ledger tag key carrying the response-style profile name (stamped by
#: the spawn path at completion time; see task_lifecycle).
RESPONSE_PROFILE_TAG = "response_profile"

#: Ledger tag key carrying the SHA-256 of the rendered style addendum.
PROFILE_CONTENT_SHA_TAG = "profile_content_sha256"

#: Bucket label for ledger entries that carry no profile tag (runs that
#: predate response-style profiles).
UNATTRIBUTED_LABEL = "unattributed"

#: Bucket label for entries of tasks with a recorded profile transition.
EXCLUDED_LABEL = "excluded (profile transition)"

#: Filename of the transition event record, next to the spend ledger.
TRANSITIONS_FILENAME = "profile_transitions.jsonl"


def default_transitions_path(sdd_dir: Path) -> Path:
    """Return the canonical transitions path under an ``.sdd`` directory."""
    return sdd_dir / "cost" / TRANSITIONS_FILENAME


# ---------------------------------------------------------------------------
# Transition events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProfileTransition:
    """One ``profile_transition`` event row.

    Written when a task that already carries a stamped
    ``response_profile`` is re-spawned under a different profile, so
    per-profile attribution can exclude the task instead of guessing
    which tokens belong to which profile.
    """

    ts: float
    ts_iso: str
    task_id: str
    agent_id: str
    from_profile: str
    to_profile: str
    from_sha256: str
    to_sha256: str

    def to_json(self) -> str:
        """Return a stable single-line JSON encoding."""
        return json.dumps(asdict(self), sort_keys=False, separators=(",", ":"))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ProfileTransition:
        """Deserialise from a parsed JSON dict; missing fields default."""
        return cls(
            ts=float(d.get("ts", 0.0) or 0.0),
            ts_iso=str(d.get("ts_iso", "")),
            task_id=str(d.get("task_id", "")),
            agent_id=str(d.get("agent_id", "")),
            from_profile=str(d.get("from_profile", "")),
            to_profile=str(d.get("to_profile", "")),
            from_sha256=str(d.get("from_sha256", "")),
            to_sha256=str(d.get("to_sha256", "")),
        )


def record_profile_transition(
    path: Path,
    *,
    task_id: str,
    agent_id: str,
    from_profile: str,
    to_profile: str,
    from_sha256: str = "",
    to_sha256: str = "",
    ts: float | None = None,
) -> ProfileTransition:
    """Append one transition event to *path* and return it.

    Best-effort append-only JSONL, mirroring the spend ledger's IO
    contract: a failed write is logged, never raised, because the
    transition record is attribution metadata and must not block a
    spawn.
    """
    now = ts if ts is not None else time.time()
    rec = ProfileTransition(
        ts=now,
        ts_iso=datetime.fromtimestamp(now, tz=UTC).isoformat(timespec="seconds"),
        task_id=task_id,
        agent_id=agent_id,
        from_profile=from_profile,
        to_profile=to_profile,
        from_sha256=from_sha256,
        to_sha256=to_sha256,
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(rec.to_json())
            fh.write("\n")
            fh.flush()
    except OSError as exc:  # pragma: no cover - IO failure path
        logger.warning("profile_attribution: failed to append transition: %s", exc)
    return rec


def load_transitions(path: Path) -> list[ProfileTransition]:
    """Read every transition row; missing file yields an empty list.

    Malformed lines are skipped - partial recovery over a crash,
    consistent with :meth:`SpendLedger.load_entries`.
    """
    if not path.exists():
        return []
    out: list[ProfileTransition] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except ValueError:
                    continue
                if isinstance(parsed, dict):
                    out.append(ProfileTransition.from_dict(parsed))
    except OSError as exc:  # pragma: no cover
        logger.warning("profile_attribution: failed to read %s: %s", path, exc)
    return out


def transitioned_task_ids(transitions: Iterable[ProfileTransition]) -> frozenset[str]:
    """Return the set of task ids that changed profile mid-flight."""
    return frozenset(t.task_id for t in transitions if t.task_id)


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------


def entry_profile(entry: LedgerEntry) -> str:
    """Return the response-style profile a ledger entry was recorded under.

    Empty string means the entry predates profiles (or the tag was
    dropped); such entries are grouped under
    :data:`UNATTRIBUTED_LABEL` rather than guessed.
    """
    return str(entry.tags.get(RESPONSE_PROFILE_TAG, "") or "")


@dataclass
class ProfileBucket:
    """Aggregated ledger figures for one profile (or the excluded set)."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    task_ids: set[str] = field(default_factory=set)

    @property
    def tasks(self) -> int:
        """Distinct task count in this bucket."""
        return len(self.task_ids)

    def add(self, entry: LedgerEntry) -> None:
        """Accumulate one ledger entry."""
        self.calls += 1
        self.input_tokens += entry.input_tokens
        self.output_tokens += entry.output_tokens
        self.cost_usd += entry.cost_usd
        if entry.task_id:
            self.task_ids.add(entry.task_id)


@dataclass
class ProfileAttribution:
    """Result of :func:`attribute_by_profile`."""

    profiles: dict[str, ProfileBucket]
    excluded: ProfileBucket


def attribute_by_profile(
    entries: Iterable[LedgerEntry],
    transitions: Iterable[ProfileTransition],
) -> ProfileAttribution:
    """Group ledger entries per profile, excluding transitioned tasks.

    Every entry lands in exactly one bucket: its profile tag, the
    :data:`UNATTRIBUTED_LABEL` bucket when untagged, or the excluded
    bucket when its task has a recorded transition. Bucket sums
    therefore always equal the per-entry ledger sum.
    """
    excluded_ids = transitioned_task_ids(transitions)
    profiles: dict[str, ProfileBucket] = defaultdict(ProfileBucket)
    excluded = ProfileBucket()
    for entry in entries:
        if entry.task_id and entry.task_id in excluded_ids:
            excluded.add(entry)
            continue
        profiles[entry_profile(entry) or UNATTRIBUTED_LABEL].add(entry)
    return ProfileAttribution(profiles=dict(profiles), excluded=excluded)


def aggregate_ledger_by_profile(
    entries: Iterable[LedgerEntry],
    transitions: Iterable[ProfileTransition],
) -> dict[str, dict[str, Any]]:
    """Return ``cost --by profile`` rows: label -> {tasks, calls, cost_usd, output_tokens}.

    The excluded bucket appears under :data:`EXCLUDED_LABEL` (only when
    non-empty) so the grouped total still equals the per-entry ledger
    sum to the cent.
    """
    result = attribute_by_profile(entries, transitions)
    rows: dict[str, dict[str, Any]] = {
        label: {
            "tasks": bucket.tasks,
            "calls": bucket.calls,
            "cost_usd": bucket.cost_usd,
            "output_tokens": bucket.output_tokens,
        }
        for label, bucket in result.profiles.items()
    }
    if result.excluded.calls > 0:
        rows[EXCLUDED_LABEL] = {
            "tasks": result.excluded.tasks,
            "calls": result.excluded.calls,
            "cost_usd": result.excluded.cost_usd,
            "output_tokens": result.excluded.output_tokens,
        }
    return rows


# ---------------------------------------------------------------------------
# Honesty rule: comparable cross-profile cohorts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProfileComparison:
    """A cross-profile comparison over one (role, model) cohort.

    Only produced when both profiles clear the honesty-rule bar; the
    means are computed over the cohort's tasks only, so the two sides
    are like-for-like.
    """

    profile_a: str
    profile_b: str
    role: str
    model: str
    tasks_a: int
    tasks_b: int
    mean_output_tokens_per_task_a: float
    mean_output_tokens_per_task_b: float
    mean_cost_usd_per_task_a: float
    mean_cost_usd_per_task_b: float

    def to_dict(self) -> dict[str, Any]:
        """Deterministic dict form for reports and JSON output."""
        return {
            "profile_a": self.profile_a,
            "profile_b": self.profile_b,
            "role": self.role,
            "model": self.model,
            "tasks_a": self.tasks_a,
            "tasks_b": self.tasks_b,
            "mean_output_tokens_per_task_a": round(self.mean_output_tokens_per_task_a, 2),
            "mean_output_tokens_per_task_b": round(self.mean_output_tokens_per_task_b, 2),
            "mean_cost_usd_per_task_a": round(self.mean_cost_usd_per_task_a, 6),
            "mean_cost_usd_per_task_b": round(self.mean_cost_usd_per_task_b, 6),
        }


def compute_profile_comparisons(
    entries: Iterable[LedgerEntry],
    transitions: Iterable[ProfileTransition],
    *,
    min_tasks: int = MIN_COMPARABLE_TASKS,
) -> list[ProfileComparison]:
    """Return every honest cross-profile comparison in the ledger window.

    A comparison exists for a profile pair and a (role, model) cohort
    only when both profiles have at least *min_tasks* distinct tasks in
    that cohort (transitioned tasks never count). Output order is
    deterministic: sorted by (profile_a, profile_b, role, model).
    """
    excluded_ids = transitioned_task_ids(transitions)
    # (profile, role, model) -> per-task accumulators
    cohort_tasks: dict[tuple[str, str, str], dict[str, dict[str, float]]] = defaultdict(dict)
    for entry in entries:
        profile = entry_profile(entry)
        if not profile or not entry.task_id or entry.task_id in excluded_ids:
            continue
        key = (profile, entry.role or "unknown", entry.model or "unknown")
        acc = cohort_tasks[key].setdefault(entry.task_id, {"output_tokens": 0.0, "cost_usd": 0.0})
        acc["output_tokens"] += entry.output_tokens
        acc["cost_usd"] += entry.cost_usd

    # (role, model) -> profile -> task map
    by_cohort: dict[tuple[str, str], dict[str, dict[str, dict[str, float]]]] = defaultdict(dict)
    for (profile, role, model), tasks in cohort_tasks.items():
        by_cohort[(role, model)][profile] = tasks

    comparisons: list[ProfileComparison] = []
    for (role, model), per_profile in sorted(by_cohort.items()):
        eligible = sorted(p for p, tasks in per_profile.items() if len(tasks) >= min_tasks)
        for i, profile_a in enumerate(eligible):
            for profile_b in eligible[i + 1 :]:
                tasks_a = per_profile[profile_a]
                tasks_b = per_profile[profile_b]
                comparisons.append(
                    ProfileComparison(
                        profile_a=profile_a,
                        profile_b=profile_b,
                        role=role,
                        model=model,
                        tasks_a=len(tasks_a),
                        tasks_b=len(tasks_b),
                        mean_output_tokens_per_task_a=_mean(tasks_a, "output_tokens"),
                        mean_output_tokens_per_task_b=_mean(tasks_b, "output_tokens"),
                        mean_cost_usd_per_task_a=_mean(tasks_a, "cost_usd"),
                        mean_cost_usd_per_task_b=_mean(tasks_b, "cost_usd"),
                    )
                )
    comparisons.sort(key=lambda c: (c.profile_a, c.profile_b, c.role, c.model))
    return comparisons


def _mean(tasks: dict[str, dict[str, float]], key: str) -> float:
    """Mean of one accumulator key across a per-task map (0.0 when empty)."""
    if not tasks:
        return 0.0
    # Sum in sorted task-id order so the float total (and thus every
    # downstream report byte) is independent of dict insertion order.
    return sum(tasks[tid][key] for tid in sorted(tasks)) / len(tasks)


__all__ = [
    "EXCLUDED_LABEL",
    "MIN_COMPARABLE_TASKS",
    "PROFILE_CONTENT_SHA_TAG",
    "RESPONSE_PROFILE_TAG",
    "TRANSITIONS_FILENAME",
    "UNATTRIBUTED_LABEL",
    "ProfileAttribution",
    "ProfileBucket",
    "ProfileComparison",
    "ProfileTransition",
    "aggregate_ledger_by_profile",
    "attribute_by_profile",
    "compute_profile_comparisons",
    "default_transitions_path",
    "entry_profile",
    "load_transitions",
    "record_profile_transition",
    "transitioned_task_ids",
]
