"""Jira issue-to-backlog synchronisation.

Fetches open issues from a Jira project and writes them as YAML backlog files,
mirroring the GitHub sync in :mod:`bernstein.core.github`.  Uses only stdlib
``urllib`` so there are no extra dependencies.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003 — used at runtime
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JiraSyncConfig:
    """Configuration for Jira backlog synchronisation.

    Attributes:
        base_url: Jira instance base URL (e.g. ``https://myorg.atlassian.net``).
        project_key: Jira project key used in JQL queries (e.g. ``"BERN"``).
        auth_token_env: Name of the environment variable holding the
            Base64-encoded ``email:api-token`` string for Basic auth.
    """

    base_url: str
    project_key: str
    auth_token_env: str = "JIRA_TOKEN"


def fetch_jira_issues(config: JiraSyncConfig) -> list[dict[str, Any]]:
    """Fetch open issues from Jira via the REST API.

    Uses ``/rest/api/2/search`` with JQL ``project={key} AND status != Done``.

    Args:
        config: Jira connection configuration.

    Returns:
        List of raw issue dicts from the Jira response, or an empty list on
        any network/auth error.
    """
    token = os.environ.get(config.auth_token_env, "")
    if not token:
        logger.warning("Jira auth token env var %s is not set", config.auth_token_env)
        return []

    jql = f"project={config.project_key} AND status != Done"
    url = (
        f"{config.base_url.rstrip('/')}/rest/api/2/search"
        f"?jql={jql}&maxResults=100&fields=summary,description,status,labels"
    )

    req = Request(url)
    req.add_header("Authorization", f"Basic {token}")
    req.add_header("Accept", "application/json")

    try:
        with urlopen(req, timeout=30) as resp:
            data: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
            issues: list[dict[str, Any]] = data.get("issues", [])
            return issues
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        logger.warning("Failed to fetch Jira issues: %s", exc)
        return []


def sync_jira_to_backlog(config: JiraSyncConfig, backlog_dir: Path) -> int:
    """Sync Jira issues into the backlog directory as YAML files.

    For each issue that does not already have a corresponding
    ``jira-{key}.yaml`` file, writes a YAML-frontmatter backlog file.

    Args:
        config: Jira connection configuration.
        backlog_dir: Directory to write backlog YAML files into
            (typically ``.sdd/backlog/open``).

    Returns:
        Number of new backlog files created.
    """
    backlog_dir.mkdir(parents=True, exist_ok=True)

    issues = fetch_jira_issues(config)
    if not issues:
        return 0

    # Build set of existing Jira keys already synced.
    existing_keys: set[str] = set()
    for path in backlog_dir.glob("jira-*.yaml"):
        m = re.match(r"jira-([A-Z]+-\d+)", path.name)
        if m:
            existing_keys.add(m.group(1))

    created = 0
    for issue in issues:
        key: str = issue.get("key", "")
        if not key or key in existing_keys:
            continue

        fields: dict[str, Any] = issue.get("fields", {})
        summary: str = fields.get("summary", "Untitled")
        description: str = (fields.get("description") or "")[:500]

        slug = re.sub(r"[^a-z0-9]+", "-", summary.lower()).strip("-")[:60]
        filename = f"jira-{key}-{slug}.yaml"

        content = (
            f"---\n"
            f"id: jira-{key}\n"
            f'title: "[JIRA:{key}] {summary}"\n'
            f"role: backend\n"
            f"priority: 4\n"
            f"scope: medium\n"
            f"complexity: medium\n"
            f"type: feature\n"
            f"metadata:\n"
            f"  jira_key: {key}\n"
            f"---\n\n"
            f"# [JIRA:{key}] {summary}\n\n"
            f"{description}\n"
        )

        file_path = backlog_dir / filename
        file_path.write_text(content, encoding="utf-8")
        created += 1
        logger.info("Synced Jira issue %s to backlog: %s", key, filename)

    return created
