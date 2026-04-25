"""Convert raw GitHub payloads into :class:`ReviewComment` instances.

GitHub serves two slightly different shapes:

* Webhook ``pull_request_review_comment`` events wrap the comment under a
  ``"comment"`` key alongside repository / pull-request envelopes.
* The REST endpoint ``GET /repos/{owner}/{repo}/pulls/{n}/comments`` returns
  a flat list of comment dicts.

Both feed into the same :class:`ReviewComment` so the rest of the responder
does not need to special-case the source.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from bernstein.core.review_responder.models import ReviewComment

if TYPE_CHECKING:
    from collections.abc import Mapping


class EventParseError(ValueError):
    """Raised when a webhook / API payload cannot be turned into a comment.

    The message is intentionally compact (the bad keys, no payload bytes)
    so it is safe to log without leaking comment text.
    """


def _str(d: Mapping[str, Any], key: str, default: str = "") -> str:
    """Return ``d[key]`` coerced to ``str``, or ``default`` when missing."""
    val = d.get(key, default)
    return str(val) if val is not None else default


def _int(d: Mapping[str, Any], key: str, default: int = 0) -> int:
    """Return ``d[key]`` coerced to ``int``, or ``default`` when missing."""
    val = d.get(key, default)
    if val is None:
        return default
    try:
        return int(val)
    except (TypeError, ValueError) as exc:
        raise EventParseError(f"field {key!r} is not an int: {val!r}") from exc


def _line_range(comment: Mapping[str, Any]) -> tuple[int, int]:
    """Resolve ``(line_start, line_end)`` from GitHub's overlapping fields.

    GitHub returns ``line`` for single-line comments and ``start_line`` /
    ``original_start_line`` for multi-line ones.  ``original_line`` is used
    when the position has drifted off the latest commit.

    Args:
        comment: The ``"comment"`` sub-object (already unwrapped).

    Returns:
        Tuple of ``(line_start, line_end)`` (1-based, inclusive).  If both
        fields are missing the fallback is ``(0, 0)`` so callers can flag
        the comment as stale rather than crash.
    """
    line = comment.get("line")
    if line is None:
        line = comment.get("original_line")
    if line is None:
        line = 0
    end = int(line) if isinstance(line, (int, str)) and str(line).isdigit() else 0

    start = comment.get("start_line")
    if start is None:
        start = comment.get("original_start_line")
    if start is None:
        start = end
    start_int = int(start) if isinstance(start, (int, str)) and str(start).isdigit() else end

    if start_int <= 0:
        start_int = end
    if end <= 0:
        end = start_int
    if start_int > end:
        start_int, end = end, start_int
    return start_int, end


def _repo_slug(envelope: Mapping[str, Any]) -> str:
    """Return ``owner/repo`` from a webhook envelope.

    The webhook puts repository data at the top level, while the REST API
    returns each comment with a ``"repository_url"`` we fall back on.
    """
    repo = envelope.get("repository")
    if isinstance(repo, dict):
        full = repo.get("full_name")
        if isinstance(full, str) and full:
            return full
    return ""


def _pr_number(envelope: Mapping[str, Any], comment: Mapping[str, Any]) -> int:
    """Return the PR number from either the envelope or the comment payload."""
    pr_block = envelope.get("pull_request")
    if isinstance(pr_block, dict):
        n = pr_block.get("number")
        if isinstance(n, int):
            return n
        if isinstance(n, str) and n.isdigit():
            return int(n)
    pr_url = _str(comment, "pull_request_url")
    if pr_url:
        # ``.../pulls/<n>``
        try:
            return int(pr_url.rsplit("/", 1)[-1])
        except ValueError:
            pass
    return _int(comment, "pull_request_number", 0)


def _build_comment(
    *,
    comment: Mapping[str, Any],
    repo: str,
    pr_number: int,
) -> ReviewComment:
    """Materialise a :class:`ReviewComment` from a comment dict.

    Args:
        comment: The ``"comment"`` sub-object (REST item or webhook inner).
        repo: ``owner/repo`` slug, already resolved.
        pr_number: PR number, already resolved.

    Returns:
        A :class:`ReviewComment` populated from ``comment``.
    """
    user = comment.get("user")
    reviewer = ""
    if isinstance(user, dict):
        reviewer = _str(user, "login")
    if not reviewer:
        raise EventParseError("comment missing user.login")

    line_start, line_end = _line_range(comment)
    in_reply = comment.get("in_reply_to_id")
    return ReviewComment(
        comment_id=_int(comment, "id"),
        pr_number=pr_number,
        repo=repo,
        reviewer=reviewer,
        body=_str(comment, "body"),
        path=_str(comment, "path"),
        line_start=line_start,
        line_end=line_end,
        commit_id=_str(comment, "commit_id"),
        original_commit_id=_str(comment, "original_commit_id"),
        diff_hunk=_str(comment, "diff_hunk"),
        created_at=_str(comment, "created_at"),
        updated_at=_str(comment, "updated_at") or _str(comment, "created_at"),
        in_reply_to=int(in_reply) if isinstance(in_reply, int) else None,
    )


def normalise_webhook_payload(payload: Mapping[str, Any]) -> ReviewComment:
    """Parse a ``pull_request_review_comment`` webhook envelope.

    Args:
        payload: The decoded JSON body POSTed by GitHub.

    Returns:
        The normalised :class:`ReviewComment`.

    Raises:
        EventParseError: When the payload is missing the comment block,
            cannot resolve the repo slug, or the comment lacks a user.
    """
    comment_block = payload.get("comment")
    if not isinstance(comment_block, dict):
        raise EventParseError("payload has no 'comment' object")
    comment = cast("Mapping[str, Any]", comment_block)
    repo = _repo_slug(payload)
    if not repo:
        raise EventParseError("payload has no repository.full_name")
    pr_number = _pr_number(payload, comment)
    if pr_number <= 0:
        raise EventParseError("could not determine pull-request number")
    return _build_comment(comment=comment, repo=repo, pr_number=pr_number)


def normalise_polling_payload(
    *,
    repo: str,
    pr_number: int,
    comments: list[Mapping[str, Any]],
) -> list[ReviewComment]:
    """Parse the REST API list of PR review comments.

    Args:
        repo: ``owner/repo`` slug supplied by the caller (the REST list
            response does not embed it).
        pr_number: PR number the list belongs to.
        comments: Items returned by ``GET /pulls/{n}/comments``.

    Returns:
        The normalised list, in the same order; malformed items are
        skipped silently rather than aborting the batch.
    """
    out: list[ReviewComment] = []
    for raw in comments:
        try:
            out.append(_build_comment(comment=raw, repo=repo, pr_number=pr_number))
        except EventParseError:
            continue
    return out
