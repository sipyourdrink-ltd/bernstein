"""Claim etiquette for coordinator-free issue-intake runs.

The intake mechanism has no coordinator, so multiple runs may pick up the same
issue. This module provides lightweight claim tracking through GitHub comments,
allowing runs to recognize their own claims and detect stale claims from other
runs.

Unlike the volunteer module, intake runs do not track per-donor identity, so
viewerDidAuthor checks are not needed. The focus is on simple claim/resolution
markers that enable basic deduplication.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - used at runtime for isoformat()

#: Marker embedded in claim comments to identify intake-run claims.
CLAIM_MARKER: str = "<!-- bernstein:issue:intake -->"

#: Marker added to claim comments when the claim is resolved (completed or released).
RESOLVED_MARKER: str = "<!-- bernstein:issue:resolved -->"


def build_claim_body(fingerprint: str, run_id: str, start_time: datetime) -> str:
    """Build the body for a claim comment when an intake run starts.

    Args:
        fingerprint: Worker or run fingerprint for identification.
        run_id: Unique identifier for this run.
        start_time: When the run started (timezone-aware).

    Returns:
        The comment body text including claim markers.
    """
    formatted_time = start_time.isoformat()
    return (
        f"{CLAIM_MARKER}\n"
        f"Intake run via bernstein; fingerprint `{fingerprint}`; "
        f"run_id `{run_id}`; started at {formatted_time}."
    )


def build_completion_body(fingerprint: str, run_id: str, pr_url: str | None) -> str:
    """Build the body for a claim comment when an intake run completes.

    Args:
        fingerprint: Worker or run fingerprint for identification.
        run_id: Unique identifier for this run.
        pr_url: Optional URL to the created pull request.

    Returns:
        The comment body text including claim and resolution markers.
    """
    tail = f"; PR: {pr_url}" if pr_url else "."
    return (
        f"{CLAIM_MARKER}\n"
        f"{RESOLVED_MARKER}\n"
        f"Completed via bernstein (fingerprint `{fingerprint}`, run_id `{run_id}`){tail}"
    )


def build_release_body(fingerprint: str, run_id: str, reason: str) -> str:
    """Build the body for a claim comment when an intake run releases a claim.

    Args:
        fingerprint: Worker or run fingerprint for identification.
        run_id: Unique identifier for this run.
        reason: Description of why the claim was released.

    Returns:
        The comment body text including claim and resolution markers.
    """
    return (
        f"{CLAIM_MARKER}\n"
        f"{RESOLVED_MARKER}\n"
        f"Released via bernstein (fingerprint `{fingerprint}`, run_id `{run_id}`): {reason}"
    )


def find_claim_comment(comments: list[dict]) -> dict | None:
    """Find the first comment containing the claim marker.

    Args:
        comments: List of GitHub issue comment objects (dicts with 'body' key).

    Returns:
        The first comment dict containing CLAIM_MARKER, or None if not found.
    """
    for comment in comments:
        body = comment.get("body", "")
        if isinstance(body, str) and CLAIM_MARKER in body:
            return comment
    return None


def has_marker(body: str, marker: str) -> bool:
    """Check if a comment body contains a specific marker.

    Args:
        body: The comment body text.
        marker: The marker string to search for.

    Returns:
        True if the marker is present in the body, False otherwise.
    """
    return marker in body
