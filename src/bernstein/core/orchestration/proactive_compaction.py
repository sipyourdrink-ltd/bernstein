"""Proactive threshold-triggered context compaction (issue #2246).

The reactive lane (``agent_lifecycle._try_compact_and_retry``) only fires
after a worker has already crashed on a context-window overflow, so a
long-running worker always pays one failure before recovery. This module
wires the existing :class:`~bernstein.core.tokens.compaction_pipeline.
CompactionPipeline` into the per-tick token-pressure signal instead: when
a worker's context utilization crosses the configured threshold (default
80 percent of the window), the task description - the mutable part of the
prompt - is compacted, mechanically validated, receipted, and patched
onto the task server before any overflow can occur.

Invariants:

* The summary never reaches the worker unless every zero-LLM validator
  passes (one fix-only retry allowed, then abort). An aborted proactive
  compaction changes nothing; the reactive path remains the untouched
  fallback.
* Every applied compaction is receipted: ``compaction.receipt`` in the
  HMAC audit chain, a replay-journal step carrying the pre/post hashes,
  a zero-cost spend-ledger row, and a compaction metric point.
* The proactive meta-message deliberately avoids the exact marker the
  reactive lane counts (``CONTEXT COMPACTION``); a proactive compaction
  must not consume the reactive retry budget.

Configuration comes from the ``compaction`` block
(``{proactive: bool, threshold: float, max_per_task: int}``) resolved
via :func:`resolve_compaction_settings`. The feature is off by default,
so existing runs keep their current behaviour.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Final

from bernstein.core.observability.metric_collector import get_collector
from bernstein.core.security.sanitize import sanitize_log
from bernstein.core.tokens.compaction_receipt import (
    build_receipt,
    record_compaction_artifacts,
)
from bernstein.core.tokens.compaction_validate import validate_with_fix

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

#: Default context-window fraction at which proactive compaction fires.
DEFAULT_THRESHOLD: Final[float] = 0.8

#: Default maximum proactive compaction attempts per task.
DEFAULT_MAX_PER_TASK: Final[int] = 1

#: ``reason`` recorded on pipeline runs and metric points for this lane.
PROACTIVE_REASON: Final[str] = "proactive_threshold"

#: Marker prefix for the meta-message injected after a proactive
#: compaction. MUST NOT contain the substring "CONTEXT COMPACTION":
#: ``agent_lifecycle._try_compact_and_retry`` counts that marker toward
#: the reactive retry cap, and a proactive compaction must not consume
#: the reactive retry budget (the reactive path is the unchanged
#: fallback).
PROACTIVE_META_MARKER: Final[str] = "CONTEXT PRESSURE:"


@dataclass(frozen=True, slots=True)
class CompactionSettings:
    """Resolved ``compaction`` config block.

    Attributes:
        proactive: Master switch for the proactive lane (off by default).
        threshold: Context-window fraction (0, 1] at which to compact.
        max_per_task: Maximum proactive compaction attempts per task.
            Attempts, not successes: an aborted validation counts, so a
            summary that keeps failing cannot burn pipeline calls every
            tick.
    """

    proactive: bool = False
    threshold: float = DEFAULT_THRESHOLD
    max_per_task: int = DEFAULT_MAX_PER_TASK


def _read_field(source: Any, name: str) -> Any:
    """Read *name* from a mapping or attribute source (None when absent)."""
    if isinstance(source, dict):
        return source.get(name)
    return getattr(source, name, None)


def resolve_compaction_settings(source: Any) -> CompactionSettings:
    """Resolve a ``compaction`` config block into validated settings.

    Accepts ``None`` (feature off, documented defaults), a mapping, or
    any object with ``proactive`` / ``threshold`` / ``max_per_task``
    attributes (dataclass, pydantic model). Invalid field values fall
    back to the defaults with a warning rather than crashing the tick.

    Args:
        source: The raw ``compaction`` block, or ``None``.

    Returns:
        Validated :class:`CompactionSettings`.
    """
    if source is None:
        return CompactionSettings()

    proactive = bool(_read_field(source, "proactive") or False)

    threshold = DEFAULT_THRESHOLD
    raw_threshold = _read_field(source, "threshold")
    if raw_threshold is not None:
        try:
            candidate = float(raw_threshold)
        except (TypeError, ValueError):
            candidate = -1.0
        if 0.0 < candidate <= 1.0:
            threshold = candidate
        else:
            logger.warning(
                "compaction.threshold %r out of range (0, 1]; using default %.2f",
                raw_threshold,
                DEFAULT_THRESHOLD,
            )

    max_per_task = DEFAULT_MAX_PER_TASK
    raw_max = _read_field(source, "max_per_task")
    if raw_max is not None:
        try:
            candidate_max = int(raw_max)
        except (TypeError, ValueError):
            candidate_max = 0
        if candidate_max >= 1:
            max_per_task = candidate_max
        else:
            logger.warning(
                "compaction.max_per_task %r invalid (must be >= 1); using default %d",
                raw_max,
                DEFAULT_MAX_PER_TASK,
            )

    return CompactionSettings(proactive=proactive, threshold=threshold, max_per_task=max_per_task)


def build_proactive_meta(*, utilization_fraction: float) -> str:
    """Return the meta-message injected after a proactive compaction."""
    return (
        f"{PROACTIVE_META_MARKER} the task description was compacted proactively at "
        f"{utilization_fraction:.0%} context-window pressure. Focus on the task goal - "
        f"do NOT try to reconstruct the removed context."
    )


# ---------------------------------------------------------------------------
# Proactive lane
# ---------------------------------------------------------------------------


def _attempt_counts(orch: Any) -> dict[str, int]:
    """Return (creating on first use) the per-task attempt counter."""
    counts = getattr(orch, "_proactive_compaction_counts", None)
    if counts is None:
        counts = {}
        orch._proactive_compaction_counts = counts
    return counts


def _fetch_task(orch: Any, task_id: str) -> dict[str, Any] | None:
    """Fetch the task row from the task server; None on any failure."""
    server_url = orch._config.server_url
    try:
        resp = orch._client.get(f"{server_url}/tasks/{task_id}")
        resp.raise_for_status()
        raw = resp.json()
    except Exception as exc:
        logger.warning("Proactive compaction: task fetch failed for %s: %s", sanitize_log(task_id), exc)
        return None
    return raw if isinstance(raw, dict) else None


def _patch_task(
    orch: Any,
    task_id: str,
    *,
    description: str,
    meta_messages: list[str],
) -> bool:
    """PATCH the compacted description onto the task server."""
    server_url = orch._config.server_url
    try:
        orch._client.patch(
            f"{server_url}/tasks/{task_id}",
            json={"description": description, "meta_messages": meta_messages},
        ).raise_for_status()
    except Exception as exc:
        logger.warning("Proactive compaction: task patch failed for %s: %s", sanitize_log(task_id), exc)
        return False
    return True


def maybe_compact_proactively(
    orch: Any,
    session: Any,
    *,
    utilization_fraction: float,
) -> bool:
    """Compact the worker's task description at the configured threshold.

    The full lane: threshold and budget guards, pipeline run (with the
    sensitive gate resolved against the run's audit chain), zero-LLM
    validation with one fix-only retry, receipt anchoring (chain +
    journal + ledger + metrics), and the task-server patch. Called once
    per orchestrator tick from the token monitor; every failure path
    returns ``False`` and leaves the worker's task untouched so the
    reactive overflow path stays an unchanged fallback.

    Args:
        orch: Orchestrator instance (duck-typed; see the token monitor).
        session: The live agent session under pressure.
        utilization_fraction: Context-window utilization in [0, 1].

    Returns:
        True when a validated compaction was applied and receipted.
    """
    settings = resolve_compaction_settings(getattr(orch._config, "compaction", None))
    if not settings.proactive or utilization_fraction < settings.threshold:
        return False

    task_ids = list(getattr(session, "task_ids", None) or ())
    if not task_ids:
        return False
    task_id = task_ids[0]

    counts = _attempt_counts(orch)
    if counts.get(task_id, 0) >= settings.max_per_task:
        return False

    task = _fetch_task(orch, task_id)
    if task is None:
        return False
    description = str(task.get("description") or "")
    if not description.strip():
        return False

    # From here on this is an attempt: bump the budget before running the
    # pipeline so a persistently failing summary cannot churn every tick.
    counts[task_id] = counts.get(task_id, 0) + 1

    from bernstein.core.tokens.compaction_pipeline import CompactionPipeline
    from bernstein.core.tokens.sensitive_gate import resolve_default_chain

    workdir = getattr(orch, "_workdir", None)
    chain = resolve_default_chain(workdir) if workdir is not None else resolve_default_chain()

    tokens_before = max(1, len(description) // 4)
    pipeline = CompactionPipeline(plugin_manager=getattr(orch, "_plugin_manager", None))
    try:
        result = pipeline.execute(
            session_id=session.id,
            context_text=description,
            tokens_before=tokens_before,
            reason=PROACTIVE_REASON,
            task_id=task_id,
            audit_chain=chain,
        )
    except Exception as exc:
        logger.warning("Proactive compaction pipeline failed for task %s: %s", sanitize_log(task_id), exc)
        return False

    if result.gate_action == "refused":
        # The sensitive gate refused: nothing was summarized and the
        # refusal is already in the audit chain. Leave the task untouched.
        logger.warning(
            "Proactive compaction refused by sensitive gate for task %s (rules: %s)",
            sanitize_log(task_id),
            ", ".join(result.gate_rule_ids),
        )
        return False

    if result.tokens_saved <= 0:
        logger.info("Proactive compaction for task %s saved no tokens; skipping", sanitize_log(task_id))
        return False

    outcome = validate_with_fix(
        description,
        result.compacted_text,
        fix_call=getattr(orch, "_compaction_fix_call", None),
    )
    if not outcome.passed:
        failed = ", ".join(v.name for v in outcome.verdicts if not v.passed)
        logger.warning(
            "Proactive compaction aborted for task %s: validators failed after %d fix pass(es): %s "
            "(falling back to the reactive path)",
            sanitize_log(task_id),
            outcome.retry_count,
            failed,
        )
        return False

    compacted_text = outcome.text
    tokens_after = max(1, len(compacted_text) // 4)

    receipt = build_receipt(
        task_id=task_id,
        worker_id=session.id,
        trigger="proactive",
        pre_text=description,
        post_text=compacted_text,
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        verdicts=outcome.verdicts,
        retry_count=outcome.retry_count,
        gate_action=result.gate_action,
        gate_rule_ids=result.gate_rule_ids,
        correlation_id=result.correlation_id,
    )
    record_compaction_artifacts(
        receipt=receipt,
        chain=chain,
        workdir=workdir,
        spend_ledger=getattr(orch, "_spend_ledger", None),
    )
    try:
        get_collector().record_compaction(
            session.id,
            tokens_before,
            tokens_after,
            reason=PROACTIVE_REASON,
            trigger="proactive",
            correlation_id=receipt.correlation_id,
        )
    except Exception as exc:  # pragma: no cover - metrics are best-effort
        logger.debug("Proactive compaction metric write failed for %s: %s", sanitize_log(task_id), exc)

    raw_meta = task.get("meta_messages") or []
    meta_messages = [str(m) for m in raw_meta]
    meta_messages.append(build_proactive_meta(utilization_fraction=utilization_fraction))
    if not _patch_task(orch, task_id, description=compacted_text, meta_messages=meta_messages):
        return False

    # "token" here counts LLM context tokens, not credentials.
    # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
    logger.info(
        "Proactively compacted task %s at %.0f%% pressure: %d -> %d tokens (correlation=%s, receipt chained)",
        sanitize_log(task_id),
        utilization_fraction * 100.0,
        tokens_before,
        tokens_after,
        receipt.correlation_id,
    )
    return True


__all__ = [
    "DEFAULT_MAX_PER_TASK",
    "DEFAULT_THRESHOLD",
    "PROACTIVE_META_MARKER",
    "PROACTIVE_REASON",
    "CompactionSettings",
    "build_proactive_meta",
    "maybe_compact_proactively",
    "resolve_compaction_settings",
]
