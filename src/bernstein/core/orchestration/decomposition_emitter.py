"""Emission policy for retry exhaustion decomposition proposals.

This module defines the DecompositionEmitter class that monitors task
retries and emits decomposition proposals when tasks exhaust their
retries on the same issue.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from bernstein.core.orchestration.decomposition_proposal import DecompositionProposal
from bernstein.core.tasks.models import Task, TaskStatus


@dataclass
class DecompositionEmitter:
    """Monitors task retries and emits decomposition proposals on exhaustion.

    Attributes:
        propose_count: Minimum number of terminal tasks for the same issue
            required to trigger a proposal. Default is 2.
        seen_digests: Set of content hashes of already-emitted proposals
            to prevent duplicate emissions.
        tasks_by_issue: Aggregation of terminal tasks per issue number.
    """

    propose_count: int = 2
    seen_digests: set[str] = field(default_factory=set)
    tasks_by_issue: dict[int, list[Task]] = field(default_factory=dict)

    def check_exhaustion(self, task: Task, task_store: Any = None) -> DecompositionProposal | None:
        """Check if a task's retry exhaustion warrants a decomposition proposal.

        Args:
            task: The task that may have exhausted its retries.
            task_store: Optional task store for looking up other tasks for the
                same issue. If not provided, only the passed task is considered.

        Returns:
            A DecompositionProposal if the threshold is met and this evidence
            set hasn't been seen before, otherwise None.
        """
        # Only consider tasks that have exhausted retries and are in FAILED state.
        if task.status != TaskStatus.FAILED or task.retry_count < task.max_retries:
            return None

        # Task must have issue_number in metadata.
        issue_number = task.metadata.get("issue_number")
        if issue_number is None:
            return None

        # Collect all terminal tasks for this issue.
        terminal_tasks = list(self.tasks_by_issue.get(issue_number, []))
        if task not in terminal_tasks:
            terminal_tasks.append(task)
            self.tasks_by_issue[issue_number] = terminal_tasks

        # If we have a task_store, also query it for other tasks with the same issue.
        if task_store is not None:
            # Try to fetch other tasks with the same issue_number.
            # The task_store interface is not strictly typed here, so we use duck typing.
            try:
                # Look for tasks with matching issue_number in metadata.
                # This is a best-effort query; the exact method depends on the store.
                if hasattr(task_store, "list_tasks"):
                    all_tasks = task_store.list_tasks()
                    for t in all_tasks:
                        if (
                            t.metadata.get("issue_number") == issue_number
                            and t.id != task.id
                            and t.status == TaskStatus.FAILED
                            and t.retry_count >= t.max_retries
                            and t not in terminal_tasks
                        ):
                            terminal_tasks.append(t)
            except Exception:
                # If the store query fails, fall back to in-memory aggregation.
                pass

        # Check if we have enough terminal tasks to propose.
        if len(terminal_tasks) < self.propose_count:
            return None

        # Build the failure evidence from the terminal tasks.
        evidence_digests_set: set[str] = set()
        attempts: list[dict[str, Any]] = []

        # Sort terminal_tasks by task id to ensure deterministic ordering
        # of evidence digests and attempts, so same evidence with tasks
        # in different order yields the same canonical bytes.
        terminal_tasks = sorted(terminal_tasks, key=lambda t: t.id)

        for t in terminal_tasks:
            parts: list[str] = []
            if t.terminal_reason:
                parts.append(t.terminal_reason)
            if t.result_summary:
                parts.append(t.result_summary)

            # Build evidence digest for this task.
            task_evidence = "; ".join(parts) if parts else "Unknown failure"
            task_digest = hashlib.sha256(task_evidence.encode("utf-8")).hexdigest()
            evidence_digests_set.add(task_digest)

            # Add attempt for this task.
            attempt = {
                "attempt_number": t.retry_count + 1,
                "model": t.metadata.get("model", t.model or "unknown"),
                "effort": t.metadata.get("effort", t.effort or "unknown"),
                "failure_reason": t.result_summary or "",
                "terminal_reason": t.terminal_reason or "",
            }
            attempts.append(attempt)

        evidence_digests = tuple(sorted(evidence_digests_set))

        # Validate that evidence digests are well-formed SHA256 hashes.
        for digest in evidence_digests:
            if not (len(digest) == 64 and all(c in "0123456789abcdefABCDEF" for c in digest)):
                return None  # Invalid digest format - fail closed.

        # Create the proposal with all attempts and evidence digests.
        proposal = DecompositionProposal(
            issue_number=issue_number,
            repo=task.repo or "unknown",
            evidence_digests=evidence_digests,
            attempts=attempts,
        )

        # Deduplication: check if we've already seen this proposal.
        if proposal.content_address in self.seen_digests:
            return None

        # Mark as seen.
        self.seen_digests.add(proposal.content_address)

        return proposal

    def emit_proposal(self, proposal: DecompositionProposal, gh_client: Any, issue_number: int) -> bool:
        """Post the decomposition proposal as a comment on the GitHub issue.

        Args:
            proposal: The DecompositionProposal to emit.
            gh_client: GitHub client instance with a _post_comment method.
            issue_number: The GitHub issue number to comment on.

        Returns:
            True if the comment was posted successfully, False otherwise.
        """
        try:
            gh_client._post_comment(issue_number, proposal.to_comment_body())
            return True
        except Exception:
            return False
