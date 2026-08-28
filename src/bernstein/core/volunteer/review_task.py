"""Independent review task creation for volunteer submissions.

Creates a review task on the task server after a volunteer submission,
assigning it to a role (e.g. ``"reviewer"``) rather than to a specific
worker.  The task server routes it to any available reviewer who is *not*
the submission's author -- identified by the bundle's ``worker_keyid``, which
is the donor's worker identity.

The review task is created via an HTTP POST to the task server, using the
same auth token the volunteer CLI already holds (donor-authenticated ``gh``
and ``bernstein`` share the same session).  The call is best-effort:
a failure to create the review task does not fail the submission itself.

The review task title references the original issue number and carries the
bundle digest as evidence that the work being reviewed was attested by the
volunteer.  The description copies the PR title and body into the task so
a reviewer can act without opening the PR itself.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bernstein.core.security.result_receipt_bundle import ResultBundle

logger = logging.getLogger(__name__)

#: Default role the review task is assigned to.  Override via
#: ``BERNSTEIN_REVIEW_ROLE`` in the donor's environment.
DEFAULT_REVIEW_ROLE = "reviewer"

#: Environment variable that overrides the task server URL for review task creation.
#: Falls back to the standard ``BERNSTEIN_SERVER_URL`` resolution used elsewhere.
SERVER_URL_ENV = "BERNSTEIN_SERVER_URL"

#: Environment variable that overrides the auth token for review task creation.
AUTH_TOKEN_ENV = "BERNSTEIN_AUTH_TOKEN"


def _resolve_server_url() -> str:
    """Resolve the task server URL for review task creation.

    Checks ``BERNSTEIN_REVIEW_SERVER_URL`` first (allowing the donor to
    configure a separate review server if needed), then falls back to
    ``BERNSTEIN_SERVER_URL``, then the port file, then the default.
    """
    url = os.environ.get("BERNSTEIN_REVIEW_SERVER_URL")
    if url:
        return url.rstrip("/")
    url = os.environ.get(SERVER_URL_ENV)
    if url:
        return url.rstrip("/")
    # Try the port file in the current working directory.
    port_path = os.getcwd() + "/.sdd/runtime/server.port"
    try:
        port = int(open(port_path).read().strip())  # noqa: SIM115
        if 1 <= port <= 65535:
            return f"http://127.0.0.1:{port}"
    except (OSError, ValueError):
        pass
    return "http://127.0.0.1:8052"


def _resolve_auth_token() -> str | None:
    """Resolve the auth token for review task creation.

    Checks ``BERNSTEIN_REVIEW_AUTH_TOKEN`` first, then falls back to
    ``BERNSTEIN_AUTH_TOKEN``, then the run-auth-token file in the worktree.
    Returns ``None`` when no token is available; the HTTP call proceeds
    without auth and the server accepts or rejects based on its own policy.
    """
    token = os.environ.get("BERNSTEIN_REVIEW_AUTH_TOKEN")
    if token:
        return token
    token = os.environ.get(AUTH_TOKEN_ENV)
    if token:
        return token
    # Fall back to the run auth token file in the worktree.
    try:
        token_path = os.getcwd() + "/.sdd/runtime/auth.token"
        return open(token_path).read().strip()
    except OSError:
        pass
    return None


def create_volunteer_review_task(
    bundle: ResultBundle,
    pr_url: str,
    *,
    role: str | None = None,
    server_url: str | None = None,
    auth_token: str | None = None,
) -> str | None:
    """Create an independent review task on the task server.

    The task is assigned to ``role`` (default: ``"reviewer"``) rather than
    to any specific worker, so the task server can route it to whoever is
    available.  The bundle's ``worker_keyid`` is passed as a filter so
    the server can exclude the author from being assigned the review.

    The call is best-effort: any failure is logged and ``None`` is returned.
    A failure to create the review task never propagates to the caller.

    Args:
        bundle: The signed result receipt bundle for the submission.
        pr_url: The URL of the opened volunteer PR.
        role: Task role for the review.  Defaults to ``"reviewer"``.
            Set to ``"backend"`` or another role if the project routes
            reviews through a different pool.
        server_url: Override the resolved task server URL.
        auth_token: Override the resolved auth token.

    Returns:
        The created task's ``id``, or ``None`` if the call failed or
        the server returned a non-2xx response.
    """
    import json
    import urllib.request

    review_role = role or DEFAULT_REVIEW_ROLE
    url = (server_url or _resolve_server_url()).rstrip("/")
    token = auth_token or _resolve_auth_token()

    issue_ref = f"#{bundle.task.issue_number}" if bundle.task.issue_number else f"commit {bundle.task.commit_sha[:12]}"
    task_title = f"[volunteer review] {issue_ref} — {bundle.task.repo.split('/')[-1]}"

    task_description = "\n".join(
        [
            "## Volunteer Submission Review",
            "",
            f"**Author worker keyid:** `{bundle.worker_keyid}`",
            f"**PR:** {pr_url}",
            f"**Bundle digest:** `{bundle.digest}`",
            f"**Manifest digest:** `{bundle.manifest_sha256}`",
            f"**Adapter:** {bundle.adapter_id} ({bundle.model_id})",
            f"**Created at:** {bundle.created_at}",
            "",
            "Review the submission. Verify the patch is in scope, gates passed,",
            "and the result bundle attests the work correctly.",
            "",
            "### Receipt verification",
            "",
            "Run: `bernstein receipt verify bundle.json` (offline, no server required).",
        ]
    )

    payload = {
        "title": task_title,
        "description": task_description,
        "role": review_role,
        "priority": 2,
        "scope": "small",
        "complexity": "small",
        "eu_ai_act_risk": "minimal",
        "approval_required": False,
        "risk_level": "low",
        "metadata": {
            "volunteer_submission": True,
            "author_worker_keyid": bundle.worker_keyid,
            "pr_url": pr_url,
            "bundle_digest": bundle.digest,
            "manifest_sha256": bundle.manifest_sha256,
            "sandbox_profile": bundle.sandbox_profile,
            "adapter_id": bundle.adapter_id,
            "model_id": bundle.model_id,
            "issue_number": bundle.task.issue_number,
            "repo": bundle.task.repo,
            "commit_sha": bundle.task.commit_sha,
        },
    }

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(
        f"{url}/tasks",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # nosec B310 - url constructed from config/env, not user input
            if response.status == 200 or response.status == 201:
                result = json.loads(response.read().decode("utf-8"))
                task_id = result.get("id")
                if task_id:
                    logger.info(
                        "Created volunteer review task %s for submission %s",
                        task_id,
                        bundle.digest[:12],
                    )
                    return str(task_id)
                logger.warning(
                    "Review task response missing 'id' field: %s",
                    str(result)[:200],
                )
                return None
            else:
                body = response.read().decode("utf-8", errors="replace")
                logger.warning(
                    "Review task creation returned HTTP %d: %s",
                    response.status,
                    body[:200],
                )
                return None
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        logger.warning(
            "Review task creation HTTP error %d: %s",
            exc.code,
            body[:200],
        )
        return None
    except OSError as exc:
        logger.warning("Review task creation failed (network error): %s", exc)
        return None


__all__ = [
    "DEFAULT_REVIEW_ROLE",
    "create_volunteer_review_task",
]
