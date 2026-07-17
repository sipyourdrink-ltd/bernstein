"""Ingest-path wiring for the promptware detector.

The orchestration layer (and other call sites that feed tool output to a
downstream agent) call :func:`scan_tool_output` immediately after a tool
returns. The function:

* short-circuits if :data:`promptware_detector.ENV_FLAG` is not set,
* classifies the text with :class:`PromptwareDetector`,
* records a Prometheus histogram observation,
* emits a WARN log when the score is in the warn band,
* dispatches a structured lifecycle event when the score is in the abort
  band so :mod:`bernstein.core.lifecycle` plugins can subscribe and abort
  the next-agent spawn.

The function returns the :class:`PromptwareScore` so the caller can
forward it into structured audit records without re-classifying.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.security.promptware_detector import (
    PromptwareDetector,
    PromptwareScore,
    is_enabled,
)

if TYPE_CHECKING:
    from bernstein.core.lifecycle.hooks import HookRegistry

__all__ = [
    "PROMPTWARE_LIFECYCLE_EVENT",
    "PromptwareIngestResult",
    "QuarantinedIngestResult",
    "build_lifecycle_payload",
    "get_default_detector",
    "quarantine_untrusted_payload",
    "scan_tool_output",
]

log = logging.getLogger(__name__)


# The lifecycle event name we emit when the abort threshold is crossed.
# It is a snake_case Bernstein-native event (the cross-CLI event family is
# fixed) so plugins subscribe by exact string equality with the
# ``LifecycleEvent`` value as the dispatcher does today.
PROMPTWARE_LIFECYCLE_EVENT: str = "post_task"
"""Lifecycle event under which abort signals fire.

We reuse ``post_task`` because it is the closest existing native event in
:class:`bernstein.core.lifecycle.hooks.LifecycleEvent` and is task-scoped.
The payload's ``promptware.score`` and ``promptware.verdict`` keys let
plugins distinguish a promptware-driven post_task from a normal one.
"""


@dataclass(frozen=True, slots=True)
class PromptwareIngestResult:
    """Result returned by :func:`scan_tool_output`.

    Attributes:
        score: The classifier score, even when the feature flag is off.
        emitted_warn: True when a WARN log line was emitted.
        emitted_abort_event: True when a lifecycle abort event was dispatched.
    """

    score: PromptwareScore
    emitted_warn: bool = False
    emitted_abort_event: bool = False


_default_detector: PromptwareDetector | None = None


def get_default_detector() -> PromptwareDetector:
    """Return a shared :class:`PromptwareDetector` instance."""
    global _default_detector
    if _default_detector is None:
        _default_detector = PromptwareDetector()
    return _default_detector


def build_lifecycle_payload(
    score: PromptwareScore,
    *,
    task: str | None,
    session_id: str | None,
    adapter: str | None,
    tool: str | None,
    source_url: str | None,
) -> dict[str, Any]:
    """Build the data payload posted on the lifecycle bus."""
    return {
        "session_id": session_id or "",
        "tool": tool or "",
        "args": {"source_url": source_url or ""},
        "result": "promptware_abort",
        "success": False,
        "promptware": score.to_dict(),
        "promptware_abort": True,
        "task": task or "",
        "adapter": adapter or "",
    }


def scan_tool_output(
    text: str,
    *,
    adapter: str | None = None,
    tool: str | None = None,
    task: str | None = None,
    session_id: str | None = None,
    source_url: str | None = None,
    detector: PromptwareDetector | None = None,
    hook_registry: HookRegistry | None = None,
    env: dict[str, str] | None = None,
    force: bool = False,
) -> PromptwareIngestResult:
    """Classify *text* and dispatch warn/abort side effects.

    The function is a thin coordination shim. The caller stays in control
    of what to do with the score - we never mutate the tool output and we
    never raise on a positive verdict; instead we post a lifecycle event
    and let plugins decide whether to abort the next-agent spawn.

    Args:
        text: Tool-output payload as a string.
        adapter: CLI adapter that produced the output. Used as a Prometheus
            label and embedded in the WARN log.
        tool: Tool name. Same treatment as ``adapter``.
        task: Task identifier for the WARN log and lifecycle context.
        session_id: Agent session identifier for the lifecycle context.
        source_url: Originating URL when the tool was an HTTP fetch.
        detector: Custom detector; defaults to a shared singleton.
        hook_registry: Lifecycle registry to dispatch on. When ``None`` the
            function still classifies but skips the lifecycle event.
        env: Override for environment lookup; defaults to ``os.environ``.
        force: When ``True``, classify regardless of the env flag. Tests
            and ``bernstein doctor promptware-scan`` rely on this.

    Returns:
        A :class:`PromptwareIngestResult` describing the classifier output
        and which side effects fired.
    """
    if not force and not is_enabled(env):
        # Return a benign zero-score placeholder so callers can still log
        # uniformly when the feature is off.
        zero = PromptwareScore(score=0.0, verdict=_benign_verdict())
        return PromptwareIngestResult(score=zero)

    det = detector if detector is not None else get_default_detector()
    score = det.classify(text)
    _record_histogram(score, adapter=adapter, tool=tool)

    emitted_warn = False
    emitted_abort_event = False

    if score.is_warn:
        emitted_warn = True
        log.warning(
            "promptware suspected in tool output: score=%.3f verdict=%s "
            "task=%s adapter=%s tool=%s source_url=%s reasons=%s",
            score.score,
            score.verdict.value,
            task or "?",
            adapter or "?",
            tool or "?",
            source_url or "-",
            list(score.reasons),
        )

    if score.is_abort and hook_registry is not None:
        try:
            _dispatch_abort_event(
                hook_registry,
                score=score,
                task=task,
                session_id=session_id,
                adapter=adapter,
                tool=tool,
                source_url=source_url,
            )
            emitted_abort_event = True
        except Exception:
            # Hook failures must never propagate into the orchestrator hot
            # path; they would defeat the safety property the detector is
            # supposed to provide. We log and move on.
            log.exception("promptware abort lifecycle event dispatch failed")

    return PromptwareIngestResult(
        score=score,
        emitted_warn=emitted_warn,
        emitted_abort_event=emitted_abort_event,
    )


@dataclass(frozen=True, slots=True)
class QuarantinedIngestResult:
    """Result of quarantining an untrusted payload on the ingest path.

    Attributes:
        extract: The schema-validated structural extraction. Only these
            fields are safe to place into worker prompt context; free text is
            withheld and represented by a content hash.
        scan: The promptware scan of the *raw* payload. The scan runs for
            detection and alerting, but its input never reaches context -- the
            raw bytes are quarantined behind the structural extractor.
    """

    extract: object
    scan: PromptwareIngestResult


def quarantine_untrusted_payload(
    payload: object,
    schema: object,
    *,
    adapter: str | None = None,
    tool: str | None = None,
    task: str | None = None,
    session_id: str | None = None,
    source_url: str | None = None,
    detector: PromptwareDetector | None = None,
    hook_registry: HookRegistry | None = None,
    env: dict[str, str] | None = None,
    force: bool = False,
) -> QuarantinedIngestResult:
    """Quarantine an untrusted payload before anything reaches worker context.

    The raw payload is scanned for promptware (detection only) and, in the
    same call, passed through the quarantined structural parser so only
    schema-validated fields survive. The instruction-bearing free text inside
    the payload is never returned for context injection; callers place
    ``result.extract.fields`` into context and anchor a lineage edge from that
    extraction back to the tainted source via ``result.extract.source_content_hash``.

    Args:
        payload: The untrusted structured payload (or a raw string body).
        schema: Field name -> ``FieldSpec`` mapping for the structural parser.
        adapter, tool, task, session_id, source_url, detector, hook_registry,
        env, force: Forwarded to :func:`scan_tool_output`.

    Returns:
        A :class:`QuarantinedIngestResult`.
    """
    # Local import keeps the ingest module importable in trimmed CLI subsets
    # that strip the structural parser.
    from bernstein.core.security.quarantined_parser import extract_structured

    extract = extract_structured(payload, schema)  # type: ignore[arg-type]

    # Scan the raw payload text for promptware (detection). This is the only
    # place the raw bytes are read; they are never handed onward to context.
    if isinstance(payload, str):
        raw_text = payload
    else:
        import json as _json

        raw_text = _json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)

    scan = scan_tool_output(
        raw_text,
        adapter=adapter,
        tool=tool,
        task=task,
        session_id=session_id,
        source_url=source_url,
        detector=detector,
        hook_registry=hook_registry,
        env=env,
        force=force,
    )
    return QuarantinedIngestResult(extract=extract, scan=scan)


def _dispatch_abort_event(
    hook_registry: HookRegistry,
    *,
    score: PromptwareScore,
    task: str | None,
    session_id: str | None,
    adapter: str | None,
    tool: str | None,
    source_url: str | None,
) -> None:
    """Send a lifecycle event so plugins can subscribe-and-abort."""
    # Local imports keep promptware_detector importable in environments
    # that strip lifecycle out (e.g. air-gapped CLI subsets).
    from bernstein.core.lifecycle.hooks import LifecycleContext, LifecycleEvent

    data = build_lifecycle_payload(
        score,
        task=task,
        session_id=session_id,
        adapter=adapter,
        tool=tool,
        source_url=source_url,
    )
    event = LifecycleEvent(PROMPTWARE_LIFECYCLE_EVENT)
    context = LifecycleContext(
        event=event,
        task=task,
        session_id=session_id,
        data=data,
    )
    hook_registry.run(event, context)


def _record_histogram(
    score: PromptwareScore,
    *,
    adapter: str | None,
    tool: str | None,
) -> None:
    """Observe the score on the Prometheus histogram, if available."""
    try:
        from bernstein.core.observability import promptware_metrics
    except Exception:
        # Observability module is optional in some deployments.
        return
    promptware_metrics.observe_score(
        score.score,
        adapter=adapter or "unknown",
        tool=tool or "unknown",
        bucket=score.size_bucket.value,
    )


def _benign_verdict() -> Any:
    from bernstein.core.security.promptware_detector import PromptwareVerdict

    return PromptwareVerdict.BENIGN
