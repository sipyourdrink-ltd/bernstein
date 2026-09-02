"""Event-to-task conversion: maps GitLab webhook events to Bernstein task payloads.

Mirror of :mod:`bernstein.github_app.mapper` for GitLab.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from bernstein.core.tasks.instruction_provenance import (
    SPAN_ORIGIN_EXTERNAL,
    SPAN_ORIGIN_REPOSITORY,
    make_span,
    render_instruction,
    spans_to_metadata,
)

if TYPE_CHECKING:
    from bernstein.gitlab_app.webhooks import GitLabWebhookEvent

logger = logging.getLogger(__name__)

# Labels that trigger automatic Bernstein task creation on MR / issue.
TRIGGER_LABELS: frozenset[str] = frozenset({"bernstein", "agent-fix", "agent-task"})

# Label → priority mapping (lower = higher priority).
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

# Label → role mapping.
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

# Patterns indicating an actionable MR note.
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
    """Extract label names from an MR / issue payload."""
    raw: object = payload.get("labels", [])
    if not isinstance(raw, list):
        return []
    raw_typed: list[Any] = raw  # type: ignore[assignment]
    out: list[str] = []
    for lbl_item in raw_typed:
        if isinstance(lbl_item, dict):
            lbl_dict: dict[str, Any] = lbl_item  # type: ignore[assignment]
            title: Any = lbl_dict.get("title") or lbl_dict.get("name") or ""
            if title:
                out.append(str(title).lower())
        elif isinstance(lbl_item, str):
            out.append(lbl_item.lower())
    return out


def _priority_from_labels(labels: list[str]) -> int:
    for label in labels:
        if label in _LABEL_PRIORITY:
            return _LABEL_PRIORITY[label]
    return 2


def _role_from_labels(labels: list[str]) -> str:
    for label in labels:
        if label in _LABEL_ROLE:
            return _LABEL_ROLE[label]
    return "backend"


def _scope_from_body(body: str) -> str:
    if len(body) < 200:
        return "small"
    if len(body) > 1000:
        return "large"
    return "medium"


def _is_actionable(text: str) -> bool:
    return any(pattern.search(text) for pattern in _ACTIONABLE_PATTERNS)


def merge_request_to_tasks(event: GitLabWebhookEvent) -> list[dict[str, Any]]:
    """Convert a ``Merge Request Hook`` event into Bernstein task payloads.

    Only fires for ``action == "open"``.  Labels drive role / priority
    and the description is truncated to 2000 chars.

    Args:
        event: Parsed GitLab webhook event.

    Returns:
        List with at most one task creation dict.
    """
    if event.object_kind != "merge_request":
        return []
    if event.action not in {"open", "reopen"}:
        return []

    attrs: dict[str, Any] = event.payload.get("object_attributes", {})
    title = str(attrs.get("title", "Untitled merge request"))
    body = str(attrs.get("description", "") or "")
    iid = int(attrs.get("iid", 0))

    labels = _extract_labels(attrs)
    priority = _priority_from_labels(labels)
    role = _role_from_labels(labels)
    scope = _scope_from_body(body)

    spans = [
        make_span(
            f"GitLab merge request !{iid} from @{event.sender} in {event.project_path}.\n\n",
            SPAN_ORIGIN_REPOSITORY,
        ),
        make_span(body[:2000], SPAN_ORIGIN_EXTERNAL),
    ]

    task: dict[str, Any] = {
        "title": f"[GL-MR!{iid}] {title}"[:120],
        "description": render_instruction(spans),
        "role": role,
        "priority": priority,
        "scope": scope,
        "task_type": "standard",
        "metadata": spans_to_metadata(spans),
    }

    logger.info(
        "Mapped MR !%d to task: role=%s priority=%d scope=%s",
        iid,
        role,
        priority,
        scope,
    )

    return [task]


def note_to_task(event: GitLabWebhookEvent) -> dict[str, Any] | None:
    """Convert a ``Note Hook`` event (MR or issue comment) into a task.

    Falls through to the slash-command parser when the comment contains
    a ``/bernstein`` invocation.  Otherwise builds a fix task when the
    note body contains actionable review language.

    Args:
        event: Parsed GitLab webhook event with
            ``object_kind == "note"``.

    Returns:
        Task creation dict, or ``None`` when the note has neither a
        recognised slash command nor actionable language.
    """
    if event.object_kind != "note":
        return None

    attrs: dict[str, Any] = event.payload.get("object_attributes", {})
    note_body = str(attrs.get("note", "") or "")
    noteable_type = str(attrs.get("noteable_type", "")).lower()

    # Slash command takes precedence.
    from bernstein.gitlab_app.slash_commands import parse_slash_command, slash_command_to_task

    parsed = parse_slash_command(note_body)
    if parsed is not None:
        action, args = parsed
        return slash_command_to_task(event, action, args)

    if not _is_actionable(note_body):
        return None

    mr: dict[str, Any] = event.payload.get("merge_request", {}) or {}
    issue: dict[str, Any] = event.payload.get("issue", {}) or {}
    target = mr or issue
    iid = int(target.get("iid", 0))
    target_title = str(target.get("title", ""))

    role_hint = "qa" if "merge" in noteable_type else "backend"

    spans = [
        make_span(f"GitLab MR note on {noteable_type or 'item'} !{iid} (", SPAN_ORIGIN_REPOSITORY),
        make_span(target_title, SPAN_ORIGIN_EXTERNAL),
        make_span(f") in {event.project_path} by @{event.sender}.\n\nNote:\n", SPAN_ORIGIN_REPOSITORY),
        make_span(note_body[:2000], SPAN_ORIGIN_EXTERNAL),
    ]

    task: dict[str, Any] = {
        "title": f"[GL-MR!{iid}] Fix: {note_body[:80]}"[:120],
        "description": render_instruction(spans),
        "role": role_hint,
        "priority": 1,
        "scope": "small",
        "task_type": "fix",
        "metadata": spans_to_metadata(spans),
    }

    logger.info(
        "Mapped GitLab MR note to fix task: iid=%d role=%s",
        iid,
        role_hint,
    )

    return task


def pipeline_to_tasks(
    event: GitLabWebhookEvent,
    retry_count: int = 0,
    token: str = "",
) -> list[dict[str, Any]]:
    """Convert a failed ``Pipeline Hook`` into a ci-fix task payload.

    When *token* is provided we attempt to fetch the trace of the first
    failed job, parse it via :class:`~bernstein.adapters.ci.gitlab_ci.GitLabCIParser`
    and enrich the resulting task with concrete failure summaries.
    When the trace cannot be fetched (no token / network failure) the
    task is still created but uses only the high-level pipeline info.

    Args:
        event: Parsed GitLab webhook event.
        retry_count: Previous ci-fix attempt count for this branch.
        token: Optional GitLab API token to fetch job traces.

    Returns:
        List with at most one task creation dict.
    """
    if event.object_kind != "pipeline":
        return []

    attrs: dict[str, Any] = event.payload.get("object_attributes", {})
    status = str(attrs.get("status", ""))
    if status != "failed":
        return []

    builds: list[dict[str, Any]] = event.payload.get("builds", []) or []
    failed_builds = [b for b in builds if str(b.get("status", "")) in {"failed", "canceled"}]

    from bernstein.gitlab_app.ci_router import (
        GitLabCIBlameResult,
        build_pipeline_routing_payload,
        fetch_and_parse_failures,
    )

    project_id = event.payload.get("project", {}).get("id", "")

    failures = fetch_and_parse_failures(
        project_id=project_id,
        failed_builds=failed_builds,
        token=token,
    )

    head_sha = str(attrs.get("sha", ""))
    ref = str(attrs.get("ref", ""))
    pipeline_url = str(attrs.get("url", ""))
    pipeline_id = int(attrs.get("id", 0))

    blame = GitLabCIBlameResult(
        head_sha=head_sha,
        ref=ref,
        responsible_files=[f for failure in failures for f in failure.affected_files][:10],
    )

    payload = build_pipeline_routing_payload(
        failures=failures,
        blame=blame,
        pipeline_id=pipeline_id,
        pipeline_url=pipeline_url,
        failed_builds=failed_builds,
        retry_count=retry_count,
    )

    logger.info(
        "Mapped GitLab pipeline failure to ci-fix task: pipeline=%d ref=%s retry=%d",
        pipeline_id,
        ref,
        retry_count + 1,
    )

    return [payload]
