"""Post-session token waste report.

Analyses the token-sidecar records written by agent sessions and identifies
categories of waste:

- **Retries** — repeated token bursts (input spikes) with no file output
  between them, indicating the agent re-sent a large prompt multiple times.
- **Loops** — quadratic growth in per-interval token deltas, meaning the
  context is growing super-linearly (usually from accumulated tool output).
- **Oversized contexts** — any single interval where tokens consumed exceed
  the ``oversized_threshold`` parameter without a corresponding file change.

The report is written to ``.sdd/metrics/token_waste_{session_id}.json`` and
returned as a :class:`TokenWasteReport` dataclass.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from bernstein.core.defaults import TOKEN

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thresholds — sourced from bernstein.core.defaults.TOKEN
# ---------------------------------------------------------------------------

#: Minimum token burst (in a single interval) that counts as a "retry spike".
_RETRY_SPIKE_TOKENS: int = 5_000

#: Ratio of consecutive growth windows to classify as a loop.
_LOOP_GROWTH_RATIO: float = 1.8

#: Token delta in a single interval to flag as an oversized-context event.
_OVERSIZED_INTERVAL_TOKENS: int = TOKEN.oversized_interval_tokens

#: Minimum number of samples required to check for loops.
_MIN_LOOP_SAMPLES: int = TOKEN.min_loop_samples


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class TokenRecord:
    """One parsed record from a ``.tokens`` sidecar file.

    Attributes:
        ts: Unix timestamp of the record.
        input_tokens: Input tokens consumed in this turn.
        output_tokens: Output tokens generated in this turn.
    """

    ts: float
    input_tokens: int
    output_tokens: int

    @property
    def total(self) -> int:
        """Total tokens for this record."""
        return self.input_tokens + self.output_tokens


@dataclass
class WasteFinding:
    """A single waste event identified in the session.

    Attributes:
        category: One of ``retry``, ``loop``, ``oversized_context``.
        token_count: Tokens involved in this waste event.
        description: Human-readable description of the finding.
        record_index: Index into the token record list where this occurred.
    """

    category: str  # "retry" | "loop" | "oversized_context"
    token_count: int
    description: str
    record_index: int = -1


@dataclass
class TokenWasteReport:
    """Post-session token waste analysis for one agent session.

    Attributes:
        session_id: Agent session identifier.
        total_tokens: Cumulative tokens across all records.
        findings: List of identified waste events.
        wasted_tokens: Sum of tokens across all findings.
        efficiency_pct: Fraction of tokens *not* identified as waste (0-100).
        generated_at: ISO-8601 timestamp when the report was generated.
    """

    session_id: str
    total_tokens: int
    findings: list[WasteFinding] = field(default_factory=list)
    wasted_tokens: int = 0
    efficiency_pct: float = 100.0
    generated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def summary(self) -> str:
        """Return a human-readable one-line summary.

        Returns:
            Summary string with total tokens, waste count, and efficiency.
        """
        return (
            f"session={self.session_id} total={self.total_tokens:,} "
            f"wasted={self.wasted_tokens:,} efficiency={self.efficiency_pct:.1f}% "
            f"findings={len(self.findings)}"
        )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_token_records(raw: str) -> list[TokenRecord]:
    """Parse newline-delimited JSON token records from a sidecar file.

    Args:
        raw: Full text content of a ``.tokens`` sidecar file.

    Returns:
        Ordered list of :class:`TokenRecord` objects (malformed lines skipped).
    """
    records: list[TokenRecord] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            records.append(
                TokenRecord(
                    ts=float(obj.get("ts", 0.0)),
                    input_tokens=int(obj.get("in", 0)),
                    output_tokens=int(obj.get("out", 0)),
                )
            )
        except (ValueError, TypeError):
            continue
    return records


# ---------------------------------------------------------------------------
# Detection logic
# ---------------------------------------------------------------------------


def _detect_retries(records: list[TokenRecord], spike_threshold: int) -> list[WasteFinding]:
    """Detect input-token spikes that suggest the agent retried a large prompt.

    A retry is flagged when consecutive records both have input tokens above
    ``spike_threshold``.  The second record is counted as the waste event
    because the first occurrence was necessary; only the repetition is waste.

    Args:
        records: Ordered token records for the session.
        spike_threshold: Minimum input tokens per record to count as a spike.

    Returns:
        List of :class:`WasteFinding` with ``category="retry"``.
    """
    findings: list[WasteFinding] = []
    prev_was_spike = False
    for i, rec in enumerate(records):
        is_spike = rec.input_tokens >= spike_threshold
        if is_spike and prev_was_spike:
            findings.append(
                WasteFinding(
                    category="retry",
                    token_count=rec.input_tokens,
                    description=(
                        f"Retry spike at record {i}: {rec.input_tokens:,} input tokens "
                        f"(>= threshold {spike_threshold:,}) following another spike"
                    ),
                    record_index=i,
                )
            )
        prev_was_spike = is_spike
    return findings


def _detect_loops(records: list[TokenRecord], growth_ratio: float) -> list[WasteFinding]:
    """Detect super-linear (quadratic) token growth indicating context loops.

    Checks whether the last per-record token delta is >= ``growth_ratio``
    times the previous delta across the last three consecutive records.

    Args:
        records: Ordered token records for the session.
        growth_ratio: Minimum ratio of consecutive deltas to flag as a loop.

    Returns:
        List of :class:`WasteFinding` with ``category="loop"``.
    """
    findings: list[WasteFinding] = []
    if len(records) < _MIN_LOOP_SAMPLES:
        return findings

    totals = [r.total for r in records]
    deltas = [totals[i] - totals[i - 1] for i in range(1, len(totals))]

    for i in range(1, len(deltas)):
        d_prev = deltas[i - 1]
        d_curr = deltas[i]
        if d_prev > 0 and d_curr >= growth_ratio * d_prev:
            findings.append(
                WasteFinding(
                    category="loop",
                    token_count=d_curr,
                    description=(
                        f"Loop detected at record {i + 1}: delta grew from "
                        f"{d_prev:,} to {d_curr:,} tokens "
                        f"({d_curr / d_prev:.1f}x >= threshold {growth_ratio:.1f}x)"
                    ),
                    record_index=i + 1,
                )
            )
    return findings


def _detect_oversized_contexts(records: list[TokenRecord], interval_threshold: int) -> list[WasteFinding]:
    """Detect single records where token consumption exceeds the interval threshold.

    A large single-record total suggests the agent included an unnecessarily
    large context (e.g., reading entire large files into the prompt).

    Args:
        records: Ordered token records for the session.
        interval_threshold: Minimum total tokens in one record to flag.

    Returns:
        List of :class:`WasteFinding` with ``category="oversized_context"``.
    """
    findings: list[WasteFinding] = []
    for i, rec in enumerate(records):
        if rec.total >= interval_threshold:
            findings.append(
                WasteFinding(
                    category="oversized_context",
                    token_count=rec.total,
                    description=(
                        f"Oversized context at record {i}: {rec.total:,} tokens "
                        f"in a single turn (in={rec.input_tokens:,}, out={rec.output_tokens:,}, "
                        f"threshold={interval_threshold:,})"
                    ),
                    record_index=i,
                )
            )
    return findings


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def analyze_token_waste(
    session_id: str,
    records: list[TokenRecord],
    *,
    retry_spike_threshold: int = _RETRY_SPIKE_TOKENS,
    loop_growth_ratio: float = _LOOP_GROWTH_RATIO,
    oversized_threshold: int = _OVERSIZED_INTERVAL_TOKENS,
) -> TokenWasteReport:
    """Analyse a session's token records and return a waste report.

    Args:
        session_id: Agent session identifier.
        records: Ordered :class:`TokenRecord` list for the session.
        retry_spike_threshold: Input-token count per record to count as a
            retry spike (default 5,000).
        loop_growth_ratio: Delta growth ratio above which a loop is flagged
            (default 1.8x).
        oversized_threshold: Total tokens in a single record to flag as an
            oversized-context event (default 20,000).

    Returns:
        :class:`TokenWasteReport` with all identified findings.
    """
    total_tokens = sum(r.total for r in records)

    findings: list[WasteFinding] = []
    findings.extend(_detect_retries(records, retry_spike_threshold))
    findings.extend(_detect_loops(records, loop_growth_ratio))
    findings.extend(_detect_oversized_contexts(records, oversized_threshold))

    wasted = sum(f.token_count for f in findings)
    efficiency = 100.0 * (1.0 - wasted / total_tokens) if total_tokens > 0 else 100.0

    return TokenWasteReport(
        session_id=session_id,
        total_tokens=total_tokens,
        findings=findings,
        wasted_tokens=wasted,
        efficiency_pct=max(0.0, efficiency),
    )


def generate_session_waste_report(
    session_id: str,
    workdir: Path,
    *,
    retry_spike_threshold: int = _RETRY_SPIKE_TOKENS,
    loop_growth_ratio: float = _LOOP_GROWTH_RATIO,
    oversized_threshold: int = _OVERSIZED_INTERVAL_TOKENS,
    save: bool = True,
) -> TokenWasteReport:
    """Read a session's token sidecar file and generate a waste report.

    Reads ``.sdd/runtime/{session_id}.tokens``, runs all detectors, and
    optionally saves the report to ``.sdd/metrics/token_waste_{session_id}.json``.

    Args:
        session_id: Agent session identifier.
        workdir: Project working directory (parent of ``.sdd/``).
        retry_spike_threshold: Input-token spike threshold for retry detection.
        loop_growth_ratio: Growth-ratio threshold for loop detection.
        oversized_threshold: Single-record token threshold for oversized contexts.
        save: When ``True`` (default), write the report to the metrics directory.

    Returns:
        :class:`TokenWasteReport` for the session.
    """
    from pathlib import Path as _Path

    tokens_file = _Path(workdir) / ".sdd" / "runtime" / f"{session_id}.tokens"
    records: list[TokenRecord] = []

    if tokens_file.exists():
        try:
            raw = tokens_file.read_text(encoding="utf-8", errors="ignore")
            records = _parse_token_records(raw)
        except OSError as exc:
            logger.warning("token_waste_report: could not read %s: %s", tokens_file, exc)

    report = analyze_token_waste(
        session_id,
        records,
        retry_spike_threshold=retry_spike_threshold,
        loop_growth_ratio=loop_growth_ratio,
        oversized_threshold=oversized_threshold,
    )

    logger.info("token_waste_report: %s", report.summary())

    if save:
        _save_report(report, _Path(workdir))

    return report


def _save_report(report: TokenWasteReport, workdir: Path) -> None:
    """Persist a :class:`TokenWasteReport` to the metrics directory.

    Args:
        report: The report to persist.
        workdir: Project working directory.
    """
    from pathlib import Path as _Path

    metrics_dir = _Path(workdir) / ".sdd" / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    out_path = metrics_dir / f"token_waste_{report.session_id}.json"
    payload = {
        "session_id": report.session_id,
        "total_tokens": report.total_tokens,
        "wasted_tokens": report.wasted_tokens,
        "efficiency_pct": report.efficiency_pct,
        "generated_at": report.generated_at,
        "findings": [
            {
                "category": f.category,
                "token_count": f.token_count,
                "description": f.description,
                "record_index": f.record_index,
            }
            for f in report.findings
        ],
    }
    try:
        out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("token_waste_report: could not save report: %s", exc)
