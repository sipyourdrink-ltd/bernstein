"""Linear/Asana/Shortcut project management sync.

Build API request payloads for syncing Bernstein tasks with external
project management tools. This module constructs requests and parses
responses but never makes actual HTTP calls — callers use their own
HTTP client.

Supported providers:
- **Linear** — GraphQL API (``https://api.linear.app/graphql``)
- **Asana** — REST API (``https://app.asana.com/api/1.0``)
- **Shortcut** — REST API (``https://api.app.shortcut.com/api/v3``)

Usage::

    from bernstein.core.protocols.pm_sync import (
        PMClient,
        PMProvider,
        PMSyncConfig,
        PMTask,
        convert_bernstein_status,
        render_sync_report,
    )

    config = PMSyncConfig(
        provider=PMProvider.LINEAR,
        api_key_env="LINEAR_API_KEY",
        project_id="proj-123",
    )

    client = PMClient()
    req = client.build_list_tasks_request(config)
    # => {"method": "POST", "url": ..., "headers": ..., "body": ...}
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class PMProvider(StrEnum):
    """Supported project management providers."""

    LINEAR = "linear"
    ASANA = "asana"
    SHORTCUT = "shortcut"


class PMTaskStatus(StrEnum):
    """Normalized task status across all providers."""

    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PMTask:
    """A normalised task from a project management provider.

    Attributes:
        provider_id: The task's unique ID within the provider.
        title: Short summary of the task.
        description: Full description or body text.
        status: Normalised status.
        assignee: Display name or email of the assignee, if any.
        priority: Provider-specific priority label, if any.
        labels: Immutable sequence of label/tag names.
        url: Web URL to view the task in the provider UI, if known.
    """

    provider_id: str
    title: str
    description: str
    status: PMTaskStatus
    assignee: str | None = None
    priority: str | None = None
    labels: tuple[str, ...] = ()
    url: str | None = None


@dataclass(frozen=True)
class PMSyncConfig:
    """Configuration for connecting to a PM provider.

    Attributes:
        provider: Which PM tool to sync with.
        api_key_env: Name of the environment variable holding the API key.
        project_id: Project or team identifier in the provider.
        workspace_id: Workspace/organization identifier, if required.
    """

    provider: PMProvider
    api_key_env: str
    project_id: str
    workspace_id: str | None = None


@dataclass(frozen=True)
class PMSyncResult:
    """Summary of a sync operation.

    Attributes:
        created: Number of tasks newly created in the provider.
        updated: Number of tasks whose status was updated.
        skipped: Number of tasks left unchanged.
        errors: Immutable sequence of error descriptions.
    """

    created: int
    updated: int
    skipped: int
    errors: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# API URL helpers
# ---------------------------------------------------------------------------

_API_BASE_URLS: dict[PMProvider, str] = {
    PMProvider.LINEAR: "https://api.linear.app",
    PMProvider.ASANA: "https://app.asana.com/api/1.0",
    PMProvider.SHORTCUT: "https://api.app.shortcut.com/api/v3",
}

# Shared string constants to avoid duplication (Sonar S1192).
_CONTENT_TYPE_JSON = "application/json"
_GRAPHQL_ENDPOINT = "/graphql"
_CAST_DICT_STR_ANY = "dict[str, Any]"
_CAST_LIST_DICT_STR_ANY = "list[dict[str, Any]]"

# ---------------------------------------------------------------------------
# PMClient
# ---------------------------------------------------------------------------


class PMClient:
    """Builds API request dicts for PM providers.

    Every ``build_*`` method returns a plain dict describing the HTTP
    request (method, url, headers, body).  The caller is responsible for
    executing it with their HTTP library of choice.
    """

    # -- public helpers ----------------------------------------------------

    @staticmethod
    def get_api_url(provider: PMProvider, endpoint: str) -> str:
        """Construct the full API URL for *provider* and *endpoint*.

        Args:
            provider: Target PM provider.
            endpoint: Path segment to append (leading ``/`` optional).

        Returns:
            Fully-qualified URL string.
        """
        base = _API_BASE_URLS[provider]
        sep = "" if endpoint.startswith("/") else "/"
        return f"{base}{sep}{endpoint}"

    @staticmethod
    def get_headers(config: PMSyncConfig) -> dict[str, str]:
        """Return authorization headers for *config*.

        The API key value is represented by the placeholder
        ``${ENV_VAR_NAME}`` so that callers can perform their own
        environment-variable substitution.

        Args:
            config: Provider configuration.

        Returns:
            Header dict suitable for an HTTP request.
        """
        key_placeholder = f"${{{config.api_key_env}}}"

        if config.provider == PMProvider.LINEAR:
            return {
                "Authorization": key_placeholder,
                "Content-Type": _CONTENT_TYPE_JSON,
            }
        if config.provider == PMProvider.ASANA:
            return {
                "Authorization": f"Bearer {key_placeholder}",
                "Content-Type": _CONTENT_TYPE_JSON,
            }
        # Shortcut
        return {
            "Shortcut-Token": key_placeholder,
            "Content-Type": _CONTENT_TYPE_JSON,
        }

    # -- request builders --------------------------------------------------

    def build_list_tasks_request(
        self,
        config: PMSyncConfig,
    ) -> dict[str, Any]:
        """Build a request to list tasks from the PM provider.

        Args:
            config: Provider configuration.

        Returns:
            Dict with keys ``method``, ``url``, ``headers``, and ``body``.
        """
        headers = self.get_headers(config)

        if config.provider == PMProvider.LINEAR:
            query = (
                "query { issues(filter: { project: { id: { eq: "
                f'"{config.project_id}"'
                " } } }) { nodes { id title description state { name } "
                "assignee { name } priority priorityLabel labels { nodes { name } } "
                "url } } }"
            )
            return {
                "method": "POST",
                "url": self.get_api_url(PMProvider.LINEAR, _GRAPHQL_ENDPOINT),
                "headers": headers,
                "body": {"query": query},
            }

        if config.provider == PMProvider.ASANA:
            return {
                "method": "GET",
                "url": self.get_api_url(
                    PMProvider.ASANA,
                    f"/projects/{config.project_id}/tasks"
                    "?opt_fields=name,notes,assignee.name,completed,"
                    "memberships.section.name,tags.name",
                ),
                "headers": headers,
                "body": None,
            }

        # Shortcut
        return {
            "method": "GET",
            "url": self.get_api_url(
                PMProvider.SHORTCUT,
                f"/projects/{config.project_id}/stories",
            ),
            "headers": headers,
            "body": None,
        }

    def build_create_task_request(
        self,
        config: PMSyncConfig,
        task: PMTask,
    ) -> dict[str, Any]:
        """Build a request to create a new task in the PM provider.

        Args:
            config: Provider configuration.
            task: The task data to create.

        Returns:
            Dict with keys ``method``, ``url``, ``headers``, and ``body``.
        """
        headers = self.get_headers(config)

        if config.provider == PMProvider.LINEAR:
            mutation = (
                "mutation { issueCreate(input: { "
                f'title: "{task.title}", '
                f'description: "{task.description}", '
                f'projectId: "{config.project_id}"'
                " }) { success issue { id url } } }"
            )
            return {
                "method": "POST",
                "url": self.get_api_url(PMProvider.LINEAR, _GRAPHQL_ENDPOINT),
                "headers": headers,
                "body": {"query": mutation},
            }

        if config.provider == PMProvider.ASANA:
            body: dict[str, Any] = {
                "data": {
                    "name": task.title,
                    "notes": task.description,
                    "projects": [config.project_id],
                },
            }
            if task.assignee:
                body["data"]["assignee"] = task.assignee
            return {
                "method": "POST",
                "url": self.get_api_url(PMProvider.ASANA, "/tasks"),
                "headers": headers,
                "body": body,
            }

        # Shortcut
        sc_body: dict[str, Any] = {
            "name": task.title,
            "description": task.description,
            "project_id": config.project_id,
        }
        if task.labels:
            sc_body["labels"] = [{"name": lbl} for lbl in task.labels]
        return {
            "method": "POST",
            "url": self.get_api_url(PMProvider.SHORTCUT, "/stories"),
            "headers": headers,
            "body": sc_body,
        }

    def build_update_status_request(
        self,
        config: PMSyncConfig,
        task_id: str,
        status: PMTaskStatus,
    ) -> dict[str, Any]:
        """Build a request to update a task's status.

        Args:
            config: Provider configuration.
            task_id: Provider-specific task identifier.
            status: New normalised status.

        Returns:
            Dict with keys ``method``, ``url``, ``headers``, and ``body``.
        """
        headers = self.get_headers(config)

        if config.provider == PMProvider.LINEAR:
            state_name = _PM_TO_LINEAR_STATE[status]
            mutation = (
                f'mutation {{ issueUpdate(id: "{task_id}", input: {{ stateId: "{state_name}" }}) {{ success }} }}'
            )
            return {
                "method": "POST",
                "url": self.get_api_url(PMProvider.LINEAR, _GRAPHQL_ENDPOINT),
                "headers": headers,
                "body": {"query": mutation},
            }

        if config.provider == PMProvider.ASANA:
            completed = status == PMTaskStatus.DONE
            return {
                "method": "PUT",
                "url": self.get_api_url(PMProvider.ASANA, f"/tasks/{task_id}"),
                "headers": headers,
                "body": {"data": {"completed": completed}},
            }

        # Shortcut
        sc_state = _PM_TO_SHORTCUT_STATE[status]
        return {
            "method": "PUT",
            "url": self.get_api_url(PMProvider.SHORTCUT, f"/stories/{task_id}"),
            "headers": headers,
            "body": {"workflow_state_id": sc_state},
        }

    # -- response parsers --------------------------------------------------

    @staticmethod
    def parse_linear_task(data: dict[str, Any]) -> PMTask:
        """Parse a Linear GraphQL issue node into a :class:`PMTask`.

        Args:
            data: A single issue node from the Linear GraphQL response.

        Returns:
            Normalised PMTask.
        """
        state_name = ""
        state_raw: object = data.get("state")
        if isinstance(state_raw, dict):
            state_dict = cast(_CAST_DICT_STR_ANY, state_raw)
            state_name = str(state_dict.get("name", ""))
        status = _linear_state_to_status(state_name)

        assignee_raw: object = data.get("assignee")
        assignee: str | None = None
        if isinstance(assignee_raw, dict):
            assignee_dict = cast(_CAST_DICT_STR_ANY, assignee_raw)
            assignee = str(assignee_dict.get("name", "")) or None

        labels_raw: object = data.get("labels")
        label_names: tuple[str, ...] = ()
        if isinstance(labels_raw, dict):
            labels_dict = cast(_CAST_DICT_STR_ANY, labels_raw)
            nodes_raw: object = labels_dict.get("nodes")
            if isinstance(nodes_raw, list):
                nodes_list = cast(_CAST_LIST_DICT_STR_ANY, nodes_raw)
                label_names = tuple(str(n.get("name", "")) for n in nodes_list)

        priority_val: object = data.get("priorityLabel")
        priority = str(priority_val) if priority_val else None

        return PMTask(
            provider_id=str(data.get("id", "")),
            title=str(data.get("title", "")),
            description=str(data.get("description", "")),
            status=status,
            assignee=assignee,
            priority=priority,
            labels=label_names,
            url=str(data.get("url", "")) or None,
        )

    @staticmethod
    def parse_asana_task(data: dict[str, Any]) -> PMTask:
        """Parse an Asana task JSON object into a :class:`PMTask`.

        Args:
            data: A single task object from the Asana REST response.

        Returns:
            Normalised PMTask.
        """
        completed = bool(data.get("completed", False))
        section_name = ""
        memberships_raw: object = data.get("memberships")
        if isinstance(memberships_raw, list):
            memberships_list = cast(_CAST_LIST_DICT_STR_ANY, memberships_raw)
            for m_entry in memberships_list:
                sec_raw: object = m_entry.get("section")
                if isinstance(sec_raw, dict):
                    sec_dict = cast(_CAST_DICT_STR_ANY, sec_raw)
                    section_name = str(sec_dict.get("name", ""))
                    break

        if completed:
            status = PMTaskStatus.DONE
        elif section_name.lower() in ("in progress", "in_progress", "doing"):
            status = PMTaskStatus.IN_PROGRESS
        else:
            status = PMTaskStatus.TODO

        assignee_raw: object = data.get("assignee")
        assignee: str | None = None
        if isinstance(assignee_raw, dict):
            asn_dict = cast(_CAST_DICT_STR_ANY, assignee_raw)
            assignee = str(asn_dict.get("name", "")) or None

        tags_raw: object = data.get("tags")
        labels: tuple[str, ...] = ()
        if isinstance(tags_raw, list):
            tags_list = cast(_CAST_LIST_DICT_STR_ANY, tags_raw)
            labels = tuple(str(t.get("name", "")) for t in tags_list)

        gid = str(data.get("gid", ""))
        url = f"https://app.asana.com/0/0/{gid}" if gid else None

        return PMTask(
            provider_id=gid,
            title=str(data.get("name", "")),
            description=str(data.get("notes", "")),
            status=status,
            assignee=assignee,
            labels=labels,
            url=url,
        )

    @staticmethod
    def parse_shortcut_story(data: dict[str, Any]) -> PMTask:
        """Parse a Shortcut story JSON object into a :class:`PMTask`.

        Args:
            data: A single story object from the Shortcut REST response.

        Returns:
            Normalised PMTask.
        """
        story_type = str(data.get("story_type", ""))
        completed = bool(data.get("completed", False))
        started = bool(data.get("started", False))

        if completed:
            status = PMTaskStatus.DONE
        elif started:
            status = PMTaskStatus.IN_PROGRESS
        else:
            status = PMTaskStatus.TODO

        sc_labels_raw: object = data.get("labels")
        labels: tuple[str, ...] = ()
        if isinstance(sc_labels_raw, list):
            sc_labels_list = cast(_CAST_LIST_DICT_STR_ANY, sc_labels_raw)
            labels = tuple(str(lbl.get("name", "")) for lbl in sc_labels_list)

        owner_ids_raw: object = data.get("owner_ids")
        assignee: str | None = None
        if isinstance(owner_ids_raw, list) and owner_ids_raw:
            owner_ids = cast("list[object]", owner_ids_raw)
            assignee = str(owner_ids[0])

        story_id = str(data.get("id", ""))
        app_url = data.get("app_url")
        url = str(app_url) if app_url else None

        return PMTask(
            provider_id=story_id,
            title=str(data.get("name", "")),
            description=str(data.get("description", "")),
            status=status,
            assignee=assignee,
            priority=story_type or None,
            labels=labels,
            url=url,
        )


# ---------------------------------------------------------------------------
# Status mapping helpers
# ---------------------------------------------------------------------------

_PM_TO_LINEAR_STATE: dict[PMTaskStatus, str] = {
    PMTaskStatus.TODO: "Todo",
    PMTaskStatus.IN_PROGRESS: "In Progress",
    PMTaskStatus.DONE: "Done",
}

_PM_TO_SHORTCUT_STATE: dict[PMTaskStatus, str] = {
    PMTaskStatus.TODO: "unstarted",
    PMTaskStatus.IN_PROGRESS: "started",
    PMTaskStatus.DONE: "done",
}

# Bernstein task statuses → PM normalised statuses
_BERNSTEIN_STATUS_MAP: dict[str, PMTaskStatus] = {
    "open": PMTaskStatus.TODO,
    "pending": PMTaskStatus.TODO,
    "in_progress": PMTaskStatus.IN_PROGRESS,
    "running": PMTaskStatus.IN_PROGRESS,
    "done": PMTaskStatus.DONE,
    "completed": PMTaskStatus.DONE,
    "failed": PMTaskStatus.DONE,
}


def convert_bernstein_status(status: str) -> PMTaskStatus:
    """Map a Bernstein internal task status to a PM-normalised status.

    Args:
        status: Bernstein task status string (e.g. ``"open"``, ``"done"``).

    Returns:
        Corresponding :class:`PMTaskStatus`.

    Raises:
        ValueError: If the status string is not recognised.
    """
    normalised = status.lower().strip()
    result = _BERNSTEIN_STATUS_MAP.get(normalised)
    if result is None:
        raise ValueError(
            f"Unknown Bernstein status {status!r}. Known values: {', '.join(sorted(_BERNSTEIN_STATUS_MAP))}"
        )
    return result


def _linear_state_to_status(state_name: str) -> PMTaskStatus:
    """Convert a Linear workflow state name to normalised status."""
    lower = state_name.lower()
    if lower in ("done", "completed", "closed", "cancelled", "canceled"):
        return PMTaskStatus.DONE
    if lower in ("in progress", "in_progress", "started", "in review"):
        return PMTaskStatus.IN_PROGRESS
    return PMTaskStatus.TODO


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def render_sync_report(result: PMSyncResult) -> str:
    """Render a Markdown summary of a sync result.

    Args:
        result: The completed sync result.

    Returns:
        Markdown-formatted string.
    """
    total = result.created + result.updated + result.skipped
    lines = [
        "## PM Sync Report",
        "",
        "| Metric | Count |",
        "|--------|-------|",
        f"| Created | {result.created} |",
        f"| Updated | {result.updated} |",
        f"| Skipped | {result.skipped} |",
        f"| **Total** | **{total}** |",
    ]

    if result.errors:
        lines.append("")
        lines.append(f"### Errors ({len(result.errors)})")
        lines.append("")
        for err in result.errors:
            lines.append(f"- {err}")

    lines.append("")
    return "\n".join(lines)
