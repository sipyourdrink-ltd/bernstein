"""Claude Code Routine trigger source — normalizes Routine webhook payloads."""

from __future__ import annotations

import time
from typing import Any

from bernstein.core.tasks.models import TriggerEvent


def normalize_routine_webhook(
    _headers: dict[str, str],
    payload: dict[str, Any],
) -> TriggerEvent:
    """Normalize a Claude Code Routine webhook payload into a TriggerEvent.

    Called when a Routine session POSTs to Bernstein's webhook endpoint.
    The Routine typically includes GitHub context (PR number, branch, etc.)
    and optionally a scenario_id to invoke.
    """
    # Extract GitHub context if present
    github_ctx = payload.get("github", {})
    scenario_id = payload.get("scenario_id", "")
    goal = payload.get("goal", "")

    metadata: dict[str, Any] = {
        "source_type": "routine",
        "routine_session_id": payload.get("session_id", ""),
        "routine_session_url": payload.get("session_url", ""),
    }

    if github_ctx:
        metadata["github_event"] = github_ctx.get("event_type", "")
        metadata["github_repo"] = github_ctx.get("repo", "")
        metadata["github_pr_number"] = github_ctx.get("pr_number")
        metadata["github_ref"] = github_ctx.get("ref", "")
        metadata["github_author"] = github_ctx.get("author", "")

    if scenario_id:
        metadata["scenario_id"] = scenario_id

    message = goal or payload.get("text", "") or str(payload.get("message", ""))

    return TriggerEvent(
        source="routine",
        timestamp=time.time(),
        raw_payload=payload,
        message=message[:500],
        metadata=metadata,
    )


def extract_github_context(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract structured GitHub context from a Routine webhook payload.

    Returns a dict with pr_number, pr_title, repo, ref, author, labels,
    changed_files — useful for task decomposition.
    """
    gh = payload.get("github", {})
    return {
        "event_type": gh.get("event_type", ""),
        "repo": gh.get("repo", ""),
        "ref": gh.get("ref", ""),
        "pr_number": gh.get("pr_number"),
        "pr_title": gh.get("pr_title", ""),
        "pr_body": gh.get("pr_body", ""),
        "issue_number": gh.get("issue_number"),
        "issue_title": gh.get("issue_title", ""),
        "author": gh.get("author", ""),
        "labels": gh.get("labels", []),
        "changed_files": gh.get("changed_files", []),
    }
