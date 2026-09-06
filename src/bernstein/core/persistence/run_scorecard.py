"""Per-run scorecard derivation + content-addressed artifact (#5404).

A scorecard is the deterministic, content-addressed projection of a run's
work ledger that the CLI hands back to operators and downstream tools.
It carries the same terminal facts ``bernstein runs report`` already
classifies (``run_id``, ``branch``, ``outcome``, ``evidence``,
``started_at``, ``ended_at``) and adds a small set of counters the
scorecard CLI surface needs (``tasks_total``, ``tasks_completed``,
``tasks_failed``, ``tasks_started``, ``cost_usd``, ``host``,
``parent_run_id``, ``attempt_count``, ``elapsed_seconds``, ``steps``).

The module keeps three invariants:

* **Canonical JSON.** The hashed payload is serialised with sorted
  keys, compact separators, and ASCII escapes; the same ledger always
  produces the same bytes on every machine.
* **No timestamps inside the hashed payload.** The scorecard embeds
  ``started_at`` and ``ended_at`` because the work ledger does -- they
  are ledger facts, not wall-clock context. ``build_run_scorecard``
  never reads ``time.time()`` or any other non-deterministic source.
* **Content-addressed artifact.** The artifact written to disk is
  named ``<sha256>.json`` and keyed by the SHA-256 hex of the canonical
  bytes, so re-running over the same ledger overwrites the identical
  file -- the operation is idempotent by construction.

The scorecard's field names are an internal contract this slice owns;
no other module in the tree depends on the schema yet. The CLI wiring
in #5404 is the first consumer.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.cost.spend_ledger import LedgerEntry as CostLedgerEntry
from bernstein.core.cost.spend_ledger import SpendLedger
from bernstein.core.persistence.work_ledger import (
    KIND_RUN_CLOSED,
    KIND_TASK_COMPLETED,
    KIND_TASK_FAILED,
    KIND_TASK_STARTED,
    LedgerReader,
    LedgerState,
    WorkLedger,
    replay_state,
    run_ledger_dir,
)

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.persistence.runs_report import RunOutcome

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Payload schema version. Bump when the content shape changes; the
#: hash covers the version so v1 and v2 scorecards never collide.
SCORECARD_VERSION = 1

#: Discriminator embedded in every scorecard payload.
SCORECARD_KIND = "run_scorecard"

#: Filename of the artifact under the per-run ``scorecard/`` directory.
#: The full path includes the content hash; the on-disk name is exactly
#: ``<sha256>.json``.
_ARTIFACT_SUFFIX = ".json"

#: Number of decimal places retained on the cost figure. Matches the
#: rounding used by :mod:`bernstein.core.cost.profile_report`.
_COST_PRECISION = 6


# ---------------------------------------------------------------------------
# Canonical encoding
# ---------------------------------------------------------------------------


def canonical_json_bytes(payload: Any) -> bytes:
    """Serialise *payload* to canonical JSON bytes.

    Sorted keys, compact separators, ASCII-escaped: the encoding is a
    pure function of the value, independent of platform and locale.
    Same convention as
    :func:`bernstein.core.cost.profile_report.canonical_json_bytes`.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunScorecard:
    """A built scorecard: hashed content plus its content address.

    The on-disk artifact envelope mirrors the per-profile report
    convention: ``{"content": ..., "sha256": ...}`` named by the content
    hash. ``to_canonical_json`` returns the canonical bytes of the
    envelope -- the same bytes ``write_scorecard_artifact`` writes.
    """

    content: dict[str, Any]
    sha256: str

    def to_canonical_json(self) -> bytes:
        """Return the canonical UTF-8 bytes of the scorecard envelope.

        Two scorecards built from the same ledger produce byte-identical
        output; this is the property ``write_scorecard_artifact`` and
        ``verify_scorecard`` rely on.
        """
        envelope = {"content": self.content, "sha256": self.sha256}
        return canonical_json_bytes(envelope)

    @property
    def artifact_name(self) -> str:
        """Content-addressed filename of the artifact."""
        return f"{self.sha256}{_ARTIFACT_SUFFIX}"


@dataclass(frozen=True)
class VerifyResult:
    """Outcome of :func:`verify_scorecard`.

    Attributes:
        ok: True when the artifact's canonical bytes match what the live
            ledger re-derives.
        artifact_sha256: The SHA-256 the artifact was keyed by.
        recomputed_sha256: The SHA-256 the live ledger produced on re-derivation.
        description: Human-readable verdict, naming the field(s) that
            differ on mismatch.
    """

    ok: bool
    artifact_sha256: str
    recomputed_sha256: str
    description: str = ""


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def _step_count(entries: list[Any]) -> int:
    """Return how many distinct task ids touched the ledger.

    A "step" is one task id that recorded at least one entry; this
    matches the operator's intuition that a step is a unit of work, not
    a single transition.
    """
    ids = {entry.task_id for entry in entries if entry.task_id}
    return len(ids)


def _sdd_dir_from_ledger(ledger_dir: Path) -> Path | None:
    """Return the ``.sdd`` directory that owns *ledger_dir*, or ``None``.

    The work-ledger convention is ``<sdd_dir>/runtime/ledger/<run_id>``;
    two ``.parent`` walks take us back to ``<sdd_dir>``. A ledger that
    does not sit under a ``runtime/ledger/`` segment cannot be
    resolved, and the caller is told to skip the cost lookup rather
    than guess.
    """
    ledger_root = ledger_dir.parent
    if ledger_root.name != "ledger":
        return None
    runtime = ledger_root.parent
    if runtime.name != "runtime":
        return None
    return runtime.parent


def _run_cost(reader: LedgerReader, run_id: str) -> float:
    """Sum ``cost_usd`` from the cost ledger for *run_id*.

    The cost ledger is best-effort evidence: missing or malformed rows
    are skipped (matching :meth:`SpendLedger.load_entries`), and a
    missing ledger file contributes ``0.0``. The scorecard is an
    operator summary, so a partial picture is still useful.
    """
    sdd_dir = _sdd_dir_from_ledger(reader.ledger_dir)
    if sdd_dir is None:
        return 0.0
    ledger_path = sdd_dir / "cost" / "ledger.jsonl"
    if not ledger_path.exists():
        return 0.0
    total = 0.0
    for row in SpendLedger.load_entries(ledger_path):
        if isinstance(row, CostLedgerEntry) and row.run_id == run_id:
            total += row.cost_usd
    return round(total, _COST_PRECISION)


def _task_counts(entries: list[Any]) -> dict[str, int]:
    """Count per-kind task entries (started / completed / failed).

    The counters name the entries, not the projected state: a task
    that started three times and completed once still records
    ``tasks_started == 3`` and ``tasks_completed == 1``. ``tasks_total``
    is the count of distinct task ids, matching ``steps``.
    """
    started = 0
    completed = 0
    failed = 0
    distinct: set[str] = set()
    for entry in entries:
        if not entry.task_id:
            continue
        distinct.add(entry.task_id)
        if entry.kind == KIND_TASK_STARTED:
            started += 1
        elif entry.kind == KIND_TASK_COMPLETED:
            completed += 1
        elif entry.kind == KIND_TASK_FAILED:
            failed += 1
    return {
        "total": len(distinct),
        "started": started,
        "completed": completed,
        "failed": failed,
    }


def _last_run_field(entries: list[Any], name: str) -> str:
    """Return the last string *name* recorded on a run-level entry.

    Mirrors :func:`bernstein.core.persistence.runs_report._run_field`
    so the two classifiers cannot drift on what ``host`` or
    ``parent_run_id`` means.
    """
    for entry in reversed(entries):
        if not entry.kind.startswith("run."):
            continue
        raw = entry.payload.get(name)
        if isinstance(raw, str) and raw:
            return raw
    return ""


def _wrapup_payload(entries: list[Any]) -> dict[str, Any]:
    """Return the payload of the last ``run.closed`` entry, or ``{}``."""
    for entry in reversed(entries):
        if entry.kind == KIND_RUN_CLOSED:
            return dict(entry.payload)
    return {}


def _classify_from_state(state: LedgerState, payload: dict[str, Any]) -> tuple[RunOutcome, str]:
    """Derive (outcome, evidence) from a replayed state and wrap-up.

    A scorecard is built after the fact, so an absent ``run.closed``
    entry does not raise -- the classifier returns ``infra-error`` with
    an explanatory evidence line. Reusing the rule the
    ``runs_report.classify_run`` already documents keeps the two
    surfaces in agreement.
    """
    # Local re-import keeps the public surface narrow: classify_run
    # belongs to runs_report and is the single source of truth.
    from bernstein.core.persistence.runs_report import RunWrapUp, classify_run

    wrapup = RunWrapUp.from_payload(payload) if payload else None
    return classify_run(state, wrapup)


def _scorecard_content(
    *,
    state: LedgerState,
    entries: list[Any],
    cost_usd: float,
) -> dict[str, Any]:
    """Assemble the scorecard content dict from a replayed state.

    Field order is the public contract (the spec); callers must not
    re-order keys, because the canonical bytes change with key order
    under ``sort_keys=True`` anyway, but stable order makes the JSON
    easier to read in test failure output.
    """
    payload = _wrapup_payload(entries)
    branch = ""
    if payload.get("branch"):
        branch = str(payload["branch"])
    if not branch:
        branch = _last_run_field(entries, "branch")
    outcome, evidence = _classify_from_state(state, payload)

    started_at = entries[0].ts if entries else 0.0
    ended_at = entries[-1].ts if entries else 0.0
    elapsed = ended_at - started_at if entries else 0.0
    counts = _task_counts(entries)

    return {
        "kind": SCORECARD_KIND,
        "version": SCORECARD_VERSION,
        "run_id": state.run_id or "",
        "branch": branch,
        "outcome": outcome.value,
        "evidence": evidence,
        "started_at": started_at,
        "ended_at": ended_at,
        "elapsed_seconds": elapsed,
        "host": _last_run_field(entries, "host"),
        "parent_run_id": _last_run_field(entries, "parent_run_id"),
        "attempt_count": sum(task.attempts for task in state.tasks.values()),
        "steps": _step_count(entries),
        "tasks_total": counts["total"],
        "tasks_completed": counts["completed"],
        "tasks_failed": counts["failed"],
        "tasks_started": counts["started"],
        "cost_usd": cost_usd,
        "scorecard_version": SCORECARD_VERSION,
    }


def build_run_scorecard(journal: WorkLedger) -> RunScorecard:
    """Derive a per-run scorecard from a live ``WorkLedger``.

    The ledger is replayed once via an internal ``LedgerReader`` (the
    same read-only view ``runs_report.classify_ledger_dir`` already
    uses); the resulting ``LedgerState`` plus the raw entry list is
    what every field of the scorecard is projected from, so the
    scorecard is a pure function of the chain. Two replays of the same
    chain produce byte-identical canonical bytes.

    Args:
        journal: An open :class:`WorkLedger` for the run. The ledger is
            not closed or mutated by this call; the function only reads
            its on-disk bucket.

    Returns:
        The built :class:`RunScorecard`.
    """
    reader = LedgerReader(journal.ledger_dir)
    entries = list(reader.entries())
    state = replay_state(entries, run_id=journal.ledger_dir.name)
    run_id = state.run_id or journal.ledger_dir.name
    cost_usd = _run_cost(reader, run_id)
    content = _scorecard_content(state=state, entries=entries, cost_usd=cost_usd)
    payload_bytes = canonical_json_bytes(content)
    return RunScorecard(content=content, sha256=_sha256_hex(payload_bytes))


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _scorecard_dir(root: Path, run_id: str) -> Path:
    """Return the per-run ``scorecard/`` directory under ``.sdd/runs``.

    Path containment mirrors the rest of the runs-area: ``run_id`` is a
    caller-supplied single segment, so it goes through
    :func:`bernstein.core.security.path_containment.contained_path`
    before the directory is built. The install root (``<root>/.sdd``)
    is the boundary; the on-disk layout is
    ``<root>/.sdd/runs/<run_id>/scorecard/`` per the spec.
    """
    from bernstein.core.security.path_containment import contained_path

    return contained_path(root, ".sdd", "runs", run_id, "scorecard", label="run id")


def write_scorecard_artifact(root: Path, run_id: str, scorecard: RunScorecard) -> Path:
    """Write the content-addressed artifact and return its path.

    The artifact is ``<sha256>.json`` under
    ``<root>/.sdd/runs/<run_id>/scorecard/``. Re-running over the same
    ledger overwrites the identical file -- the operation is
    idempotent by construction because the filename is the hash.
    """
    out_dir = _scorecard_dir(root, run_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / scorecard.artifact_name
    out.write_bytes(scorecard.to_canonical_json())
    return out


def read_scorecard_artifact(path: Path) -> RunScorecard:
    """Read a scorecard artifact back from disk.

    The artifact envelope is ``{"content": ..., "sha256": ...}``; the
    SHA-256 inside is verified against the content hash to catch a
    truncated or hand-edited file before a caller trusts it.
    """
    raw = path.read_bytes()
    envelope = json.loads(raw.decode("utf-8"))
    if not isinstance(envelope, dict):
        msg = f"scorecard artifact {path}: expected object envelope"
        raise ValueError(msg)
    content = envelope.get("content")
    sha = envelope.get("sha256")
    if not isinstance(content, dict) or not isinstance(sha, str):
        msg = f"scorecard artifact {path}: missing content or sha256"
        raise ValueError(msg)
    expected = _sha256_hex(canonical_json_bytes(content))
    if expected != sha:
        msg = f"scorecard artifact {path}: sha256 mismatch (stored {sha[:16]}..., recomputed {expected[:16]}...)"
        raise ValueError(msg)
    return RunScorecard(content=content, sha256=sha)


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


def _diff_fields(left: dict[str, Any], right: dict[str, Any]) -> list[str]:
    """Return the sorted list of keys whose values differ between *left* and *right*."""
    differing: list[str] = []
    for key in sorted(set(left) | set(right)):
        if left.get(key) != right.get(key):
            differing.append(key)
    return differing


def verify_scorecard(root: Path, run_id: str, path: Path) -> VerifyResult:
    """Recompute the scorecard from the live ledger and compare bytes.

    The recomputation reads ``<root>/.sdd/runs/<run_id>/``'s work
    ledger afresh via :class:`LedgerReader` + the same
    :func:`build_run_scorecard` derivation. A read-only reader is used
    on purpose: tampering must surface as a ``ok=False`` with the
    diverging field named, not a chain-validation crash. The on-disk
    artifact is read back via :func:`read_scorecard_artifact`; the two
    canonical envelopes are compared key by key.
    """
    artifact = read_scorecard_artifact(path)
    ledger_dir = run_ledger_dir(root / ".sdd", run_id)
    if not ledger_dir.exists():
        return VerifyResult(
            ok=False,
            artifact_sha256=artifact.sha256,
            recomputed_sha256="",
            description=f"live ledger at {ledger_dir} not found",
        )
    reader = LedgerReader(ledger_dir)
    entries = list(reader.entries())
    state = replay_state(entries, run_id=run_id)
    cost_usd = _run_cost(reader, state.run_id or run_id)
    content = _scorecard_content(state=state, entries=entries, cost_usd=cost_usd)
    payload_bytes = canonical_json_bytes(content)
    recomputed = RunScorecard(content=content, sha256=_sha256_hex(payload_bytes))

    if artifact.sha256 == recomputed.sha256:
        return VerifyResult(
            ok=True,
            artifact_sha256=artifact.sha256,
            recomputed_sha256=recomputed.sha256,
            description="scorecard matches live ledger",
        )

    differing = _diff_fields(artifact.content, recomputed.content)
    if not differing:
        description = "canonical bytes differ but no field-level diff detected"
    else:
        description = "scorecard diverges from live ledger on field(s): " + ", ".join(differing)
    return VerifyResult(
        ok=False,
        artifact_sha256=artifact.sha256,
        recomputed_sha256=recomputed.sha256,
        description=description,
    )


__all__ = [
    "SCORECARD_KIND",
    "SCORECARD_VERSION",
    "RunScorecard",
    "VerifyResult",
    "build_run_scorecard",
    "canonical_json_bytes",
    "read_scorecard_artifact",
    "verify_scorecard",
    "write_scorecard_artifact",
]
