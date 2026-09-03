"""Absence coverage verification and completion classification (issue #3771)."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

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

logger = logging.getLogger(__name__)

#: Glob for the per-agent tool-call ledgers written by
#: :class:`bernstein.core.instrumentation.RunInstrumenter` under a project's
#: ``.sdd/`` tree. Mirrors
#: :func:`bernstein.core.instrumentation.resolve_agent_dir`.
_TOOL_CALLS_GLOB = ".sdd/runs/*/tasks/*/agents/*/tool-calls.jsonl"


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


def _canonical_coverage_bytes(payload: dict[str, Any]) -> bytes:
    """Encode a coverage payload exactly as :func:`anchor_coverage_record` does."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _recorded_coverage_payload(workdir: Path, tool_call_id: str) -> dict[str, Any] | None:
    """Return the coverage payload the instrumenter recorded for ``tool_call_id``.

    Scans the per-agent ``tool-calls.jsonl`` ledgers under ``workdir/.sdd/runs``.
    A batched agent mirrors identical lines into several task directories, so
    the first match is authoritative; paths are sorted to keep the choice
    deterministic when several runs are present.
    """
    for path in sorted(workdir.glob(_TOOL_CALLS_GLOB)):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            logger.warning("absence coverage: cannot read tool-call ledger %s: %s", path, exc)
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                parsed: object = json.loads(line)
            except ValueError:
                # A torn or corrupted ledger line is exactly the "the search may
                # not have run" case; skip it rather than treat it as evidence.
                continue
            if not isinstance(parsed, dict):
                continue
            row = cast("dict[str, Any]", parsed)
            if row.get("call_id") != tool_call_id:
                continue
            payload = row.get("coverage")
            if isinstance(payload, dict):
                return cast("dict[str, Any]", payload)
    return None


def verify_anchored_absence_claim(
    *,
    tool_call_id: str,
    workdir: Path,
    lineage_store: LineageStore | None = None,
) -> tuple[bool, str]:
    """Verify one absence claim against the coverage anchored to its own tool call.

    An absence claim ("no occurrences found") is readable as verified only when
    all of the following hold:

    1. The tool call recorded a coverage payload (issue #3769).
    2. A lineage entry of kind ``coverage`` is anchored to the *same*
       ``tool_call_id`` (issue #3770) - coverage anchored to another call is
       not this claim's scope and never stands in for it.
    3. The recorded payload's canonical digest equals that entry's
       ``content_hash``, so the scope cannot be rewritten after sealing.
    4. The walk terminated normally (``coverage == "complete"``, not truncated)
       and the tool's exit status was actually checked.

    Any failure returns ``(False, "unverified: ...")`` rather than raising, so a
    caller evaluating a list of signals needs no guard at the call site.

    Args:
        tool_call_id: Identifier of the call that reported the absence.
        workdir: Project root containing ``.sdd/``.
        lineage_store: Store override; defaults to ``workdir/.sdd/lineage``.

    Returns:
        Tuple of ``(verified, detail)``.
    """
    payload = _recorded_coverage_payload(workdir, tool_call_id)
    if payload is None:
        return False, f"unverified: absence claim for tool call {tool_call_id!r} has no coverage record"

    store = lineage_store
    if store is None:
        store_root = workdir / ".sdd" / "lineage"
        if not store_root.exists():
            return False, f"unverified: coverage for tool call {tool_call_id!r} is not anchored (no lineage log)"
        from bernstein.core.lineage.store import LineageStore as _LineageStore

        store = _LineageStore(store_root)

    from bernstein.core.lineage.coverage import find_coverage_for_tool_call

    entry = find_coverage_for_tool_call(store, tool_call_id)
    if entry is None:
        return False, f"unverified: coverage for tool call {tool_call_id!r} is not anchored in lineage"

    digest = "sha256:" + hashlib.sha256(_canonical_coverage_bytes(payload)).hexdigest()
    if digest != entry.content_hash:
        return (
            False,
            f"unverified: recorded coverage for tool call {tool_call_id!r} does not match its anchor "
            f"(recorded {digest}, anchored {entry.content_hash})",
        )

    record = ToolCoverageRecord.from_dict(payload)
    if record.truncated or record.coverage != "complete":
        reason = record.truncation_reason or "no reason recorded"
        return (
            False,
            f"unverified: coverage for tool call {tool_call_id!r} is {record.coverage} "
            f"(truncated={record.truncated}, reason={reason})",
        )
    if not record.exit_checked:
        return (
            False,
            f"unverified: coverage for tool call {tool_call_id!r} reports exit status "
            f"{record.exit_status!r} that was never checked",
        )

    return (
        True,
        f"verified absence: tool call {tool_call_id} covered {record.file_count} item(s) "
        f"(corpus {record.corpus_digest}, anchored {digest})",
    )
