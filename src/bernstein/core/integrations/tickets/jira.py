"""Jira cloud ticket fetcher.

Uses the Jira REST API v3. Credentials are read from two environment
variables at call time:

* ``JIRA_EMAIL`` -- the user's Atlassian account email.
* ``JIRA_API_TOKEN`` -- a Jira Cloud API token.
"""

from __future__ import annotations

import base64
import os
import re
from typing import Any
from urllib.parse import urlparse

from bernstein.core.integrations.tickets import (
    TicketAuthError,
    TicketParseError,
    TicketPayload,
)
from bernstein.core.integrations.tickets._http import http_get_json

__all__ = ["fetch_jira"]


_EMAIL_ENV = "JIRA_EMAIL"
_TOKEN_ENV = "JIRA_API_TOKEN"
_TIMEOUT_S = 10.0

_BROWSE_RE = re.compile(r"/browse/(?P<key>[A-Z][A-Z0-9_]+-\d+)", re.IGNORECASE)


def _parse_url(url: str) -> tuple[str, str]:
    """Return ``(base_url, issue_key)`` for a Jira browse URL."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise TicketParseError(f"Not a valid Jira URL: {url!r}")
    match = _BROWSE_RE.search(parsed.path)
    if match is None:
        raise TicketParseError(f"Could not extract Jira issue key from {url!r}")
    base = f"{parsed.scheme}://{parsed.netloc}"
    return base, match.group("key").upper()


def _flatten_adf(node: Any) -> str:
    """Flatten an Atlassian Document Format node tree into plain text.

    ADF is a nested JSON structure where leaf text nodes carry ``type: "text"``
    and ``text: "..."``. Block nodes (paragraphs, bullet lists, etc.) contain a
    ``content`` list of children. This function walks the tree depth-first and
    joins text pieces, inserting line breaks between top-level blocks.
    """
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    node_type = node.get("type")
    if node_type == "text":
        return str(node.get("text") or "")
    if node_type == "hardBreak":
        return "\n"
    children = node.get("content") or []
    parts: list[str] = []
    if isinstance(children, list):
        for child in children:
            parts.append(_flatten_adf(child))
    joined = "".join(parts)
    # Block-level nodes get a trailing newline so paragraphs stay separated.
    if node_type in {"paragraph", "heading", "bulletList", "orderedList", "listItem", "codeBlock", "blockquote"}:
        return joined + "\n"
    return joined


def _render_description(raw: Any) -> str:
    """Render a Jira description, handling both plain strings and ADF."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, dict):
        return _flatten_adf(raw).strip()
    return ""


def _get(base_url: str, issue_key: str, email: str, token: str) -> dict[str, Any]:
    creds = f"{email}:{token}".encode()
    auth = base64.b64encode(creds).decode("ascii")
    endpoint = f"{base_url}/rest/api/3/issue/{issue_key}"
    headers = {"Authorization": f"Basic {auth}", "Accept": "application/json"}
    return http_get_json(
        url=endpoint,
        headers=headers,
        provider_label="Jira",
        auth_env_var=f"{_EMAIL_ENV} and {_TOKEN_ENV}",
        timeout=_TIMEOUT_S,
    )


def fetch_jira(url: str) -> TicketPayload:
    """Fetch a Jira issue and return it as a :class:`TicketPayload`.

    Raises:
        TicketAuthError: env vars ``JIRA_EMAIL`` / ``JIRA_API_TOKEN`` are missing
            or were rejected by the server.
        TicketParseError: URL could not be parsed or the response is malformed.
    """
    base_url, key = _parse_url(url)
    # Vault-first: a `bernstein connect jira` flow stores email + token + base_url
    # together, so a single resolve hits the keychain. Fall back to the env-var
    # pair for users mid-migration with a deprecation warning.
    from bernstein.core.security.vault.factory import open_vault_silent
    from bernstein.core.security.vault.resolver import resolve_secret

    vault = open_vault_silent()
    email: str | None = None
    token: str | None = None
    if vault is not None:
        try:
            stored = vault.get("jira")
        except Exception:  # pragma: no cover - depends on backend
            stored = None
        if stored is not None and stored.secret:
            token = stored.secret
            if stored.metadata:
                email = stored.metadata.get("email")
                stored_base = stored.metadata.get("base_url")
                if stored_base:
                    base_url = stored_base
    if not (email and token):
        # Trigger the deprecation warning via the env-var path for any
        # piece that the vault didn't supply.
        env_resolution = resolve_secret("jira", vault=None)
        if env_resolution.found:
            token = token or env_resolution.secret
            email = email or os.environ.get(_EMAIL_ENV)
    if not email or not token:
        raise TicketAuthError(
            f"Missing Jira credentials. Run `bernstein connect jira` or set both {_EMAIL_ENV} and {_TOKEN_ENV}."
        )

    raw = _get(base_url, key, email, token)
    fields = raw.get("fields") or {}
    if not isinstance(fields, dict):
        raise TicketParseError(f"Jira response for {key} is missing 'fields'")

    labels_raw = fields.get("labels") or []
    labels = tuple(str(label) for label in labels_raw if isinstance(label, str))

    assignee_obj = fields.get("assignee") or {}
    assignee: str | None = None
    if isinstance(assignee_obj, dict):
        name = assignee_obj.get("displayName") or assignee_obj.get("emailAddress")
        if isinstance(name, str):
            assignee = name

    return TicketPayload(
        id=str(raw.get("key") or key),
        title=str(fields.get("summary") or "").strip(),
        description=_render_description(fields.get("description")),
        labels=labels,
        assignee=assignee,
        url=url,
        source="jira",
    )
