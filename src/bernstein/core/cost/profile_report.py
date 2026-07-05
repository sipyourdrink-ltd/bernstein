"""Content-addressed per-profile cost report over the spend ledger.

``bernstein cost profile-report`` needs a report a third party can
recompute byte-identically from the ledger alone, so this module keeps
three invariants:

* **Canonical JSON.** The hashed payload is serialised with sorted
  keys, compact separators, and ASCII escapes; the same content always
  produces the same bytes on every machine.
* **No timestamps inside the hashed payload.** Report identity derives
  from ledger content only. Wall-clock context (when the report was
  produced) lives in the audit chain event, outside the hash.
* **Ledger anchoring.** The payload embeds the line-hash range of the
  ledger window it was computed from (first/last line SHA-256, line
  count, and a digest over all included lines), so tampering with any
  ledger line changes the report hash.

The artifact written to disk is ``{"content": ..., "sha256": ...}``
named ``<sha256>.json`` - the report is content-addressed by the hash
of its canonical content.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.cost.profile_attribution import (
    MIN_COMPARABLE_TASKS,
    attribute_by_profile,
    compute_profile_comparisons,
)
from bernstein.core.cost.spend_ledger import LedgerEntry

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.cost.profile_attribution import ProfileTransition

logger = logging.getLogger(__name__)

#: Discriminator embedded in every report payload.
REPORT_KIND = "cost_profile_report"

#: Payload schema version. Bump when the content shape changes; the
#: hash covers the version so v1 and v2 reports never collide.
REPORT_VERSION = 1


def canonical_json_bytes(payload: Any) -> bytes:
    """Serialise *payload* to canonical JSON bytes.

    Sorted keys, compact separators, ASCII-escaped: the encoding is a
    pure function of the value, independent of platform and locale.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_ledger_window(ledger_path: Path, cutoff: float = 0.0) -> tuple[list[LedgerEntry], list[str]]:
    """Read the ledger rows in the window, keeping their raw line bytes.

    Returns ``(entries, raw_lines)`` where ``raw_lines[i]`` is the exact
    stripped JSONL line ``entries[i]`` was parsed from - the anchoring
    hashes are computed over these lines so a verifier holding the same
    ledger recomputes the same digests. Malformed lines are skipped,
    matching :meth:`SpendLedger.load_entries`.
    """
    if not ledger_path.exists():
        return [], []
    entries: list[LedgerEntry] = []
    raw_lines: list[str] = []
    try:
        with ledger_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = LedgerEntry.from_dict(json.loads(line))
                except (ValueError, KeyError, TypeError):
                    continue
                if cutoff > 0 and entry.ts < cutoff:
                    continue
                entries.append(entry)
                raw_lines.append(line)
    except OSError as exc:  # pragma: no cover - IO failure path
        logger.warning("profile_report: failed to read %s: %s", ledger_path, exc)
    return entries, raw_lines


@dataclass(frozen=True)
class ProfileReport:
    """A built report: hashed content plus its content address."""

    content: dict[str, Any]
    sha256: str

    def artifact_bytes(self) -> bytes:
        """Canonical bytes of the on-disk artifact envelope."""
        return canonical_json_bytes({"content": self.content, "sha256": self.sha256})

    @property
    def artifact_name(self) -> str:
        """Content-addressed filename of the artifact."""
        return f"{self.sha256}.json"


def _ledger_block(raw_lines: list[str]) -> dict[str, Any]:
    """Anchor block: line-hash range of the ledger window."""
    encoded = [line.encode("utf-8") for line in raw_lines]
    return {
        "line_count": len(raw_lines),
        "first_line_sha256": _sha256_hex(encoded[0]) if encoded else "",
        "last_line_sha256": _sha256_hex(encoded[-1]) if encoded else "",
        "lines_sha256": _sha256_hex(b"\n".join(encoded)),
    }


def _quality_block(task_ids: set[str], outcomes: dict[str, bool]) -> dict[str, Any] | None:
    """Join quality outcomes for a profile's tasks; ``None`` when none join."""
    joined = [outcomes[tid] for tid in sorted(task_ids) if tid in outcomes]
    if not joined:
        return None
    return {
        "tasks_with_outcome": len(joined),
        "verdict_pass_rate": round(sum(joined) / len(joined), 4),
    }


def _task_outcomes(task_records: list[dict[str, Any]]) -> dict[str, bool]:
    """Extract per-task manager verdicts from metrics task records.

    Records are deduplicated by task id keeping the last occurrence
    (matching the ``bernstein cost`` dedup rule). A record contributes
    an outcome only when it carries the ``janitor_passed`` verdict
    field; tasks without a recorded verdict are omitted rather than
    guessed.
    """
    outcomes: dict[str, bool] = {}
    for rec in task_records:
        tid = str(rec.get("task_id", "") or "")
        if not tid or "janitor_passed" not in rec:
            continue
        outcomes[tid] = bool(rec.get("janitor_passed"))
    return outcomes


def build_profile_report(
    *,
    ledger_path: Path,
    task_records: list[dict[str, Any]],
    transitions: list[ProfileTransition],
    window_label: str = "all",
    cutoff: float = 0.0,
    min_comparable_tasks: int = MIN_COMPARABLE_TASKS,
) -> ProfileReport:
    """Build the per-profile report over one ledger window.

    Args:
        ledger_path: The spend ledger JSONL to compute from.
        task_records: Metrics task records (``.sdd/metrics/tasks.jsonl``
            shape) used only for the quality-outcome join.
        transitions: Profile transition events; their task ids are
            excluded from attribution.
        window_label: Human window spec (``"7d"``) recorded in the
            payload. Not a timestamp - the same ledger and label always
            hash identically.
        cutoff: Unix-timestamp lower bound applied to ledger rows
            (0 means the whole ledger).
        min_comparable_tasks: Honesty-rule threshold for cross-profile
            comparisons.

    Returns:
        The built :class:`ProfileReport`.
    """
    entries, raw_lines = read_ledger_window(ledger_path, cutoff)
    attribution = attribute_by_profile(entries, transitions)
    outcomes = _task_outcomes(task_records)

    profiles: dict[str, Any] = {}
    for label in sorted(attribution.profiles):
        bucket = attribution.profiles[label]
        row: dict[str, Any] = {
            "tasks": bucket.tasks,
            "calls": bucket.calls,
            "output_tokens": bucket.output_tokens,
            "cost_usd": round(bucket.cost_usd, 6),
            "mean_output_tokens_per_task": round(bucket.output_tokens / bucket.tasks, 2) if bucket.tasks else 0.0,
            "mean_cost_usd_per_task": round(bucket.cost_usd / bucket.tasks, 6) if bucket.tasks else 0.0,
        }
        quality = _quality_block(bucket.task_ids, outcomes)
        if quality is not None:
            row["quality"] = quality
        profiles[label] = row

    comparisons = compute_profile_comparisons(entries, transitions, min_tasks=min_comparable_tasks)

    content: dict[str, Any] = {
        "kind": REPORT_KIND,
        "version": REPORT_VERSION,
        "window": window_label,
        "min_comparable_tasks": min_comparable_tasks,
        "ledger": _ledger_block(raw_lines),
        "profiles": profiles,
        "excluded": {
            "tasks": attribution.excluded.tasks,
            "calls": attribution.excluded.calls,
            "output_tokens": attribution.excluded.output_tokens,
            "cost_usd": round(attribution.excluded.cost_usd, 6),
            "task_ids": sorted(attribution.excluded.task_ids),
            "reason": "profile_transition",
        },
        "comparisons": [c.to_dict() for c in comparisons],
        "insufficient_comparable_runs": not comparisons,
    }
    return ProfileReport(content=content, sha256=_sha256_hex(canonical_json_bytes(content)))


def write_report_artifact(report: ProfileReport, reports_dir: Path) -> Path:
    """Write the content-addressed artifact and return its path.

    The filename is the content hash, so re-running over the same
    ledger overwrites the identical file - the operation is idempotent
    by construction.
    """
    reports_dir.mkdir(parents=True, exist_ok=True)
    out = reports_dir / report.artifact_name
    out.write_bytes(report.artifact_bytes())
    return out


__all__ = [
    "REPORT_KIND",
    "REPORT_VERSION",
    "ProfileReport",
    "build_profile_report",
    "canonical_json_bytes",
    "read_ledger_window",
    "write_report_artifact",
]
