"""Absence coverage verification and completion classification (issue #3771)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from bernstein.core.replay.journal import (
    JournalCoverageStatus,
    JournalVerifyResult,
    verify_journal,
)
from bernstein.core.tools.coverage import ToolCoverageRecord

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.lineage.entry import LineageEntry
    from bernstein.core.lineage.store import LineageStore
    from bernstein.core.replay.journal import JournalSeal


class CompletionCoverageStatus(StrEnum):
    """Whether a completion / absence claim is backed by complete coverage."""

    VERIFIED = "verified"
    UNVERIFIED = "unverified"


@dataclass(frozen=True, slots=True)
class CompletionAbsenceVerdict:
    """Verdict for an absence-based completion claim.

    Attributes:
        status: CompletionCoverageStatus ("verified" | "unverified").
        is_verified: Whether the completion is backed by verified coverage.
        passed: Whether the verification passed.
        detail: Human-readable explanation.
        coverage_record: Optional ToolCoverageRecord associated with the verdict.
    """

    status: CompletionCoverageStatus
    is_verified: bool
    passed: bool
    detail: str
    coverage_record: ToolCoverageRecord | None = None


def verify_journal_absence_coverage(
    journal_path: Path,
    *,
    seal: JournalSeal | None = None,
) -> tuple[bool, str, JournalVerifyResult]:
    """Verify journal absence claims against reader coverage and chain consistency.

    Replays through :func:`bernstein.core.replay.journal.verify_journal`.
    A journal with reader-dropped rows (partial coverage) degrades to
    unverified rather than reporting a passing absence claim.
    """
    result = verify_journal(journal_path, seal=seal)
    if not result.chain_consistent:
        errors = ", ".join(result.errors) if result.errors else "broken chain link"
        if result.coverage != JournalCoverageStatus.COMPLETE:
            discarded = ", ".join(str(i) for i in result.discarded_line_indices)
            return (
                False,
                f"unverified: chain inconsistent ({errors}); partial coverage (discarded lines: {discarded})",
                result,
            )
        return False, f"unverified: chain inconsistent ({errors})", result
    if result.coverage != JournalCoverageStatus.COMPLETE:
        discarded = ", ".join(str(i) for i in result.discarded_line_indices)
        return False, f"unverified: partial journal coverage (discarded lines: {discarded})", result
    return True, "verified", result


def verify_tool_absence_coverage(
    *,
    tool_name: str,
    pattern_or_query: str,
    workdir: Path,
    coverage_record: ToolCoverageRecord | LineageEntry | dict[str, Any] | None = None,
    lineage_store: LineageStore | None = None,
) -> tuple[bool, str]:
    """Verify whether a tool-reported absence claim carries an intact coverage record.

    If no coverage record is supplied, searches the lineage store under
    ``workdir/.sdd/lineage`` (or ``lineage_store`` when provided).
    """
    record: ToolCoverageRecord | None = None

    if coverage_record is not None:
        if isinstance(coverage_record, ToolCoverageRecord):
            record = coverage_record
        elif isinstance(coverage_record, dict):
            record = ToolCoverageRecord.from_dict(coverage_record)
        else:
            # LineageEntry
            try:
                from bernstein.core.lineage.entry import LineageEntry

                if isinstance(coverage_record, LineageEntry):
                    store_root = workdir / ".sdd" / "lineage"
                    if store_root.exists():
                        from bernstein.core.lineage.store import LineageStore

                        ls = lineage_store or LineageStore(store_root)
                        # Find matching entry content or read from store
                        record = _parse_entry_coverage(coverage_record, ls)
            except Exception:
                record = None

    if record is None:
        # Check lineage store under workdir
        store_root = workdir / ".sdd" / "lineage"
        if lineage_store is not None:
            store = lineage_store
        elif store_root.exists():
            from bernstein.core.lineage.store import LineageStore

            store = LineageStore(store_root)
        else:
            store = None

        if store is not None:
            from bernstein.core.lineage.coverage import find_all_coverage_for_run

            coverage_entries = find_all_coverage_for_run(store)
            if coverage_entries:
                # Use the latest matching coverage entry
                record = _parse_entry_coverage(coverage_entries[-1], store)

    if record is None:
        return False, f"unverified: absence claim for {pattern_or_query!r} has no coverage record"

    if record.truncated or record.coverage != "complete":
        reason = f" ({record.truncation_reason})" if record.truncation_reason else ""
        return (
            False,
            f"unverified: partial coverage for {pattern_or_query!r} (truncated={record.truncated}{reason})",
        )

    return True, f"verified absence: covered {record.file_count} file(s) (digest={record.corpus_digest})"


def _parse_entry_coverage(entry: LineageEntry, store: LineageStore) -> ToolCoverageRecord | None:
    """Read content of a lineage coverage entry and parse it into a ToolCoverageRecord."""
    try:
        # Check by-artefact or read log
        proj_path = store._projection_path(entry.artefact_path)
        if proj_path.exists():
            lines = proj_path.read_text(encoding="utf-8").splitlines()
            if lines:
                row = json.loads(lines[-1])
                if "content" in row and isinstance(row["content"], dict):
                    return ToolCoverageRecord.from_dict(row["content"])
        log_path = store.log_path
        if log_path.exists():
            for line in log_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                content = row.get("content")
                if row.get("artefact_path") == entry.artefact_path and isinstance(content, dict):
                    return ToolCoverageRecord.from_dict(content)
    except Exception:
        pass
    # No real coverage payload could be recovered for this entry: a lineage
    # entry anchors a `content_hash` commitment (see LineageEntry / entry.py),
    # never the payload bytes themselves, so a coverage-kind entry whose
    # content cannot be located in the by-artefact projection or the raw log
    # carries no verifiable claim about what was actually covered - the id
    # dangles, or the record predates any content it could be checked
    # against. Fabricating a "complete, zero-file, non-truncated" record here
    # would let *any* anchored (or malformed) coverage entry read as a fully
    # verified absence claim regardless of what it actually covered, which is
    # exactly the self-referential/empty-record spoof this module exists to
    # refuse. Fail closed: report no coverage record rather than an invented
    # one.
    return None


def classify_completion_coverage(
    *,
    verify_result: JournalVerifyResult | None = None,
    coverage_record: ToolCoverageRecord | None = None,
    passed: bool = False,
    detail: str = "",
) -> CompletionAbsenceVerdict:
    """Classify a task completion verdict based on journal or tool coverage."""
    if verify_result is not None:
        if verify_result.chain_consistent and verify_result.coverage == JournalCoverageStatus.COMPLETE:
            return CompletionAbsenceVerdict(
                status=CompletionCoverageStatus.VERIFIED,
                is_verified=True,
                passed=True,
                detail="verified: complete journal coverage",
            )
        return CompletionAbsenceVerdict(
            status=CompletionCoverageStatus.UNVERIFIED,
            is_verified=False,
            passed=False,
            detail=f"unverified: journal coverage is {verify_result.coverage}",
        )

    if coverage_record is not None:
        if coverage_record.coverage == "complete" and not coverage_record.truncated:
            return CompletionAbsenceVerdict(
                status=CompletionCoverageStatus.VERIFIED,
                is_verified=True,
                passed=True,
                detail=f"verified: covered {coverage_record.file_count} files",
                coverage_record=coverage_record,
            )
        return CompletionAbsenceVerdict(
            status=CompletionCoverageStatus.UNVERIFIED,
            is_verified=False,
            passed=False,
            detail=f"unverified: partial tool coverage (truncated={coverage_record.truncated})",
            coverage_record=coverage_record,
        )

    if passed and "verified" in detail.lower() and "unverified" not in detail.lower():
        return CompletionAbsenceVerdict(
            status=CompletionCoverageStatus.VERIFIED,
            is_verified=True,
            passed=True,
            detail=detail or "verified",
        )

    return CompletionAbsenceVerdict(
        status=CompletionCoverageStatus.UNVERIFIED,
        is_verified=False,
        passed=passed,
        detail=detail or "unverified: no coverage record",
    )
