"""Security incident response automation with containment procedures.

When a security event is detected (sandbox escape attempt, credential
exfiltration, anomalous behaviour), this module automatically executes a
containment procedure:

1. **Kill the agent** — write a structured kill-signal file so the
   orchestrator terminates the session on its next tick.
2. **Quarantine the worktree** — preserve the git branch and write quarantine
   metadata for forensic review.
3. **Snapshot state for forensics** — capture task, session, and environment
   state at time of detection so investigators have a complete picture.
4. **Block the task from retry** — write a block marker file so the
   orchestrator will not re-schedule this task.
5. **Notify the security team** — append to the security incident audit log
   and write a bulletin-board entry that other agents can read.

This module is deliberately distinct from :mod:`bernstein.core.security_correlation`
(SEC-022), which correlates events *across runs*.  This module provides
*automated response*, executed immediately when an event fires.

Usage::

    from pathlib import Path
    from bernstein.core.security_incident_response import SecurityIncidentResponder

    responder = SecurityIncidentResponder(workdir=Path("."))
    result = responder.contain(
        event_type="sandbox_escape_attempt",
        session_id="agent-session-42",
        task_id="task-abc123",
        detail="Agent attempted to read /etc/shadow",
        severity="critical",
    )
    print(result.incident_id)   # e.g. "SEC-INC-1712940000-001"
    print(result.steps_taken)   # list of containment step names
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path  # noqa: TC003 — used at runtime in dataclass fields and method bodies
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class SecurityEventType(StrEnum):
    """Recognised security event categories."""

    SANDBOX_ESCAPE_ATTEMPT = "sandbox_escape_attempt"
    CREDENTIAL_EXFILTRATION = "credential_exfiltration"
    ANOMALOUS_BEHAVIOR = "anomalous_behavior"
    DANGEROUS_COMMAND = "dangerous_command"
    SUSPICIOUS_FILE_ACCESS = "suspicious_file_access"
    SUSPICIOUS_NETWORK_ENDPOINT = "suspicious_network_endpoint"
    PERMISSION_ESCALATION = "permission_escalation"
    MERGE_PIPELINE_ATTACK = "merge_pipeline_attack"
    MCP_TOOL_ABUSE = "mcp_tool_abuse"
    UNKNOWN = "unknown"


class ContainmentStep(StrEnum):
    """Steps in the containment procedure, in execution order."""

    KILL_SIGNAL = "kill_signal"
    QUARANTINE_WORKTREE = "quarantine_worktree"
    FORENSIC_SNAPSHOT = "forensic_snapshot"
    BLOCK_RETRY = "block_retry"
    NOTIFY = "notify"


@dataclass(frozen=True)
class ContainmentResult:
    """Outcome of a containment procedure execution.

    Attributes:
        incident_id: Unique ID for this security incident.
        session_id: The agent session that was contained.
        task_id: The task the agent was working on.
        event_type: Category of the security event that triggered containment.
        severity: Severity level of the incident.
        steps_taken: Ordered list of containment steps that completed successfully.
        steps_failed: Steps that failed (with error messages).
        snapshot_path: Path to the forensic snapshot file, if written.
        kill_signal_path: Path to the kill signal file.
        block_path: Path to the task retry-block marker.
        timestamp: UNIX timestamp when containment was initiated.
    """

    incident_id: str
    session_id: str
    task_id: str
    event_type: str
    severity: str
    steps_taken: list[str]
    steps_failed: list[str]
    snapshot_path: str | None
    kill_signal_path: str | None
    block_path: str | None
    timestamp: float

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dictionary for JSON persistence."""
        return {
            "incident_id": self.incident_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "event_type": self.event_type,
            "severity": self.severity,
            "steps_taken": self.steps_taken,
            "steps_failed": self.steps_failed,
            "snapshot_path": self.snapshot_path,
            "kill_signal_path": self.kill_signal_path,
            "block_path": self.block_path,
            "timestamp": self.timestamp,
            "datetime": datetime.fromtimestamp(self.timestamp, tz=UTC).isoformat(),
        }


@dataclass
class SecurityIncidentResponder:
    """Automated security incident response with containment procedures.

    Instantiate once per orchestrator run and call :meth:`contain` whenever
    a security event is detected.  Each call executes all five containment
    steps and returns a :class:`ContainmentResult` describing what happened.

    Args:
        workdir: Project root directory (all paths are resolved relative to this).
        notify_via_bulletin: Whether to post a bulletin-board entry.  Set to
            False in tests to avoid bulletin-board side-effects.
    """

    workdir: Path
    notify_via_bulletin: bool = True
    _incident_counter: int = field(default=0, init=False, repr=False)

    def contain(
        self,
        event_type: str,
        session_id: str,
        task_id: str,
        detail: str,
        *,
        severity: str = "critical",
        extra: dict[str, Any] | None = None,
        branch: str | None = None,
        task_context: dict[str, Any] | None = None,
    ) -> ContainmentResult:
        """Execute the full containment procedure for a security event.

        Steps are executed in order.  Failures in one step do not prevent
        subsequent steps from running — we want maximum containment even
        if individual steps encounter errors (e.g., disk full on snapshot).

        Args:
            event_type: Category of the security event (use :class:`SecurityEventType`
                values for well-known categories).
            session_id: Agent session ID to kill and quarantine.
            task_id: Task ID the agent was working on.
            detail: Human-readable description of what was detected.
            severity: Incident severity (``"critical"``, ``"high"``, etc.).
            extra: Additional context for the forensic snapshot (free-form dict).
            branch: Agent's git branch name (used for quarantine metadata).
                Defaults to ``agent/{session_id}`` if not provided.
            task_context: Snapshot of the task's current state fields for
                forensic purposes (title, role, priority, etc.).

        Returns:
            :class:`ContainmentResult` describing which steps succeeded/failed.
        """
        self._incident_counter += 1
        ts = time.time()
        incident_id = f"SEC-INC-{int(ts)}-{self._incident_counter:03d}"
        effective_branch = branch or f"agent/{session_id}"

        steps_taken: list[str] = []
        steps_failed: list[str] = []
        kill_signal_path: str | None = None
        snapshot_path: str | None = None
        block_path: str | None = None

        logger.critical(
            "SECURITY INCIDENT %s [%s]: %s — initiating containment for session %s task %s",
            incident_id,
            severity.upper(),
            event_type,
            session_id,
            task_id,
        )

        # 1. Kill the agent
        try:
            kill_signal_path = self._write_kill_signal(incident_id, session_id, event_type, detail)
            steps_taken.append(ContainmentStep.KILL_SIGNAL)
        except Exception:
            logger.exception("Containment step KILL_SIGNAL failed for %s", incident_id)
            steps_failed.append(ContainmentStep.KILL_SIGNAL)

        # 2. Quarantine the worktree
        try:
            self._quarantine_worktree(
                incident_id,
                session_id,
                event_type,
                detail,
                branch=effective_branch,
            )
            steps_taken.append(ContainmentStep.QUARANTINE_WORKTREE)
        except Exception:
            logger.exception("Containment step QUARANTINE_WORKTREE failed for %s", incident_id)
            steps_failed.append(ContainmentStep.QUARANTINE_WORKTREE)

        # 3. Forensic snapshot
        try:
            snapshot_path = self._write_forensic_snapshot(
                incident_id=incident_id,
                session_id=session_id,
                task_id=task_id,
                event_type=event_type,
                severity=severity,
                detail=detail,
                branch=effective_branch,
                extra=extra or {},
                task_context=task_context or {},
                ts=ts,
            )
            steps_taken.append(ContainmentStep.FORENSIC_SNAPSHOT)
        except Exception:
            logger.exception("Containment step FORENSIC_SNAPSHOT failed for %s", incident_id)
            steps_failed.append(ContainmentStep.FORENSIC_SNAPSHOT)

        # 4. Block the task from retry
        try:
            block_path = self._block_task_retry(incident_id, task_id, session_id, event_type, detail)
            steps_taken.append(ContainmentStep.BLOCK_RETRY)
        except Exception:
            logger.exception("Containment step BLOCK_RETRY failed for %s", incident_id)
            steps_failed.append(ContainmentStep.BLOCK_RETRY)

        # 5. Notify the security team
        try:
            self._notify(
                incident_id=incident_id,
                session_id=session_id,
                task_id=task_id,
                event_type=event_type,
                severity=severity,
                detail=detail,
                steps_taken=steps_taken,
                ts=ts,
            )
            steps_taken.append(ContainmentStep.NOTIFY)
        except Exception:
            logger.exception("Containment step NOTIFY failed for %s", incident_id)
            steps_failed.append(ContainmentStep.NOTIFY)

        result = ContainmentResult(
            incident_id=incident_id,
            session_id=session_id,
            task_id=task_id,
            event_type=event_type,
            severity=severity,
            steps_taken=steps_taken,
            steps_failed=steps_failed,
            snapshot_path=snapshot_path,
            kill_signal_path=kill_signal_path,
            block_path=block_path,
            timestamp=ts,
        )

        if steps_failed:
            logger.error(
                "Incident %s containment incomplete — %d steps failed: %s",
                incident_id,
                len(steps_failed),
                ", ".join(steps_failed),
            )
        else:
            logger.info("Incident %s fully contained (%d steps)", incident_id, len(steps_taken))

        return result

    # ------------------------------------------------------------------
    # Step 1: Kill the agent
    # ------------------------------------------------------------------

    def _write_kill_signal(
        self,
        incident_id: str,
        session_id: str,
        event_type: str,
        detail: str,
    ) -> str:
        """Write a kill signal file for the orchestrator to act on.

        Returns the path of the written kill file.
        """
        runtime_dir = self.workdir / ".sdd" / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)

        kill_payload: dict[str, Any] = {
            "ts": time.time(),
            "reason": "security_incident",
            "event_type": event_type,
            "incident_id": incident_id,
            "detail": detail,
            "requester": "security_incident_responder",
        }
        kill_file = runtime_dir / f"{session_id}.kill"
        kill_file.write_text(json.dumps(kill_payload), encoding="utf-8")
        logger.warning(
            "Kill signal written for agent %s (incident=%s, event=%s)",
            session_id,
            incident_id,
            event_type,
        )
        return str(kill_file)

    # ------------------------------------------------------------------
    # Step 2: Quarantine the worktree
    # ------------------------------------------------------------------

    def _quarantine_worktree(
        self,
        incident_id: str,
        session_id: str,
        event_type: str,
        detail: str,
        *,
        branch: str,
    ) -> None:
        """Write quarantine metadata and attempt to preserve the agent branch.

        The quarantine metadata is written to ``.sdd/quarantine/{session_id}.json``
        so human reviewers know which branch to examine.  We also attempt a
        lightweight ``git tag`` to anchor the branch state for forensics.
        """
        quarantine_dir = self.workdir / ".sdd" / "quarantine"
        quarantine_dir.mkdir(parents=True, exist_ok=True)

        metadata: dict[str, Any] = {
            "session_id": session_id,
            "quarantined_at": datetime.now(UTC).isoformat(),
            "reason": "security_incident",
            "event_type": event_type,
            "incident_id": incident_id,
            "detail": detail,
            "branch": branch,
            "status": "under_investigation",
        }

        # Attempt to capture the current git HEAD of the agent's branch so
        # investigators can checkout the exact commit that was active when the
        # incident fired.  This is best-effort — failures are non-fatal.
        git_info = self._capture_branch_head(branch)
        if git_info:
            metadata.update(git_info)

        out_path = quarantine_dir / f"{session_id}.json"
        out_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        logger.info(
            "Quarantine metadata written for agent %s (incident=%s, branch=%s)",
            session_id,
            incident_id,
            branch,
        )

    def _capture_branch_head(self, branch: str) -> dict[str, str]:
        """Return the HEAD commit info for *branch*, or empty dict on failure."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", branch],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=str(self.workdir),
            )
            if result.returncode == 0:
                commit_hash = result.stdout.strip()
                return {
                    "branch_head_commit": commit_hash,
                    "branch_head_captured_at": datetime.now(UTC).isoformat(),
                }
        except (subprocess.TimeoutExpired, OSError):
            pass
        return {}

    # ------------------------------------------------------------------
    # Step 3: Forensic snapshot
    # ------------------------------------------------------------------

    def _write_forensic_snapshot(
        self,
        *,
        incident_id: str,
        session_id: str,
        task_id: str,
        event_type: str,
        severity: str,
        detail: str,
        branch: str,
        extra: dict[str, Any],
        task_context: dict[str, Any],
        ts: float,
    ) -> str:
        """Write a comprehensive forensic snapshot for post-incident analysis.

        The snapshot captures everything available at detection time:
        - Incident metadata
        - Agent and task identifiers
        - Environment variables (filtered to exclude secrets)
        - Git state summary
        - Any extra context provided by the caller

        Returns the path to the written snapshot file.
        """
        incidents_dir = self.workdir / ".sdd" / "runtime" / "security_incidents"
        incidents_dir.mkdir(parents=True, exist_ok=True)

        snapshot: dict[str, Any] = {
            "schema_version": "1",
            "incident_id": incident_id,
            "detected_at": datetime.fromtimestamp(ts, tz=UTC).isoformat(),
            "event_type": event_type,
            "severity": severity,
            "detail": detail,
            "agent": {
                "session_id": session_id,
                "branch": branch,
            },
            "task": {
                "task_id": task_id,
                **task_context,
            },
            "environment": self._safe_env_snapshot(),
            "git_state": self._capture_git_state(),
            "extra": extra,
        }

        snapshot_file = incidents_dir / f"{incident_id}.json"
        snapshot_file.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        logger.info(
            "Forensic snapshot written for incident %s at %s",
            incident_id,
            snapshot_file,
        )
        return str(snapshot_file)

    @staticmethod
    def _safe_env_snapshot() -> dict[str, str]:
        """Capture env vars, redacting any that look like secrets."""
        _SECRET_KEYS = frozenset(
            {
                "token",
                "secret",
                "password",
                "passwd",
                "apikey",
                "api_key",
                "auth",
                "credential",
                "private",
                "key",
            }
        )
        result: dict[str, str] = {}
        for k, v in os.environ.items():
            lower_k = k.lower()
            if any(secret_kw in lower_k for secret_kw in _SECRET_KEYS):
                result[k] = "[REDACTED]"
            else:
                # Truncate very long values to avoid huge snapshots
                result[k] = v[:500] if len(v) > 500 else v
        return result

    def _capture_git_state(self) -> dict[str, Any]:
        """Capture current git status/log for forensics.  Best-effort."""
        git_state: dict[str, Any] = {}
        try:
            # Current HEAD commit
            r = subprocess.run(
                ["git", "log", "-1", "--format=%H %s"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=str(self.workdir),
            )
            if r.returncode == 0:
                git_state["head"] = r.stdout.strip()

            # Dirty working tree (untracked + modified files)
            r2 = subprocess.run(
                ["git", "status", "--short"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=str(self.workdir),
            )
            if r2.returncode == 0:
                lines = r2.stdout.strip().splitlines()
                # Cap at 50 files to avoid unbounded snapshot size
                git_state["dirty_files"] = lines[:50]
                git_state["dirty_file_count"] = len(lines)
        except (subprocess.TimeoutExpired, OSError):
            git_state["error"] = "git state capture timed out"
        return git_state

    # ------------------------------------------------------------------
    # Step 4: Block the task from retry
    # ------------------------------------------------------------------

    def _block_task_retry(
        self,
        incident_id: str,
        task_id: str,
        session_id: str,
        event_type: str,
        detail: str,
    ) -> str:
        """Write a block marker so the orchestrator will not reschedule the task.

        The marker is written to ``.sdd/runtime/task_blocks/{task_id}.block``.
        The orchestrator's task-selection code checks for these markers and
        skips blocked tasks.  Operators can remove the file to unblock a task
        after investigation is complete.

        Returns the path to the block marker file.
        """
        blocks_dir = self.workdir / ".sdd" / "runtime" / "task_blocks"
        blocks_dir.mkdir(parents=True, exist_ok=True)

        block_payload: dict[str, Any] = {
            "task_id": task_id,
            "blocked_at": datetime.now(UTC).isoformat(),
            "blocked_by": incident_id,
            "session_id": session_id,
            "event_type": event_type,
            "detail": detail,
            "reason": "security_incident_containment",
            "unblock_instructions": (
                "Investigate the forensic snapshot, confirm the task is safe, "
                "then delete this file to allow rescheduling."
            ),
        }

        block_file = blocks_dir / f"{task_id}.block"
        block_file.write_text(json.dumps(block_payload, indent=2), encoding="utf-8")
        logger.warning(
            "Task %s blocked from retry (incident=%s, event=%s)",
            task_id,
            incident_id,
            event_type,
        )
        return str(block_file)

    # ------------------------------------------------------------------
    # Step 5: Notify the security team
    # ------------------------------------------------------------------

    def _notify(
        self,
        *,
        incident_id: str,
        session_id: str,
        task_id: str,
        event_type: str,
        severity: str,
        detail: str,
        steps_taken: list[str],
        ts: float,
    ) -> None:
        """Record the incident in the security audit log.

        Appends a JSONL entry to ``.sdd/metrics/security_incidents.jsonl``
        and, if ``notify_via_bulletin`` is enabled, writes a bulletin-board
        message so other agents can read the alert.
        """
        # Security audit log — machine-readable, append-only
        metrics_dir = self.workdir / ".sdd" / "metrics"
        metrics_dir.mkdir(parents=True, exist_ok=True)

        audit_entry: dict[str, Any] = {
            "event_type": "security_incident",
            "incident_id": incident_id,
            "detected_at": datetime.fromtimestamp(ts, tz=UTC).isoformat(),
            "security_event_type": event_type,
            "severity": severity,
            "session_id": session_id,
            "task_id": task_id,
            "detail": detail,
            "containment_steps": steps_taken,
        }
        with open(metrics_dir / "security_incidents.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(audit_entry) + "\n")

        logger.critical(
            "SECURITY AUDIT [%s] %s — %s (session=%s, task=%s) — contained via: %s",
            severity.upper(),
            incident_id,
            event_type,
            session_id,
            task_id,
            ", ".join(steps_taken),
        )

        if self.notify_via_bulletin:
            self._write_bulletin_message(
                incident_id=incident_id,
                session_id=session_id,
                task_id=task_id,
                event_type=event_type,
                severity=severity,
                detail=detail,
            )

    def _write_bulletin_message(
        self,
        *,
        incident_id: str,
        session_id: str,
        task_id: str,
        event_type: str,
        severity: str,
        detail: str,
    ) -> None:
        """Write a security alert to the bulletin board for cross-agent visibility.

        Uses the raw bulletin file directly to avoid importing the bulletin
        module (which would create a circular dependency).
        """
        bulletin_dir = self.workdir / ".sdd" / "runtime"
        bulletin_dir.mkdir(parents=True, exist_ok=True)
        bulletin_file = bulletin_dir / "bulletin.jsonl"

        message: dict[str, Any] = {
            "type": "security_alert",
            "timestamp": datetime.now(UTC).isoformat(),
            "incident_id": incident_id,
            "security_event_type": event_type,
            "severity": severity,
            "session_id": session_id,
            "task_id": task_id,
            "detail": detail,
            "content": (
                f"[SECURITY ALERT] {incident_id}: {event_type} detected "
                f"(severity={severity}). Agent {session_id} has been "
                f"contained. Task {task_id} is blocked from retry."
            ),
        }

        try:
            with open(bulletin_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(message) + "\n")
        except OSError:
            logger.exception("Failed to write security alert to bulletin board for %s", incident_id)


# ---------------------------------------------------------------------------
# Convenience: check if a task is blocked from retry
# ---------------------------------------------------------------------------


def is_task_blocked(workdir: Path, task_id: str) -> bool:
    """Return True if *task_id* has an active security block marker.

    The orchestrator's task-selection loop should call this before scheduling
    any task to ensure blocked tasks are not retried.

    Args:
        workdir: Project root directory.
        task_id: The task identifier to check.

    Returns:
        True if the task has an unresolved security block.
    """
    block_file = workdir / ".sdd" / "runtime" / "task_blocks" / f"{task_id}.block"
    return block_file.exists()


def load_block_metadata(workdir: Path, task_id: str) -> dict[str, Any] | None:
    """Return the block metadata for *task_id*, or None if not blocked.

    Args:
        workdir: Project root directory.
        task_id: The task identifier to look up.

    Returns:
        Deserialized block payload, or None if the block file does not exist.
    """
    block_file = workdir / ".sdd" / "runtime" / "task_blocks" / f"{task_id}.block"
    if not block_file.exists():
        return None
    try:
        return json.loads(block_file.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    except (json.JSONDecodeError, OSError):
        logger.warning("Failed to read block metadata for task %s", task_id)
        return None


def list_active_security_incidents(workdir: Path) -> list[dict[str, Any]]:
    """Return all security incidents recorded in the audit log.

    Reads ``.sdd/metrics/security_incidents.jsonl`` and returns all entries
    as a list of dictionaries, newest-last.

    Args:
        workdir: Project root directory.

    Returns:
        List of incident audit entries.  Empty list if no incidents.
    """
    log_file = workdir / ".sdd" / "metrics" / "security_incidents.jsonl"
    if not log_file.exists():
        return []
    incidents: list[dict[str, Any]] = []
    for line in log_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            incidents.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return incidents
