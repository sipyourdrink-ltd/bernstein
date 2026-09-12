"""
Outbound boundary model call check records and coverage verification (#5477).

Captures an immutable, content-addressed coverage record at every model-call
boundary. Ensures that "checked and clean" and "never checked" are distinct,
queryable states, and commits to prompts strictly by cryptographic digest without
carrying prompt content or secrets in the record bytes.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.quality.absence_coverage import CompletionCoverageStatus

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from bernstein.core.lineage.spine import LineageSpine
    from bernstein.core.security.guardrail_pipeline import GuardrailPipeline


def _canonical_json(data: Any) -> str:
    """Deterministic JSON string (sorted keys, compact separators)."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


_IN_RECORDED_CALL_VAR = contextvars.ContextVar[bool]("_in_recorded_call", default=False)


def is_in_recorded_call() -> bool:
    """Return True if the current async context is already within an outer recorded call."""
    return _IN_RECORDED_CALL_VAR.get()


@contextmanager
def in_recorded_call_scope() -> Iterator[None]:
    """Scope guard indicating that the outer caller is handling outbound recording."""
    token = _IN_RECORDED_CALL_VAR.set(True)
    try:
        yield
    finally:
        _IN_RECORDED_CALL_VAR.reset(token)


@dataclass(frozen=True)
class OutboundCheckRecord:
    """Immutable record of an outbound model call's inspection and coverage state.

    Attributes:
        call_id: Identifier of the outbound call.
        prompt_digest: SHA-256 digest of the raw prompt text (never raw text).
        request_digest: SHA-256 digest of canonical request parameters.
        model: Target model identifier.
        provider: Provider identifier (e.g. openrouter_free, openai).
        status: CompletionCoverageStatus ('verified' or 'unverified').
        passed: Whether all configured guardrail checks passed.
        checks_run: Tuple of guardrail/check names executed.
        violations: Tuple of sanitized rule descriptions / failure reason codes.
        timestamp: Time of inspection (Unix epoch).
        details: Human-readable diagnostic description.
        replay: Whether this call was replayed from a deterministic store.
    """

    call_id: str
    prompt_digest: str
    request_digest: str
    model: str
    provider: str
    status: CompletionCoverageStatus
    passed: bool
    checks_run: tuple[str, ...] = field(default_factory=tuple)
    violations: tuple[str, ...] = field(default_factory=tuple)
    timestamp: float = field(default_factory=time.time)
    details: str = ""
    replay: bool = False

    def canonical_dict(self) -> dict[str, Any]:
        """Convert to canonical dictionary (no prompt content, only digests)."""
        return {
            "call_id": self.call_id,
            "checks_run": list(self.checks_run),
            "details": self.details,
            "model": self.model,
            "passed": self.passed,
            "prompt_digest": self.prompt_digest,
            "provider": self.provider,
            "replay": self.replay,
            "request_digest": self.request_digest,
            "status": self.status.value,
            "timestamp": self.timestamp,
            "violations": list(self.violations),
        }

    def to_dict(self) -> dict[str, Any]:
        return self.canonical_dict()

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.canonical_dict()).encode("utf-8")

    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True)
class OutboundCoverageFold:
    """Aggregated coverage fold across all outbound model calls in a run.

    Attributes:
        total_calls: Total number of outbound model calls.
        verified_calls: Number of calls inspected and verified clean.
        unverified_calls: Number of calls that bypassed inspection.
        refused_calls: Number of calls that failed guardrail checks.
        coverage_ratio: Proportion of calls verified in [0.0, 1.0].
        is_fully_covered: Whether all calls were verified without gaps.
        records: Tuple of individual :class:`OutboundCheckRecord` items.
        gaps: Tuple of missing or unanchored call identifiers.
    """

    total_calls: int
    verified_calls: int
    unverified_calls: int
    refused_calls: int
    coverage_ratio: float
    is_fully_covered: bool
    records: tuple[OutboundCheckRecord, ...]
    gaps: tuple[str, ...] = field(default_factory=tuple)

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "coverage_ratio": self.coverage_ratio,
            "gaps": list(self.gaps),
            "is_fully_covered": self.is_fully_covered,
            "records": [r.canonical_dict() for r in self.records],
            "refused_calls": self.refused_calls,
            "total_calls": self.total_calls,
            "unverified_calls": self.unverified_calls,
            "verified_calls": self.verified_calls,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.canonical_dict()

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.canonical_dict()).encode("utf-8")


def fold_outbound_coverage(
    records: Sequence[OutboundCheckRecord],
    expected_call_ids: Sequence[str] | None = None,
) -> OutboundCoverageFold:
    """Aggregate outbound call records into a deterministic, verifiable fold."""
    record_map = {r.call_id: r for r in records}
    gaps: list[str] = []

    if expected_call_ids is not None:
        for cid in expected_call_ids:
            if cid not in record_map:
                gaps.append(cid)
        total = len(expected_call_ids)
    else:
        total = len(records)

    verified = sum(1 for r in records if r.status == CompletionCoverageStatus.VERIFIED and r.passed)
    refused = sum(1 for r in records if not r.passed)
    unverified = total - verified

    ratio = (verified / total) if total > 0 else 1.0
    fully_covered = (unverified == 0 and len(gaps) == 0 and total > 0) or (total == 0)

    return OutboundCoverageFold(
        total_calls=total,
        verified_calls=verified,
        unverified_calls=unverified,
        refused_calls=refused,
        coverage_ratio=ratio,
        is_fully_covered=fully_covered,
        records=tuple(records),
        gaps=tuple(gaps),
    )


# ---------------------------------------------------------------------------
# Module-level Active Recorder Context
# ---------------------------------------------------------------------------

_active_outbound_recorder: OutboundCallRecorder | None = None


def get_active_outbound_recorder() -> OutboundCallRecorder | None:
    """Return the currently active OutboundCallRecorder, or None."""
    return _active_outbound_recorder


def set_active_outbound_recorder(recorder: OutboundCallRecorder | None) -> None:
    """Set the process-level active OutboundCallRecorder."""
    global _active_outbound_recorder
    _active_outbound_recorder = recorder


@contextmanager
def active_outbound_recorder(
    recorder: OutboundCallRecorder | None,
) -> Iterator[OutboundCallRecorder | None]:
    """Context manager to activate an OutboundCallRecorder temporarily."""
    prev = get_active_outbound_recorder()
    set_active_outbound_recorder(recorder)
    try:
        yield recorder
    finally:
        set_active_outbound_recorder(prev)


# ---------------------------------------------------------------------------
# Outbound Call Recorder
# ---------------------------------------------------------------------------


class OutboundCallRecorder:
    """Inspects outbound model calls and seals check records into the lineage chain."""

    def __init__(
        self,
        pipeline: GuardrailPipeline | None = None,
        spine: LineageSpine | None = None,
        run_id: str = "",
    ) -> None:
        self.pipeline = pipeline
        self.spine = spine
        self.run_id = run_id
        self._records: list[OutboundCheckRecord] = []
        self._call_counter = 0

    @property
    def records(self) -> list[OutboundCheckRecord]:
        return list(self._records)

    def check_and_record(
        self,
        *,
        prompt: str,
        model: str,
        provider: str,
        temperature: float = 0.7,
        max_tokens: int = 4000,
        call_id: str | None = None,
        replay: bool = False,
        context: dict[str, Any] | None = None,
    ) -> OutboundCheckRecord:
        """Inspect the prompt and record an OutboundCheckRecord."""
        self._call_counter += 1
        prompt_digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()

        req_payload = {
            "max_tokens": max_tokens,
            "model": model,
            "prompt_digest": prompt_digest,
            "provider": provider,
            "temperature": temperature,
        }
        request_digest = hashlib.sha256(_canonical_json(req_payload).encode("utf-8")).hexdigest()

        resolved_call_id = call_id or f"outbound-call-{self._call_counter:04d}-{request_digest[:8]}"

        if self.pipeline is not None and len(self.pipeline.guardrails) > 0:
            results = self.pipeline.check_input(prompt, context or {})
            passed = self.pipeline.all_passed(results)
            checks_run = tuple(r.guardrail_name for r in results)
            raw_violations = self.pipeline.violations(results)

            # Sanitize violations so no prompt substring or token is leaked
            sanitized_violations = []
            for v in raw_violations:
                # Keep rule/detector identifier, strip payload fragments
                if ":" in v:
                    prefix = v.split(":", 1)[0].strip()
                    sanitized_violations.append(prefix)
                else:
                    sanitized_violations.append(v)

            status = CompletionCoverageStatus.VERIFIED if passed else CompletionCoverageStatus.UNVERIFIED
            details = (
                "verified: all guardrails passed" if passed else f"refused: {len(sanitized_violations)} violations"
            )
            violations_tuple = tuple(sanitized_violations)
        else:
            # Unchecked call: record status is unverified
            passed = True
            checks_run = ()
            violations_tuple = ()
            status = CompletionCoverageStatus.UNVERIFIED
            details = "unverified: no checks configured"

        record = OutboundCheckRecord(
            call_id=resolved_call_id,
            prompt_digest=prompt_digest,
            request_digest=request_digest,
            model=model,
            provider=provider,
            status=status,
            passed=passed,
            checks_run=checks_run,
            violations=violations_tuple,
            timestamp=time.time(),
            details=details,
            replay=replay,
        )

        self._records.append(record)

        if self.spine is not None:
            with contextlib.suppress(Exception):
                self.spine.record(
                    artifact_path=f"outbound_calls/{resolved_call_id}.json",
                    content=record.canonical_bytes(),
                    actor="outbound_boundary",
                    step_id=f"outbound_call:{resolved_call_id}",
                    model=model,
                    timestamp=int(record.timestamp * 1_000_000_000),
                )

        return record
