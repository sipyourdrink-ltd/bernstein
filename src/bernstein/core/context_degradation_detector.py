"""Context degradation detector: monitor agent quality over time, restart when degraded.

When an agent's cross-model review verdicts decline (N consecutive request_changes),
checkpoint progress, send a graceful shutdown, and arrange for the replacement agent
to receive a summarised context encoding what was already tried.  This turns context
window exhaustion — Devin's main weakness — into a Bernstein strength.

Lifecycle
---------
1. After each cross-model verification run ``record_verdict()`` is called.
2. If ``consecutive_reject_threshold`` consecutive rejections accumulate, the
   session is added to ``degraded_sessions()``.
3. The orchestrator calls ``checkpoint()`` to snapshot progress, then sends a
   SHUTDOWN signal.
4. The recovery context is stored keyed by task ID so the next spawn includes
   a lessons-learned preamble.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.cross_model_verifier import CrossModelVerdict
    from bernstein.core.models import AgentSession

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextDegradationConfig:
    """Settings for context degradation detection.

    Attributes:
        enabled: Master on/off switch.
        consecutive_reject_threshold: Fire after this many consecutive
            ``request_changes`` verdicts from the cross-model verifier.
            Default 2 — two failures in a row signals quality drop.
        min_tasks_before_detection: Don't fire until at least this many
            cross-model verdicts have been recorded for the session.
            Prevents a single noisy review from triggering a restart.
        max_tokens_before_restart: Restart the session when its cumulative
            token count reaches this value, regardless of verdict history.
            Set to 0 (default) to disable the token ceiling.
        checkpoint_dir: Directory (relative to workdir) where checkpoint
            JSON files are written.  Created automatically if absent.
    """

    enabled: bool = False
    consecutive_reject_threshold: int = 2
    min_tasks_before_detection: int = 1
    max_tokens_before_restart: int = 0
    checkpoint_dir: str = ".sdd/runtime/context_checkpoints"


# ---------------------------------------------------------------------------
# Internal verdict record
# ---------------------------------------------------------------------------


@dataclass
class _VerdictRecord:
    """A single cross-model review result for a session."""

    task_id: str
    verdict: Literal["approve", "request_changes"]
    timestamp: float
    reviewer_model: str


# ---------------------------------------------------------------------------
# Checkpoint
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextDegradationCheckpoint:
    """Snapshot of an agent session captured just before termination.

    Attributes:
        session_id: The session being terminated.
        task_ids: Task batch assigned to this session.
        verdict_count: Total cross-model verdicts recorded for this session.
        consecutive_rejects: Trailing reject count that triggered the restart.
        timestamp: Unix timestamp when this checkpoint was created.
        tokens_used: Cumulative tokens consumed by the agent.
        recovery_context: Markdown block to prepend to the replacement agent's
            prompt so it knows what was already attempted.
    """

    session_id: str
    task_ids: list[str]
    verdict_count: int
    consecutive_rejects: int
    timestamp: float
    tokens_used: int
    recovery_context: str


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------


class ContextDegradationDetector:
    """Track cross-model review verdicts per session; trigger respawn on quality drop.

    Intended usage::

        detector = ContextDegradationDetector(config, workdir)

        # After every cross-model verification run:
        detector.record_verdict(session_id, task_id, cmv_verdict)

        # In the orchestrator tick (after processing completions):
        for sid in detector.degraded_sessions():
            checkpoint = detector.checkpoint(agent_sessions[sid])
            store_recovery_context(checkpoint)
            send_shutdown(sid)
            detector.clear(sid)

    Args:
        config: Tuning knobs for the detector.
        workdir: Project root path; used to resolve ``checkpoint_dir``.
    """

    def __init__(self, config: ContextDegradationConfig, workdir: Path) -> None:
        self._config = config
        self._workdir = workdir
        # session_id → chronological list of verdict records
        self._history: dict[str, list[_VerdictRecord]] = {}
        # Sessions that have crossed the degradation threshold
        self._degraded: set[str] = set()

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record_verdict(
        self,
        session_id: str,
        task_id: str,
        verdict: CrossModelVerdict,
    ) -> None:
        """Record a cross-model review result and check for degradation.

        Idempotent — recording the same verdict twice for the same task won't
        cause issues, but callers should avoid it.

        Args:
            session_id: Agent session that produced the reviewed diff.
            task_id: Task the diff corresponds to.
            verdict: ``CrossModelVerdict`` returned by the verifier.
        """
        if not self._config.enabled:
            return
        record = _VerdictRecord(
            task_id=task_id,
            verdict=verdict.verdict,
            timestamp=time.time(),
            reviewer_model=verdict.reviewer_model,
        )
        history = self._history.setdefault(session_id, [])
        history.append(record)

        if session_id not in self._degraded and self._should_flag(session_id):
            self._degraded.add(session_id)
            logger.warning(
                "context_degradation: session %s flagged — %d consecutive rejection(s) (%d total verdicts); tasks=%s",
                session_id,
                self._consecutive_rejects(session_id),
                len(history),
                [r.task_id for r in history],
            )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def should_restart(self, session_id: str, tokens_used: int = 0) -> bool:
        """Return True if this session should be terminated and respawned.

        A session should restart when:
        - It has been flagged for degradation (consecutive reject threshold),  or
        - Its cumulative token count has crossed the configured ceiling.

        Args:
            session_id: Session to query.
            tokens_used: Current token count for the session (for token ceiling).

        Returns:
            True when the session is degraded or has exceeded the token ceiling.
        """
        if not self._config.enabled:
            return False
        if session_id in self._degraded:
            return True
        if (
            self._config.max_tokens_before_restart > 0
            and tokens_used >= self._config.max_tokens_before_restart
            and session_id in self._history
        ):
            logger.info(
                "context_degradation: session %s hit token ceiling (%d >= %d)",
                session_id,
                tokens_used,
                self._config.max_tokens_before_restart,
            )
            return True
        return False

    def degraded_sessions(self) -> set[str]:
        """Return a snapshot of sessions currently flagged for restart."""
        return set(self._degraded)

    # ------------------------------------------------------------------
    # Checkpoint & context generation
    # ------------------------------------------------------------------

    def build_recovery_context(self, session: AgentSession) -> str:
        """Generate a markdown summary for the replacement agent's prompt.

        The summary encodes what was attempted in the previous session so the
        fresh agent can avoid repeating the same mistakes.

        Args:
            session: The session being terminated.

        Returns:
            Markdown block to prepend to the replacement agent's task prompt.
        """
        history = self._history.get(session.id, [])
        n_rejects = self._consecutive_rejects(session.id)
        lines: list[str] = [
            "## Context transfer (previous agent summary)\n",
            f"The previous agent for this task batch was terminated after "
            f"{len(history)} code review(s) with {n_rejects} consecutive "
            f"rejection(s) from the cross-model reviewer.\n",
        ]
        if history:
            lines.append("\n### Review history\n")
            for rec in history:
                icon = "✓" if rec.verdict == "approve" else "✗"
                lines.append(f"- {icon} task `{rec.task_id}`: **{rec.verdict}** (reviewer: {rec.reviewer_model})")
        lines.append(
            "\n### Guidance for this session\n"
            "- Run the test suite and linter *before* marking tasks complete.\n"
            "- Address all correctness and security issues raised by the reviewer.\n"
            "- If unsure about expected behaviour, write a failing test first.\n"
            "- Keep diffs focused — one task, one concern.\n"
        )
        return "\n".join(lines)

    def checkpoint(self, session: AgentSession) -> ContextDegradationCheckpoint:
        """Capture a progress snapshot and persist it to disk.

        Args:
            session: The session being terminated.

        Returns:
            Populated :class:`ContextDegradationCheckpoint`.
        """
        history = self._history.get(session.id, [])
        recovery_context = self.build_recovery_context(session)
        cp = ContextDegradationCheckpoint(
            session_id=session.id,
            task_ids=list(session.task_ids),
            verdict_count=len(history),
            consecutive_rejects=self._consecutive_rejects(session.id),
            timestamp=time.time(),
            tokens_used=session.tokens_used,
            recovery_context=recovery_context,
        )
        self._persist_checkpoint(cp)
        return cp

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------

    def clear(self, session_id: str) -> None:
        """Remove tracking data for a session after it has been handled.

        Args:
            session_id: Session to evict from internal state.
        """
        self._history.pop(session_id, None)
        self._degraded.discard(session_id)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _should_flag(self, session_id: str) -> bool:
        history = self._history.get(session_id, [])
        if len(history) < self._config.min_tasks_before_detection:
            return False
        return self._consecutive_rejects(session_id) >= self._config.consecutive_reject_threshold

    def _consecutive_rejects(self, session_id: str) -> int:
        """Return the number of trailing ``request_changes`` verdicts."""
        history = self._history.get(session_id, [])
        count = 0
        for record in reversed(history):
            if record.verdict == "request_changes":
                count += 1
            else:
                break
        return count

    def _persist_checkpoint(self, cp: ContextDegradationCheckpoint) -> None:
        cp_dir = self._workdir / self._config.checkpoint_dir
        try:
            cp_dir.mkdir(parents=True, exist_ok=True)
            (cp_dir / f"{cp.session_id}.json").write_text(
                json.dumps(
                    {
                        "session_id": cp.session_id,
                        "task_ids": cp.task_ids,
                        "verdict_count": cp.verdict_count,
                        "consecutive_rejects": cp.consecutive_rejects,
                        "timestamp": cp.timestamp,
                        "tokens_used": cp.tokens_used,
                        "recovery_context": cp.recovery_context,
                    },
                    indent=2,
                )
            )
        except OSError as exc:
            logger.warning("context_degradation: failed to persist checkpoint: %s", exc)
