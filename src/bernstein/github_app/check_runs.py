"""GitHub Check Runs API client.

Posts and updates GitHub App check runs on pull requests so that Bernstein
agent verification results appear as native status checks in the GitHub UI.

Check run lifecycle:
  1. ``create`` - called when a fix/QA task is picked up; status=in_progress
  2. ``update`` - called when the task completes; status=completed + conclusion

Authentication is delegated to the ``gh`` CLI (its token environment), so
this client needs only the repository slug.  All operations degrade
gracefully when the slug is missing - the methods return ``None`` instead
of raising.

GitHub API reference:
  https://docs.github.com/en/rest/checks/runs
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Check run name shown in the GitHub UI
_CHECK_RUN_NAME = "bernstein / agent verification"
# Check run name for volunteer receipt verification
_VERIFICATION_CHECK_RUN_NAME = "bernstein / volunteer receipt verification"


@dataclass
class CheckRunResult:
    """Result of a check run create/update operation.

    Attributes:
        check_run_id: GitHub check run ID (use to update later).
        html_url: URL of the check run detail page.
    """

    check_run_id: int
    html_url: str


@dataclass
class ComparisonCheckRunResult:
    """Result of a comparison check run operation.

    This extends CheckRunResult with comparison-specific details.
    """

    check_run_id: int
    html_url: str
    errors: list[str] = field(default_factory=list)


class CheckRunClient:
    """Thin wrapper around the GitHub Check Runs API via ``gh`` CLI.

    Authentication is delegated entirely to ``gh`` (its token environment),
    so the client itself needs only the repository slug to operate.

    Args:
        repo: ``owner/repo`` slug.  Required; if empty all operations are
            no-ops.
    """

    def __init__(self, repo: str) -> None:
        self._repo = repo

    @property
    def _configured(self) -> bool:
        """True if the client has enough config to make API calls."""
        return bool(self._repo)

    def create(
        self,
        head_sha: str,
        task_title: str,
        details_url: str = "",
    ) -> CheckRunResult | None:
        """Create an in-progress check run for a Bernstein task.

        Args:
            head_sha: Git SHA of the commit being checked.
            task_title: Bernstein task title (shown in the check run output).
            details_url: Optional URL linking back to the Bernstein dashboard.

        Returns:
            ``CheckRunResult`` on success, ``None`` on any error or when not
            configured.
        """
        if not self._configured:
            logger.debug("CheckRunClient not configured - skipping create")
            return None

        body: dict[str, Any] = {
            "name": _CHECK_RUN_NAME,
            "head_sha": head_sha,
            "status": "in_progress",
            "started_at": _iso_now(),
            "output": {
                "title": "Agent verification in progress",
                "summary": f"Bernstein task: {task_title}",
            },
        }
        if details_url:
            body["details_url"] = details_url

        return self._api_post(f"/repos/{self._repo}/check-runs", body)

    def update(
        self,
        check_run_id: int,
        conclusion: str,
        summary: str,
        details_url: str = "",
    ) -> CheckRunResult | None:
        """Mark a check run as completed with a conclusion.

        Args:
            check_run_id: GitHub check run ID from a previous ``create`` call.
            conclusion: One of ``"success"``, ``"failure"``, ``"neutral"``,
                ``"cancelled"``, ``"timed_out"``, ``"action_required"``.
            summary: Markdown summary shown in the GitHub UI.
            details_url: Optional URL linking back to the Bernstein dashboard.

        Returns:
            ``CheckRunResult`` on success, ``None`` on error.
        """
        if not self._configured:
            logger.debug("CheckRunClient not configured - skipping update")
            return None

        body: dict[str, Any] = {
            "status": "completed",
            "conclusion": conclusion,
            "completed_at": _iso_now(),
            "output": {
                "title": f"Agent verification: {conclusion}",
                "summary": summary,
            },
        }
        if details_url:
            body["details_url"] = details_url

        return self._api_patch(f"/repos/{self._repo}/check-runs/{check_run_id}", body)

    def create_verification_check_run(
        self,
        head_sha: str,
        summary: str,
        details: str = "",
        conclusion: str = "neutral",
        details_url: str = "",
    ) -> ComparisonCheckRunResult | None:
        """Create a comparison check run for volunteer receipt verification.

        Args:
            head_sha: Git SHA of the commit being checked.
            summary: Markdown summary shown in the GitHub UI.
            details: Detailed markdown content shown in the check run details.
            conclusion: One of ``"success"``, ``"failure"``, ``"neutral"``,
                ``"cancelled"``, ``"timed_out"``, ``"action_required"``.
            details_url: Optional URL linking back to the Bernstein dashboard.

        Returns:
            ``ComparisonCheckRunResult`` on success, ``None`` on error.
        """
        if not self._configured:
            logger.debug("CheckRunClient not configured - skipping verification check run create")
            return None

        body: dict[str, Any] = {
            "name": _VERIFICATION_CHECK_RUN_NAME,
            "head_sha": head_sha,
            "status": "completed",
            "conclusion": conclusion,
            "completed_at": _iso_now(),
            "output": {
                "title": f"Volunteer receipt verification: {conclusion}",
                "summary": summary,
                "details": details,
            },
        }
        if details_url:
            body["details_url"] = details_url

        result = self._api_post(f"/repos/{self._repo}/check-runs", body)
        if result is None:
            return None

        return ComparisonCheckRunResult(
            check_run_id=result.check_run_id,
            html_url=result.html_url,
        )

    def update_verification_check_run(
        self,
        check_run_id: int,
        summary: str,
        details: str = "",
        conclusion: str = "neutral",
        details_url: str = "",
    ) -> ComparisonCheckRunResult | None:
        """Update an existing comparison check run for volunteer receipt verification.

        Args:
            check_run_id: GitHub check run ID from a previous ``create_verification_check_run`` call.
            summary: Markdown summary shown in the GitHub UI.
            details: Detailed markdown content shown in the check run details.
            conclusion: One of ``"success"``, ``"failure"``, ``"neutral"``,
                ``"cancelled"``, ``"timed_out"``, ``"action_required"``.
            details_url: Optional URL linking back to the Bernstein dashboard.

        Returns:
            ``ComparisonCheckRunResult`` on success, ``None`` on error.
        """
        if not self._configured:
            logger.debug("CheckRunClient not configured - skipping verification check run update")
            return None

        body: dict[str, Any] = {
            "status": "completed",
            "conclusion": conclusion,
            "completed_at": _iso_now(),
            "output": {
                "title": f"Volunteer receipt verification: {conclusion}",
                "summary": summary,
                "details": details,
            },
        }
        if details_url:
            body["details_url"] = details_url

        result = self._api_patch(f"/repos/{self._repo}/check-runs/{check_run_id}", body)
        if result is None:
            return None

        return ComparisonCheckRunResult(
            check_run_id=result.check_run_id,
            html_url=result.html_url,
        )

    def _api_post(self, path: str, body: dict[str, Any]) -> CheckRunResult | None:
        """POST to a GitHub API path and return the parsed result."""
        return self._gh_api(path, method="POST", body=body)

    def _api_patch(self, path: str, body: dict[str, Any]) -> CheckRunResult | None:
        """PATCH to a GitHub API path and return the parsed result."""
        return self._gh_api(path, method="PATCH", body=body)

    def _gh_api(self, path: str, method: str, body: dict[str, Any]) -> CheckRunResult | None:
        """Call the GitHub API via ``gh api``.

        Uses ``gh api --method <METHOD> <path> --input -`` with the body
        piped via stdin to avoid shell-escaping issues.

        Args:
            path: API path (e.g. ``/repos/owner/repo/check-runs``).
            method: HTTP method string (``POST``, ``PATCH``).
            body: Request body dict (will be JSON-encoded).

        Returns:
            Parsed ``CheckRunResult``, or ``None`` on any error.
        """
        body_bytes = json.dumps(body).encode("utf-8")
        args = [
            "gh",
            "api",
            "--method",
            method,
            path,
            "--input",
            "-",
            "--header",
            "Accept: application/vnd.github+json",
            "--header",
            "X-GitHub-Api-Version: 2022-11-28",
        ]
        try:
            result = subprocess.run(
                args,
                input=body_bytes,
                capture_output=True,
                timeout=30,
            )
            if result.returncode != 0:
                logger.warning(
                    "gh api %s %s failed (rc=%d): %s",
                    method,
                    path,
                    result.returncode,
                    result.stderr.decode("utf-8", errors="replace").strip(),
                )
                return None
            data: dict[str, Any] = json.loads(result.stdout)
            return CheckRunResult(
                check_run_id=int(data.get("id", 0)),
                html_url=str(data.get("html_url", "")),
            )
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as exc:
            logger.debug("gh api call failed: %s", exc)
            return None


def _iso_now() -> str:
    """Return the current time in ISO-8601 format required by GitHub API."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
