"""Execution, cache, timeout, and journal runtime for declared governance probes."""

from __future__ import annotations

import hashlib
import json
import math
import queue
import random
import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from bernstein.core.govern.probe import CollectionMethod, Probe, ProbeSet
    from bernstein.core.lineage.spine import LineageSpine

__all__ = [
    "ProbeCollector",
    "ProbeFactCache",
    "ProbeFailure",
    "ProbeFailureKind",
    "ProbeResult",
    "ProbeRunJournalEntry",
    "ProbeRunResult",
    "ProbeStatus",
    "record_probe_run",
    "run_probe_set",
]

_DEFAULT_START_JITTER_S = 0.05
_PROBE_JOURNAL_ARTIFACT_PATH = "govern-discover/probe-run-journal.json"


class ProbeStatus(StrEnum):
    """Whether one probe produced a value for one target."""

    ANSWERED = "answered"
    UNKNOWN = "unknown"


class ProbeFailureKind(StrEnum):
    """Normalized reasons one collection attempt did not answer."""

    TIMEOUT = "timeout"
    EXCEPTION = "exception"
    NO_ANSWER = "no_answer"
    UNAVAILABLE = "unavailable"


class ProbeCollector(Protocol):
    """Collect one declared fact from one target."""

    def __call__(self, target: str, probe: Probe) -> Any | None:
        """Return a value, or ``None`` when this source cannot answer."""
        ...


@dataclass(frozen=True, slots=True)
class ProbeFailure:
    """One normalized failed source attempt, safe to place in a journal."""

    target: str
    probe_id: str
    source: CollectionMethod
    attempt: int
    kind: ProbeFailureKind
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "target": self.target,
            "probe_id": self.probe_id,
            "source": str(self.source),
            "attempt": self.attempt,
            "kind": str(self.kind),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """The value state of one probe against one target."""

    target: str
    probe_id: str
    attribute: str
    status: ProbeStatus
    value: Any | None = None
    source: CollectionMethod | None = None
    cached: bool = False


@dataclass(frozen=True, slots=True)
class ProbeRunJournalEntry:
    """The single canonical journal record for one probe-set run."""

    probe_set_version: str
    attempted_targets: tuple[str, ...]
    answered_targets: tuple[str, ...]
    failures: tuple[ProbeFailure, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "probe_set_version": self.probe_set_version,
            "attempted_targets": list(self.attempted_targets),
            "answered_targets": list(self.answered_targets),
            "failures": [failure.to_dict() for failure in self.failures],
        }

    def to_canonical_bytes(self) -> bytes:
        """Serialize to canonical JSON bytes."""
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class ProbeRunResult:
    """Every per-target probe result plus the one run journal entry."""

    results: tuple[ProbeResult, ...]
    journal: ProbeRunJournalEntry

    def result_for(self, target: str, probe_id: str) -> ProbeResult | None:
        """Return one target/probe result, or ``None`` if it was not attempted."""
        return next(
            (result for result in self.results if result.target == target and result.probe_id == probe_id),
            None,
        )


@dataclass(frozen=True, slots=True)
class _ProbeCacheEntry:
    value: Any
    source: CollectionMethod
    collected_at: float


@dataclass(slots=True)
class ProbeFactCache:
    """In-memory fact cache keyed by ``(target, probe id)``."""

    _entries: dict[tuple[str, str], _ProbeCacheEntry] = field(default_factory=dict[tuple[str, str], _ProbeCacheEntry])

    def get_fresh(self, target: str, probe: Probe, *, now: float) -> _ProbeCacheEntry | None:
        """Return a cached fact only while the probe's refresh window is open."""
        if not math.isfinite(probe.refresh_interval_s) or probe.refresh_interval_s <= 0:
            return None
        entry = self._entries.get((target, probe.id))
        if entry is None or now - entry.collected_at >= probe.refresh_interval_s:
            return None
        return entry

    def put(self, target: str, probe: Probe, *, value: Any, source: CollectionMethod, collected_at: float) -> None:
        """Store one answered fact under the stable target/probe key."""
        self._entries[(target, probe.id)] = _ProbeCacheEntry(value=value, source=source, collected_at=collected_at)


@dataclass(frozen=True, slots=True)
class _CallOutcome:
    value: Any | None = None
    error_type: str | None = None
    timed_out: bool = False


def run_probe_set(
    probe_set: ProbeSet,
    *,
    targets: Iterable[str],
    collectors: Mapping[CollectionMethod, ProbeCollector],
    cache: ProbeFactCache | None = None,
    clock: Callable[[], float] = time.monotonic,
    rng: Callable[[], float] = random.random,
    sleeper: Callable[[float], None] = time.sleep,
    jitter_max_s: float = _DEFAULT_START_JITTER_S,
) -> ProbeRunResult:
    """Run a declared probe set without letting one failed probe abort the run.

    Cached facts inside ``refresh_interval_s`` bypass collection. Every other
    target/probe pair receives a bounded full-jitter start delay, then the
    primary method and declared fallbacks are tried in order. ``timeout_s`` is
    a deadline for the whole target/probe invocation, not for each retry.

    Args:
        probe_set: Declarations to execute.
        targets: Target ids. Duplicate ids are collapsed in first-seen order.
        collectors: Runtime implementations keyed by collection method.
        cache: Optional fact cache reused across runs.
        clock: Monotonic clock, injectable for cache-boundary tests.
        rng: Random value producer in ``[0, 1)`` for full jitter.
        sleeper: Delay function, injectable so tests do not actually sleep.
        jitter_max_s: Internal maximum jitter bound.

    Returns:
        Per-probe results and exactly one canonical run journal entry.

    Raises:
        ValueError: ``jitter_max_s`` is negative.
    """
    if jitter_max_s < 0:
        raise ValueError("jitter_max_s must not be negative")

    resolved_targets = tuple(dict.fromkeys(targets))
    fact_cache = cache if cache is not None else ProbeFactCache()
    results: list[ProbeResult] = []
    failures: list[ProbeFailure] = []

    for target in resolved_targets:
        for probe in probe_set:
            cached = fact_cache.get_fresh(target, probe, now=clock())
            if cached is not None:
                results.append(
                    ProbeResult(
                        target=target,
                        probe_id=probe.id,
                        attribute=probe.attribute,
                        status=ProbeStatus.ANSWERED,
                        value=cached.value,
                        source=cached.source,
                        cached=True,
                    )
                )
                continue

            sleeper(rng() * jitter_max_s)
            result, probe_failures = _collect_probe(
                target=target,
                probe=probe,
                collectors=collectors,
                clock=clock,
            )
            results.append(result)
            failures.extend(probe_failures)
            if result.status is ProbeStatus.ANSWERED and result.source is not None:
                fact_cache.put(
                    target,
                    probe,
                    value=result.value,
                    source=result.source,
                    collected_at=clock(),
                )

    answered_targets = tuple(
        target
        for target in resolved_targets
        if any(result.target == target and result.status is ProbeStatus.ANSWERED for result in results)
    )
    journal = ProbeRunJournalEntry(
        probe_set_version=probe_set.version,
        attempted_targets=resolved_targets,
        answered_targets=answered_targets,
        failures=tuple(failures),
    )
    return ProbeRunResult(results=tuple(results), journal=journal)


def record_probe_run(
    run: ProbeRunResult,
    *,
    spine: LineageSpine,
    timestamp: int,
    actor: str = "bernstein.govern.discover",
) -> str:
    """Anchor exactly one journal record for a completed probe-set run."""
    content = run.journal.to_canonical_bytes()
    step_id = "sha256:" + hashlib.sha256(content).hexdigest()
    return spine.record(
        artifact_path=_PROBE_JOURNAL_ARTIFACT_PATH,
        content=content,
        actor=actor,
        step_id=step_id,
        model="",
        timestamp=timestamp,
    )


def _collect_probe(
    *,
    target: str,
    probe: Probe,
    collectors: Mapping[CollectionMethod, ProbeCollector],
    clock: Callable[[], float],
) -> tuple[ProbeResult, list[ProbeFailure]]:
    failures: list[ProbeFailure] = []
    timeout_s = _safe_timeout_s(probe.timeout_s)
    if timeout_s is None:
        return _unknown_result(target, probe), [
            ProbeFailure(
                target=target,
                probe_id=probe.id,
                source=probe.collection_method,
                attempt=1,
                kind=ProbeFailureKind.TIMEOUT,
                reason="invalid probe timeout",
            )
        ]
    deadline = clock() + timeout_s
    sources = (probe.collection_method, *(probe.fallback_methods or ()))
    attempts_per_source = 1 + (probe.max_retries or 0)

    for source in sources:
        collector = collectors.get(source)
        if collector is None:
            failures.append(
                ProbeFailure(
                    target=target,
                    probe_id=probe.id,
                    source=source,
                    attempt=1,
                    kind=ProbeFailureKind.UNAVAILABLE,
                    reason="collector unavailable",
                )
            )
            continue

        for attempt in range(1, attempts_per_source + 1):
            remaining = deadline - clock()
            if remaining <= 0:
                failures.append(
                    ProbeFailure(
                        target=target,
                        probe_id=probe.id,
                        source=source,
                        attempt=attempt,
                        kind=ProbeFailureKind.TIMEOUT,
                        reason="probe deadline exceeded",
                    )
                )
                return _unknown_result(target, probe), failures

            outcome = _call_with_timeout(lambda collector=collector: collector(target, probe), remaining)
            if outcome.timed_out:
                failures.append(
                    ProbeFailure(
                        target=target,
                        probe_id=probe.id,
                        source=source,
                        attempt=attempt,
                        kind=ProbeFailureKind.TIMEOUT,
                        reason="probe deadline exceeded",
                    )
                )
                return _unknown_result(target, probe), failures
            if outcome.error_type is not None:
                failures.append(
                    ProbeFailure(
                        target=target,
                        probe_id=probe.id,
                        source=source,
                        attempt=attempt,
                        kind=ProbeFailureKind.EXCEPTION,
                        reason=outcome.error_type,
                    )
                )
                continue
            if outcome.value is None:
                failures.append(
                    ProbeFailure(
                        target=target,
                        probe_id=probe.id,
                        source=source,
                        attempt=attempt,
                        kind=ProbeFailureKind.NO_ANSWER,
                        reason="collector returned no value",
                    )
                )
                continue

            return (
                ProbeResult(
                    target=target,
                    probe_id=probe.id,
                    attribute=probe.attribute,
                    status=ProbeStatus.ANSWERED,
                    value=outcome.value,
                    source=source,
                ),
                failures,
            )

    return _unknown_result(target, probe), failures


def _unknown_result(target: str, probe: Probe) -> ProbeResult:
    return ProbeResult(
        target=target,
        probe_id=probe.id,
        attribute=probe.attribute,
        status=ProbeStatus.UNKNOWN,
    )


def _call_with_timeout(call: Callable[[], Any | None], timeout_s: float) -> _CallOutcome:
    if _safe_timeout_s(timeout_s) is None:
        return _CallOutcome(timed_out=True)
    result_queue: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def _worker() -> None:
        try:
            value = call()
        except Exception as exc:
            result_queue.put((False, type(exc).__name__))
        else:
            result_queue.put((True, value))

    worker = threading.Thread(target=_worker, name="bernstein-probe", daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        return _CallOutcome(timed_out=True)

    try:
        answered, payload = result_queue.get_nowait()
    except queue.Empty:
        return _CallOutcome(error_type="collector terminated without a result")
    if answered:
        return _CallOutcome(value=payload)
    return _CallOutcome(error_type=str(payload))


def _safe_timeout_s(value: object) -> float | None:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    try:
        timeout_s = float(value)
    except OverflowError:
        return None
    if not math.isfinite(timeout_s) or timeout_s <= 0 or timeout_s > threading.TIMEOUT_MAX:
        return None
    return timeout_s
