"""Slash command parser for /bernstein comments on GitHub issues and PRs.

Parses ``/bernstein <action> [args]`` from comment bodies and converts them
into Bernstein task payloads.

Supported actions:
- ``fix [description]`` — create a targeted fix task for the current issue/PR
- ``plan [description]`` — create a planning task that decomposes the work
- ``evolve [description]`` — create an evolution/upgrade proposal task
- ``qa [description]`` — create a QA verification task
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bernstein.github_app.webhooks import WebhookEvent

logger = logging.getLogger(__name__)

# Matches /bernstein <action> [rest of line]
_SLASH_RE = re.compile(r"^\s*/bernstein\s+(\w+)(?:\s+(.+))?$", re.MULTILINE | re.IGNORECASE)

# Supported actions and their task_type / role mappings
_ACTION_MAP: dict[str, dict[str, str]] = {
    "fix": {"task_type": "fix", "role": "backend"},
    "plan": {"task_type": "planning", "role": "manager"},
    "evolve": {"task_type": "upgrade_proposal", "role": "backend"},
    "qa": {"task_type": "standard", "role": "qa"},
    "review": {"task_type": "standard", "role": "qa"},
}


def parse_slash_command(text: str) -> tuple[str, str] | None:
    """Extract the first ``/bernstein`` command from *text*.

    Args:
        text: Comment body from GitHub (may be multi-line).

    Returns:
        ``(action, args)`` tuple where *action* is the lowercased command word
        and *args* is the remainder of the line (stripped).  Returns ``None``
        if no slash command is found.
    """
    match = _SLASH_RE.search(text)
    if match is None:
        return None
    action = match.group(1).lower()
    args = (match.group(2) or "").strip()
    return (action, args)


def slash_command_to_task(
    event: WebhookEvent,
    action: str,
    args: str,
) -> dict[str, Any] | None:
    """Build a Bernstein task payload from a ``/bernstein`` slash command.

    Args:
        event: The webhook event that contained the slash command.
        action: Command action word (``fix``, ``plan``, ``evolve``, ``qa``).
        args: Optional arguments / description from the command line.

    Returns:
        Task creation dict compatible with ``TaskCreate`` fields, or ``None``
        if the action is not recognised.
    """
    spec = _ACTION_MAP.get(action)
    if spec is None:
        logger.info("Unknown /bernstein action %r — ignoring", action)
        return None

    # Determine context from event payload
    issue: dict[str, Any] = event.payload.get("issue", {})
    pr: dict[str, Any] = event.payload.get("pull_request", {})
    comment: dict[str, Any] = event.payload.get("comment", {})

    issue_number = issue.get("number") or pr.get("number", 0)
    issue_title = issue.get("title") or pr.get("title", "")
    comment_body = comment.get("body", "") or ""

    # Build description from available context
    args_line = f" — {args}" if args else ""
    description = (
        f"Slash command `/bernstein {action}`{args_line} by @{event.sender} "
        f"on #{issue_number} in {event.repo_full_name}.\n\n"
        f"Issue/PR: {issue_title}\n\n"
        f"Comment context:\n{comment_body[:1000]}"
    )

    if args:
        title = f"[/bernstein {action}] {args}"[:120]
    elif issue_title:
        title = f"[/bernstein {action}] {issue_title}"[:120]
    else:
        title = f"[/bernstein {action}] #{issue_number}"

    priority = 1 if action in ("fix",) else 2

    task: dict[str, Any] = {
        "title": title,
        "description": description,
        "role": spec["role"],
        "priority": priority,
        "scope": "small",
        "task_type": spec["task_type"],
    }

    logger.info(
        "Slash command /bernstein %s → task: role=%s priority=%d repo=%s",
        action,
        spec["role"],
        priority,
        event.repo_full_name,
    )

    return task
