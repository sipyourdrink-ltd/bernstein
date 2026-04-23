"""Linear ticket fetcher.

Uses the Linear GraphQL API at https://api.linear.app/graphql. The API key is
read from the ``LINEAR_API_KEY`` environment variable at call time (not at
import time), so this module is safe to import without credentials.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, cast

from bernstein.core.integrations.tickets import (
    TicketAuthError,
    TicketParseError,
    TicketPayload,
)

__all__ = ["fetch_linear"]


_LINEAR_ENDPOINT = "https://api.linear.app/graphql"
_LINEAR_ENV = "LINEAR_API_KEY"
_TIMEOUT_S = 10.0

_KEY_RE = re.compile(r"([A-Z0-9]{1,16}-\d{1,8})", re.IGNORECASE)


_QUERY = """
query IssueByIdentifier($id: String!) {
  issue(id: $id) {
    id
    identifier
    title
    description
    url
    labels { nodes { name } }
    assignee { name displayName }
  }
}
""".strip()


def _extract_key(url: str) -> str:
    match = _KEY_RE.search(url)
    if match is None:
        raise TicketParseError(f"Could not extract Linear issue key from {url!r}")
    return match.group(1).upper()


def _post_graphql(api_key: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    """POST a GraphQL request, returning the decoded JSON body."""
    body = {"query": query, "variables": variables}
    try:
        import httpx

        headers = {"Authorization": api_key, "Content-Type": "application/json"}
        resp = httpx.post(_LINEAR_ENDPOINT, json=body, headers=headers, timeout=_TIMEOUT_S)
        if resp.status_code in (401, 403):
            raise TicketAuthError(
                f"Linear rejected the request (HTTP {resp.status_code}). Check the {_LINEAR_ENV} environment variable."
            )
        if resp.status_code >= 400:
            raise TicketParseError(f"Linear API returned HTTP {resp.status_code}: {resp.text[:200]}")
        return cast(dict[str, Any], resp.json())
    except ImportError:  # pragma: no cover - httpx is a declared dependency
        import urllib.error
        import urllib.request

        req = urllib.request.Request(
            _LINEAR_ENDPOINT,
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": api_key, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as handle:
                raw = handle.read().decode("utf-8")
            return cast(dict[str, Any], json.loads(raw))
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise TicketAuthError(
                    f"Linear rejected the request (HTTP {exc.code}). Check the {_LINEAR_ENV} environment variable."
                ) from exc
            raise TicketParseError(f"Linear API returned HTTP {exc.code}") from exc


def _extract_labels(issue: dict[str, Any]) -> tuple[str, ...]:
    """Pull a tuple of label names from a Linear issue node."""
    nodes = (issue.get("labels") or {}).get("nodes") or []
    return tuple(str(node.get("name", "")).strip() for node in nodes if isinstance(node, dict) and node.get("name"))


def _extract_assignee(issue: dict[str, Any]) -> str | None:
    """Pull the human-readable assignee name from a Linear issue node."""
    assignee_obj = issue.get("assignee")
    if not isinstance(assignee_obj, dict):
        return None
    name = assignee_obj.get("displayName") or assignee_obj.get("name")
    return str(name) if name else None


def fetch_linear(url: str) -> TicketPayload:
    """Fetch a Linear issue and return it as a :class:`TicketPayload`.

    Raises:
        TicketAuthError: ``LINEAR_API_KEY`` is missing or rejected.
        TicketParseError: URL could not be parsed or the response shape is unexpected.
    """
    api_key = os.environ.get(_LINEAR_ENV)
    if not api_key:
        raise TicketAuthError(
            f"Missing Linear credentials. Set the {_LINEAR_ENV} environment variable to your personal API key."
        )
    key = _extract_key(url)
    data = _post_graphql(api_key, _QUERY, {"id": key})
    if data.get("errors"):
        raise TicketParseError(f"Linear GraphQL error: {data['errors']}")
    issue = ((data.get("data") or {}).get("issue")) or None
    if not isinstance(issue, dict):
        raise TicketParseError(f"Linear issue {key} not found in response")

    return TicketPayload(
        id=str(issue.get("identifier") or key),
        title=str(issue.get("title") or "").strip(),
        description=str(issue.get("description") or "").strip(),
        labels=_extract_labels(issue),
        assignee=_extract_assignee(issue),
        url=str(issue.get("url") or url),
        source="linear",
    )
