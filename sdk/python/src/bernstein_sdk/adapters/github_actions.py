"""GitHub Actions adapter — create Bernstein tasks from CI events.

Parses GitHub Actions workflow run and check-run webhook payloads and converts
CI failures into Bernstein tasks for automated remediation.

Setup
-----
In your GitHub repository settings add a webhook that sends
``workflow_run`` and ``check_run`` events to your Bernstein server::

    POST https://your-server/webhooks/github
    Content-Type: application/json
    Secret: <GITHUB_WEBHOOK_SECRET>

Or call the adapter directly in a GitHub Actions step::

    - name: Create Bernstein task on failure
      if: failure()
      env:
        BERNSTEIN_URL: http://your-server:8052
        GITHUB_WORKFLOW: ${{ github.workflow }}
        GITHUB_RUN_ID: ${{ github.run_id }}
        GITHUB_REPOSITORY: ${{ github.repository }}
        GITHUB_REF: ${{ github.ref }}
        GITHUB_SHA: ${{ github.sha }}
      run: |
        python -c "
        from bernstein_sdk.adapters.github_actions import CITaskFactory
        from bernstein_sdk import BernsteinClient
        factory = CITaskFactory.from_github_env()
        client = BernsteinClient()
        task = client.create_task(**vars(factory.task_from_env()))
        print('Created task', task.id)
        "

Usage from webhook payload::

    from bernstein_sdk.adapters.github_actions import CITaskFactory

    payload = request.json()
    factory = CITaskFactory()
    task_create = factory.task_from_workflow_webhook(payload)
    if task_create:
        client.create_task(**vars(task_create))
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from bernstein_sdk.models import TaskComplexity, TaskCreate, TaskScope

log = logging.getLogger(__name__)


@dataclass
class CIRunInfo:
    """Normalized CI run context."""

    workflow_name: str  # e.g. "CI"
    run_id: str  # GitHub Actions run ID
    repository: str  # e.g. "org/repo"
    branch: str  # e.g. "main" or "refs/heads/feature/foo"
    commit_sha: str  # full SHA
    conclusion: str  # "failure", "cancelled", "timed_out", etc.
    run_url: str  # URL to the workflow run

    @property
    def short_sha(self) -> str:
        return self.commit_sha[:7]

    @property
    def branch_name(self) -> str:
        return self.branch.removeprefix("refs/heads/")

    @classmethod
    def from_workflow_webhook(cls, payload: dict[str, Any]) -> CIRunInfo | None:
        """Parse a GitHub ``workflow_run`` webhook payload.

        Returns ``None`` if the event is not a failure/cancellation.
        """
        run: dict[str, Any] = payload.get("workflow_run", {})
        if not run:
            return None
        conclusion = run.get("conclusion", "")
        if conclusion not in ("failure", "cancelled", "timed_out", "action_required"):
            return None
        return cls(
            workflow_name=run.get("name", "CI"),
            run_id=str(run.get("id", "")),
            repository=payload.get("repository", {}).get("full_name", ""),
            branch=run.get("head_branch", ""),
            commit_sha=run.get("head_sha", ""),
            conclusion=conclusion,
            run_url=run.get("html_url", ""),
        )

    @classmethod
    def from_check_run_webhook(cls, payload: dict[str, Any]) -> CIRunInfo | None:
        """Parse a GitHub ``check_run`` webhook payload.

        Returns ``None`` if the check run did not fail.
        """
        check_run: dict[str, Any] = payload.get("check_run", {})
        if not check_run:
            return None
        conclusion = check_run.get("conclusion", "")
        if conclusion not in ("failure", "timed_out", "cancelled", "action_required"):
            return None
        return cls(
            workflow_name=check_run.get("name", "check"),
            run_id=str(check_run.get("id", "")),
            repository=payload.get("repository", {}).get("full_name", ""),
            branch=check_run.get("check_suite", {}).get("head_branch", ""),
            commit_sha=check_run.get("head_sha", ""),
            conclusion=conclusion,
            run_url=check_run.get("html_url", ""),
        )

    @classmethod
    def from_env(cls) -> CIRunInfo:
        """Build from GitHub Actions environment variables.

        Reads: ``GITHUB_WORKFLOW``, ``GITHUB_RUN_ID``, ``GITHUB_REPOSITORY``,
        ``GITHUB_REF``, ``GITHUB_SHA``, ``GITHUB_SERVER_URL``.
        """
        repo = os.getenv("GITHUB_REPOSITORY", "")
        run_id = os.getenv("GITHUB_RUN_ID", "")
        server_url = os.getenv("GITHUB_SERVER_URL", "https://github.com")
        run_url = (
            f"{server_url}/{repo}/actions/runs/{run_id}" if repo and run_id else ""
        )
        return cls(
            workflow_name=os.getenv("GITHUB_WORKFLOW", "CI"),
            run_id=run_id,
            repository=repo,
            branch=os.getenv("GITHUB_REF", ""),
            commit_sha=os.getenv("GITHUB_SHA", ""),
            conclusion="failure",
            run_url=run_url,
        )


class CITaskFactory:
    """Create Bernstein tasks from GitHub Actions CI failures.

    Args:
        default_role: Agent role to assign to CI fix tasks.
        conclusion_to_priority: Override priority by conclusion.
            Defaults to: ``{"failure": 1, "timed_out": 2, "cancelled": 3}``.
    """

    _DEFAULT_PRIORITY: dict[str, int] = {
        "failure": 1,
        "action_required": 1,
        "timed_out": 2,
        "cancelled": 3,
    }

    def __init__(
        self,
        default_role: str = "qa",
        conclusion_to_priority: dict[str, int] | None = None,
    ) -> None:
        self._default_role = default_role
        self._conclusion_to_priority = conclusion_to_priority or self._DEFAULT_PRIORITY

    def task_from_run(self, run: CIRunInfo) -> TaskCreate:
        """Convert a :class:`CIRunInfo` to a :class:`TaskCreate`.

        Args:
            run: Normalized CI run information.

        Returns:
            A :class:`TaskCreate` ready to submit to ``POST /tasks``.
        """
        priority = self._conclusion_to_priority.get(run.conclusion, 1)
        conclusion_label = run.conclusion.replace("_", " ")

        title = (
            f"Fix CI {conclusion_label}: {run.workflow_name} "
            f"on {run.branch_name} ({run.short_sha})"
        )
        description_parts = [
            f"Workflow **{run.workflow_name}** {conclusion_label} "
            f"on branch `{run.branch_name}`.",
            f"Commit: `{run.commit_sha}`",
            f"Repository: `{run.repository}`",
        ]
        if run.run_url:
            description_parts.append(f"Run: {run.run_url}")

        return TaskCreate(
            title=title,
            role=self._default_role,
            description="\n".join(description_parts),
            priority=priority,
            scope=TaskScope.SMALL,
            complexity=TaskComplexity.MEDIUM,
            external_ref=f"github_actions:{run.repository}/{run.run_id}",
            metadata={
                "ci_provider": "github_actions",
                "workflow": run.workflow_name,
                "run_id": run.run_id,
                "repository": run.repository,
                "branch": run.branch_name,
                "commit": run.commit_sha,
                "conclusion": run.conclusion,
                "run_url": run.run_url,
            },
        )

    def task_from_workflow_webhook(self, payload: dict[str, Any]) -> TaskCreate | None:
        """Create a task from a ``workflow_run`` webhook payload.

        Returns ``None`` if the payload is not a failure event.
        """
        run = CIRunInfo.from_workflow_webhook(payload)
        if run is None:
            return None
        return self.task_from_run(run)

    def task_from_check_run_webhook(self, payload: dict[str, Any]) -> TaskCreate | None:
        """Create a task from a ``check_run`` webhook payload.

        Returns ``None`` if the check run did not fail.
        """
        run = CIRunInfo.from_check_run_webhook(payload)
        if run is None:
            return None
        return self.task_from_run(run)

    def task_from_env(self) -> TaskCreate:
        """Create a task from the current GitHub Actions environment.

        Call this from a workflow step that runs ``if: failure()``.
        """
        run = CIRunInfo.from_env()
        return self.task_from_run(run)
