"""Best-effort claim etiquette for coordinator-free volunteer runs.

The volunteer PoC has no coordinator, so two donors can pick the same issue off
a project's backlog.  Occasional duplication is acceptable; silently duplicating
*every* task is not.  This module adds the one cheap defence available without a
lock: before starting, re-read the issue and skip it if it is already assigned,
closed, or carries a *fresh* claim comment written by someone else; on claim,
post a sticky comment; on completion or abort, edit that same comment.

None of this is a lock.  Two donors reading the issue in the same second both
see it free and both claim it -- that race is accepted and documented as such.
What the module buys is that the *second* donor to arrive a minute later sees
the first one's claim and steps aside.

Why the shape here, rather than reusing ``IssuePRClient``
--------------------------------------------------------

``bernstein.core.orchestration.issue_to_pr.IssuePRClient`` is the right *model*
-- a ``gh`` subprocess wrapper that authenticates as whoever ran ``gh``, never
as an installed GitHub App -- and this module copies that model rather than
importing it.  The reason is not that ``core/volunteer/`` is self-contained (it
is not; it already imports :mod:`bernstein.core.path_scope`,
:mod:`bernstein.core.git.worktree` and several ``core.security`` modules).  The
reason is a property those clients do not have and this one needs.

``IssuePRClient`` reads issue comments through the REST API, whose comment
payloads do not carry ``viewerDidAuthor``.  Both it and
``github_app.cost_reporter`` search for their sticky marker with no concept of
*whose* comment matched, because in both a single identity posts every comment.
Here the thread is written by many donors, each authenticated as themselves, and
GitHub lets a comment be edited only by its original author.  Editing a claim
another donor wrote fails with 403 -- a bug that never appears in local testing
and reads as intermittent in the field.

So the fetch here is ``gh issue view --json comments``, which returns
``viewerDidAuthor`` per comment, computed against the active ``gh`` auth.  A
worker edits only comments it authored; it never compares a login against a
stored username.  That same property answers the restart question: a worker that
finds its *own* fresh claim resumes rather than treating itself as a competitor.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, cast
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

#: Embedded in every claim comment and searched for on every read.  Keeping the
#: marker in the comment body -- rather than remembering a comment id anywhere --
#: is what lets a restarted worker recognise its own claim across process
#: boundaries, which is the whole point of a coordinator-free design.
CLAIM_MARKER: str = "<!-- bernstein:volunteer:claim -->"

#: Added to a claim comment's body when it is edited to a completion or a
#: release.  ``CLAIM_MARKER`` stays untouched so the comment is still found by
#: :func:`find_own_claim` and still renders as a claim; this second marker is
#: what tells *another* donor reading the thread that the claim is no longer
#: live, since GitHub does not move a comment's ``createdAt`` on an edit and a
#: resolved comment would otherwise look exactly as fresh as an active one.
RESOLVED_MARKER: str = "<!-- bernstein:volunteer:resolved -->"

#: How long a claim comment is trusted before the task is treated as free again.
#: Conservative on purpose: a donor whose machine died mid-task should not pin an
#: issue for others past a working session.  Configurable per call.
DEFAULT_CLAIM_STALENESS: timedelta = timedelta(hours=6)

#: A claim comment's REST id lives in its permalink fragment
#: (``...#issuecomment-<databaseId>``).  ``gh issue view --json`` exposes the
#: permalink but not the numeric id directly, and the numeric id is what the
#: REST edit endpoint needs, so it is recovered from the URL.
_ISSUECOMMENT_RE = re.compile(r"#issuecomment-(\d+)\b")

#: Signature of the ``gh`` runner.  Injected so tests stub one callable instead
#: of patching :func:`subprocess.run`.  Mirrors the shape used by
#: ``issue_to_pr.IssuePRClient`` deliberately.
GhRunner = Callable[[list[str], "str | None"], "subprocess.CompletedProcess[str]"]


def _default_runner(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run ``gh`` with optional stdin, capturing stdout/stderr.

    A donor box without ``gh`` on ``PATH``, or a call that times out, raises
    ``OSError``/``TimeoutExpired`` from :func:`subprocess.run` itself, before
    any of the module's own ``returncode != 0`` handling gets a look at it.
    Both are caught here and turned into a synthetic non-zero result, so a
    missing or slow ``gh`` degrades the same way a ``gh`` failure already
    does -- never as an exception out of ``fetch_state`` before the clone
    even starts, which is the one thing this module promises cannot happen.
    """
    try:
        return subprocess.run(  # nosec B603 - args constructed by caller, never a shell
            ["gh", *args],
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("gh invocation failed: %s", exc)
        return subprocess.CompletedProcess(args=["gh", *args], returncode=1, stdout="", stderr=str(exc))


class SkipReason(Enum):
    """Why a task was skipped before any work started."""

    ASSIGNED = "assigned"
    CLOSED = "closed"
    FRESH_CLAIM = "fresh_claim"


@dataclass(frozen=True, slots=True)
class SkipDecision:
    """Whether to skip a task, and why.

    Attributes:
        skip: True when the worker should step aside without starting.
        reason: The reason when :attr:`skip` is True, otherwise ``None``.
    """

    skip: bool
    reason: SkipReason | None = None


@dataclass(frozen=True, slots=True)
class ClaimComment:
    """One marker-bearing claim comment, reduced to what a decision needs.

    Attributes:
        rest_id: The numeric REST comment id, recovered from the permalink, or
            ``None`` when it could not be recovered (the comment can still be
            read, just not edited).
        created_at: When the comment was posted; timezone-aware.
        viewer_did_author: Whether the active ``gh`` identity wrote it.
        resolved: Whether the comment carries :data:`RESOLVED_MARKER`, i.e. it
            was already edited to a completion or a release and no longer
            represents a live claim.
    """

    rest_id: int | None
    created_at: datetime
    viewer_did_author: bool
    resolved: bool = False


@dataclass(frozen=True, slots=True)
class IssueClaimState:
    """The slice of an issue that claim etiquette reasons about.

    Attributes:
        number: The issue number.
        closed: Whether the issue is closed.
        assigned: Whether the issue has any assignee.
        claims: Marker-bearing comments only, in the order GitHub returned them.
    """

    number: int
    closed: bool
    assigned: bool
    claims: tuple[ClaimComment, ...]


def repo_slug(repo_url: str) -> str | None:
    """Return ``owner/name`` for ``repo_url``, or ``None`` if it names no host.

    Accepts the URL forms a claimed task may carry (``http``, ``https``,
    ``ssh``, ``git``).  A URL with no host segment -- a bare local filesystem
    path, an explicit ``file://`` URL, or an scp-style ``user@host:path``
    remote, none of which ``urlparse`` gives a ``netloc`` -- names no GitHub
    issue and yields ``None``, which the caller reads as "no claim etiquette
    for this task".  Both a fixture repo and an already-mirrored clone are
    named this way (see
    :func:`~bernstein.core.volunteer.runner.repo_url_problem`), so this is the
    ordinary case for a local run, not an edge case.
    """
    trimmed = repo_url.strip()
    if trimmed.endswith(".git"):
        trimmed = trimmed[:-4]
    parsed = urlparse(trimmed)
    if not parsed.netloc:
        return None
    path = parsed.path.strip("/")
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) < 2:
        return None
    return "/".join(segments[-2:])


def _parse_iso(value: object) -> datetime | None:
    """Parse a gh ISO-8601 timestamp (with trailing ``Z``) into an aware datetime.

    ``requires-python >= 3.12`` means :func:`datetime.fromisoformat` handles the
    trailing ``Z`` natively; malformed input yields ``None`` rather than raising.
    A naive result -- ``gh`` always emits a zone-qualified timestamp, so this is
    a defensive branch, not an expected one -- yields ``None`` too, rather than
    a value :func:`should_skip` would raise ``TypeError`` subtracting from an
    aware ``now``.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _rest_id_from_url(url: object) -> int | None:
    """Recover a comment's numeric REST id from its permalink."""
    if not isinstance(url, str):
        return None
    match = _ISSUECOMMENT_RE.search(url)
    return int(match.group(1)) if match else None


def _parse_state(payload: dict[str, Any]) -> IssueClaimState | None:
    """Build an :class:`IssueClaimState` from ``gh issue view --json`` output."""
    number = payload.get("number")
    if not isinstance(number, int):
        return None
    assigned = bool(payload.get("assignees"))
    closed = bool(payload.get("closed"))

    claims: list[ClaimComment] = []
    raw_comments = payload.get("comments")
    if isinstance(raw_comments, list):
        for raw in cast("list[Any]", raw_comments):
            if not isinstance(raw, dict):
                continue
            comment = cast("dict[str, Any]", raw)
            body = comment.get("body")
            if not isinstance(body, str) or CLAIM_MARKER not in body:
                continue
            created_at = _parse_iso(comment.get("createdAt"))
            if created_at is None:
                # A claim we cannot date cannot be judged fresh or stale; drop it
                # rather than guess.  gh always emits a valid timestamp, so this
                # is a defensive branch, not an expected one.
                continue
            claims.append(
                ClaimComment(
                    rest_id=_rest_id_from_url(comment.get("url")),
                    created_at=created_at,
                    viewer_did_author=bool(comment.get("viewerDidAuthor")),
                    resolved=RESOLVED_MARKER in body,
                )
            )
    return IssueClaimState(number=number, closed=closed, assigned=assigned, claims=tuple(claims))


def find_own_claim(state: IssueClaimState) -> ClaimComment | None:
    """The newest claim comment the active ``gh`` identity authored, if any.

    Scoped to ``viewerDidAuthor`` rather than a login comparison: it is the only
    signal that is correct per-donor *and* survives a restart, and it is what
    keeps an edit from landing on another donor's comment (a 403).
    """
    mine = [claim for claim in state.claims if claim.viewer_did_author]
    if not mine:
        return None
    return max(mine, key=lambda claim: claim.created_at)


def should_skip(state: IssueClaimState, *, now: datetime, staleness: timedelta) -> SkipDecision:
    """Whether to step aside from a task before starting it.

    Skip when the issue is assigned, closed, or carries a claim comment newer
    than ``staleness`` that *someone else* authored and has not since been
    resolved.  A worker's own claim never triggers a skip, whatever its age,
    so a restarted worker resumes its own in-flight task instead of locking
    itself out.  A claim already edited to a completion or a release --
    :attr:`ClaimComment.resolved` -- never triggers a skip either: GitHub does
    not move a comment's ``createdAt`` on an edit, so without this check a
    task another donor gave up minutes ago would look freshly claimed for the
    rest of the staleness window.

    Args:
        state: The issue slice, from :meth:`ClaimClient.fetch_state`.
        now: Current time; timezone-aware.  Injected so tests are deterministic.
        staleness: How long another donor's claim is honoured.
    """
    if state.assigned:
        return SkipDecision(True, SkipReason.ASSIGNED)
    if state.closed:
        return SkipDecision(True, SkipReason.CLOSED)
    for claim in state.claims:
        if claim.viewer_did_author or claim.resolved:
            continue
        if now - claim.created_at < staleness:
            return SkipDecision(True, SkipReason.FRESH_CLAIM)
    return SkipDecision(False, None)


def _format_window(window: timedelta) -> str:
    """A compact human string for a staleness window (e.g. ``6h``, ``90m``)."""
    seconds = int(window.total_seconds())
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def build_claim_body(*, fingerprint: str, window: timedelta) -> str:
    """The sticky claim comment posted when a worker picks the task up."""
    return (
        f"{CLAIM_MARKER}\n"
        f"Volunteering via bernstein; worker fingerprint `{fingerprint}`; "
        f"will report back or release within {_format_window(window)}."
    )


def build_completion_body(*, fingerprint: str, pr_url: str | None = None) -> str:
    """The edit applied to the claim comment when the worker finishes.

    Includes the PR link when one is known; a completion can be recorded before
    a PR exists (the gates passed and a signed bundle was produced), in which
    case the link is simply omitted.  Carries :data:`RESOLVED_MARKER` so another
    donor reading the thread later sees this claim as no longer live.
    """
    tail = f": {pr_url}" if pr_url else "."
    return f"{CLAIM_MARKER}\n{RESOLVED_MARKER}\nCompleted via bernstein (worker fingerprint `{fingerprint}`){tail}"


def build_release_body(*, fingerprint: str, reason: str) -> str:
    """The edit applied to the claim comment when the worker gives the task up.

    Carries :data:`RESOLVED_MARKER`, for the same reason :func:`build_completion_body`
    does: a release has to stop looking like a live claim to the next donor who
    reads the thread, not just to the worker that gave the task up.
    """
    return f"{CLAIM_MARKER}\n{RESOLVED_MARKER}\nReleased via bernstein (worker fingerprint `{fingerprint}`): {reason}"


def resolve_fingerprint(explicit: str | None) -> str:
    """The fingerprint to stamp into a claim comment.

    Requires an explicit value.  Earlier this fell back to the install-rev
    identity token when none was given, but that token returns the same
    16-zero sentinel on every donor's machine unless an operator has opted
    into identity emission (:data:`bernstein.core.identity.install_rev.IDENTITY_EMISSION_ENABLED`
    is ``False`` by default), so every claim comment carried the identical,
    meaningless fingerprint.  A caller with a signing key should stamp the
    same worker keyid the signed result bundle carries
    (:func:`bernstein.core.security.audit_dsse.keyid_from_public_key`), which
    is real and makes the public comment matchable against the signed result;
    :func:`~bernstein.core.volunteer.task_finish.finish_volunteer_task` does
    this by default.  A caller with no signing key yet (claim etiquette runs
    before an agent has produced anything to sign) must decide explicitly
    what to stamp instead of silently getting a value that means nothing.

    Raises:
        ValueError: ``explicit`` is ``None`` or empty.
    """
    if not explicit:
        raise ValueError(
            "claim_fingerprint is required: pass a real per-worker identifier "
            "(e.g. the signing key's worker keyid) rather than leaving the "
            "claim comment's fingerprint field meaningless"
        )
    return explicit


@dataclass
class ClaimClient:
    """A ``gh`` wrapper for reading and writing claim comments.

    Every call authenticates as whoever ran ``gh``; the client holds no token of
    its own.  All methods are best-effort: a ``gh`` failure returns ``None`` or
    ``False`` rather than raising, because claim etiquette must never fail a run
    that would otherwise succeed.
    """

    runner: GhRunner = _default_runner

    def fetch_state(self, repo: str, number: int) -> IssueClaimState | None:
        """Read the issue's claim-relevant state in one ``gh issue view`` call.

        Returns ``None`` on any failure, which the caller treats as "proceed
        without etiquette" rather than blocking real work.
        """
        result = self.runner(
            [
                "issue",
                "view",
                str(number),
                "--repo",
                repo,
                "--json",
                "number,state,closed,assignees,comments",
            ],
            None,
        )
        if result.returncode != 0:
            logger.warning("gh issue view failed for %s#%d: %s", repo, number, result.stderr.strip())
            return None
        try:
            payload = json.loads(result.stdout or "{}")
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        return _parse_state(cast("dict[str, Any]", payload))

    def post_claim(self, repo: str, number: int, body: str) -> int | None:
        """Post a claim comment, returning its numeric REST id."""
        payload = json.dumps({"body": body})
        result = self.runner(
            ["api", "-X", "POST", f"repos/{repo}/issues/{number}/comments", "--input", "-"],
            payload,
        )
        if result.returncode != 0:
            logger.warning("gh claim post failed for %s#%d: %s", repo, number, result.stderr.strip())
            return None
        try:
            created = json.loads(result.stdout or "{}")
        except ValueError:
            return None
        if not isinstance(created, dict):
            return None
        comment_id = cast("dict[str, Any]", created).get("id")
        return comment_id if isinstance(comment_id, int) else None

    def edit_claim(self, repo: str, comment_id: int, body: str) -> bool:
        """Edit an existing claim comment in place (completion or release)."""
        payload = json.dumps({"body": body})
        result = self.runner(
            ["api", "-X", "PATCH", f"repos/{repo}/issues/comments/{comment_id}", "--input", "-"],
            payload,
        )
        if result.returncode != 0:
            logger.warning("gh claim edit failed for %s comment %d: %s", repo, comment_id, result.stderr.strip())
            return False
        return True
