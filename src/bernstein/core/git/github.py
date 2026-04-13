"""GitHub API integration for evolve coordination.

Wraps the ``gh`` CLI to synchronise evolution proposals with GitHub Issues.
This allows multiple Bernstein instances to coordinate on self-evolution
without generating duplicate work.

Distributed evolve protocol:
  1. Instance fetches open ``bernstein-evolve`` issues.
  2. Claims an unclaimed issue (adds ``evolve-claimed`` label), or creates a new one.
  3. Works on the task locally.
  4. Closes the issue on completion, optionally linking to a PR.

Community evolve protocol:
  1. Community members file issues with ``evolve-candidate`` or ``feature-request`` labels.
  2. ``bernstein evolve run --community`` scans for these issues.
  3. Issues are prioritised by 👍 reaction count (community voting).
  4. Trust check: issue author must be a repo collaborator, OR the issue must
     carry the ``maintainer-approved`` label.
  5. Bernstein claims the issue (``evolve-in-progress``), converts it to a
     proposal, and opens a PR referencing the issue.
  6. On merge, the issue is automatically closed.

Labels:
  - ``bernstein-evolve``    — all evolution proposals
  - ``auto-generated``      — machine-generated proposals
  - ``evolve-claimed``      — issue is being worked on by an instance
  - ``evolve-hash-<hex>``   — 8-char SHA-256 prefix of the lowercased title
                              (used for deduplication across instances)
  - ``evolve-candidate``    — community request eligible for community evolve
  - ``feature-request``     — community feature request (also eligible)
  - ``evolve-in-progress``  — Bernstein is actively working on this issue
  - ``maintainer-approved`` — maintainer trust override for community issues

All operations degrade gracefully when ``gh`` is unavailable or unauthenticated.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# Standard labels applied to all evolution proposals.
_LABEL_EVOLVE = "bernstein-evolve"
_LABEL_CLAIMED = "evolve-claimed"
_LABEL_AUTO = "auto-generated"

# Community evolve labels.
_LABEL_EVOLVE_CANDIDATE = "evolve-candidate"
_LABEL_FEATURE_REQUEST = "feature-request"
_LABEL_IN_PROGRESS = "evolve-in-progress"
_LABEL_MAINTAINER_APPROVED = "maintainer-approved"

# Prefix for per-proposal dedup labels.
_HASH_LABEL_PREFIX = "evolve-hash-"

# Community issue scanning labels — either of these qualifies.
_COMMUNITY_LABELS: tuple[str, ...] = (_LABEL_EVOLVE_CANDIDATE, _LABEL_FEATURE_REQUEST)


def _hash_title(title: str) -> str:
    """Compute a short dedup hash from a proposal title.

    Args:
        title: Proposal title string.

    Returns:
        8-character lowercase hex string derived from SHA-256 of the
        lowercased, stripped title.
    """
    return hashlib.sha256(title.lower().strip().encode()).hexdigest()[:8]


@dataclass
class GitHubIssue:
    """Lightweight representation of a GitHub Issue.

    Attributes:
        number: Issue number (unique within repo).
        title: Issue title.
        url: HTML URL to the issue.
        labels: List of label names attached to the issue.
        state: ``"open"`` or ``"closed"``.
        body: Issue body text (may be empty if not requested).
        author: Login of the issue author (may be empty if not requested).
        thumbs_up: Number of 👍 reactions (used for community priority).
    """

    number: int
    title: str
    url: str
    labels: list[str] = field(default_factory=list[str])
    state: str = "open"
    body: str = ""
    author: str = ""
    thumbs_up: int = 0

    @property
    def is_claimed(self) -> bool:
        """True if the issue has the ``evolve-claimed`` label."""
        return _LABEL_CLAIMED in self.labels

    @property
    def is_in_progress(self) -> bool:
        """True if Bernstein is actively working on this community issue."""
        return _LABEL_IN_PROGRESS in self.labels

    @property
    def is_maintainer_approved(self) -> bool:
        """True if a maintainer has approved this community issue for evolve."""
        return _LABEL_MAINTAINER_APPROVED in self.labels

    @property
    def hash_label(self) -> str | None:
        """Return the ``evolve-hash-*`` label if present, else None."""
        for label in self.labels:
            if label.startswith(_HASH_LABEL_PREFIX):
                return label
        return None

    @classmethod
    def from_gh_json(cls, data: dict[str, Any]) -> GitHubIssue:
        """Parse from ``gh issue list --json`` output.

        Args:
            data: Parsed JSON object from the GitHub CLI.

        Returns:
            Populated GitHubIssue.
        """
        labels = [str(lbl["name"]) for lbl in cast("list[Any]", data.get("labels", []))]
        # Reaction counts are present when the ``reactions`` field is requested.
        reactions = data.get("reactions", {})
        thumbs_up = 0
        if isinstance(reactions, dict):
            r = cast("dict[str, Any]", reactions)
            thumbs_up = int(r.get("+1", 0))
        # Author login.
        author_obj = data.get("author", {})
        author: str = str(cast("dict[str, Any]", author_obj).get("login", "")) if isinstance(author_obj, dict) else ""
        return cls(
            number=data["number"],
            title=data.get("title", ""),
            url=data.get("url", ""),
            labels=labels,
            state=data.get("state", "open"),
            body=data.get("body", "") or "",
            author=author,
            thumbs_up=thumbs_up,
        )


class GitHubClient:
    """Thin wrapper around the ``gh`` CLI for evolve coordination.

    All methods return gracefully (``None`` / empty list) when the ``gh``
    CLI is unavailable, unauthenticated, or the repository has no remote.

    Args:
        repo: Optional ``owner/repo`` slug.  If ``None``, inferred from the
            current git context (works inside a git repository with a GitHub
            remote).
    """

    def __init__(self, repo: str | None = None) -> None:
        self._repo = repo
        self._available: bool | None = None  # lazy, cached after first check

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """True if ``gh`` is in PATH and authenticated."""
        if self._available is None:
            self._available = self._check_available()
        return self._available

    def fetch_open_evolve_issues(self) -> list[GitHubIssue]:
        """Return all open ``bernstein-evolve`` issues from GitHub.

        Returns:
            List of open issues labelled ``bernstein-evolve``, or an empty
            list on any error.
        """
        if not self.available:
            return []

        args = [
            "gh",
            "issue",
            "list",
            "--label",
            _LABEL_EVOLVE,
            "--state",
            "open",
            "--json",
            "number,title,url,labels,state",
            "--limit",
            "100",
        ]
        if self._repo:
            args += ["--repo", self._repo]

        result = self._run(args)
        if result is None:
            return []

        try:
            raw: list[dict[str, Any]] = json.loads(result)
            return [GitHubIssue.from_gh_json(item) for item in raw]
        except (json.JSONDecodeError, KeyError):
            logger.warning("Failed to parse GitHub issue list response")
            return []

    def find_unclaimed(self) -> list[GitHubIssue]:
        """Return open evolve issues that have not been claimed yet.

        Returns:
            Issues without the ``evolve-claimed`` label, sorted by issue
            number ascending (oldest first).
        """
        issues = self.fetch_open_evolve_issues()
        unclaimed = [i for i in issues if not i.is_claimed]
        unclaimed.sort(key=lambda i: i.number)
        return unclaimed

    def find_by_hash(self, title: str) -> GitHubIssue | None:
        """Find an existing open issue with a matching title hash.

        Args:
            title: Proposal title to check for duplicates.

        Returns:
            First matching ``GitHubIssue``, or ``None`` if not found.
        """
        label = _HASH_LABEL_PREFIX + _hash_title(title)
        issues = self.fetch_open_evolve_issues()
        for issue in issues:
            if label in issue.labels:
                return issue
        return None

    def create_issue(self, title: str, body: str) -> GitHubIssue | None:
        """Create a new GitHub issue for an evolution proposal.

        Applies labels: ``bernstein-evolve``, ``auto-generated``, and
        ``evolve-hash-<hex>`` for deduplication.

        Args:
            title: Issue title (proposal title).
            body: Markdown body describing the proposal.

        Returns:
            Created ``GitHubIssue``, or ``None`` on error.
        """
        if not self.available:
            return None

        hash_label = _HASH_LABEL_PREFIX + _hash_title(title)
        label_str = ",".join([_LABEL_EVOLVE, _LABEL_AUTO, hash_label])

        self._ensure_labels([_LABEL_EVOLVE, _LABEL_AUTO, hash_label])

        args = [
            "gh",
            "issue",
            "create",
            "--title",
            title,
            "--body",
            body,
            "--label",
            label_str,
        ]
        if self._repo:
            args += ["--repo", self._repo]

        result = self._run(args)
        if result is None:
            return None

        # gh issue create outputs the issue URL on stdout (e.g.
        # "https://github.com/owner/repo/issues/123\n").
        url = result.strip()
        try:
            number = int(url.rstrip("/").split("/")[-1])
        except (ValueError, IndexError):
            logger.warning("Could not parse issue number from URL: %s", url)
            number = 0

        return GitHubIssue(
            number=number,
            title=title,
            url=url,
            labels=[_LABEL_EVOLVE, _LABEL_AUTO, hash_label],
            state="open",
        )

    def claim_issue(self, issue_number: int) -> bool:
        """Mark an issue as claimed by adding the ``evolve-claimed`` label.

        Args:
            issue_number: GitHub issue number.

        Returns:
            ``True`` if the label was added successfully.
        """
        if not self.available:
            return False

        self._ensure_labels([_LABEL_CLAIMED])
        args = [
            "gh",
            "issue",
            "edit",
            str(issue_number),
            "--add-label",
            _LABEL_CLAIMED,
        ]
        if self._repo:
            args += ["--repo", self._repo]

        return self._run(args) is not None

    def unclaim_issue(self, issue_number: int) -> bool:
        """Remove the ``evolve-claimed`` label from an issue.

        Call this when an instance abandons a claimed issue (e.g. on error)
        so another instance can pick it up.

        Args:
            issue_number: GitHub issue number.

        Returns:
            ``True`` if successful.
        """
        if not self.available:
            return False

        args = [
            "gh",
            "issue",
            "edit",
            str(issue_number),
            "--remove-label",
            _LABEL_CLAIMED,
        ]
        if self._repo:
            args += ["--repo", self._repo]

        return self._run(args) is not None

    # ------------------------------------------------------------------
    # Community evolve API
    # ------------------------------------------------------------------

    def fetch_community_issues(self) -> list[GitHubIssue]:
        """Return open community issues eligible for the evolve pipeline.

        Fetches issues labelled ``evolve-candidate`` or ``feature-request``
        that do NOT already have ``evolve-in-progress``.  Results are sorted
        by 👍 reaction count descending (community voting).

        Returns:
            List of community issues, most-upvoted first.
        """
        if not self.available:
            return []

        seen: dict[int, GitHubIssue] = {}
        for label in _COMMUNITY_LABELS:
            args = [
                "gh",
                "issue",
                "list",
                "--label",
                label,
                "--state",
                "open",
                "--json",
                "number,title,url,labels,state,body,author,reactions",
                "--limit",
                "50",
            ]
            if self._repo:
                args += ["--repo", self._repo]
            result = self._run(args)
            if result is None:
                continue
            try:
                raw: list[dict[str, Any]] = json.loads(result)
                for item in raw:
                    issue = GitHubIssue.from_gh_json(item)
                    if not issue.is_in_progress and issue.number not in seen:
                        seen[issue.number] = issue
            except (json.JSONDecodeError, KeyError):
                logger.warning("Failed to parse community issue list for label '%s'", label)

        issues = list(seen.values())
        issues.sort(key=lambda i: i.thumbs_up, reverse=True)
        return issues

    def check_is_collaborator(self, username: str) -> bool:
        """Check whether a GitHub user is a collaborator on this repo.

        Args:
            username: GitHub login to check.

        Returns:
            ``True`` if the user has collaborator access, ``False`` otherwise
            or on any error.
        """
        if not self.available or not username:
            return False

        args = ["gh", "api", f"repos/{{owner}}/{{repo}}/collaborators/{username}"]
        if self._repo:
            # gh api expands {owner}/{repo} automatically when --repo is set,
            # but we need to build the path with the literal repo slug.
            owner_repo = self._repo
            args = ["gh", "api", f"repos/{owner_repo}/collaborators/{username}"]

        result = self._run(args)
        # gh api returns HTTP 204 (empty body) if user IS a collaborator.
        # Returns non-zero exit code + error body if not.
        return result is not None

    def mark_in_progress(self, issue_number: int, comment: str | None = None) -> bool:
        """Mark a community issue as in-progress by Bernstein.

        Adds the ``evolve-in-progress`` label and optionally posts a status
        comment so the original reporter knows work has started.

        Args:
            issue_number: GitHub issue number.
            comment: Optional Markdown comment (defaults to a standard message).

        Returns:
            ``True`` if the label was added successfully.
        """
        if not self.available:
            return False

        self._ensure_labels([_LABEL_IN_PROGRESS])
        args = [
            "gh",
            "issue",
            "edit",
            str(issue_number),
            "--add-label",
            _LABEL_IN_PROGRESS,
        ]
        if self._repo:
            args += ["--repo", self._repo]

        ok = self._run(args) is not None
        if ok:
            body = comment or (
                "Bernstein is now working on this request. "
                "A pull request will be opened when the implementation is ready."
            )
            self._post_comment(issue_number, body)

        return ok

    def unmark_in_progress(self, issue_number: int) -> bool:
        """Remove the ``evolve-in-progress`` label from a community issue.

        Call this when Bernstein abandons a community issue so it can be
        picked up again later.

        Args:
            issue_number: GitHub issue number.

        Returns:
            ``True`` if the label was removed successfully.
        """
        if not self.available:
            return False

        args = [
            "gh",
            "issue",
            "edit",
            str(issue_number),
            "--remove-label",
            _LABEL_IN_PROGRESS,
        ]
        if self._repo:
            args += ["--repo", self._repo]
        return self._run(args) is not None

    def create_pr(
        self,
        title: str,
        body: str,
        head: str,
        base: str = "main",
        draft: bool = False,
    ) -> str | None:
        """Create a pull request.

        Args:
            title: PR title.
            body: PR body (Markdown).
            head: Source branch name.
            base: Target branch (default ``"main"``).
            draft: If True, open as a draft PR.

        Returns:
            PR URL string on success, ``None`` on error.
        """
        if not self.available:
            return None

        args = [
            "gh",
            "pr",
            "create",
            "--title",
            title,
            "--body",
            body,
            "--head",
            head,
            "--base",
            base,
        ]
        if draft:
            args.append("--draft")
        if self._repo:
            args += ["--repo", self._repo]

        result = self._run(args)
        return result.strip() if result else None

    def close_issue(
        self,
        issue_number: int,
        comment: str | None = None,
    ) -> bool:
        """Close an issue, optionally posting a closing comment.

        Args:
            issue_number: GitHub issue number.
            comment: Optional Markdown comment to post before closing
                (e.g. a link to the PR that implements the proposal).

        Returns:
            ``True`` if the issue was closed successfully.
        """
        if not self.available:
            return False

        if comment:
            comment_args = [
                "gh",
                "issue",
                "comment",
                str(issue_number),
                "--body",
                comment,
            ]
            if self._repo:
                comment_args += ["--repo", self._repo]
            self._run(comment_args)

        close_args = ["gh", "issue", "close", str(issue_number)]
        if self._repo:
            close_args += ["--repo", self._repo]

        return self._run(close_args) is not None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_available(self) -> bool:
        """Check if ``gh`` CLI is in PATH and authenticated.

        Returns:
            ``True`` if ``gh auth status`` exits with code 0.
        """
        try:
            result = subprocess.run(
                ["gh", "auth", "status"],
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            logger.debug("gh CLI not available or timed out during auth check")
            return False

    def _run(self, args: list[str]) -> str | None:
        """Run a ``gh`` subcommand and return decoded stdout.

        Args:
            args: Full argument list including ``"gh"`` as the first element.

        Returns:
            Decoded stdout string on success, or ``None`` if the command
            fails or times out.
        """
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            if result.returncode != 0:
                logger.debug(
                    "gh command failed (rc=%d): %s\nstderr: %s",
                    result.returncode,
                    " ".join(args[:3]),
                    result.stderr.strip(),
                )
                return None
            return result.stdout
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.debug("gh command error: %s", exc)
            return None

    def _post_comment(self, issue_number: int, body: str) -> bool:
        """Post a comment on a GitHub issue.

        Args:
            issue_number: GitHub issue number.
            body: Markdown comment body.

        Returns:
            ``True`` if the comment was posted successfully.
        """
        args = [
            "gh",
            "issue",
            "comment",
            str(issue_number),
            "--body",
            body,
        ]
        if self._repo:
            args += ["--repo", self._repo]
        return self._run(args) is not None

    def _ensure_labels(self, names: list[str]) -> None:
        """Create labels if they do not already exist.

        Uses ``--force`` so the command is idempotent.  Errors are silently
        ignored (the label may already exist or the user may lack permission).

        Args:
            names: Label names to ensure exist in the repository.
        """
        for name in names:
            color = _label_color(name)
            args = [
                "gh",
                "label",
                "create",
                name,
                "--color",
                color,
                "--force",
            ]
            if self._repo:
                args += ["--repo", self._repo]
            self._run(args)


def _label_color(name: str) -> str:
    """Return a hex color string for a label based on its semantic meaning.

    Args:
        name: Label name.

    Returns:
        6-character hex color string (without ``#``).
    """
    _colors: dict[str, str] = {
        _LABEL_EVOLVE: "0075ca",  # blue — evolution
        _LABEL_CLAIMED: "e4e669",  # yellow — claimed
        _LABEL_AUTO: "cfd3d7",  # grey — machine-generated
        _LABEL_EVOLVE_CANDIDATE: "a2eeef",  # cyan — community request
        _LABEL_FEATURE_REQUEST: "a2eeef",  # cyan — community request
        _LABEL_IN_PROGRESS: "fbca04",  # orange — in progress
        _LABEL_MAINTAINER_APPROVED: "0e8a16",  # green — trusted
    }
    if name in _colors:
        return _colors[name]
    if name.startswith(_HASH_LABEL_PREFIX):
        return "d4edda"  # light green — dedup key
    return "ededed"


# ---------------------------------------------------------------------------
# GitHub Issues -> backlog sync
# ---------------------------------------------------------------------------

# Label -> priority mapping for GitHub Issues.
# GH issues get lower priority (higher numbers) than backlog tickets
# so agents finish backlog work first, then move to GH issues.
_ISSUE_LABEL_PRIORITY: dict[str, int] = {
    "bug": 3,
    "critical": 2,
    "security": 2,
    "agent-fix": 3,
    "enhancement": 4,
    "feature": 4,
    "docs": 4,
    "documentation": 4,
    "chore": 4,
}

# Label -> role mapping (mirrors github_app/mapper.py)
_ISSUE_LABEL_ROLE: dict[str, str] = {
    "backend": "backend",
    "frontend": "frontend",
    "qa": "qa",
    "security": "security",
    "docs": "docs",
    "documentation": "docs",
    "infra": "backend",
    "devops": "backend",
}


def _priority_from_labels(labels: list[str]) -> int:
    """Determine task priority from GitHub issue labels.

    Args:
        labels: Lowercase label name strings.

    Returns:
        Priority integer (1=critical, 2=normal, 3=nice-to-have).
    """
    for label in labels:
        if label in _ISSUE_LABEL_PRIORITY:
            return _ISSUE_LABEL_PRIORITY[label]
    return 4  # default: GH issues are lower priority than backlog tickets


def _role_from_labels(labels: list[str]) -> str:
    """Determine agent role from GitHub issue labels.

    Args:
        labels: Lowercase label name strings.

    Returns:
        Role string (e.g. ``"backend"``, ``"qa"``).
    """
    for label in labels:
        if label in _ISSUE_LABEL_ROLE:
            return _ISSUE_LABEL_ROLE[label]
    return "backend"


def sync_github_issues_to_backlog(workdir: Path) -> int:
    """Fetch open GitHub Issues and create backlog YAML files for new ones.

    Runs ``gh issue list --state open`` and, for each issue that does not
    already have a corresponding ``.sdd/backlog/open/gh-{number}-*.yaml``
    file, writes a YAML-frontmatter backlog file that the orchestrator's
    ``ingest_backlog()`` / ``sync_backlog_to_server()`` will pick up.

    This is intentionally cheap: one ``gh`` call, pure file I/O, no server
    dependency.  Safe to call on every startup.

    Args:
        workdir: Project root directory (parent of ``.sdd/``).

    Returns:
        Number of new backlog files created.
    """
    backlog_open = workdir / ".sdd" / "backlog" / "open"
    backlog_open.mkdir(parents=True, exist_ok=True)

    # Fetch open issues via gh CLI.
    # ``assignees`` is read so we can skip any issue that already has a
    # human volunteer — otherwise bernstein races the contributor to
    # implement it and closes their assigned ticket out from under them
    # (incident 2026-04-11, GH#684 → GH#680 apology).
    try:
        result = subprocess.run(
            [
                "gh",
                "issue",
                "list",
                "--state",
                "open",
                "--json",
                "number,title,body,labels,assignees",
                "--limit",
                "500",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            cwd=str(workdir),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("gh issue list failed: %s", exc)
        return 0

    if result.returncode != 0:
        logger.debug(
            "gh issue list returned rc=%d: %s",
            result.returncode,
            result.stderr.strip(),
        )
        return 0

    try:
        issues: list[dict[str, Any]] = json.loads(result.stdout)
    except ValueError:
        logger.warning("Failed to parse gh issue list JSON output")
        return 0

    # Build set of issue numbers that already have backlog files.
    # Files are named  gh-{number}-{slug}.yaml
    existing_numbers: set[int] = set()
    for path in backlog_open.glob("gh-*-*.yaml"):
        m = re.match(r"gh-(\d+)-", path.name)
        if m:
            existing_numbers.add(int(m.group(1)))
    # Also check issues/, claimed/, done/, closed/ so we don't re-create
    for subdir in ("issues", "claimed", "done", "closed"):
        check_dir = workdir / ".sdd" / "backlog" / subdir
        if not check_dir.is_dir():
            continue
        for path in check_dir.glob("gh-*-*.yaml"):
            m = re.match(r"gh-(\d+)-", path.name)
            if m:
                existing_numbers.add(int(m.group(1)))

    # Also build a title dedup set from ALL files in issues/ and open/
    # (many backlog files are named road-*, agent-*, etc. — not gh-NNN-*)
    existing_titles: set[str] = set()
    for src_dir in (backlog_open, workdir / ".sdd" / "backlog" / "issues"):
        if not src_dir.is_dir():
            continue
        for path in [*src_dir.glob("*.yaml"), *src_dir.glob("*.md")]:
            try:
                import yaml as _yaml

                raw_text = path.read_text(encoding="utf-8")
                if raw_text.startswith("---"):
                    end = raw_text.find("\n---", 3)
                    if end != -1:
                        fm_parsed: dict[str, object] = dict(_yaml.safe_load(raw_text[3:end]) or {})
                        title_val = fm_parsed.get("title")
                        if title_val:
                            # Strip common prefixes like "[GH#123] " for matching
                            t = re.sub(r"^\[GH#\d+\]\s*", "", str(title_val))
                            existing_titles.add(t.lower().strip())
            except Exception:
                continue

    created = 0
    skipped_assigned = 0
    for issue in issues:
        number: int = issue.get("number", 0)
        if not number or number in existing_numbers:
            continue

        # Skip issues that have a human assignee — never race a
        # contributor. This has to live in the sync step, not the
        # spawner: once a backlog file has been created, the
        # orchestrator has no easy way to correlate it back to the
        # original GitHub issue to check assignment.
        assignees_raw: list[dict[str, Any]] = issue.get("assignees", []) or []
        if any(a.get("login") for a in assignees_raw):
            skipped_assigned += 1
            logger.info(
                "Skipping GitHub issue #%d — already assigned to %s",
                number,
                ",".join(str(a.get("login", "?")) for a in assignees_raw),
            )
            continue

        title: str = issue.get("title", "Untitled issue")
        if title.lower().strip() in existing_titles:
            continue  # Already covered by an existing backlog file

        # Task filter: skip issues that don't match the pattern (e.g., "gh-62")
        task_filter = os.environ.get("BERNSTEIN_TASK_FILTER")
        if task_filter:
            slug_preview = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60]
            filename_preview = f"gh-{number}-{slug_preview}"
            if task_filter.lower() not in filename_preview.lower():
                logger.debug("Skipping issue #%d - does not match filter '%s'", number, task_filter)
                continue

        title: str = issue.get("title", "Untitled issue")
        body: str = (issue.get("body") or "")[:500]
        labels_raw: list[dict[str, Any]] = issue.get("labels", [])
        labels: list[str] = [str(lbl.get("name", "")).lower() for lbl in labels_raw if lbl.get("name")]

        priority = _priority_from_labels(labels)
        role = _role_from_labels(labels)

        # Build a filename slug from the title
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60]
        filename = f"gh-{number}-{slug}.yaml"

        # Write YAML-frontmatter format (parsed by backlog_parser._parse_yaml_frontmatter)
        content = (
            f"---\n"
            f"id: gh-{number}\n"
            f'title: "[GH#{number}] {title}"\n'
            f"role: {role}\n"
            f"priority: {priority}\n"
            f"scope: medium\n"
            f"complexity: medium\n"
            f"type: feature\n"
            f"metadata:\n"
            f"  issue_number: {number}\n"
            f"---\n\n"
            f"# [GH#{number}] {title}\n\n"
            f"{body}\n"
        )

        file_path = backlog_open / filename
        file_path.write_text(content, encoding="utf-8")
        created += 1
        logger.info("Synced GitHub issue #%d to backlog: %s", number, filename)

    if skipped_assigned:
        logger.info(
            "Skipped %d assigned GitHub issue(s) during backlog sync",
            skipped_assigned,
        )
    return created
