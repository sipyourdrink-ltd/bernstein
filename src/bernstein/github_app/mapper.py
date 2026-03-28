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
    from bernstein.github_app.webhooks import WebhookEvent

logger = logging.getLogger(__name__)

# Label → priority mapping (lower = higher priority)
_LABEL_PRIORITY: dict[str, int] = {
    "bug": 1,
    "critical": 1,
    "security": 1,
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
