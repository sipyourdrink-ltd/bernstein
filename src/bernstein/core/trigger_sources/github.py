"""GitHub trigger source adapters — normalize GitHub webhook payloads into TriggerEvents."""

from __future__ import annotations

import time
from typing import Any

from bernstein.core.models import TriggerEvent


def normalize_push(payload: dict[str, Any], sender: str, repo: str) -> TriggerEvent:
    """Normalize a GitHub push webhook payload into a TriggerEvent.

    Args:
        payload: Raw GitHub push webhook JSON payload.
        sender: GitHub username that triggered the push.
        repo: Repository full name (owner/repo).

    Returns:
        Normalized TriggerEvent.
    """
    ref = payload.get("ref", "")
    branch = ref.split("/")[-1] if "/" in ref else ref

    commits = payload.get("commits", [])
    commit_messages = "\n".join(c.get("message", "") for c in commits)
    head_commit = payload.get("head_commit", {})
    sha = head_commit.get("id", payload.get("after", ""))

    # Collect all changed files from all commits
    changed_files: list[str] = []
    for commit in commits:
        changed_files.extend(commit.get("added", []))
        changed_files.extend(commit.get("modified", []))
        changed_files.extend(commit.get("removed", []))
    changed_files = list(dict.fromkeys(changed_files))  # dedupe, preserve order

    return TriggerEvent(
        source="github_push",
        timestamp=time.time(),
        raw_payload=payload,
        repo=repo,
        branch=branch,
        sha=sha,
        sender=sender,
        changed_files=tuple(changed_files),
        message=commit_messages,
        metadata={"commit_count": len(commits)},
    )


def normalize_workflow_run(payload: dict[str, Any], sender: str, repo: str) -> TriggerEvent:
    """Normalize a GitHub workflow_run webhook payload into a TriggerEvent.

    Args:
        payload: Raw GitHub workflow_run webhook JSON payload.
        sender: GitHub username that triggered the event.
        repo: Repository full name (owner/repo).

    Returns:
        Normalized TriggerEvent.
    """
    run = payload.get("workflow_run", {})
    conclusion = run.get("conclusion", "")
    workflow_name = run.get("name", "")
    head_branch = run.get("head_branch", "")
    head_sha = run.get("head_sha", "")
    run_url = run.get("html_url", "")

    return TriggerEvent(
        source="github_workflow_run",
        timestamp=time.time(),
        raw_payload=payload,
        repo=repo,
        branch=head_branch,
        sha=head_sha,
        sender=sender,
        message=f"Workflow '{workflow_name}' {conclusion}",
        metadata={
            "conclusion": conclusion,
            "workflow_name": workflow_name,
            "run_url": run_url,
        },
    )


def normalize_issues(payload: dict[str, Any], sender: str, repo: str, action: str) -> TriggerEvent:
    """Normalize a GitHub issues webhook payload into a TriggerEvent.

    Args:
        payload: Raw GitHub issues webhook JSON payload.
        sender: GitHub username that triggered the event.
        repo: Repository full name (owner/repo).
        action: Event action (opened, labeled, etc.).

    Returns:
        Normalized TriggerEvent.
    """
    issue = payload.get("issue", {})
    title = issue.get("title", "")
    body = issue.get("body", "") or ""
    number = issue.get("number", 0)

    return TriggerEvent(
        source="github_issues",
        timestamp=time.time(),
        raw_payload=payload,
        repo=repo,
        sender=sender,
        message=f"#{number}: {title}",
        metadata={
            "action": action,
            "issue_number": number,
            "issue_title": title,
            "issue_body": body[:2000],
        },
    )
