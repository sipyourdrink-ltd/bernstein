"""Tiny ``gh`` CLI wrapper used by the responder.

Centralised here so tests can stub a single ``GhClient`` rather than
patching ``subprocess.run`` in five places.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

GhRunner = Callable[[list[str], "str | None"], subprocess.CompletedProcess[str]]


def _default_runner(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run ``gh`` with optional stdin, capturing stdout/stderr.

    Args:
        args: Arguments after ``gh``.
        stdin: Optional text fed to the process.

    Returns:
        The completed process; callers inspect ``returncode`` / ``stdout``.
    """
    return subprocess.run(  # nosec B603 - args is fully constructed by caller
        ["gh", *args],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )


@dataclass
class GhClient:
    """Thin wrapper around the ``gh`` CLI for review-responder operations.

    Args:
        runner: Subprocess invoker.  Tests pass a fake; production code
            uses the default that shells out to the real ``gh`` binary.
    """

    runner: GhRunner = _default_runner

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_pr_diff_lines(self, repo: str, pr_number: int) -> dict[str, set[int]]:
        """Return the set of lines modified in the PR, keyed by file path.

        Used by the staleness check: a comment whose path/line is no
        longer in the diff is dismissed instead of re-attempted.

        Args:
            repo: ``owner/repo`` slug.
            pr_number: Pull-request number.

        Returns:
            Mapping ``{path: {line_no, ...}}``.  Empty mapping on failure.
        """
        result = self.runner(
            ["api", f"repos/{repo}/pulls/{pr_number}/files?per_page=100"],
            None,
        )
        if result.returncode != 0:
            logger.warning("gh pulls/files failed: %s", result.stderr.strip())
            return {}
        try:
            data = json.loads(result.stdout or "[]")
        except ValueError:
            return {}
        out: dict[str, set[int]] = {}
        if not isinstance(data, list):
            return out
        for item in data:
            if not isinstance(item, dict):
                continue
            path = item.get("filename")
            patch = item.get("patch")
            if not isinstance(path, str) or not isinstance(patch, str):
                continue
            out[path] = _lines_in_patch(patch)
        return out

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def reply_to_comment(
        self,
        *,
        repo: str,
        pr_number: int,
        comment_id: int,
        body: str,
    ) -> bool:
        """Post an inline reply threaded under ``comment_id``.

        Args:
            repo: Repository slug.
            pr_number: PR number.
            comment_id: Parent review-comment id.
            body: Markdown body of the reply.

        Returns:
            ``True`` on HTTP 2xx, ``False`` otherwise.
        """
        payload = json.dumps({"body": body, "in_reply_to": comment_id})
        result = self.runner(
            [
                "api",
                "-X",
                "POST",
                f"repos/{repo}/pulls/{pr_number}/comments",
                "--input",
                "-",
            ],
            payload,
        )
        if result.returncode != 0:
            logger.warning(
                "Failed to reply to comment #%d: %s",
                comment_id,
                result.stderr.strip(),
            )
            return False
        return True

    def patch_resolve_comment(self, *, repo: str, comment_id: int) -> bool:
        """Attempt to PATCH a review comment as resolved.

        GitHub's REST API does not consistently expose a "resolve" verb on
        review comments — when the call fails the responder falls back to
        a reply citing the commit SHA.  This method simply reports whether
        the PATCH succeeded.

        Args:
            repo: Repository slug.
            comment_id: Review-comment id to mark resolved.

        Returns:
            ``True`` on HTTP 2xx, ``False`` otherwise.
        """
        # The graphql ``resolveReviewThread`` mutation is the canonical path,
        # but it requires the *thread* id rather than the comment id. We
        # attempt the REST PATCH first because it's the verb described in
        # the ticket; callers should still treat False as "fall back".
        payload = json.dumps({"resolved": True})
        result = self.runner(
            [
                "api",
                "-X",
                "PATCH",
                f"repos/{repo}/pulls/comments/{comment_id}",
                "--input",
                "-",
            ],
            payload,
        )
        return result.returncode == 0

    def post_pr_comment(self, *, repo: str, pr_number: int, body: str) -> bool:
        """Post a top-level (non-inline) comment on the PR's issue thread.

        Used for the round-summary reply so the bundle resolution is
        visible without the reviewer having to click through to a single
        inline thread.

        Args:
            repo: Repository slug.
            pr_number: PR number.
            body: Markdown body.

        Returns:
            ``True`` on HTTP 2xx, ``False`` otherwise.
        """
        payload = json.dumps({"body": body})
        result = self.runner(
            [
                "api",
                "-X",
                "POST",
                f"repos/{repo}/issues/{pr_number}/comments",
                "--input",
                "-",
            ],
            payload,
        )
        if result.returncode != 0:
            logger.warning("Failed to post PR summary on #%d: %s", pr_number, result.stderr.strip())
            return False
        return True


def _lines_in_patch(patch: str) -> set[int]:
    """Parse a unified-diff hunk string into the set of new-side line numbers.

    Args:
        patch: Unified diff hunk text returned by ``GET /pulls/{n}/files``.

    Returns:
        Set of 1-based line numbers present on the new side of the patch.
    """
    lines: set[int] = set()
    cursor = 0
    for raw in patch.splitlines():
        if raw.startswith("@@"):
            # @@ -a,b +c,d @@
            try:
                new_part = raw.split("+", 1)[1].split(" ", 1)[0]
                start = int(new_part.split(",", 1)[0])
            except (IndexError, ValueError):
                continue
            cursor = start
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            lines.add(cursor)
            cursor += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            # deletion advances old-side, not new-side
            continue
        else:
            # context line
            cursor += 1
    return lines


def lines_in_patch(patch: str) -> set[int]:
    """Public alias of the internal ``_lines_in_patch`` parser."""
    return _lines_in_patch(patch)


__all__ = ["GhClient", "GhRunner", "_default_runner", "_lines_in_patch", "lines_in_patch"]
