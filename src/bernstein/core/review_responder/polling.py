"""Polling fallback that fetches PR review comments via ``gh api``.

When no public tunnel is available the responder falls back to polling
the REST API.  We shell out to the ``gh`` CLI rather than calling
``api.github.com`` directly so authentication piggy-backs on the
operator's existing ``gh auth login``.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from collections.abc import Callable
from typing import TYPE_CHECKING

from bernstein.core.review_responder.normaliser import normalise_polling_payload

if TYPE_CHECKING:
    from collections.abc import Iterable

    from bernstein.core.review_responder.models import ReviewComment

logger = logging.getLogger(__name__)


GhRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _default_gh_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run ``gh`` with captured stdout/stderr and a 30-second timeout.

    Args:
        args: Arguments to pass after ``gh`` (e.g. ``["api", "repos/..."]``).

    Returns:
        The completed process; callers should inspect ``returncode``.
    """
    return subprocess.run(  # nosec B603 - args is fully constructed by caller
        ["gh", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )


class PollingListener:
    """Periodically fetches comments via the GitHub REST API.

    The listener owns no thread by itself — callers schedule
    :meth:`tick` on whatever loop they prefer (the daemon command uses
    a bare ``threading.Timer``).  This keeps the listener trivially
    testable: feed it a mock runner, call ``tick``, assert the callback.

    Args:
        repo: ``owner/repo`` slug.
        pr_numbers: Iterable of PR numbers to poll.  Empty means "discover
            open PRs on each tick".
        on_comment: Callback receiving each new comment.
        gh_runner: Override of the ``gh`` subprocess invoker.  Tests pass
            an in-memory fake here.
    """

    def __init__(
        self,
        *,
        repo: str,
        pr_numbers: Iterable[int] | None,
        on_comment: Callable[[ReviewComment], None],
        gh_runner: GhRunner | None = None,
    ) -> None:
        """Capture configuration and the optional fake gh runner."""
        self._repo = repo
        self._pr_numbers = tuple(pr_numbers) if pr_numbers else ()
        self._on_comment = on_comment
        self._gh = gh_runner or _default_gh_runner
        self._last_seen_per_pr: dict[int, str] = {}

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def _ensure_gh(self) -> bool:
        """Return ``True`` when the real ``gh`` CLI is on PATH (skipped by fakes)."""
        if self._gh is not _default_gh_runner:
            return True
        if shutil.which("gh") is None:
            logger.warning("gh CLI not found — PollingListener cannot fetch comments")
            return False
        return True

    def _list_open_prs(self) -> list[int]:
        """Discover open PRs in the configured repo.

        Returns:
            List of PR numbers; empty when discovery fails or no PRs are
            open.  Errors are logged at WARNING and never propagate.
        """
        result = self._gh(
            [
                "api",
                f"repos/{self._repo}/pulls?state=open&per_page=50",
            ]
        )
        if result.returncode != 0:
            logger.warning("gh pulls list failed: %s", result.stderr.strip())
            return []
        try:
            data = json.loads(result.stdout or "[]")
        except ValueError:
            return []
        if not isinstance(data, list):
            return []
        out: list[int] = []
        for item in data:
            if isinstance(item, dict):
                n = item.get("number")
                if isinstance(n, int):
                    out.append(n)
        return out

    def _fetch_comments(self, pr_number: int) -> list[dict[str, object]]:
        """Fetch the latest review comments for ``pr_number``.

        Args:
            pr_number: Pull-request number to query.

        Returns:
            Raw comment dicts; empty on failure or no comments.
        """
        result = self._gh(
            [
                "api",
                f"repos/{self._repo}/pulls/{pr_number}/comments?per_page=100&sort=updated&direction=desc",
            ]
        )
        if result.returncode != 0:
            logger.warning(
                "gh comments fetch failed for PR #%d: %s",
                pr_number,
                result.stderr.strip(),
            )
            return []
        try:
            data = json.loads(result.stdout or "[]")
        except ValueError:
            return []
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    def tick(self) -> int:
        """Fetch new comments once and dispatch them to the callback.

        Returns:
            The number of comments dispatched in this tick (those with an
            ``updated_at`` strictly newer than the last seen high-water
            mark for their PR).
        """
        if not self._ensure_gh():
            return 0

        pr_numbers = list(self._pr_numbers) or self._list_open_prs()
        dispatched = 0
        for pr in pr_numbers:
            raw = self._fetch_comments(pr)
            comments = normalise_polling_payload(
                repo=self._repo,
                pr_number=pr,
                comments=raw,
            )
            high_water = self._last_seen_per_pr.get(pr, "")
            new_high_water = high_water
            for c in comments:
                if c.updated_at <= high_water:
                    continue
                self._on_comment(c)
                dispatched += 1
                if c.updated_at > new_high_water:
                    new_high_water = c.updated_at
            if new_high_water != high_water:
                self._last_seen_per_pr[pr] = new_high_water
        return dispatched

    def reset(self, pr_number: int | None = None) -> None:
        """Forget the last-seen high-water marks (forces re-emission).

        Args:
            pr_number: When set, reset only that PR; otherwise reset all.
        """
        if pr_number is None:
            self._last_seen_per_pr.clear()
        else:
            self._last_seen_per_pr.pop(pr_number, None)
