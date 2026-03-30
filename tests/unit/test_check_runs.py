"""Tests for the GitHub Check Runs API client."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from bernstein.github_app.check_runs import CheckRunClient, CheckRunResult, _iso_now

# ---------------------------------------------------------------------------
# _iso_now
# ---------------------------------------------------------------------------


def test_iso_now_format() -> None:
    ts = _iso_now()
    # Should look like "2026-03-30T12:00:00Z"
    assert "T" in ts
    assert ts.endswith("Z")
    parts = ts.split("T")
    assert len(parts) == 2
    date_parts = parts[0].split("-")
    assert len(date_parts) == 3


# ---------------------------------------------------------------------------
# CheckRunResult
# ---------------------------------------------------------------------------


def test_check_run_result_fields() -> None:
    result = CheckRunResult(check_run_id=12345, html_url="https://github.com/checks/12345")
    assert result.check_run_id == 12345
    assert result.html_url == "https://github.com/checks/12345"


# ---------------------------------------------------------------------------
# CheckRunClient.create
# ---------------------------------------------------------------------------


class TestCheckRunClientCreate:
    def test_create_not_configured_returns_none(self) -> None:
        """No installation ID → silent no-op."""
        client = CheckRunClient(repo="acme/widgets", installation_id=None)
        result = client.create(head_sha="abc123", task_title="Fix the bug")
        assert result is None

    def test_create_empty_installation_returns_none(self) -> None:
        client = CheckRunClient(repo="acme/widgets", installation_id="")
        result = client.create(head_sha="abc123", task_title="Fix the bug")
        assert result is None

    def test_create_calls_gh_api(self) -> None:
        client = CheckRunClient(repo="acme/widgets", installation_id="42")
        response_data = {"id": 999, "html_url": "https://github.com/checks/999"}
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(response_data).encode()
        mock_result.stderr = b""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = client.create(head_sha="abc123def456", task_title="Fix null check")

        assert result is not None
        assert result.check_run_id == 999
        assert result.html_url == "https://github.com/checks/999"
        # Verify gh api was called with POST
        call_args = mock_run.call_args[0][0]
        assert "gh" in call_args
        assert "api" in call_args
        assert "POST" in call_args

    def test_create_with_details_url(self) -> None:
        client = CheckRunClient(repo="acme/widgets", installation_id="42")
        response_data = {"id": 100, "html_url": "https://github.com/checks/100"}
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(response_data).encode()
        mock_result.stderr = b""

        with patch("subprocess.run", return_value=mock_result):
            result = client.create(
                head_sha="abc123",
                task_title="QA check",
                details_url="http://localhost:8052/dashboard",
            )

        assert result is not None

    def test_create_gh_failure_returns_none(self) -> None:
        client = CheckRunClient(repo="acme/widgets", installation_id="42")
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = b""
        mock_result.stderr = b"Not Found"

        with patch("subprocess.run", return_value=mock_result):
            result = client.create(head_sha="abc123", task_title="Test")

        assert result is None

    def test_create_file_not_found_returns_none(self) -> None:
        client = CheckRunClient(repo="acme/widgets", installation_id="42")
        with patch("subprocess.run", side_effect=FileNotFoundError("gh not found")):
            result = client.create(head_sha="abc123", task_title="Test")
        assert result is None


# ---------------------------------------------------------------------------
# CheckRunClient.update
# ---------------------------------------------------------------------------


class TestCheckRunClientUpdate:
    def test_update_not_configured_returns_none(self) -> None:
        client = CheckRunClient(repo="acme/widgets", installation_id=None)
        result = client.update(check_run_id=123, conclusion="success", summary="All good")
        assert result is None

    def test_update_success_conclusion(self) -> None:
        client = CheckRunClient(repo="acme/widgets", installation_id="42")
        response_data = {"id": 123, "html_url": "https://github.com/checks/123"}
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(response_data).encode()
        mock_result.stderr = b""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = client.update(
                check_run_id=123,
                conclusion="success",
                summary="All tests passed.",
            )

        assert result is not None
        assert result.check_run_id == 123
        # Verify PATCH was used
        call_args = mock_run.call_args[0][0]
        assert "PATCH" in call_args

    def test_update_failure_conclusion(self) -> None:
        client = CheckRunClient(repo="acme/widgets", installation_id="42")
        response_data = {"id": 456, "html_url": "https://github.com/checks/456"}
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(response_data).encode()
        mock_result.stderr = b""

        with patch("subprocess.run", return_value=mock_result):
            result = client.update(
                check_run_id=456,
                conclusion="failure",
                summary="Tests failed: 3 errors.",
            )

        assert result is not None

    def test_update_request_body_contains_conclusion(self) -> None:
        client = CheckRunClient(repo="acme/widgets", installation_id="42")
        response_data = {"id": 789, "html_url": ""}
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(response_data).encode()
        mock_result.stderr = b""

        captured_input: list[bytes] = []

        def capture_run(*args: object, **kwargs: object) -> MagicMock:
            captured_input.append(kwargs.get("input", b""))
            return mock_result

        with patch("subprocess.run", side_effect=capture_run):
            client.update(check_run_id=789, conclusion="neutral", summary="Skipped.")

        assert len(captured_input) == 1
        body = json.loads(captured_input[0])
        assert body["conclusion"] == "neutral"
        assert body["status"] == "completed"
