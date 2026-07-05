"""Periodic liveness watchdog for live adapter sessions.

This is the smallest-viable slice of the self-healing watchdog (#1224):
one tick-driven loop that scans each running adapter session for a
single liveness signal and applies a single recovery action when it
fires.

Slice scope
-----------
The slice handles **pre-approved confirmation prompts**: when the
operator opted into yolo-mode for a class of safety prompts (e.g.
``Continue? [y/N]``) and the agent stalls on one of those prompts, the
watchdog answers ``y`` once and emits an audit event. Prompts that look
like the model asking the operator a clarifying question are explicitly
*not* auto-answered - those escalate.

Out of slice (deferred)
-----------------------
- Context-pressure ``/compact`` recovery: now handled by the proactive
  compaction lane (:mod:`bernstein.core.orchestration.proactive_compaction`),
  which runs in the token-monitor tick and receipts every compaction.
- ``redacted_thinking`` corruption restart-with-replay.
- Stuck-class ML classifier.
- Dashboard surfacing / per-session recovery counters in
  ``bernstein run report``.

The module exposes :func:`tick` so the orchestrator can call it once
per tick under the existing slow-tick gate, and :func:`is_enabled` so
the call site can short-circuit before importing tick state.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

logger = logging.getLogger(__name__)


__all__ = [
    "DEFAULT_SAFETY_PROMPT_PATTERNS",
    "FEATURE_FLAG_ENV",
    "PromptKind",
    "RecoveryAction",
    "SessionSnapshot",
    "WatchdogResult",
    "classify_prompt",
    "is_enabled",
    "tick",
]


FEATURE_FLAG_ENV: Final[str] = "BERNSTEIN_WATCHDOG_ENABLED"
"""Env var that gates the watchdog tick.

Set to ``"1"``, ``"true"``, or ``"yes"`` (case-insensitive) to enable.
Off by default so existing runs keep their current behaviour.
"""

# Patterns that look like the agent waiting on an operator confirmation
# the operator has *already* said yes to in yolo mode. These are
# deliberately conservative - the right side of the bracketed default
# (uppercase ``N``) marks the keystroke we'd send.
DEFAULT_SAFETY_PROMPT_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"continue\?\s*\[y/n\]\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"proceed\?\s*\[y/n\]\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"are you sure\?\s*\[y/n\]\s*$", re.IGNORECASE | re.MULTILINE),
)

# Patterns that mean the *model* is asking the operator for a decision.
# Hits here block auto-answer regardless of the safety match because the
# whole point of the slice is "never silently answer a model question".
_MODEL_QUESTION_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"which (?:of|file|path|option)", re.IGNORECASE),
    re.compile(r"did you mean", re.IGNORECASE),
    re.compile(r"please clarify", re.IGNORECASE),
    re.compile(r"could you (?:tell|specify|confirm)", re.IGNORECASE),
)


PromptKind = str
"""Classification of an awaited prompt.

One of:

- ``"safety"`` - pre-approved confirmation; safe to auto-answer.
- ``"model_question"`` - model asking the operator; never auto-answer.
- ``"none"`` - no prompt currently awaited.
"""


@dataclass(frozen=True)
class SessionSnapshot:
    """Minimum view of one running adapter session.

    The watchdog is intentionally decoupled from the full
    ``AgentSession`` dataclass so it can run against test stubs,
    bridged sessions, and future remote adapters without dragging in
    the orchestrator's session graph.

    Attributes:
        session_id: Stable identifier for the live adapter session.
        recent_output: The last few hundred bytes of streamed stdout
            from the agent. The watchdog only inspects the tail -
            specifically whatever shows up after the last newline - so
            callers may pass the full ring-buffer slice without
            trimming.
        is_paused: ``True`` when the session is blocked on an awaited
            prompt. Sessions with ``is_paused=False`` are skipped to
            avoid racing live output.
        approved_prompt_classes: Operator-approved auto-answer classes,
            e.g. ``frozenset({"safety"})`` for yolo mode. Empty set
            disables auto-answer for the session.
    """

    session_id: str
    recent_output: str
    is_paused: bool = False
    approved_prompt_classes: frozenset[str] = field(default_factory=frozenset)


class _RespondFn(Protocol):
    """Callable that delivers a keystroke to a paused adapter session.

    Implementations are provided by the orchestrator (real adapter
    write) or by tests (mock). The signature is intentionally minimal
    so the same callable works for stdin pipes, pty writes, and
    bridged remote sessions.
    """

    def __call__(self, session_id: str, keystroke: str) -> bool:
        """Send ``keystroke`` to ``session_id``.

        Args:
            session_id: Session to address.
            keystroke: One or more characters to write. The watchdog
                always writes a single ASCII char followed by ``"\\n"``
                so the adapter sees one complete line.

        Returns:
            ``True`` when the write succeeded; ``False`` otherwise.
            Failures get logged at WARNING and emitted as a failed
            recovery audit event.
        """
        ...


@dataclass(frozen=True)
class RecoveryAction:
    """Description of what the watchdog did (or planned to do).

    Attributes:
        recovery_id: UUID4 string. Stable across the
            ``detected``/``succeeded``/``failed`` audit lifecycle so
            postmortems can join the rows.
        rule: The rule that fired, e.g. ``"prompt_waiting.safety"``.
        action: Human-readable label, e.g. ``"auto_answer:y"``.
        session_id: Session the action targeted.
    """

    recovery_id: str
    rule: str
    action: str
    session_id: str


@dataclass(frozen=True)
class WatchdogResult:
    """Outcome of one :func:`tick` invocation.

    Attributes:
        recoveries: All recoveries the watchdog attempted this tick.
            One per session that hit a rule.
        skipped_model_questions: Sessions where a model-question
            prompt was detected - auto-answer was suppressed and the
            caller should escalate via the existing notifier path.
    """

    recoveries: tuple[RecoveryAction, ...]
    skipped_model_questions: tuple[str, ...]


def is_enabled(env: dict[str, str] | None = None) -> bool:
    """Return whether the watchdog tick should run.

    Args:
        env: Optional environment mapping (defaults to ``os.environ``).
            Pass an explicit dict in tests so the production env stays
            untouched.

    Returns:
        ``True`` iff :data:`FEATURE_FLAG_ENV` is set to a truthy value.
    """
    source = env if env is not None else os.environ
    raw = source.get(FEATURE_FLAG_ENV, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _last_line(text: str) -> str:
    """Return the trailing line of ``text`` without trailing whitespace."""
    if not text:
        return ""
    # Take everything after the final newline, then strip right-side
    # whitespace so the regex anchors line up regardless of how the
    # adapter terminated the prompt.
    tail = text.rsplit("\n", 1)[-1]
    return tail.rstrip()


def classify_prompt(
    recent_output: str,
    *,
    safety_patterns: Iterable[re.Pattern[str]] = DEFAULT_SAFETY_PROMPT_PATTERNS,
) -> PromptKind:
    """Classify what kind of prompt (if any) the agent is waiting on.

    The classifier inspects the last line of ``recent_output``. It
    returns ``"model_question"`` whenever the line looks like the model
    asking the operator a question - that branch wins even if a safety
    pattern would also match, because mis-classifying a model question
    as a safety prompt is the worst failure mode for this primitive.

    Args:
        recent_output: Tail of the streamed agent output.
        safety_patterns: Compiled regexes for pre-approved safety
            prompts. Tests override this to assert tight coverage.

    Returns:
        :data:`PromptKind` literal.
    """
    tail = _last_line(recent_output)
    if not tail:
        return "none"
    for pat in _MODEL_QUESTION_PATTERNS:
        if pat.search(tail):
            return "model_question"
    for pat in safety_patterns:
        if pat.search(tail):
            return "safety"
    return "none"


def _emit_audit_event(audit_path: Path, event: str, payload: dict[str, object]) -> None:
    """Append one JSONL audit event.

    Failures are swallowed - the watchdog is a best-effort supervisor,
    so a write error must not crash the tick.
    """
    try:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        record: dict[str, object] = {
            "timestamp": time.time(),
            "event": event,
        } | payload
        with audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError as exc:
        logger.warning("watchdog audit write failed (%s): %s", audit_path, exc)


def _try_recover_safety_prompt(
    session: SessionSnapshot,
    respond: _RespondFn,
    audit_path: Path,
) -> RecoveryAction | None:
    """Auto-answer a pre-approved safety prompt for ``session``.

    Returns the :class:`RecoveryAction` when the watchdog acted (even
    on failed delivery, so the audit chain captures the attempt) or
    ``None`` when the session was not eligible.
    """
    if "safety" not in session.approved_prompt_classes:
        return None
    recovery = RecoveryAction(
        recovery_id=str(uuid.uuid4()),
        rule="prompt_waiting.safety",
        action="auto_answer:y",
        session_id=session.session_id,
    )
    base_payload: dict[str, object] = {
        "recovery_id": recovery.recovery_id,
        "rule": recovery.rule,
        "action": recovery.action,
        "session_id": recovery.session_id,
    }
    _emit_audit_event(audit_path, "watchdog.recover.detected", base_payload.copy())
    delivered = False
    try:
        delivered = bool(respond(session.session_id, "y\n"))
    except Exception as exc:  # respond is operator-supplied; isolate failures
        logger.warning(
            "watchdog auto-answer raised for session %s: %s",
            session.session_id,
            exc,
        )
        delivered = False
    outcome_event = "watchdog.recover.succeeded" if delivered else "watchdog.recover.failed"
    _emit_audit_event(audit_path, outcome_event, base_payload.copy())
    logger.info(
        "watchdog recovery %s: session=%s rule=%s action=%s recovery_id=%s",
        "SUCCEEDED" if delivered else "FAILED",
        session.session_id,
        recovery.rule,
        recovery.action,
        recovery.recovery_id,
    )
    return recovery


def tick(
    sessions: Iterable[SessionSnapshot],
    respond: _RespondFn,
    audit_path: Path,
    *,
    env: dict[str, str] | None = None,
) -> WatchdogResult:
    """Run one watchdog pass over ``sessions``.

    The pass is a pure function of its inputs apart from the audit
    JSONL append and the operator-supplied ``respond`` callback, which
    keeps the loop trivially testable.

    Args:
        sessions: Live adapter sessions to inspect.
        respond: Callable that delivers a keystroke to a session.
        audit_path: JSONL file to append recovery events to. Created
            (parents included) on first write.
        env: Optional env mapping for the feature gate; defaults to
            ``os.environ``.

    Returns:
        :class:`WatchdogResult` summarising what happened. The result
        is empty when the feature flag is off.
    """
    if not is_enabled(env):
        return WatchdogResult(recoveries=(), skipped_model_questions=())

    recoveries: list[RecoveryAction] = []
    skipped: list[str] = []
    sessions = list(sessions)
    paused_count = sum(1 for s in sessions if s.is_paused)
    logger.debug(
        "watchdog tick: probing %d session(s), %d paused",
        len(sessions),
        paused_count,
    )

    for session in sessions:
        if not session.is_paused:
            continue
        kind = classify_prompt(session.recent_output)
        # Every paused-session probe conclusion, logged with the classifier
        # input (last line) and verdict -- this is the decision that
        # determines auto-answer vs. escalate vs. no-op.
        logger.info(
            "watchdog probe: session=%s is_paused=True classified=%s last_line=%r approved_classes=%s",
            session.session_id,
            kind,
            _last_line(session.recent_output),
            sorted(session.approved_prompt_classes),
        )
        if kind == "model_question":
            skipped.append(session.session_id)
            logger.warning(
                "watchdog: session=%s prompt classified as model_question -- "
                "auto-answer suppressed, escalating to operator",
                session.session_id,
            )
            _emit_audit_event(
                audit_path,
                "watchdog.recover.skipped",
                {
                    "rule": "prompt_waiting.model_question",
                    "action": "escalate",
                    "session_id": session.session_id,
                },
            )
            continue
        if kind != "safety":
            continue
        recovery = _try_recover_safety_prompt(session, respond, audit_path)
        if recovery is not None:
            recoveries.append(recovery)

    logger.info(
        "watchdog tick complete: %d recoveries attempted, %d model-question escalations",
        len(recoveries),
        len(skipped),
    )
    return WatchdogResult(
        recoveries=tuple(recoveries),
        skipped_model_questions=tuple(skipped),
    )
