"""Lineage anchoring for tool coverage records (issue #3770)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from bernstein.core.tools.coverage import ToolCoverageRecord

if TYPE_CHECKING:
    from bernstein.core.lineage.entry import LineageEntry
    from bernstein.core.lineage.identity import AgentCard
    from bernstein.core.lineage.signed_write import SignedLineageLog
    from bernstein.core.lineage.store import LineageStore

COVERAGE_ARTEFACT_KIND = "coverage"
"""``artefact_kind`` used for an absence coverage record."""


def _coverage_artefact_path(tool_name: str, tool_call_id: str) -> str:
    """Return canonical repo-relative artefact path for a coverage record."""
    clean_tool = tool_name.strip().replace("/", "_") or "tool"
    clean_id = tool_call_id.strip().replace("/", "_") or "unknown"
    return f"coverage/{clean_tool}/{clean_id}"


def anchor_coverage_record(
    recorder: SignedLineageLog,
    *,
    tool_name: str,
    tool_call_id: str,
    coverage: ToolCoverageRecord | dict[str, Any] | bytes,
    agent_id: str,
    agent_card: AgentCard,
    private_key_pem: str,
    span_id: str = "0000000000000000",
    trust_class: str | None = None,
    extra_parents: list[str] | None = None,
    attachment_digests: list[str] | None = None,
    ts_ns: int | None = None,
) -> str:
    """Seal a tool coverage record into the signed lineage chain.

    Args:
        recorder: Active SignedLineageLog instance.
        tool_name: Name of the tool whose coverage is being recorded.
        tool_call_id: Unique tool call identifier matching the originating call.
        coverage: ToolCoverageRecord dataclass, dict, or pre-encoded JSON bytes.
        agent_id: Identifier of the recording agent.
        agent_card: Agent's public credentials.
        private_key_pem: Agent's private key for signing.
        span_id: OpenTelemetry span id or 16-hex default.
        trust_class: Optional trust class for provenance.
        extra_parents: Optional explicit parent hashes.
        attachment_digests: Optional operator attachment digests.
        ts_ns: Timestamp in nanoseconds.

    Returns:
        Entry hash of the newly recorded lineage entry.
    """
    if isinstance(coverage, bytes):
        content_bytes = coverage
    elif isinstance(coverage, ToolCoverageRecord):
        content_bytes = json.dumps(coverage.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    elif isinstance(coverage, dict):
        content_bytes = json.dumps(coverage, sort_keys=True, separators=(",", ":")).encode("utf-8")
    else:
        raise TypeError(f"unsupported coverage payload type: {type(coverage).__name__}")

    artefact_path = _coverage_artefact_path(tool_name, tool_call_id)
    return recorder.record_write(
        artefact_path=artefact_path,
        new_content=content_bytes,
        agent_id=agent_id,
        agent_card=agent_card,
        private_key_pem=private_key_pem,
        tool_call_id=tool_call_id,
        span_id=span_id,
        artefact_kind=COVERAGE_ARTEFACT_KIND,
        trust_class=trust_class,
        extra_parents=extra_parents,
        attachment_digests=attachment_digests,
        ts_ns=ts_ns,
    )


def find_coverage_for_tool_call(store: LineageStore, tool_call_id: str) -> LineageEntry | None:
    """Find the anchored coverage entry for a given tool_call_id, if any."""
    for entry, _ in store.read_log():
        if entry.tool_call_id == tool_call_id and entry.artefact_kind == COVERAGE_ARTEFACT_KIND:
            return entry
    return None


def find_all_coverage_for_run(store: LineageStore) -> list[LineageEntry]:
    """Find all anchored coverage entries in the store."""
    return [entry for entry, _ in store.read_log() if entry.artefact_kind == COVERAGE_ARTEFACT_KIND]
