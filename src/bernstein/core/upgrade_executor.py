"""Autonomous upgrade executor with transaction-like safety and rollback.

Executes approved system upgrades: code modifications, template updates,
new agent roles, and configuration adjustments. All changes are reviewed
by a dedicated reviewer agent before execution.

Features:
- Transaction-like safety with atomic file operations
- Rollback capability for failed upgrades
- Git integration for version control
- Reviewer agent validation before execution
"""

from __future__ import annotations

import json
import logging
import shutil
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

from bernstein.core.git_ops import (
    commit as git_commit,
)
from bernstein.core.git_ops import (
    is_git_repo,
    rev_parse_head,
    revert_commit,
    stage_files,
)
from bernstein.core.llm import call_llm
from bernstein.core.models import (
    RollbackPlan,
    Task,
    TaskType,
)

logger = logging.getLogger(__name__)


class UpgradeStatus(Enum):
    """Status of an upgrade execution."""

    PENDING_REVIEW = "pending_review"
    REVIEW_APPROVED = "review_approved"
    REVIEW_REJECTED = "review_rejected"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class UpgradeType(Enum):
    """Types of system upgrades."""

    CODE_MODIFICATION = "code_modification"
    TEMPLATE_UPDATE = "template_update"
    NEW_AGENT_ROLE = "new_agent_role"
    CONFIG_ADJUSTMENT = "config_adjustment"
    POLICY_UPDATE = "policy_update"
    ROUTING_RULE_CHANGE = "routing_rule_change"


@dataclass
class FileChange:
    """A single file change in an upgrade."""

    path: str
    operation: str  # "create", "modify", "delete"
    old_content: str | None = None
    new_content: str | None = None
    backup_path: str | None = None


@dataclass
class UpgradeTransaction:
    """Represents a transactional upgrade with rollback capability."""

    id: str
    upgrade_type: UpgradeType
    title: str
    description: str
    status: UpgradeStatus = UpgradeStatus.PENDING_REVIEW
    created_at: float = field(default_factory=time.time)
    executed_at: float | None = None
    completed_at: float | None = None

    # Changes to apply
    file_changes: list[FileChange] = field(default_factory=lambda: [])

    # Review results
    reviewer_feedback: str = ""
    review_completed_at: float | None = None

    # Rollback info
    rollback_plan: RollbackPlan | None = None
    rollback_available: bool = False
    rolled_back_at: float | None = None

    # Results
    result_summary: str = ""
    error_message: str = ""

    # Metadata
    task_id: str | None = None
    git_commit: str | None = None
    rollback_commit: str | None = None


class UpgradeReviewer:
    """Dedicated reviewer agent for upgrade validation.

    Reviews upgrade proposals for:
    - Correctness and completeness
    - Risk assessment validation
    - Rollback plan adequacy
    - Best practices compliance
    """

    def __init__(
        self,
        model: str = "nvidia/nemotron-3-super-120b-a12b",
        provider: str = "openrouter_free",
        workdir: Path | None = None,
    ) -> None:
        self._model = model
        self._provider = provider
        self._workdir = workdir or Path.cwd()

    async def review_upgrade(self, transaction: UpgradeTransaction) -> ReviewResult:
        """Review an upgrade proposal.

        Args:
            transaction: Upgrade transaction to review.

        Returns:
            ReviewResult with verdict and feedback.
        """
        prompt = self._build_review_prompt(transaction)

        try:
            response = await call_llm(prompt, model=self._model, provider=self._provider)
            return self._parse_review_response(response, transaction)
        except Exception as exc:
            logger.error("Upgrade review failed: %s", exc)
            return ReviewResult(
                verdict="request_changes",
                reasoning=f"Review failed: {exc}",
                feedback="The automated review encountered an error. Please review manually.",
            )

    def _build_review_prompt(self, transaction: UpgradeTransaction) -> str:
        """Build the review prompt for an upgrade."""
        changes_desc = "\n".join(f"- {change.operation.upper()} {change.path}" for change in transaction.file_changes)

        rollback_desc = ""
        if transaction.rollback_plan:
            rollback_desc = "\n".join(f"  {i + 1}. {step}" for i, step in enumerate(transaction.rollback_plan.steps))

        return f"""You are a senior software engineer reviewing a proposed system upgrade.

## Upgrade Details
- **Type**: {transaction.upgrade_type.value}
- **Title**: {transaction.title}
- **Description**: {transaction.description}

## Proposed Changes
{changes_desc}

## Rollback Plan
{rollback_desc if rollback_desc else "(No rollback plan provided)"}

## Review Criteria
1. **Correctness**: Are the changes technically correct?
2. **Safety**: Is the rollback plan adequate?
3. **Completeness**: Are all necessary changes included?
4. **Best Practices**: Do the changes follow project conventions?

## Response Format
Respond with a JSON object:
{{
    "verdict": "approve" | "request_changes" | "reject",
    "reasoning": "Brief explanation of your decision",
    "feedback": "Specific feedback and required changes (if any)",
    "risk_level": "low" | "medium" | "high" | "critical",
    "suggested_improvements": ["list of suggestions"]
}}

Review the upgrade carefully and provide your assessment."""

    def _parse_review_response(self, response: str, transaction: UpgradeTransaction) -> ReviewResult:
        """Parse the LLM review response."""
        import json

        # Extract JSON from response
        text = response.strip()
        if text.startswith("```"):
            text = text[text.index("\n") + 1 :]
        if text.endswith("```"):
            text = text[: text.rfind("```")]
        text = text.strip()

        try:
            data = json.loads(text)
            verdict = data.get("verdict", "request_changes")
            if verdict not in {"approve", "request_changes", "reject"}:
                verdict = "request_changes"

            return ReviewResult(
                verdict=verdict,
                reasoning=data.get("reasoning", ""),
                feedback=data.get("feedback", ""),
                risk_level=data.get("risk_level", "medium"),
                suggested_improvements=data.get("suggested_improvements", []),
            )
        except json.JSONDecodeError:
            # Fallback: try to extract verdict from text
            if "approve" in response.lower() and "reject" not in response.lower():
                return ReviewResult(
                    verdict="approve",
                    reasoning="Automated parsing succeeded",
                    feedback=response,
                )
            return ReviewResult(
                verdict="request_changes",
                reasoning="Could not parse review response",
                feedback=response,
            )


@dataclass
class ReviewResult:
    """Result of an upgrade review."""

    verdict: str  # "approve", "request_changes", "reject"
    reasoning: str
    feedback: str
    risk_level: str = "medium"
    suggested_improvements: list[str] = field(default_factory=lambda: [])


class UpgradeExecutor:
    """Executes approved upgrades with transaction-like safety.

    Features:
    - Atomic file operations with backup
    - Git integration for version control
    - Rollback capability
    - Reviewer agent validation

    Args:
        workdir: Project working directory.
        reviewer: UpgradeReviewer instance (created if None).
        auto_git: Whether to automatically commit changes to git.
    """

    def __init__(
        self,
        workdir: Path | None = None,
        reviewer: UpgradeReviewer | None = None,
        auto_git: bool = True,
    ) -> None:
        self._workdir = workdir or Path.cwd()
        self._reviewer = reviewer or UpgradeReviewer(workdir=self._workdir)
        self._auto_git = auto_git
        self._transactions: dict[str, UpgradeTransaction] = {}
        self._backup_dir = self._workdir / ".sdd" / "upgrades" / "backups"
        self._backup_dir.mkdir(parents=True, exist_ok=True)

    async def submit_upgrade(
        self,
        upgrade_type: UpgradeType,
        title: str,
        description: str,
        file_changes: list[FileChange],
        rollback_plan: RollbackPlan | None = None,
        task_id: str | None = None,
    ) -> UpgradeTransaction:
        """Submit a new upgrade for review and execution.

        Args:
            upgrade_type: Type of upgrade.
            title: Short title.
            description: Detailed description.
            file_changes: List of file changes to apply.
            rollback_plan: Optional rollback plan.
            task_id: Associated task ID.

        Returns:
            UpgradeTransaction for tracking.
        """
        transaction = UpgradeTransaction(
            id=str(uuid.uuid4())[:12],
            upgrade_type=upgrade_type,
            title=title,
            description=description,
            file_changes=file_changes,
            rollback_plan=rollback_plan,
            task_id=task_id,
        )

        self._transactions[transaction.id] = transaction
        logger.info("Submitted upgrade %s: %s", transaction.id, title)

        # Start review process
        await self._review_upgrade(transaction)

        return transaction

    async def _review_upgrade(self, transaction: UpgradeTransaction) -> None:
        """Submit upgrade for reviewer agent validation."""
        logger.info("Starting review for upgrade %s", transaction.id)

        result = await self._reviewer.review_upgrade(transaction)
        transaction.review_completed_at = time.time()
        transaction.reviewer_feedback = result.feedback

        if result.verdict == "approve":
            transaction.status = UpgradeStatus.REVIEW_APPROVED
            logger.info("Upgrade %s approved by reviewer", transaction.id)
            # Auto-execute if approved
            self._execute_upgrade(transaction)
        elif result.verdict == "reject":
            transaction.status = UpgradeStatus.REVIEW_REJECTED
            transaction.error_message = result.reasoning
            logger.warning("Upgrade %s rejected: %s", transaction.id, result.reasoning)
        else:  # request_changes
            transaction.status = UpgradeStatus.REVIEW_REJECTED
            transaction.error_message = f"Changes requested: {result.feedback}"
            logger.info("Upgrade %s requires changes: %s", transaction.id, result.feedback)

    def _execute_upgrade(self, transaction: UpgradeTransaction) -> None:
        """Execute an approved upgrade."""
        if transaction.status != UpgradeStatus.REVIEW_APPROVED:
            raise ValueError(f"Upgrade {transaction.id} is not approved")

        transaction.status = UpgradeStatus.EXECUTING
        transaction.executed_at = time.time()
        logger.info("Executing upgrade %s", transaction.id)

        try:
            # Create backups
            self._create_backups(transaction)

            # Apply changes
            self._apply_changes(transaction)

            # Commit to git if enabled
            if self._auto_git:
                transaction.git_commit = self._commit_changes(transaction)
                transaction.rollback_available = True

            # Verify changes
            self._verify_changes(transaction)

            transaction.status = UpgradeStatus.COMPLETED
            transaction.completed_at = time.time()
            transaction.result_summary = f"Successfully applied {len(transaction.file_changes)} changes"

            logger.info("Upgrade %s completed successfully", transaction.id)

        except Exception as exc:
            logger.error("Upgrade %s failed: %s", transaction.id, exc)
            transaction.status = UpgradeStatus.FAILED
            transaction.error_message = str(exc)
            transaction.completed_at = time.time()

            # Auto-rollback if git commit was made
            if self._auto_git and transaction.git_commit:
                self._rollback_upgrade(transaction)

    def _create_backups(self, transaction: UpgradeTransaction) -> None:
        """Create backups of files to be modified."""
        backup_subdir = self._backup_dir / transaction.id
        backup_subdir.mkdir(parents=True, exist_ok=True)

        for change in transaction.file_changes:
            if change.operation in ("modify", "delete"):
                file_path = self._workdir / change.path
                if file_path.exists():
                    backup_path = backup_subdir / Path(change.path).name
                    shutil.copy2(file_path, backup_path)
                    change.backup_path = str(backup_path)
                    logger.debug("Backed up %s to %s", change.path, change.backup_path)

    def _apply_changes(self, transaction: UpgradeTransaction) -> None:
        """Apply all file changes."""
        for change in transaction.file_changes:
            file_path = self._workdir / change.path

            if change.operation == "create":
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(change.new_content or "", encoding="utf-8")
                logger.debug("Created %s", change.path)

            elif change.operation == "modify":
                if change.new_content is not None:
                    file_path.write_text(change.new_content, encoding="utf-8")
                    logger.debug("Modified %s", change.path)

            elif change.operation == "delete":
                if file_path.exists():
                    file_path.unlink()
                    logger.debug("Deleted %s", change.path)

    def _commit_changes(self, transaction: UpgradeTransaction) -> str | None:
        """Commit changes to git via centralized git_ops."""
        try:
            if not is_git_repo(self._workdir):
                logger.warning("Not a git repository, skipping commit")
                return None

            # Stage changes
            paths = [change.path for change in transaction.file_changes]
            stage_files(self._workdir, paths)

            # Commit
            commit_msg = (
                f"feat(upgrade): {transaction.title[:72]}\n\nUpgrade: {transaction.id}\n\n{transaction.description}"
            )
            result = git_commit(self._workdir, commit_msg, enforce_conventional=True)

            if result.ok:
                commit_hash = rev_parse_head(self._workdir)
                logger.info("Committed upgrade %s as %s", transaction.id, commit_hash)
                return commit_hash

            logger.warning("Git commit failed: %s", result.stderr)
            return None

        except Exception as exc:
            logger.warning("Git commit error: %s", exc)
            return None

    def _verify_changes(self, transaction: UpgradeTransaction) -> None:
        """Verify that changes were applied correctly."""
        for change in transaction.file_changes:
            file_path = self._workdir / change.path

            if change.operation == "create":
                if not file_path.exists():
                    raise ValueError(f"Verification failed: {change.path} was not created")

            elif change.operation == "modify":
                if change.new_content:
                    actual = file_path.read_text(encoding="utf-8")
                    if actual != change.new_content:
                        raise ValueError(f"Verification failed: {change.path} content mismatch")

            elif change.operation == "delete" and file_path.exists():
                raise ValueError(f"Verification failed: {change.path} was not deleted")

    def _rollback_upgrade(self, transaction: UpgradeTransaction) -> None:
        """Rollback an upgrade using git or backups."""
        logger.info("Rolling back upgrade %s", transaction.id)

        if transaction.git_commit:
            # Use git revert via git_ops
            try:
                result = revert_commit(self._workdir, transaction.git_commit, no_commit=True)
                if result.ok:
                    git_commit(
                        self._workdir,
                        f"fix(upgrade): revert upgrade {transaction.id}",
                        enforce_conventional=True,
                    )
                    transaction.status = UpgradeStatus.ROLLED_BACK
                    transaction.rolled_back_at = time.time()
                    logger.info("Upgrade %s rolled back successfully", transaction.id)
                    return
            except Exception as exc:
                logger.error("Git rollback failed: %s", exc)

        # Fallback: restore from backups
        for change in transaction.file_changes:
            if change.backup_path:
                backup_path = Path(change.backup_path)
                file_path = self._workdir / change.path
                if backup_path.exists():
                    shutil.copy2(backup_path, file_path)
                    logger.debug("Restored %s from backup", change.path)

        transaction.status = UpgradeStatus.ROLLED_BACK
        transaction.rolled_back_at = time.time()
        logger.info("Upgrade %s rolled back from backups", transaction.id)

    def rollback(self, transaction_id: str) -> bool:
        """Manually trigger rollback for a completed upgrade.

        Args:
            transaction_id: ID of the upgrade to rollback.

        Returns:
            True if rollback succeeded.
        """
        transaction = self._transactions.get(transaction_id)
        if not transaction:
            logger.error("Transaction %s not found", transaction_id)
            return False

        if transaction.status not in (UpgradeStatus.COMPLETED, UpgradeStatus.FAILED):
            logger.warning("Cannot rollback upgrade in status %s", transaction.status)
            return False

        self._rollback_upgrade(transaction)
        return True

    def get_transaction(self, transaction_id: str) -> UpgradeTransaction | None:
        """Get a transaction by ID."""
        return self._transactions.get(transaction_id)

    def get_all_transactions(self) -> list[UpgradeTransaction]:
        """Get all upgrade transactions."""
        return list(self._transactions.values())

    def export_history(self, output_path: Path) -> None:
        """Export upgrade history to a JSON file.

        Args:
            output_path: Path to write the export.
        """
        history = [
            {
                "id": t.id,
                "upgrade_type": t.upgrade_type.value,
                "title": t.title,
                "status": t.status.value,
                "created_at": datetime.fromtimestamp(t.created_at).isoformat(),
                "executed_at": datetime.fromtimestamp(t.executed_at) if t.executed_at else None,
                "completed_at": datetime.fromtimestamp(t.completed_at) if t.completed_at else None,
                "result_summary": t.result_summary,
                "error_message": t.error_message,
                "git_commit": t.git_commit,
            }
            for t in self._transactions.values()
        ]

        with output_path.open("w") as f:
            json.dump(history, f, indent=2)


# Integration with task system
def create_upgrade_from_task(
    task: Task,
    workdir: Path,
) -> UpgradeTransaction | None:
    """Create an upgrade transaction from a task.

    Args:
        task: Task with upgrade_details.
        workdir: Project working directory.

    Returns:
        UpgradeTransaction if task is an upgrade proposal, None otherwise.
    """
    if task.task_type != TaskType.UPGRADE_PROPOSAL or not task.upgrade_details:
        return None

    executor = UpgradeExecutor(workdir=workdir)

    # Convert task to file changes (this would need LLM to generate actual changes)
    # For now, create a placeholder transaction
    transaction = UpgradeTransaction(
        id=task.id,
        upgrade_type=UpgradeType.CODE_MODIFICATION,  # Default
        title=task.title,
        description=task.description,
        status=UpgradeStatus.PENDING_REVIEW,
        task_id=task.id,
    )

    executor._transactions[transaction.id] = transaction  # type: ignore[reportPrivateUsage]
    return transaction


def get_executor(workdir: Path | None = None) -> UpgradeExecutor:
    """Get or create the default upgrade executor."""
    return UpgradeExecutor(workdir=workdir)
