"""Linear ticket fetcher.

Uses the Linear GraphQL API at https://api.linear.app/graphql. The API key is
read from the ``LINEAR_API_KEY`` environment variable at call time (not at
import time), so this module is safe to import without credentials.
"""

from __future__ import annotations

import re
from typing import Any

from bernstein.core.integrations.tickets import (
    TicketAuthError,
    TicketParseError,
    TicketPayload,
)
from bernstein.core.integrations.tickets._http import http_post_json

__all__ = ["fetch_linear"]


_LINEAR_ENDPOINT = "https://api.linear.app/graphql"
_LINEAR_ENV = "LINEAR_API_KEY"
_TIMEOUT_S = 10.0

_KEY_RE = re.compile(r"([A-Z0-9]{1,16}-\d{1,8})", re.IGNORECASE)


_QUERY = """\
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
}\
"""


def _extract_key(url: str) -> str:
    match = _KEY_RE.search(url)
    if match is None:
        raise TicketParseError(f"Could not extract Linear issue key from {url!r}")
    return match.group(1).upper()


def _post_graphql(api_key: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    """POST a GraphQL request, returning the decoded JSON body."""
    body = {"query": query, "variables": variables}
    headers = {"Authorization": api_key, "Content-Type": "application/json"}
    return http_post_json(
        url=_LINEAR_ENDPOINT,
        headers=headers,
        json_data=body,
        provider_label="Linear",
        auth_env_var=_LINEAR_ENV,
        timeout=_TIMEOUT_S,
    )


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

    Resolves the API key in vault-first order: the OS keychain (via
    :mod:`bernstein.core.security.vault`), then the legacy
    ``LINEAR_API_KEY`` env-var with a one-time deprecation warning.

    Raises:
        TicketAuthError: vault entry missing AND ``LINEAR_API_KEY`` unset
            (or rejected by the API).
        TicketParseError: URL could not be parsed or the response shape is unexpected.
    """
    from bernstein.core.security.vault.factory import open_vault_silent
    from bernstein.core.security.vault.resolver import resolve_secret

    resolution = resolve_secret(
        "linear",
        vault=open_vault_silent(),
    )
    if not resolution.found:
        raise TicketAuthError(
            "Missing Linear credentials. Run `bernstein connect linear` to store your API key, "
            f"or set the legacy {_LINEAR_ENV} env var."
        )
    api_key = resolution.secret
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
