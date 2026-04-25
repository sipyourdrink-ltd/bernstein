"""PR ownership checks for the autofix daemon.

Two gates protect Bernstein from touching arbitrary pull requests:

1. **Session ownership** — the PR description (or commit trailers)
   must contain a ``bernstein-session-id: <id>`` line written by
   ``bernstein pr``.  The daemon refuses to touch a PR without a
   matching trailer.
2. **Label gating** — the operator must add the
   ``bernstein-autofix`` label to the PR; removing the label aborts
   any in-flight attempt within one tick.

Both checks are pure functions over typed metadata so they can be
exhaustively unit-tested without spinning up the daemon.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pathlib import Path


class _SessionLookup(Protocol):
    """Minimal protocol used to verify a session id exists locally."""

    def __call__(self, session_id: str) -> bool: ...


# ---------------------------------------------------------------------------
# Session-id trailer helpers
# ---------------------------------------------------------------------------

#: Trailer key written by ``bernstein pr`` (and recognised here).  The
#: value is a stable session id (typically the first 12 chars of the
#: full id, but any non-empty token is accepted).
SESSION_TRAILER_KEY = "bernstein-session-id"

#: Regex used to extract a session id from arbitrary PR/commit text.
#: Tolerant of leading whitespace and various separators (``: ``,
#: ``=``, ``- ``).  Captures the first non-whitespace run after the
#: separator.
_TRAILER_RE = re.compile(
    rf"(?im)^[\s>*-]*{re.escape(SESSION_TRAILER_KEY)}\s*[:=]\s*([^\s,]+)\s*$",
)


def extract_session_id(text: str) -> str | None:
    """Return the first ``bernstein-session-id`` trailer value, or ``None``.

    Args:
        text: PR body, commit message, or the concatenation of both.

    Returns:
        The captured trailer value, or ``None`` when no trailer is
        present.
    """
    if not text:
        return None
    match = _TRAILER_RE.search(text)
    if not match:
        return None
    value = match.group(1).strip()
    return value or None


def render_session_trailer(session_id: str) -> str:
    """Render the canonical trailer line for ``session_id``.

    The output is always ``"bernstein-session-id: <id>"`` (with a
    single space separator) so consumers can do exact-match scans
    when they need to.
    """
    return f"{SESSION_TRAILER_KEY}: {session_id}"


# ---------------------------------------------------------------------------
# Session store probe
# ---------------------------------------------------------------------------


def session_id_known(session_id: str, sessions_dir: Path) -> bool:
    """Return ``True`` when ``session_id`` is present in the local session store.

    The check is intentionally cheap — it scans for any file in
    ``sessions_dir`` whose name contains the id (the wrap-up file
    naming convention used by ``pr_gen``).  When the directory is
    absent the function returns ``False`` so unknown sessions never
    pass the ownership gate.

    Args:
        session_id: The trailer value to look up.  Treated literally.
        sessions_dir: ``.sdd/sessions`` for the active workspace.

    Returns:
        ``True`` when at least one file in the directory matches.
    """
    if not session_id:
        return False
    if not sessions_dir.exists():
        return False
    try:
        for entry in sessions_dir.iterdir():
            if session_id in entry.name:
                return True
    except OSError:
        return False
    return False


# ---------------------------------------------------------------------------
# Typed pull-request metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PullRequestMetadata:
    """The subset of PR fields the daemon consults each tick.

    Attributes:
        repo: ``owner/name`` repository identifier.
        number: Pull-request number.
        title: PR title (first line of the synthesised commit goal).
        body: PR body (where ``bernstein pr`` writes the session
            trailer).
        labels: Labels currently attached to the PR.
        head_sha: SHA of the latest commit on the PR head — used to
            key the per-push attempt cap.
        head_branch: Source branch.
        head_repo_full_name: ``owner/name`` of the source repo; for
            same-repo PRs this matches ``repo``.
        is_fork: ``True`` when the PR comes from a fork that the
            daemon cannot push to.  Such PRs are skipped.
    """

    repo: str
    number: int
    title: str = ""
    body: str = ""
    labels: tuple[str, ...] = ()
    head_sha: str = ""
    head_branch: str = ""
    head_repo_full_name: str = ""
    is_fork: bool = False


@dataclass(frozen=True)
class OwnershipDecision:
    """The outcome of running the ownership gate over a PR.

    Attributes:
        eligible: ``True`` when the daemon may dispatch an attempt.
        session_id: The trailer value that established ownership;
            empty when ``eligible`` is ``False``.
        reason: Human-readable explanation that is suitable for
            logging *or* posting to a PR comment.  Always populated.
        signals: Structured signals consumed for the decision so the
            audit trail can replay it.
    """

    eligible: bool
    session_id: str = ""
    reason: str = ""
    signals: dict[str, str] = field(default_factory=dict[str, str])


def decide_ownership(
    pr: PullRequestMetadata,
    *,
    expected_label: str,
    session_lookup: _SessionLookup,
) -> OwnershipDecision:
    """Decide whether the daemon may touch a given PR.

    The gate proceeds in three steps so the caller can attribute a
    rejection to a single, specific reason:

    1. Cross-fork PRs are rejected outright — the daemon cannot
       push there.
    2. The PR must carry the ``expected_label``.  A removed label
       is the documented escape hatch for operators.
    3. The PR body (or commit trailers, joined into ``body`` by
       caller code) must contain a ``bernstein-session-id`` trailer
       whose value resolves through ``session_lookup``.

    Args:
        pr: Typed PR metadata for the candidate PR.
        expected_label: The opt-in label, typically taken from the
            per-repo :class:`~bernstein.core.autofix.config.RepoConfig`.
        session_lookup: Callable that returns ``True`` when the
            session id appears in the local store.  Injected so
            tests can stub it without touching the filesystem.

    Returns:
        A populated :class:`OwnershipDecision` describing the
        verdict.
    """
    signals: dict[str, str] = {
        "repo": pr.repo,
        "pr_number": str(pr.number),
        "expected_label": expected_label,
    }

    if pr.is_fork:
        return OwnershipDecision(
            eligible=False,
            reason="PR originates from a fork the daemon cannot push to.",
            signals={**signals, "skip_reason": "fork"},
        )

    labels = {label.strip().lower() for label in pr.labels}
    if expected_label.lower() not in labels:
        return OwnershipDecision(
            eligible=False,
            reason=(
                f"PR is missing the opt-in label '{expected_label}'. "
                "Add the label to authorise autofix."
            ),
            signals={**signals, "skip_reason": "missing_label"},
        )

    session_id = extract_session_id(pr.body) or extract_session_id(pr.title)
    if not session_id:
        return OwnershipDecision(
            eligible=False,
            reason=(
                "PR is missing the 'bernstein-session-id' trailer; "
                "open it via `bernstein pr` so ownership can be claimed."
            ),
            signals={**signals, "skip_reason": "missing_trailer"},
        )

    if not session_lookup(session_id):
        return OwnershipDecision(
            eligible=False,
            session_id=session_id,
            reason=(
                f"Session '{session_id}' is not present in the local session "
                "store; the daemon refuses to claim a PR it did not open."
            ),
            signals={**signals, "skip_reason": "unknown_session", "session_id": session_id},
        )

    return OwnershipDecision(
        eligible=True,
        session_id=session_id,
        reason=f"PR claimed via Bernstein session '{session_id}'.",
        signals={**signals, "session_id": session_id},
    )
