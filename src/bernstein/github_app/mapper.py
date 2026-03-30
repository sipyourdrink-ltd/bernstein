"""Event-to-task conversion: maps GitHub webhook events to Bernstein task payloads.

Each mapper function accepts a ``WebhookEvent`` and returns one or more
task creation payloads (dicts matching ``TaskCreate`` fields) ready to be
posted to the task server.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.github_app.webhooks import WebhookEvent

logger = logging.getLogger(__name__)

# Labels that trigger automatic Bernstein task creation (in addition to opened issues)
TRIGGER_LABELS: frozenset[str] = frozenset({"bernstein", "agent-fix", "agent-task"})

# Label → priority mapping (lower = higher priority)
_LABEL_PRIORITY: dict[str, int] = {
    "bug": 1,
    "critical": 1,
    "security": 1,
    "bernstein": 2,
    "agent-fix": 1,
    "agent-task": 2,
    "enhancement": 2,
    "feature": 2,
    "docs": 3,
    "documentation": 3,
    "chore": 3,
}

# Label → role mapping
_LABEL_ROLE: dict[str, str] = {
    "backend": "backend",
    "frontend": "frontend",
    "qa": "qa",
    "security": "security",
    "docs": "docs",
    "documentation": "docs",
    "infra": "backend",
    "devops": "backend",
}

# File path prefix → role mapping for PR reviews
_PATH_ROLE: list[tuple[str, str]] = [
    ("tests/", "qa"),
    ("test_", "qa"),
    ("docs/", "docs"),
    ("README", "docs"),
    (".github/", "backend"),
    ("deploy/", "backend"),
    ("src/bernstein/adapters/", "backend"),
    ("src/bernstein/cli/", "backend"),
    ("src/bernstein/core/", "backend"),
]

# Patterns that indicate an actionable PR review comment
_ACTIONABLE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bfix\b", re.IGNORECASE),
    re.compile(r"\bchange\b", re.IGNORECASE),
    re.compile(r"\bupdate\b", re.IGNORECASE),
    re.compile(r"\breplace\b", re.IGNORECASE),
    re.compile(r"\bremove\b", re.IGNORECASE),
    re.compile(r"\badd\b", re.IGNORECASE),
    re.compile(r"\brefactor\b", re.IGNORECASE),
    re.compile(r"\bshould\b", re.IGNORECASE),
    re.compile(r"\bmust\b", re.IGNORECASE),
    re.compile(r"\bconsider\b", re.IGNORECASE),
    re.compile(r"```suggestion", re.IGNORECASE),
]


def _extract_labels(payload: dict[str, Any]) -> list[str]:
    """Extract label names from an issue or PR payload."""
    labels_raw: list[dict[str, Any]] = payload.get("issue", payload).get("labels", [])
    return [lbl.get("name", "").lower() for lbl in labels_raw if lbl.get("name")]


def _priority_from_labels(labels: list[str]) -> int:
    """Determine task priority from GitHub labels. Default is 2."""
    for label in labels:
        if label in _LABEL_PRIORITY:
            return _LABEL_PRIORITY[label]
    return 2


def _role_from_labels(labels: list[str]) -> str:
    """Determine role from GitHub labels. Default is 'backend'."""
    for label in labels:
        if label in _LABEL_ROLE:
            return _LABEL_ROLE[label]
    return "backend"


def _role_from_path(path: str) -> str:
    """Determine role from a file path."""
    for prefix, role in _PATH_ROLE:
        if path.startswith(prefix):
            return role
    return "backend"


def _is_actionable(text: str) -> bool:
    """Determine if a comment is actionable (contains a suggestion/fix request)."""
    return any(pattern.search(text) for pattern in _ACTIONABLE_PATTERNS)


def issue_to_tasks(event: WebhookEvent) -> list[dict[str, Any]]:
    """Convert a new issue event into one or more Bernstein task payloads.

    - Parses issue labels for role hints (``label:backend`` -> ``role:backend``)
    - Parses issue body for scope hints
    - Sets priority based on labels (bug=1, enhancement=2, docs=3)

    Args:
        event: A webhook event with ``event_type == "issues"`` and
            ``action == "opened"``.

    Returns:
        List of task creation dicts matching ``TaskCreate`` fields.
    """
    if event.event_type != "issues" or event.action != "opened":
        return []

    issue: dict[str, Any] = event.payload.get("issue", {})
    title = issue.get("title", "Untitled issue")
    body = issue.get("body", "") or ""
    number = issue.get("number", 0)

    labels = _extract_labels(event.payload)
    priority = _priority_from_labels(labels)
    role = _role_from_labels(labels)

    # Estimate scope from body length
    scope = "small" if len(body) < 200 else ("large" if len(body) > 1000 else "medium")

    description = f"GitHub issue #{number} from {event.sender} in {event.repo_full_name}.\n\n{body[:2000]}"

    task: dict[str, Any] = {
        "title": f"[GH#{number}] {title}"[:120],
        "description": description,
        "role": role,
        "priority": priority,
        "scope": scope,
        "task_type": "standard",
    }

    logger.info(
        "Mapped issue #%d to task: role=%s priority=%d scope=%s",
        number,
        role,
        priority,
        scope,
    )

    return [task]


def pr_review_to_task(event: WebhookEvent) -> dict[str, Any] | None:
    """Convert a PR review comment into a fix task if actionable.

    Only creates a task if the comment body contains actionable language
    (fix, change, replace, etc.) or a code suggestion block.

    Args:
        event: A webhook event with ``event_type == "pull_request_review_comment"``
            or ``event_type == "issue_comment"`` on a PR.

    Returns:
        Task creation dict, or ``None`` if the comment is not actionable.
    """
    comment: dict[str, Any] = event.payload.get("comment", {})
    comment_body = comment.get("body", "") or ""

    if not _is_actionable(comment_body):
        return None

    # Determine the file path for role inference
    path = comment.get("path", "")
    role = _role_from_path(path) if path else "backend"

    pr: dict[str, Any] = event.payload.get("pull_request", {})
    pr_number = pr.get("number", 0)
    pr_title = pr.get("title", "")

    description = (
        f"PR review comment on #{pr_number} ({pr_title}) "
        f"in {event.repo_full_name} by {event.sender}.\n\n"
        f"File: {path}\n\n"
        f"Comment:\n{comment_body[:2000]}"
    )

    task: dict[str, Any] = {
        "title": f"[GH-PR#{pr_number}] Fix: {comment_body[:80]}"[:120],
        "description": description,
        "role": role,
        "priority": 1,
        "scope": "small",
        "task_type": "fix",
    }

    logger.info(
        "Mapped PR review comment to fix task: pr=#%d role=%s path=%s",
        pr_number,
        role,
        path,
    )

    return task


def push_to_tasks(event: WebhookEvent) -> list[dict[str, Any]]:
    """Convert a push event to a CI verification task.

    Creates a QA task to verify the push, including commit messages
    in the task description.

    Args:
        event: A webhook event with ``event_type == "push"``.

    Returns:
        List containing a single QA verification task.
    """
    if event.event_type != "push":
        return []

    ref = event.payload.get("ref", "")
    commits: list[dict[str, Any]] = event.payload.get("commits", [])

    # Build commit summary
    commit_lines: list[str] = []
    for commit in commits[:10]:  # Cap at 10 commits
        sha = commit.get("id", "")[:8]
        msg = commit.get("message", "").split("\n")[0]
        commit_lines.append(f"  - {sha}: {msg}")

    commit_summary = "\n".join(commit_lines) if commit_lines else "  (no commits)"

    description = (
        f"Push to {ref} in {event.repo_full_name} by {event.sender}.\n\n"
        f"Commits:\n{commit_summary}\n\n"
        f"Verify that all tests pass and no regressions were introduced."
    )

    task: dict[str, Any] = {
        "title": f"[GH-push] Verify push to {ref.split('/')[-1]}"[:120],
        "description": description,
        "role": "qa",
        "priority": 2,
        "scope": "small",
        "task_type": "standard",
    }

    logger.info(
        "Mapped push to QA task: ref=%s commits=%d",
        ref,
        len(commits),
    )

    return [task]


def workflow_run_to_task(
    event: WebhookEvent,
    retry_count: int = 0,
    cwd: Path | None = None,
) -> list[dict[str, Any]]:
    """Convert a ``workflow_run`` failure event into a ci-fix task payload.

    Downloads the failed-step log via ``gh run view --log-failed``, parses
    it with :class:`~bernstein.adapters.ci.github_actions.GitHubActionsParser`,
    attributes the failure to the triggering commit, and returns one task
    payload enriched with commit diff context.

    Returns an empty list when:
    - the workflow did not fail (conclusion != ``"failure"``),
    - the log download fails (degraded: no log available),
    - no parseable failures are found.

    Args:
        event: A webhook event with ``event_type == "workflow_run"`` and
            ``action == "completed"``.
        retry_count: Number of previous ci-fix attempts for this branch.
            Passed through to :func:`~bernstein.github_app.ci_router.build_ci_routing_payload`
            for model/effort escalation.
        cwd: Repository root for git operations.  ``None`` means current dir.

    Returns:
        List with at most one task creation dict matching ``TaskCreate`` fields.
    """
    if event.event_type != "workflow_run" or event.action != "completed":
        return []

    run: dict[str, Any] = event.payload.get("workflow_run", {})
    if run.get("conclusion") != "failure":
        return []

    from bernstein.adapters.ci.github_actions import (
        GitHubActionsParser,
        download_github_actions_log,
    )
    from bernstein.github_app.ci_router import (
        MAX_CI_RETRIES,
        CIBlameResult,
        blame_ci_failures,
        build_ci_routing_payload,
    )

    workflow_name: str = run.get("name", "CI")
    head_sha: str = run.get("head_sha", "")
    run_url: str = run.get("html_url", "")

    # Download and parse the CI log.
    raw_log = ""
    try:
        raw_log = download_github_actions_log(run_url)
    except Exception as exc:
        logger.warning("Could not download CI log for %s: %s", run_url[:80], exc)

    failures = GitHubActionsParser().parse(raw_log) if raw_log else []

    if not failures:
        logger.info(
            "workflow_run_to_task: no parseable failures in run %s",
            run_url[:60] or head_sha[:8],
        )
        return []

    # Attribute blame to the triggering commit.
    blame = blame_ci_failures(failures, head_sha, cwd) if head_sha else CIBlameResult(head_sha="")

    payload = build_ci_routing_payload(
        failures=failures,
        blame=blame,
        workflow_name=workflow_name,
        run_url=run_url,
        retry_count=retry_count,
    )

    logger.info(
        "Mapped workflow_run failure to ci-fix task: workflow=%s sha=%s retry=%d/%d",
        workflow_name,
        head_sha[:8],
        retry_count + 1,
        MAX_CI_RETRIES,
    )

    return [payload]


def trigger_label_to_task(event: WebhookEvent) -> dict[str, Any] | None:
    """Convert a ``bernstein`` / ``agent-fix`` label event into a task.

    Only triggers when a ``TRIGGER_LABELS`` label is *added* to an issue that
    is not already assigned as a task.

    Args:
        event: A webhook event with ``event_type == "issues"`` and
            ``action == "labeled"``.

    Returns:
        Task creation dict, or ``None`` if the label is not a trigger label.
    """
    if event.action != "labeled":
        return None

    label: dict[str, Any] = event.payload.get("label", {})
    label_name = label.get("name", "").lower()

    if label_name not in TRIGGER_LABELS:
        return None

    issue: dict[str, Any] = event.payload.get("issue", {})
    title = issue.get("title", "Untitled issue")
    body = issue.get("body", "") or ""
    number = issue.get("number", 0)

    # Infer priority and role from all issue labels
    all_labels = _extract_labels(event.payload)
    priority = _priority_from_labels(all_labels)
    role = _role_from_labels(all_labels)
    scope = "small" if len(body) < 200 else ("large" if len(body) > 1000 else "medium")

    description = (
        f"GitHub issue #{number} assigned to Bernstein via `{label_name}` label "
        f"by @{event.sender} in {event.repo_full_name}.\n\n{body[:2000]}"
    )

    task: dict[str, Any] = {
        "title": f"[GH#{number}] {title}"[:120],
        "description": description,
        "role": role,
        "priority": priority,
        "scope": scope,
        "task_type": "fix" if label_name == "agent-fix" else "standard",
    }

    logger.info(
        "Mapped trigger label %r on issue #%d to task: role=%s priority=%d",
        label_name,
        number,
        role,
        priority,
    )

    return task


def label_to_action(event: WebhookEvent) -> dict[str, Any] | None:
    """Convert a label event for ``evolve-candidate`` into an evolution task.

    Only triggers when the ``evolve-candidate`` label is added to an issue.

    Args:
        event: A webhook event with ``event_type == "issues"`` and
            ``action == "labeled"``.

    Returns:
        Task creation dict for an evolution task, or ``None`` if the label
        is not ``evolve-candidate``.
    """
    if event.action != "labeled":
        return None

    label: dict[str, Any] = event.payload.get("label", {})
    label_name = label.get("name", "").lower()

    if label_name != "evolve-candidate":
        return None

    issue: dict[str, Any] = event.payload.get("issue", {})
    title = issue.get("title", "Untitled")
    body = issue.get("body", "") or ""
    number = issue.get("number", 0)

    description = (
        f"Evolution candidate from GitHub issue #{number} in {event.repo_full_name}.\n\nTitle: {title}\n\n{body[:2000]}"
    )

    task: dict[str, Any] = {
        "title": f"[evolve] {title}"[:120],
        "description": description,
        "role": "backend",
        "priority": 2,
        "scope": "medium",
        "task_type": "upgrade_proposal",
    }

    logger.info(
        "Mapped evolve-candidate label to evolution task: issue=#%d",
        number,
    )

    return task


# ---------------------------------------------------------------------------
# Handler classes — typed wrappers over the functional mappers above
# ---------------------------------------------------------------------------


class IssueHandler:
    """Handles GitHub ``issues`` events and maps them to Bernstein tasks."""

    def handle(self, event: WebhookEvent) -> list[dict[str, Any]]:
        """Process a single issues event.

        Args:
            event: Parsed webhook event.

        Returns:
            List of task creation dicts (may be empty).
        """
        if event.action == "opened":
            return issue_to_tasks(event)
        if event.action == "labeled":
            # Handle both trigger labels and evolve-candidate
            trigger = trigger_label_to_task(event)
            if trigger is not None:
                return [trigger]
            evolve = label_to_action(event)
            if evolve is not None:
                return [evolve]
        return []


class PRCommentHandler:
    """Handles ``pull_request_review_comment`` and ``issue_comment`` events."""

    def handle(self, event: WebhookEvent) -> list[dict[str, Any]]:
        """Process a PR review or issue comment event.

        Checks for actionable review language and slash commands.

        Args:
            event: Parsed webhook event.

        Returns:
            List of task creation dicts (may be empty).
        """
        from bernstein.github_app.slash_commands import parse_slash_command, slash_command_to_task

        comment: dict[str, Any] = event.payload.get("comment", {})
        body = comment.get("body", "") or ""

        # Slash command takes precedence over review heuristic
        parsed = parse_slash_command(body)
        if parsed is not None:
            action, args = parsed
            task = slash_command_to_task(event, action, args)
            if task is not None:
                return [task]
            return []

        task = pr_review_to_task(event)
        if task is not None:
            return [task]
        return []


class PushHandler:
    """Handles GitHub ``push`` events and creates QA verification tasks."""

    def handle(self, event: WebhookEvent) -> list[dict[str, Any]]:
        """Process a push event.

        Args:
            event: Parsed webhook event.

        Returns:
            List of task creation dicts (may be empty).
        """
        return push_to_tasks(event)


class SlashCommandHandler:
    """Handles slash commands from any comment event type.

    A thin pass-through that explicitly handles ``/bernstein`` commands
    regardless of whether the comment is on an issue or PR.
    """

    def handle(self, event: WebhookEvent, comment_body: str) -> dict[str, Any] | None:
        """Parse and convert a slash command from *comment_body*.

        Args:
            event: Parsed webhook event (provides context like repo, sender).
            comment_body: The full comment body text to search for commands.

        Returns:
            Task creation dict, or ``None`` if no command found or unsupported.
        """
        from bernstein.github_app.slash_commands import parse_slash_command, slash_command_to_task

        parsed = parse_slash_command(comment_body)
        if parsed is None:
            return None
        action, args = parsed
        return slash_command_to_task(event, action, args)
