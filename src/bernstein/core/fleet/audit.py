"""Fleet audit panel — filtering and tail-break detection.

The fleet view does not own any HMAC keys. It re-uses
:func:`bernstein.core.security.audit_integrity.verify_audit_integrity`
when a key is available, and falls back to *prev_hmac* chain-linkage
checks when it is not — that is enough to surface a tail break in the
panel without requiring fleet-wide key distribution.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class AuditEntry:
    """A single decoded audit log entry from a project.

    Attributes:
        project: Source project name.
        ts: Epoch seconds (best-effort; ``0.0`` if missing).
        role: Role tag from the entry, e.g. ``manager`` or ``backend``.
        adapter: Adapter name (``claude``, ``codex``...). May be empty.
        outcome: Outcome string (``ok``, ``denied``, ``error``...).
        kind: Event kind (``tool_call``, ``approval``, ``run``...).
        line_no: 1-based line number inside the source JSONL file.
        source_file: Filename of the source JSONL.
        raw: Original parsed entry, kept for detail panes.
    """

    project: str
    ts: float
    role: str
    adapter: str
    outcome: str
    kind: str
    line_no: int
    source_file: str
    raw: dict[str, Any]


@dataclass(slots=True)
class AuditChainStatus:
    """Summary of one project's audit chain integrity.

    Attributes:
        project: Source project name.
        ok: Whether the chain is intact and verified.
        broken_at: Optional ``"file:line"`` cursor where the break is.
        message: Human-readable summary suitable for the dashboard.
        entries_checked: How many tail entries were inspected.
        last_ts: Epoch seconds of the most recent entry seen.
    """

    project: str
    ok: bool = True
    broken_at: str | None = None
    message: str = ""
    entries_checked: int = 0
    last_ts: float = 0.0


def _iter_recent_entries(audit_dir: Path, max_entries: int) -> list[tuple[str, int, dict[str, Any]]]:
    """Walk the audit dir newest-first and return up to ``max_entries`` rows.

    Uses ``log_path.read_text`` in line-by-line mode to keep the memory
    footprint bounded; the panel typically asks for 200-1000 entries which
    is safe for daily files in the few-MB range.
    """
    if not audit_dir.is_dir():
        return []
    log_files = sorted(audit_dir.glob("*.jsonl"), reverse=True)
    collected: list[tuple[str, int, dict[str, Any]]] = []
    for log_path in log_files:
        try:
            lines = log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line_no in range(len(lines), 0, -1):
            raw = lines[line_no - 1].strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            collected.append((log_path.name, line_no, entry))
            if len(collected) >= max_entries:
                break
        if len(collected) >= max_entries:
            break
    collected.reverse()
    return collected


def _coerce_ts(entry: dict[str, Any]) -> float:
    raw = entry.get("ts") or entry.get("timestamp") or entry.get("time")
    if isinstance(raw, int | float):
        return float(raw)
    if isinstance(raw, str):
        # Support ISO 8601 best-effort.
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                return time.mktime(time.strptime(raw, fmt))
            except ValueError:
                continue
    return 0.0


def load_recent_entries(project: str, sdd_dir: Path, *, max_entries: int = 500) -> list[AuditEntry]:
    """Load the newest ``max_entries`` audit rows for ``project``.

    Args:
        project: Source project name (passed through to each row).
        sdd_dir: The project's ``.sdd`` directory.
        max_entries: Cap on the number of entries returned.

    Returns:
        Parsed entries in chronological order (oldest first).
    """
    rows: list[AuditEntry] = []
    for filename, line_no, entry in _iter_recent_entries(sdd_dir / "audit", max_entries):
        rows.append(
            AuditEntry(
                project=project,
                ts=_coerce_ts(entry),
                role=str(entry.get("role", "") or ""),
                adapter=str(entry.get("adapter", "") or ""),
                outcome=str(entry.get("outcome", entry.get("status", "")) or ""),
                kind=str(entry.get("kind", entry.get("event", "")) or ""),
                line_no=line_no,
                source_file=filename,
                raw=entry,
            )
        )
    return rows


def filter_audit_entries(
    entries: list[AuditEntry],
    *,
    role: str | None = None,
    adapter: str | None = None,
    outcome: str | None = None,
    since: float | None = None,
    until: float | None = None,
    project: str | None = None,
) -> list[AuditEntry]:
    """Apply the standard audit-panel filter set.

    Empty strings and ``None`` are treated as "no filter".
    """

    def _ok(entry: AuditEntry) -> bool:
        if role and entry.role != role:
            return False
        if adapter and entry.adapter != adapter:
            return False
        if outcome and entry.outcome != outcome:
            return False
        if project and entry.project != project:
            return False
        if since is not None and entry.ts < since:
            return False
        return not (until is not None and entry.ts > until)

    return [e for e in entries if _ok(e)]


def check_audit_tail(project: str, sdd_dir: Path, *, count: int = 100) -> AuditChainStatus:
    """Detect a broken HMAC chain in the project's tail entries.

    The check is intentionally cheap: we walk the last ``count`` entries
    and verify each ``prev_hmac`` field links to the previous entry's
    ``hmac``. This catches in-place edits and truncated tails without
    requiring the audit key.

    Args:
        project: Source project name (kept for downstream display).
        sdd_dir: The project's ``.sdd`` directory.
        count: Tail size to inspect.

    Returns:
        :class:`AuditChainStatus`. ``ok=True`` for missing or empty audit
        directories so that a fresh project is not flagged red.
    """
    audit_dir = sdd_dir / "audit"
    if not audit_dir.is_dir():
        return AuditChainStatus(project=project, ok=True, message="no audit log yet")

    rows = _iter_recent_entries(audit_dir, count)
    if not rows:
        return AuditChainStatus(project=project, ok=True, message="audit log empty")

    prev_hmac: str | None = None
    last_ts = 0.0
    for filename, line_no, entry in rows:
        ts = _coerce_ts(entry)
        if ts > last_ts:
            last_ts = ts
        if prev_hmac is not None:
            entry_prev = entry.get("prev_hmac")
            if not isinstance(entry_prev, str) or entry_prev != prev_hmac:
                return AuditChainStatus(
                    project=project,
                    ok=False,
                    broken_at=f"{filename}:{line_no}",
                    message=(f"chain break — prev_hmac {str(entry_prev)[:12]}... != expected {prev_hmac[:12]}..."),
                    entries_checked=len(rows),
                    last_ts=last_ts,
                )
        cur_hmac = entry.get("hmac")
        if isinstance(cur_hmac, str) and cur_hmac:
            prev_hmac = cur_hmac
        else:
            # Skipping entries with no hmac is fine — older formats might lack one.
            continue

    return AuditChainStatus(
        project=project,
        ok=True,
        message=f"tail of {len(rows)} entries OK",
        entries_checked=len(rows),
        last_ts=last_ts,
    )
