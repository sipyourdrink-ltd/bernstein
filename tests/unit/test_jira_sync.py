"""Tests for Jira issue-to-backlog synchronisation."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from bernstein.core.jira_sync import JiraSyncConfig, fetch_jira_issues, sync_jira_to_backlog

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_JIRA_RESPONSE: dict[str, Any] = {
    "issues": [
        {
            "key": "BERN-1",
            "fields": {
                "summary": "Add rate limiting",
                "description": "We need rate limiting on the API.",
                "status": {"name": "To Do"},
                "labels": ["backend"],
            },
        },
        {
            "key": "BERN-2",
            "fields": {
                "summary": "Fix login page",
                "description": None,
                "status": {"name": "In Progress"},
                "labels": [],
            },
        },
    ],
}


@pytest.fixture()
def jira_config() -> JiraSyncConfig:
    return JiraSyncConfig(
        base_url="https://myorg.atlassian.net",
        project_key="BERN",
        auth_token_env="JIRA_TOKEN",
    )


def _mock_urlopen(response_data: dict[str, Any]) -> MagicMock:
    """Build a mock for urllib.request.urlopen that returns *response_data*."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(response_data).encode("utf-8")
    mock_resp.__enter__ = lambda self: self
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


# ---------------------------------------------------------------------------
# fetch_jira_issues
# ---------------------------------------------------------------------------


def test_fetch_jira_issues_returns_issues(
    jira_config: JiraSyncConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JIRA_TOKEN", "dXNlcjp0b2tlbg==")

    mock_resp = _mock_urlopen(SAMPLE_JIRA_RESPONSE)
    with patch("bernstein.core.jira_sync.urlopen", return_value=mock_resp):
        issues = fetch_jira_issues(jira_config)

    assert len(issues) == 2
    assert issues[0]["key"] == "BERN-1"
    assert issues[1]["key"] == "BERN-2"


def test_fetch_jira_issues_no_token(
    jira_config: JiraSyncConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JIRA_TOKEN", raising=False)
    issues = fetch_jira_issues(jira_config)
    assert issues == []


def test_fetch_jira_issues_network_error(
    jira_config: JiraSyncConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JIRA_TOKEN", "dXNlcjp0b2tlbg==")

    from urllib.error import URLError

    with patch("bernstein.core.jira_sync.urlopen", side_effect=URLError("timeout")):
        issues = fetch_jira_issues(jira_config)

    assert issues == []


# ---------------------------------------------------------------------------
# sync_jira_to_backlog
# ---------------------------------------------------------------------------


def test_sync_creates_yaml_files(
    jira_config: JiraSyncConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JIRA_TOKEN", "dXNlcjp0b2tlbg==")
    backlog_dir = tmp_path / "backlog" / "open"

    mock_resp = _mock_urlopen(SAMPLE_JIRA_RESPONSE)
    with patch("bernstein.core.jira_sync.urlopen", return_value=mock_resp):
        count = sync_jira_to_backlog(jira_config, backlog_dir)

    assert count == 2
    files = list(backlog_dir.glob("jira-BERN-*.yaml"))
    assert len(files) == 2

    # Verify YAML content of first file
    content = (backlog_dir / files[0].name).read_text(encoding="utf-8")
    assert "jira-BERN-" in content
    assert "title:" in content
    assert "role: backend" in content


def test_sync_deduplicates(
    jira_config: JiraSyncConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing jira-BERN-1-*.yaml files should not be re-created."""
    monkeypatch.setenv("JIRA_TOKEN", "dXNlcjp0b2tlbg==")
    backlog_dir = tmp_path / "backlog" / "open"
    backlog_dir.mkdir(parents=True)

    # Pre-create a file for BERN-1
    (backlog_dir / "jira-BERN-1-add-rate-limiting.yaml").write_text("existing", encoding="utf-8")

    mock_resp = _mock_urlopen(SAMPLE_JIRA_RESPONSE)
    with patch("bernstein.core.jira_sync.urlopen", return_value=mock_resp):
        count = sync_jira_to_backlog(jira_config, backlog_dir)

    # Only BERN-2 should be created
    assert count == 1


def test_sync_empty_response(
    jira_config: JiraSyncConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JIRA_TOKEN", "dXNlcjp0b2tlbg==")
    backlog_dir = tmp_path / "backlog" / "open"

    mock_resp = _mock_urlopen({"issues": []})
    with patch("bernstein.core.jira_sync.urlopen", return_value=mock_resp):
        count = sync_jira_to_backlog(jira_config, backlog_dir)

    assert count == 0
