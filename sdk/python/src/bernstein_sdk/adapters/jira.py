"""Jira Cloud adapter — convert Jira issues to Bernstein tasks and sync back.

This adapter handles two directions:

1. **Jira → Bernstein**: Convert a Jira issue (from the REST API or a webhook
   payload) into a :class:`~bernstein_sdk.models.TaskCreate` ready to post.
2. **Bernstein → Jira**: Transition a Jira issue when a Bernstein task changes
   state using the ``/rest/api/3/issue/{key}/transitions`` endpoint.

Authentication
--------------
Jira Cloud uses HTTP Basic with an API token::

    export JIRA_BASE_URL=https://your-org.atlassian.net
    export JIRA_EMAIL=you@example.com
    export JIRA_API_TOKEN=<token>

Then::

    from bernstein_sdk.adapters.jira import JiraAdapter
    adapter = JiraAdapter.from_env()

Webhook usage
-------------
If you receive Jira webhook payloads (e.g. ``issue_updated`` events) and want
to create/update Bernstein tasks automatically, call
:meth:`JiraAdapter.task_from_webhook`::

    payload = request.json()
    task_create = adapter.task_from_webhook(payload)
    if task_create:
        client.create_task(**vars(task_create))
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from bernstein_sdk.models import (
    TaskComplexity,
    TaskCreate,
    TaskResponse,
    TaskScope,
    TaskStatus,
)
from bernstein_sdk.state_map import BernsteinToJira, JiraToBernstein

log = logging.getLogger(__name__)

# Jira priority name → Bernstein priority integer
_JIRA_PRIORITY_MAP: dict[str, int] = {
    "highest": 1,
    "high": 1,
    "medium": 2,
    "low": 3,
    "lowest": 3,
}

# Jira story point estimate (or story_points field) → Scope
_STORY_POINTS_TO_SCOPE: list[tuple[int, TaskScope]] = [
    (3, TaskScope.SMALL),
    (8, TaskScope.MEDIUM),
    (999, TaskScope.LARGE),
]


@dataclass
class JiraIssueRef:
    """Minimal Jira issue data used by the adapter.

    Not a full Jira API model — just the fields Bernstein cares about.
    """

    key: str  # e.g. "PROJ-42"
    summary: str  # issue title
    description: str  # plain-text body (stripped of ADF/Markdown)
    status: str  # current Jira status name
    priority: str  # Jira priority name
    story_points: float | None
    labels: list[str]
    assignee_email: str | None

    @classmethod
    def from_api_response(cls, data: dict[str, Any]) -> JiraIssueRef:
        """Parse a Jira ``GET /rest/api/3/issue/{key}`` response."""
        fields: dict[str, Any] = data.get("fields", {})
        desc_raw = fields.get("description") or {}
        desc_text = (
            _extract_adf_text(desc_raw) if isinstance(desc_raw, dict) else str(desc_raw)
        )
        priority_obj = fields.get("priority") or {}
        assignee_obj = fields.get("assignee") or {}
        return cls(
            key=data.get("key", ""),
            summary=fields.get("summary", ""),
            description=desc_text,
            status=(fields.get("status") or {}).get("name", "open"),
            priority=(priority_obj.get("name") or "medium").lower(),
            story_points=fields.get("story_points") or fields.get("customfield_10016"),
            labels=fields.get("labels") or [],
            assignee_email=(assignee_obj.get("emailAddress") or None),
        )

    @classmethod
    def from_webhook_payload(cls, payload: dict[str, Any]) -> JiraIssueRef | None:
        """Parse a Jira webhook ``issue_created`` / ``issue_updated`` payload.

        Returns ``None`` if the payload doesn't contain an issue.
        """
        issue = payload.get("issue")
        if not issue:
            return None
        return cls.from_api_response(issue)


class JiraAdapter:
    """Convert Jira issues to Bernstein tasks and push state transitions back.

    Args:
        base_url: Jira Cloud base URL (e.g. ``https://org.atlassian.net``).
        email: Jira account email.
        api_token: Jira API token (from ``id.atlassian.com``).
        default_role: Bernstein agent role for tasks created from Jira issues.
        project_key_to_role: Optional mapping from Jira project key to role.
    """

    def __init__(
        self,
        base_url: str,
        email: str,
        api_token: str,
        default_role: str = "backend",
        project_key_to_role: dict[str, str] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth = (email, api_token)
        self._default_role = default_role
        self._project_key_to_role = project_key_to_role or {}

    @staticmethod
    def _validate_issue_key(key: str) -> str:
        """Validate that an issue key matches the expected Jira format."""
        import re
        if not re.match(r"^[A-Z][A-Z0-9_]+-\d+$", key):
            raise ValueError(f"Invalid Jira issue key format: {key!r}")
        return key

    @classmethod
    def from_env(cls, default_role: str = "backend") -> JiraAdapter:
        """Construct from environment variables.

        Reads ``JIRA_BASE_URL``, ``JIRA_EMAIL``, and ``JIRA_API_TOKEN``.

        Raises:
            RuntimeError: If any required env var is missing.
        """
        url = os.getenv("JIRA_BASE_URL", "")
        email = os.getenv("JIRA_EMAIL", "")
        token = os.getenv("JIRA_API_TOKEN", "")
        missing = [
            k
            for k, v in [
                ("JIRA_BASE_URL", url),
                ("JIRA_EMAIL", email),
                ("JIRA_API_TOKEN", token),
            ]
            if not v
        ]
        if missing:
            raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")
        return cls(
            base_url=url, email=email, api_token=token, default_role=default_role
        )

    # ------------------------------------------------------------------
    # Jira → Bernstein
    # ------------------------------------------------------------------

    def task_from_issue(self, issue: JiraIssueRef) -> TaskCreate:
        """Convert a :class:`JiraIssueRef` to a :class:`TaskCreate`.

        Args:
            issue: Parsed Jira issue data.

        Returns:
            A :class:`TaskCreate` ready to submit to ``POST /tasks``.
        """
        project_key = issue.key.rsplit("-", 1)[0] if "-" in issue.key else ""
        role = self._project_key_to_role.get(project_key, self._default_role)
        priority = _JIRA_PRIORITY_MAP.get(issue.priority, 2)
        scope = _story_points_to_scope(issue.story_points)
        # Infer complexity from labels or default to medium
        complexity = _labels_to_complexity(issue.labels)

        return TaskCreate(
            title=f"[{issue.key}] {issue.summary}",
            role=role,
            description=issue.description,
            priority=priority,
            scope=scope,
            complexity=complexity,
            external_ref=f"jira:{issue.key}",
            metadata={
                "jira_key": issue.key,
                "jira_status": issue.status,
                "jira_labels": issue.labels,
            },
        )

    def task_from_webhook(self, payload: dict[str, Any]) -> TaskCreate | None:
        """Convert a Jira webhook payload to a :class:`TaskCreate`.

        Handles ``issue_created`` and ``issue_updated`` events.

        Returns ``None`` if the payload doesn't contain a usable issue.
        """
        issue_ref = JiraIssueRef.from_webhook_payload(payload)
        if issue_ref is None:
            log.debug("JiraAdapter: no issue found in webhook payload")
            return None
        # Skip issues that are already terminal states
        mapped = JiraToBernstein.map(issue_ref.status)
        if mapped in (TaskStatus.DONE, TaskStatus.CANCELLED):
            log.debug("JiraAdapter: skipping terminal Jira issue %s", issue_ref.key)
            return None
        return self.task_from_issue(issue_ref)

    # ------------------------------------------------------------------
    # Bernstein → Jira
    # ------------------------------------------------------------------

    def get_issue(self, issue_key: str) -> JiraIssueRef:
        """Fetch a Jira issue by key.

        Requires ``requests`` to be installed (``pip install bernstein-sdk[jira]``).

        Raises:
            ImportError: If ``requests`` is not installed.
            requests.HTTPError: On API error.
        """
        requests = _import_requests()
        url = f"{self._base_url}/rest/api/3/issue/{issue_key}"
        resp = requests.get(
            url, auth=self._auth, headers={"Accept": "application/json"}, timeout=10
        )
        resp.raise_for_status()
        return JiraIssueRef.from_api_response(resp.json())

    def transition_issue(self, issue_key: str, target_status_name: str) -> bool:
        """Transition a Jira issue to *target_status_name*.

        Looks up the available transitions for the issue and fires the first
        one whose name matches *target_status_name* (case-insensitive).

        Args:
            issue_key: Jira issue key (e.g. ``"PROJ-42"``).
            target_status_name: The Jira status name to transition to.

        Returns:
            ``True`` if the transition was applied, ``False`` if no matching
            transition was found.

        Raises:
            requests.HTTPError: On API error.
        """
        requests = _import_requests()

        def _s(v: object) -> str:
            return str(v).replace("\n", "\\n").replace("\r", "\\r")

        issue_key = self._validate_issue_key(issue_key)
        transitions_url = f"{self._base_url}/rest/api/3/issue/{issue_key}/transitions"
        resp = requests.get(
            transitions_url,
            auth=self._auth,
            headers={"Accept": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        transitions: list[dict[str, Any]] = resp.json().get("transitions", [])

        target_lower = target_status_name.lower()
        transition_id: str | None = None
        for t in transitions:
            name: str = t.get("name", "")
            if name.lower() == target_lower:
                transition_id = t["id"]
                break

        if transition_id is None:
            log.warning(
                "JiraAdapter: no transition to %r found for %s (available: %s)",
                _s(target_status_name),
                _s(issue_key),
                [_s(t.get("name", "")) for t in transitions],
            )
            return False

        resp = requests.post(
            transitions_url,
            auth=self._auth,
            headers={"Content-Type": "application/json"},
            json={"transition": {"id": transition_id}},
            timeout=10,
        )
        resp.raise_for_status()
        log.info(
            "JiraAdapter: transitioned %s → %r",
            _s(issue_key),
            _s(target_status_name),
        )
        return True

    def sync_task_to_jira(self, task: TaskResponse) -> bool:
        """Transition the Jira issue linked to *task* to match task state.

        The issue key is read from ``task.external_ref`` (expected format:
        ``"jira:PROJ-42"``).  If the ref is missing or in a different format,
        the method logs a warning and returns ``False``.

        Args:
            task: Completed or updated Bernstein task.

        Returns:
            ``True`` if a Jira transition was applied, ``False`` otherwise.
        """
        if not task.external_ref.startswith("jira:"):
            log.debug("JiraAdapter.sync_task_to_jira: no jira ref on task %s", task.id)
            return False
        issue_key = task.external_ref[len("jira:") :]
        target_jira_status = BernsteinToJira.map(task.status)
        return self.transition_issue(issue_key, target_jira_status)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _story_points_to_scope(points: float | None) -> TaskScope:
    if points is None:
        return TaskScope.MEDIUM
    for threshold, scope in _STORY_POINTS_TO_SCOPE:
        if points <= threshold:
            return scope
    return TaskScope.LARGE


def _labels_to_complexity(labels: list[str]) -> TaskComplexity:
    lower_labels = {lbl.lower() for lbl in labels}
    if lower_labels & {"complex", "high-complexity", "architecture", "security"}:
        return TaskComplexity.HIGH
    if lower_labels & {"simple", "easy", "docs", "documentation"}:
        return TaskComplexity.LOW
    return TaskComplexity.MEDIUM


def _extract_adf_text(adf: dict[str, Any], _depth: int = 0) -> str:
    """Recursively extract plain text from a Jira ADF node."""
    if _depth > 20:
        return ""
    text = adf.get("text", "")
    children: list[dict[str, Any]] = adf.get("content", [])
    parts = [text] + [_extract_adf_text(child, _depth + 1) for child in children]
    return " ".join(p for p in parts if p).strip()


def _import_requests() -> Any:
    try:
        import requests  # type: ignore[import-untyped]

        return requests
    except ImportError as exc:
        raise ImportError(
            "The Jira adapter requires 'requests'. Install it with: "
            "pip install bernstein-sdk[jira]"
        ) from exc
