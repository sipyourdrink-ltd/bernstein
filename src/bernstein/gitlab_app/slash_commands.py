"""Slash command parser for ``/bernstein`` on GitLab MR / issue notes.

Mirror of :mod:`bernstein.github_app.slash_commands` for GitLab.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from bernstein.core.tasks.instruction_provenance import (
    SPAN_ORIGIN_EXTERNAL,
    SPAN_ORIGIN_REPOSITORY,
    InstructionSpan,
    make_span,
    render_instruction,
    spans_to_metadata,
)

if TYPE_CHECKING:
    from bernstein.gitlab_app.webhooks import GitLabWebhookEvent

logger = logging.getLogger(__name__)

# Matches /bernstein <action> [rest of line]
_SLASH_RE = re.compile(
    r"^[ \t]*/bernstein[ \t]+(\w+)(?:[ \t]+([^\r\n]*))?$",
    re.MULTILINE | re.IGNORECASE,
)

# Supported actions and their task_type / role mappings.
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
        text: Comment / note body from GitLab.

    Returns:
        ``(action, args)`` tuple where *action* is the lowercased word
        and *args* is the remainder of the line.  ``None`` when no
        slash command is present.
    """
    match = _SLASH_RE.search(text)
    if match is None:
        return None
    action = match.group(1).lower()
    args = (match.group(2) or "").strip()
    return (action, args)


def slash_command_to_task(
    event: GitLabWebhookEvent,
    action: str,
    args: str,
) -> dict[str, Any] | None:
    """Build a Bernstein task payload from a slash command.

    Args:
        event: The webhook event that carried the command.
        action: Command verb.
        args: Optional argument string.

    Returns:
        Task creation dict, or ``None`` if the verb is unsupported.
    """
    spec = _ACTION_MAP.get(action)
    if spec is None:
        logger.info("Unknown /bernstein action %r - ignoring", action)
        return None

    mr: dict[str, Any] = event.payload.get("merge_request", {}) or {}
    issue: dict[str, Any] = event.payload.get("issue", {}) or {}
    attrs: dict[str, Any] = event.payload.get("object_attributes", {})

    iid = int(mr.get("iid") or issue.get("iid") or 0)
    target_title = str(mr.get("title") or issue.get("title") or "")
    note_body = str(attrs.get("note", "") or "")

    spans: list[InstructionSpan] = [make_span(f"Slash command `/bernstein {action}`", SPAN_ORIGIN_REPOSITORY)]
    if args:
        spans.append(make_span(" - ", SPAN_ORIGIN_REPOSITORY))
        spans.append(make_span(args, SPAN_ORIGIN_EXTERNAL))
    spans.append(
        make_span(
            f" by @{event.sender} on !{iid} in {event.project_path}.\n\nMR/Issue: ",
            SPAN_ORIGIN_REPOSITORY,
        )
    )
    spans.append(make_span(target_title, SPAN_ORIGIN_EXTERNAL))
    spans.append(make_span("\n\nNote context:\n", SPAN_ORIGIN_REPOSITORY))
    spans.append(make_span(note_body[:1000], SPAN_ORIGIN_EXTERNAL))

    if args:
        title = f"[/bernstein {action}] {args}"[:120]
    elif target_title:
        title = f"[/bernstein {action}] {target_title}"[:120]
    else:
        title = f"[/bernstein {action}] !{iid}"

    priority = 1 if action == "fix" else 2

    task: dict[str, Any] = {
        "title": title,
        "description": render_instruction(spans),
        "role": spec["role"],
        "priority": priority,
        "scope": "small",
        "task_type": spec["task_type"],
        "metadata": spans_to_metadata(spans),
    }

    logger.info(
        "GitLab /bernstein %s → task: role=%s priority=%d project=%s",
        action,
        spec["role"],
        priority,
        event.project_path,
    )

    return task
