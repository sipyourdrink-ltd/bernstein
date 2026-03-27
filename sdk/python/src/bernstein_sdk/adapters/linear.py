"""Linear adapter — convert Linear issues to Bernstein tasks and sync back.

Linear uses a GraphQL API.  This adapter provides a thin wrapper that keeps
the SDK dependency-free for basic use: webhook payload parsing and state
mapping work without any extra packages.  Fetching and transitioning issues
requires the ``httpx`` package (already a core SDK dependency).

Authentication
--------------
Linear issues Personal API keys and OAuth tokens::

    export LINEAR_API_KEY=lin_api_...
    export LINEAR_TEAM_ID=<team-uuid>

Then::

    from bernstein_sdk.adapters.linear import LinearAdapter
    adapter = LinearAdapter.from_env()

Webhook usage
-------------
Linear fires ``Issue`` webhooks with ``action`` of ``create``, ``update``,
or ``remove``::

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

import httpx

from bernstein_sdk.models import (
    TaskComplexity,
    TaskCreate,
    TaskResponse,
    TaskScope,
    TaskStatus,
)
from bernstein_sdk.state_map import BernsteinToLinear, LinearToBernstein

log = logging.getLogger(__name__)

_LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"

# Linear priority integer → Bernstein priority
_LINEAR_PRIORITY_MAP: dict[int, int] = {
    0: 2,  # No priority
    1: 1,  # Urgent
    2: 1,  # High
    3: 2,  # Normal
    4: 3,  # Low
}


@dataclass
class LinearIssueRef:
    """Minimal Linear issue data consumed by the adapter."""

    identifier: str  # e.g. "ENG-42"
    title: str
    description: str
    state_name: str  # Linear state name (e.g. "In Progress")
    state_type: str  # Linear state type (e.g. "started")
    priority: int  # 0–4 (1=Urgent … 4=Low, 0=No priority)
    estimate: float | None  # cycle estimate (story points)
    labels: list[str]
    team_id: str
    assignee_email: str | None

    @classmethod
    def from_webhook_payload(cls, payload: dict[str, Any]) -> "LinearIssueRef | None":
        """Parse a Linear webhook payload.

        Linear webhook bodies have the shape::

            {
              "action": "create" | "update" | "remove",
              "type": "Issue",
              "data": { ... issue fields ... }
            }

        Returns ``None`` if ``type`` is not ``"Issue"`` or data is missing.
        """
        if payload.get("type") != "Issue":
            return None
        data: dict[str, Any] = payload.get("data", {})
        if not data:
            return None
        return cls._from_data(data)

    @classmethod
    def from_graphql_response(cls, data: dict[str, Any]) -> "LinearIssueRef":
        """Parse an issue node from a Linear GraphQL response."""
        return cls._from_data(data)

    @classmethod
    def _from_data(cls, data: dict[str, Any]) -> "LinearIssueRef":
        state: dict[str, Any] = data.get("state") or {}
        assignee: dict[str, Any] = data.get("assignee") or {}
        team: dict[str, Any] = data.get("team") or {}
        labels_conn: dict[str, Any] = data.get("labels") or {}
        label_nodes: list[dict[str, Any]] = labels_conn.get("nodes", [])
        return cls(
            identifier=data.get("identifier", ""),
            title=data.get("title", ""),
            description=data.get("description", ""),
            state_name=state.get("name", "Todo"),
            state_type=state.get("type", "unstarted"),
            priority=int(data.get("priority", 0)),
            estimate=data.get("estimate"),
            labels=[lbl.get("name", "") for lbl in label_nodes],
            team_id=team.get("id", ""),
            assignee_email=assignee.get("email"),
        )


class LinearAdapter:
    """Convert Linear issues to Bernstein tasks and push state transitions back.

    Args:
        api_key: Linear personal API key or OAuth token.
        default_role: Default Bernstein agent role for created tasks.
        team_id_to_role: Optional mapping from Linear team ID to role.
    """

    def __init__(
        self,
        api_key: str,
        default_role: str = "backend",
        team_id_to_role: dict[str, str] | None = None,
    ) -> None:
        self._api_key = api_key
        self._default_role = default_role
        self._team_id_to_role = team_id_to_role or {}
        self._http = httpx.Client(
            headers={"Authorization": api_key, "Content-Type": "application/json"},
            timeout=15.0,
        )

    @classmethod
    def from_env(cls, default_role: str = "backend") -> "LinearAdapter":
        """Construct from ``LINEAR_API_KEY`` environment variable.

        Raises:
            RuntimeError: If ``LINEAR_API_KEY`` is not set.
        """
        api_key = os.getenv("LINEAR_API_KEY", "")
        if not api_key:
            raise RuntimeError("Missing environment variable: LINEAR_API_KEY")
        return cls(api_key=api_key, default_role=default_role)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "LinearAdapter":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Linear → Bernstein
    # ------------------------------------------------------------------

    def task_from_issue(self, issue: LinearIssueRef) -> TaskCreate:
        """Convert a :class:`LinearIssueRef` to a :class:`TaskCreate`."""
        role = self._team_id_to_role.get(issue.team_id, self._default_role)
        priority = _LINEAR_PRIORITY_MAP.get(issue.priority, 2)
        scope = _estimate_to_scope(issue.estimate)
        complexity = _labels_to_complexity(issue.labels)

        return TaskCreate(
            title=f"[{issue.identifier}] {issue.title}",
            role=role,
            description=issue.description,
            priority=priority,
            scope=scope,
            complexity=complexity,
            external_ref=f"linear:{issue.identifier}",
            metadata={
                "linear_identifier": issue.identifier,
                "linear_state": issue.state_name,
                "linear_state_type": issue.state_type,
                "linear_labels": issue.labels,
            },
        )

    def task_from_webhook(self, payload: dict[str, Any]) -> TaskCreate | None:
        """Convert a Linear webhook payload to a :class:`TaskCreate`.

        Returns ``None`` for ``remove`` actions, non-Issue types, or terminal
        issues (completed/cancelled).
        """
        action = payload.get("action", "")
        if action == "remove":
            return None

        issue_ref = LinearIssueRef.from_webhook_payload(payload)
        if issue_ref is None:
            return None

        # Skip already-terminal issues
        mapped = LinearToBernstein.map(issue_ref.state_type) or LinearToBernstein.map(
            issue_ref.state_name
        )
        if mapped in (TaskStatus.DONE, TaskStatus.CANCELLED):
            log.debug("LinearAdapter: skipping terminal issue %s", issue_ref.identifier)
            return None

        return self.task_from_issue(issue_ref)

    # ------------------------------------------------------------------
    # Bernstein → Linear
    # ------------------------------------------------------------------

    def get_issue(self, identifier: str) -> LinearIssueRef:
        """Fetch a Linear issue by identifier (e.g. ``"ENG-42"``).

        Uses the GraphQL API.

        Raises:
            httpx.HTTPStatusError: On API error.
            ValueError: If the issue is not found.
        """
        query = """
        query GetIssue($identifier: String!) {
          issue(id: $identifier) {
            identifier title description priority estimate
            state { name type }
            assignee { email }
            team { id }
            labels { nodes { name } }
          }
        }
        """
        resp = self._http.post(
            _LINEAR_GRAPHQL_URL,
            json={"query": query, "variables": {"identifier": identifier}},
        )
        resp.raise_for_status()
        body = resp.json()
        issue_data = (body.get("data") or {}).get("issue")
        if not issue_data:
            raise ValueError(f"Linear issue not found: {identifier}")
        return LinearIssueRef.from_graphql_response(issue_data)

    def transition_issue(self, identifier: str, target_state_name: str) -> bool:
        """Update the state of a Linear issue.

        Queries all workflow states for the issue's team, finds one matching
        *target_state_name* (case-insensitive), and applies it via a mutation.

        Args:
            identifier: Linear issue identifier (e.g. ``"ENG-42"``).
            target_state_name: The target workflow state name.

        Returns:
            ``True`` if the transition was applied, ``False`` if the target
            state was not found in the team's workflow.
        """
        # First fetch the issue to get team_id and issue UUID
        issue_query = """
        query GetIssue($identifier: String!) {
          issue(id: $identifier) {
            id team { id }
            state { name }
          }
        }
        """
        resp = self._http.post(
            _LINEAR_GRAPHQL_URL,
            json={"query": issue_query, "variables": {"identifier": identifier}},
        )
        resp.raise_for_status()
        issue_data = (resp.json().get("data") or {}).get("issue")
        if not issue_data:
            log.warning("LinearAdapter: issue %s not found", identifier)
            return False

        issue_uuid: str = issue_data["id"]
        team_id: str = issue_data["team"]["id"]

        # Look up workflow states for the team
        states_query = """
        query GetStates($teamId: String!) {
          workflowStates(filter: { team: { id: { eq: $teamId } } }) {
            nodes { id name type }
          }
        }
        """
        resp = self._http.post(
            _LINEAR_GRAPHQL_URL,
            json={"query": states_query, "variables": {"teamId": team_id}},
        )
        resp.raise_for_status()
        states: list[dict[str, Any]] = (
            (resp.json().get("data") or {}).get("workflowStates", {}).get("nodes", [])
        )
        target_lower = target_state_name.lower()
        state_id: str | None = next(
            (s["id"] for s in states if s["name"].lower() == target_lower),
            None,
        )
        if state_id is None:
            log.warning(
                "LinearAdapter: state %r not found for team %s (available: %s)",
                target_state_name,
                team_id,
                [s["name"] for s in states],
            )
            return False

        # Apply the transition
        mutation = """
        mutation UpdateIssue($id: String!, $stateId: String!) {
          issueUpdate(id: $id, input: { stateId: $stateId }) {
            success
          }
        }
        """
        resp = self._http.post(
            _LINEAR_GRAPHQL_URL,
            json={
                "query": mutation,
                "variables": {"id": issue_uuid, "stateId": state_id},
            },
        )
        resp.raise_for_status()
        success: bool = (
            (resp.json().get("data") or {}).get("issueUpdate", {}).get("success", False)
        )
        if success:
            log.info(
                "LinearAdapter: transitioned %s → %r", identifier, target_state_name
            )
        return success

    def sync_task_to_linear(self, task: TaskResponse) -> bool:
        """Transition the Linear issue linked to *task* to match task state.

        Reads the issue identifier from ``task.external_ref`` (format:
        ``"linear:ENG-42"``).  Returns ``False`` if the ref is missing.
        """
        if not task.external_ref.startswith("linear:"):
            log.debug(
                "LinearAdapter.sync_task_to_linear: no linear ref on task %s", task.id
            )
            return False
        identifier = task.external_ref[len("linear:") :]
        target_state = BernsteinToLinear.map(task.status)
        return self.transition_issue(identifier, target_state)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _estimate_to_scope(estimate: float | None) -> TaskScope:
    if estimate is None:
        return TaskScope.MEDIUM
    if estimate <= 2:
        return TaskScope.SMALL
    if estimate <= 5:
        return TaskScope.MEDIUM
    return TaskScope.LARGE


def _labels_to_complexity(labels: list[str]) -> TaskComplexity:
    lower = {lbl.lower() for lbl in labels}
    if lower & {
        "complex",
        "high-complexity",
        "architecture",
        "security",
        "performance",
    }:
        return TaskComplexity.HIGH
    if lower & {"simple", "easy", "docs", "documentation", "chore"}:
        return TaskComplexity.LOW
    return TaskComplexity.MEDIUM
