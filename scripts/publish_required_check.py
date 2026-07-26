#!/usr/bin/env python3
"""Publish a required status context as a single check-run per head SHA.

Why this exists
---------------
A workflow job publishes a check-run named after the job, and that
check-run inherits the *job's* fate. If the job is cancelled the head SHA
keeps a check-run whose conclusion is ``cancelled``; if the job is skipped
it keeps one whose conclusion is ``skipped``.

Neither is recoverable for a required context. Branch protection folds
every check-run of a required name into its verdict and a later success
does not clear an earlier non-success, so one cancelled instance holds the
pull request at BLOCKED for the life of that commit (#3042, #3154). The
inverse is just as bad: GitHub counts ``skipped`` as passing, so a job
gated off by an ``if:`` can satisfy a gate it never ran.

This script decouples the context from any job's fate. The verdict is
written explicitly, once, as a terminal check-run, and the same instance
is reused for the life of the head SHA:

    existing instances -> PATCH every one to the current verdict
    no instances       -> POST exactly one completed check-run

Reusing the instance is what makes the context *mutable*: a commit whose
gate failed can go green when the underlying condition is fixed without a
new commit, and a commit whose gate passed can go red again if the
condition regresses. Patching *every* instance (not just the newest) also
heals a SHA already poisoned by the job-name mechanism.

Callers must not invoke this while their job is being cancelled - a
cancelled job has no verdict to publish, and leaving the context absent is
the correct fail-closed outcome. Absent reads as BLOCKED; a later run
publishes the real verdict.

Usage:
    python scripts/publish_required_check.py \\
        --repo sipyourdrink-ltd/bernstein \\
        --sha 8059528db8ac407b6f8232e425885f80d7560ffd \\
        --name review-bot-ack \\
        --conclusion success \\
        --title "No unresolved must-address findings" \\
        --summary "..."

Required environment:
    GH_TOKEN  GitHub token with `checks: write`.

Exit codes:
    0  The context was published on the head SHA.
    1  Refused to publish (unknown conclusion, missing argument, no token).
    2  GitHub API failure. The context stays absent, which reads as BLOCKED.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Protocol

# The closed set of conclusions this publisher will write. `skipped`,
# `cancelled` and `neutral` are deliberately absent: GitHub treats the
# first two as terminal states a required context cannot recover from, and
# counts `skipped` and `neutral` as passing. A gate may only say pass or
# fail, so anything else is refused rather than translated.
ALLOWED_CONCLUSIONS = ("success", "failure")

# Branch protection pins each required context to an app id. Instances
# owned by any other app cannot be patched with this token and are not
# ours to speak for, so they are left alone.
DEFAULT_APP_SLUG = "github-actions"

API_ROOT = "https://api.github.com"


class Transport(Protocol):
    """Minimal HTTP surface, injected so the upsert logic is testable."""

    def __call__(self, method: str, url: str, body: dict[str, Any] | None = None) -> Any: ...


def urllib_transport(token: str) -> Transport:
    """Build a `Transport` backed by urllib, matching review_bot_ack.py."""

    def _call(method: str, url: str, body: dict[str, Any] | None = None) -> Any:
        payload = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if payload is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub API {method} {url} failed: {exc.code} {detail[:300]}") from exc
        return json.loads(raw) if raw else None

    return _call


def conclusion_for_exit_code(code: int) -> str:
    """Map a gate script's exit code onto a check-run conclusion.

    Only a clean exit is a pass. Every other code - including the internal
    error code 2 - is a failure, so a gate that crashes blocks the merge
    instead of silently vanishing.
    """
    return "success" if code == 0 else "failure"


def existing_instances(
    transport: Transport,
    repo: str,
    sha: str,
    name: str,
    app_slug: str = DEFAULT_APP_SLUG,
) -> list[int]:
    """Return the ids of check-runs named `name` on `sha` owned by `app_slug`."""
    url = f"{API_ROOT}/repos/{repo}/commits/{sha}/check-runs?check_name={name}&per_page=100"
    payload = transport("GET", url, None) or {}
    runs = payload.get("check_runs") if isinstance(payload, dict) else None
    if not isinstance(runs, list):
        return []
    ids: list[int] = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        app = run.get("app") or {}
        if isinstance(app, dict) and app.get("slug") not in (None, app_slug):
            continue
        run_id = run.get("id")
        if isinstance(run_id, int):
            ids.append(run_id)
    return ids


def publish(
    transport: Transport,
    repo: str,
    sha: str,
    name: str,
    conclusion: str,
    title: str,
    summary: str,
    details_url: str | None = None,
    app_slug: str = DEFAULT_APP_SLUG,
) -> list[int]:
    """Upsert the required context on `sha` and return the ids written.

    Healing a stale instance is best effort. Instances created by the old
    job-name mechanism belong to the Actions service, and if it refuses the
    update the head SHA still gets a fresh instance carrying the current
    verdict - blocking the publish over an unwritable tombstone would only
    add an absent context to an already-blocked commit.

    Raises:
        ValueError: `conclusion` is outside `ALLOWED_CONCLUSIONS`. The
            caller must fail closed rather than publish a state that a
            required context cannot recover from.
    """
    if conclusion not in ALLOWED_CONCLUSIONS:
        raise ValueError(f"refusing to publish conclusion {conclusion!r}; allowed: {', '.join(ALLOWED_CONCLUSIONS)}")

    output = {"title": title, "summary": summary}
    ids = existing_instances(transport, repo, sha, name, app_slug)
    if ids:
        body: dict[str, Any] = {"status": "completed", "conclusion": conclusion, "output": output}
        if details_url:
            body["details_url"] = details_url
        patched: list[int] = []
        for run_id in ids:
            try:
                transport("PATCH", f"{API_ROOT}/repos/{repo}/check-runs/{run_id}", body)
            except (RuntimeError, OSError) as exc:
                # Instances left behind by the old job-name mechanism were
                # created by the Actions service rather than by this script.
                # Healing them is best-effort: a rejected PATCH must not stop
                # the head SHA from getting a current verdict, because an
                # absent context blocks the pull request.
                print(f"warning: could not update check-run {run_id}: {exc}", file=sys.stderr)
                continue
            patched.append(run_id)
        if patched:
            return patched
        # Nothing was writable. Fall through and post a fresh instance so the
        # SHA at least carries the current verdict.
        print(
            f"warning: none of the {len(ids)} existing `{name}` instance(s) on {sha} could be updated; posting a "
            "new one. Any stale instance still counts toward the required context and may keep this commit blocked.",
            file=sys.stderr,
        )

    created: dict[str, Any] = {
        "name": name,
        "head_sha": sha,
        "status": "completed",
        "conclusion": conclusion,
        "output": output,
    }
    if details_url:
        created["details_url"] = details_url
    result = transport("POST", f"{API_ROOT}/repos/{repo}/check-runs", created)
    new_id = result.get("id") if isinstance(result, dict) else None
    return [new_id] if isinstance(new_id, int) else []


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Publish a required status context as a single check-run.")
    p.add_argument("--repo", required=True, help="owner/name slug")
    p.add_argument("--sha", required=True, help="head SHA the context is published on")
    p.add_argument("--name", required=True, help="required context name, e.g. review-bot-ack")
    p.add_argument("--conclusion", required=True, help=f"one of: {', '.join(ALLOWED_CONCLUSIONS)}")
    p.add_argument("--title", default="", help="check-run output title")
    p.add_argument("--summary", default="", help="check-run output summary")
    p.add_argument("--details-url", default="", help="link shown on the check-run")
    p.add_argument("--app-slug", default=DEFAULT_APP_SLUG, help="only touch instances owned by this app")
    args = p.parse_args(argv)

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not token:
        print("error: GH_TOKEN is not set; cannot publish the required context", file=sys.stderr)
        return 1

    try:
        ids = publish(
            urllib_transport(token),
            repo=args.repo,
            sha=args.sha,
            name=args.name,
            conclusion=args.conclusion,
            title=args.title or args.name,
            summary=args.summary,
            details_url=args.details_url or None,
            app_slug=args.app_slug,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (RuntimeError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(
            f"::error::could not publish `{args.name}` on {args.sha}; the required context stays absent and the "
            "pull request remains blocked",
            file=sys.stderr,
        )
        return 2

    print(f"published `{args.name}` = {args.conclusion} on {args.sha} (check-run ids: {ids or 'none'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
