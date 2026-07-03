"""Basic git operations: run, status, staging, committing."""

from __future__ import annotations

import logging
import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_CONVENTIONAL_COMMIT_RE = re.compile(r"^(feat|fix|chore|docs|test|refactor)(\([a-z0-9._/-]+\))?: .+")

# Paths that must NEVER be staged, even via explicit add.
#
# Extended (defect 28 - decoy commit / secret leak): the previous list
# only excluded ``.sdd/runtime/`` and ``.sdd/metrics/``.  The decoy commit
# ``7e2364e`` on ``main`` (see
# ``work/bernstein/proofs/d2/minimax/attempt-83808a8a/DIAGNOSIS.md``)
# swept 83 ``.sdd/*`` files including ``.sdd/attestations/ed25519-signing-key.pem``,
# ``.sdd/auth/agent_identity_jwt_secret``, ``.sdd/runtime/agent_tokens/``,
# and ``bernstein.yaml`` into a single commit on a default branch.  The
# merge-preflight safety guard in :mod:`bernstein.core.git.git_pr` blocks
# the COMMIT, but to close the gap at the staging layer too we extend the
# deny list with every prefix that must never reach a default branch.
_NEVER_STAGE: frozenset[str] = frozenset(
    {
        ".sdd/",
        "attestations/",
        "auth/",
        ".sdd/runtime/",
        ".sdd/metrics/",
        ".sdd/attestations/",
        ".sdd/auth/",
        ".sdd/traces/",
        "bernstein.yaml",
        ".claude/mcp.json",
        ".env",
        "*.pid",
        "*.log",
    }
)


@dataclass(frozen=True)
class GitResult:
    """Outcome of a single git command.

    Attributes:
        returncode: Exit code (0 = success).
        stdout: Captured standard output.
        stderr: Captured standard error.
    """

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


# ------------------------------------------------------------------
# Low-level runner
# ------------------------------------------------------------------


def run_git(
    args: list[str],
    cwd: Path,
    *,
    timeout: int = 30,
    input_data: str | None = None,
    check: bool = False,
) -> GitResult:
    """Execute a git command and return structured output.

    Args:
        args: Git sub-command and arguments (e.g. ``["status", "--porcelain"]``).
        cwd: Working directory for the command.
        timeout: Seconds before the command is killed.
        input_data: Optional stdin content.
        check: If True, raise on non-zero exit code.

    Returns:
        GitResult with returncode, stdout, stderr.

    Raises:
        subprocess.CalledProcessError: When *check* is True and the command fails.
        subprocess.TimeoutExpired: When the command exceeds *timeout*.
    """
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        input=input_data,
    )
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            ["git", *args],
            output=result.stdout,
            stderr=result.stderr,
        )
    return GitResult(
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )


# ------------------------------------------------------------------
# Queries (read-only git operations used by write workflows)
# ------------------------------------------------------------------


def is_git_repo(cwd: Path) -> bool:
    """Return True if *cwd* is inside a git work tree."""
    r = run_git(["rev-parse", "--is-inside-work-tree"], cwd)
    return r.ok


def status_porcelain(cwd: Path) -> str:
    """Return raw ``git status --porcelain`` output."""
    return run_git(["status", "--porcelain"], cwd).stdout.strip()


def diff_cached_names(cwd: Path) -> list[str]:
    """Return list of staged file paths."""
    r = run_git(["diff", "--cached", "--name-only"], cwd)
    return [f.strip() for f in r.stdout.strip().splitlines() if f.strip()]


def diff_cached_stat(cwd: Path) -> str:
    """Return ``git diff --cached --stat`` output for scope detection."""
    return run_git(["diff", "--cached", "--stat"], cwd).stdout.strip()


def diff_cached(cwd: Path) -> str:
    """Return full ``git diff --cached`` output."""
    return run_git(["diff", "--cached"], cwd).stdout


def diff_head(cwd: Path, files: list[str] | None = None, refs: str = "HEAD~1") -> str:
    """Return ``git diff <refs> -- [files]``."""
    cmd = ["diff", refs, "--"]
    if files:
        cmd.extend(files)
    r = run_git(cmd, cwd, timeout=30)
    return r.stdout.strip()


def rev_parse_head(cwd: Path) -> str:
    """Return the current HEAD commit hash."""
    return run_git(["rev-parse", "HEAD"], cwd).stdout.strip()


# ------------------------------------------------------------------
# Staging
# ------------------------------------------------------------------


def stage_files(cwd: Path, paths: list[str]) -> None:
    """Stage specific file paths.

    Args:
        cwd: Repository root.
        paths: Relative file paths to stage.
    """
    safe = [p for p in paths if not any(pat.rstrip("*") in p for pat in _NEVER_STAGE)]
    if safe:
        run_git(["add", "--", *safe], cwd)


def stage_task_files(cwd: Path, owned_files: list[str]) -> list[str]:
    """Stage only files owned by a task and co-located new files.

    Never stages runtime artifacts, .env, *.pid, *.log.

    Args:
        cwd: Repository root.
        owned_files: File paths the task is allowed to touch.

    Returns:
        List of actually staged paths.
    """
    if not owned_files:
        return []

    # Also find new untracked files in the same directories
    dirs = {str(Path(f).parent) for f in owned_files}
    status_out = status_porcelain(cwd)
    extra: list[str] = []
    for line in status_out.splitlines():
        if line.startswith("??"):
            fpath = line[3:].strip()
            parent = str(Path(fpath).parent)
            if parent in dirs:
                extra.append(fpath)

    all_files = list(dict.fromkeys(owned_files + extra))  # dedupe, preserve order
    safe = [p for p in all_files if not any(pat.rstrip("*") in p for pat in _NEVER_STAGE)]
    if safe:
        run_git(["add", "--", *safe], cwd)
    return safe


def unstage_paths(cwd: Path, paths: list[str]) -> None:
    """Unstage specific paths via ``git reset HEAD``."""
    run_git(["reset", "HEAD", "--", *paths], cwd)


def stage_all_except(cwd: Path, exclude: list[str] | None = None) -> None:
    """Stage everything, then unstage excluded paths.

    Args:
        cwd: Repository root.
        exclude: Paths/globs to unstage after the bulk add.
    """
    run_git(["add", "-A"], cwd)
    to_unstage = list(exclude or [])
    # Always unstage never-stage paths that are directories
    for pat in _NEVER_STAGE:
        if pat.endswith("/"):
            to_unstage.append(pat)
    if to_unstage:
        run_git(["reset", "HEAD", "--", *to_unstage], cwd)


# ------------------------------------------------------------------
# Committing
# ------------------------------------------------------------------


def is_conventional_commit_message(message: str) -> bool:
    """Return True when *message* has a conventional-commit subject line."""
    subject = ""
    for line in message.splitlines():
        if line.strip():
            subject = line.strip()
            break
    if not subject:
        return False
    return bool(_CONVENTIONAL_COMMIT_RE.match(subject))


def commit(cwd: Path, message: str, *, enforce_conventional: bool = False) -> GitResult:
    """Create a commit with the given message.

    Args:
        cwd: Repository root.
        message: Commit message.
        enforce_conventional: When True, rejects non-conventional subjects.
    """
    if enforce_conventional and not is_conventional_commit_message(message):
        return GitResult(
            returncode=1,
            stdout="",
            stderr="commit message must follow conventional format: type(scope): summary",
        )
    return run_git(["commit", "-m", message], cwd)


def conventional_commit(
    cwd: Path,
    *,
    task_title: str | None = None,
    task_id: str | None = None,
    evolve: bool = False,
) -> GitResult:
    """Generate a conventional commit message from the staged diff and commit.

    Parses ``git diff --cached`` to determine change type and scope.
    No LLM call - purely deterministic.

    Args:
        cwd: Repository root.
        task_title: Optional task title to use as description.
        task_id: Optional task ID for ``Refs:`` footer.
        evolve: If True, prefix with ``evolve`` scope.

    Returns:
        GitResult from the commit command.
    """
    staged_files = diff_cached_names(cwd)
    if not staged_files:
        return GitResult(returncode=1, stdout="", stderr="nothing staged")

    diff_stat = diff_cached_stat(cwd)
    full_diff = diff_cached(cwd)

    # Detect change type from diff content
    change_type = _classify_change(staged_files, full_diff)
    scope = _detect_scope(staged_files)

    if evolve:
        scope = "evolution"

    # Build subject line
    if task_title:
        subject = f"{change_type}({scope}): {_truncate(task_title, 60)}"
    else:
        subject = f"{change_type}({scope}): {_summarize_from_files(staged_files)}"

    # Build body from diff stat
    body_lines = _diff_stat_to_bullets(diff_stat, max_bullets=5)

    # Build footer
    footer_parts: list[str] = []
    if task_id:
        footer_parts.append(f"Refs: #{task_id}")
    footer_parts.append("Co-Authored-By: bernstein[bot] <noreply@bernstein.dev>")

    # Assemble
    parts = [subject]
    if body_lines:
        parts.append("")
        parts.extend(body_lines)
    parts.append("")
    parts.extend(footer_parts)

    message = "\n".join(parts)
    return commit(cwd, message, enforce_conventional=True)


# ------------------------------------------------------------------
# Push / Fetch / Rebase
# ------------------------------------------------------------------


def fetch(cwd: Path, remote: str = "origin") -> GitResult:
    """Fetch from remote."""
    return run_git(["fetch", remote], cwd, timeout=60)


def _safe_abort(cwd: Path, kind: str) -> None:
    """Best-effort ``git <kind> --abort`` that never raises.

    Used on fallback failure paths to guarantee the repository is not left
    in a conflicted MERGING or REBASING state.  Swallows subprocess errors
    so the caller can always surface the original failure to its caller.

    Args:
        cwd: Repository root.
        kind: Either ``"rebase"`` or ``"merge"``.
    """
    try:
        run_git([kind, "--abort"], cwd, timeout=10)
    except (subprocess.SubprocessError, OSError) as exc:  # pragma: no cover - defensive
        logger.warning("git %s --abort failed: %s", kind, exc)


def _rebase_or_merge(cwd: Path, remote: str, branch: str) -> GitResult | None:
    """Attempt rebase, falling back to merge on failure.

    On any failure path (rebase exception, merge exception, merge conflict)
    the repository is cleaned up with ``git rebase --abort`` and/or
    ``git merge --abort`` so the next caller does not trip over a
    pre-existing MERGING/REBASING state.

    Args:
        cwd: Repository root.
        remote: Remote name.
        branch: Branch name.

    Returns:
        Failed merge result if both rebase and merge fail, else None.
    """
    try:
        rebase_r = run_git(["rebase", f"{remote}/{branch}"], cwd, timeout=120)
    except (subprocess.SubprocessError, OSError) as exc:
        logger.error("Rebase raised %s; aborting rebase", exc)
        _safe_abort(cwd, "rebase")
        return GitResult(returncode=1, stdout="", stderr=f"rebase raised: {exc}")

    if rebase_r.ok:
        return None

    logger.warning("Rebase failed, aborting and falling back to merge")
    _safe_abort(cwd, "rebase")

    try:
        merge_r = run_git(["merge", f"{remote}/{branch}", "--no-edit"], cwd, timeout=120)
    except (subprocess.SubprocessError, OSError) as exc:
        logger.error("Merge fallback raised %s; aborting merge", exc)
        _safe_abort(cwd, "merge")
        return GitResult(returncode=1, stdout="", stderr=f"merge raised: {exc}")

    if not merge_r.ok:
        logger.error("Merge fallback also failed: %s", merge_r.stderr)
        # Ensure the repo is not left in a conflicted MERGING state so the
        # next safe_push iteration (or any other git write) is not blocked.
        _safe_abort(cwd, "merge")
        return merge_r
    return None


_TRANSIENT_MARKERS = (
    "unable to access",
    "could not read",
    "connection",
    "timed out",
    "timeout",
    "reset by peer",
    "non-fast-forward",
    "fetch first",
    "cannot lock ref",
)


def _is_transient_push_error(stderr: str) -> bool:
    """Check if a push failure is transient and worth retrying.

    Args:
        stderr: Stderr output from the failed push.

    Returns:
        True if the error matches a known transient pattern.
    """
    lower = stderr.lower()
    return any(marker in lower for marker in _TRANSIENT_MARKERS)


def remote_exists(cwd: Path, remote: str = "origin") -> bool:
    """Return True when *remote* is configured for the repo at *cwd*.

    Used by :func:`safe_push` to no-op cleanly on local-only repos (the
    playground / GUI-smoke checkouts).  Previously every agent reap would
    log ``fatal: 'origin' does not appear to be a git repository`` because
    we blindly called ``git fetch origin`` and ``git push origin main``.
    """
    result = run_git(["remote", "get-url", remote], cwd, timeout=5)
    return result.ok


def _run_git_quiet(args: list[str], cwd: Path) -> GitResult:
    """Run a read-only git query, converting OS-level errors into a failed
    :class:`GitResult` instead of raising.

    Used by the branch resolvers below so they honour their "never raises"
    contract even when *cwd* does not exist, git is not installed, or the
    command times out.
    """
    try:
        return run_git(args, cwd, timeout=5)
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        logger.debug("git %s failed at %s: %s", args[0] if args else "?", cwd, exc)
        return GitResult(returncode=1, stdout="", stderr=str(exc))


def current_branch(cwd: Path) -> str | None:
    """Return the branch currently checked out at *cwd*, or ``None``.

    ``None`` covers detached-HEAD and any git failure. Never raises.
    """
    result = _run_git_quiet(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    if not result.ok:
        return None
    name = result.stdout.strip()
    if not name or name == "HEAD":  # detached HEAD
        return None
    return name


def _resolve_default_branch_from_remote_head(cwd: Path, remote: str) -> str | None:
    """Return the default branch named by ``<remote>/HEAD``, or ``None``.

    ``None`` means ``<remote>/HEAD`` is unset (common in ``git worktree``
    checkouts and partial clones where ``git remote set-head`` was never run),
    so callers must fall back to a heuristic and treat the result as ambiguous.
    """
    head = _run_git_quiet(["symbolic-ref", f"refs/remotes/{remote}/HEAD"], cwd)
    if head.ok:
        ref = head.stdout.strip()
        prefix = f"refs/remotes/{remote}/"
        if ref.startswith(prefix):
            return ref.removeprefix(prefix)

    abbrev = _run_git_quiet(["rev-parse", "--abbrev-ref", f"{remote}/HEAD"], cwd)
    if abbrev.ok:
        ref = abbrev.stdout.strip()
        if ref and ref != f"{remote}/HEAD":
            return ref.removeprefix(f"{remote}/")

    return None


def _branch_exists(cwd: Path, branch: str) -> bool:
    """Return ``True`` if a local ``branch`` ref exists at *cwd*."""
    probe = _run_git_quiet(["rev-parse", "--verify", "--quiet", branch], cwd)
    return probe.ok


def resolve_default_branch(cwd: Path, remote: str = "origin") -> str:
    """Resolve the repository's default branch (the protected trunk).

    Resolution order: ``<remote>/HEAD`` first (the authoritative signal), then
    ``init.defaultBranch`` from git config, then the ``<remote>/master``
    remote-tracking ref, then the conventional ``main``/``master`` local probe,
    and finally ``"main"`` as a last resort. Consulting ``init.defaultBranch``
    and ``<remote>/master`` BEFORE the hard-coded ``main``-first local probe
    means a repo whose real trunk is ``master`` is not mis-resolved to a stray
    local ``main`` when ``<remote>/HEAD`` is unavailable. Never raises.

    Args:
        cwd: Repository root.
        remote: Remote whose ``HEAD`` symbolic ref names the default branch.

    Returns:
        The default branch short name (e.g. ``"main"`` or ``"master"``).
    """
    from_head = _resolve_default_branch_from_remote_head(cwd, remote)
    if from_head is not None:
        return from_head

    cfg = _run_git_quiet(["config", "--get", "init.defaultBranch"], cwd)
    if cfg.ok and cfg.stdout.strip():
        configured = cfg.stdout.strip()
        if _branch_exists(cwd, configured):
            return configured

    master_tracking = _run_git_quiet(["rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/master"], cwd)
    if master_tracking.ok:
        return "master"

    for candidate in ("main", "master"):
        if _branch_exists(cwd, candidate):
            return candidate

    if cfg.ok and cfg.stdout.strip():
        return cfg.stdout.strip()

    return "main"


def protected_default_branches(cwd: Path, remote: str = "origin") -> frozenset[str]:
    """Return the set of branch names that must be treated as protected trunks.

    Normally this is the single :func:`resolve_default_branch` result. But when
    ``<remote>/HEAD`` is unavailable (unset ``set-head``, partial clone, some
    ``git worktree`` checkouts) AND both a local ``main`` and a local ``master``
    exist, the true trunk is genuinely ambiguous: neither the local probe order
    nor the remote-tracking heuristic can be trusted to pick correctly. In that
    ambiguous state this returns BOTH names so a merge guard fails closed and
    refuses to land unreviewed commits on either candidate. Never raises.

    Args:
        cwd: Repository root.
        remote: Remote whose ``HEAD`` symbolic ref names the default branch.

    Returns:
        A frozenset of protected branch short names (usually one, two when the
        default is ambiguous).
    """
    from_head = _resolve_default_branch_from_remote_head(cwd, remote)
    if from_head is not None:
        return frozenset({from_head})

    if _branch_exists(cwd, "main") and _branch_exists(cwd, "master"):
        return frozenset({"main", "master"})

    return frozenset({resolve_default_branch(cwd, remote)})


def safe_push(
    cwd: Path,
    branch: str,
    remote: str = "origin",
    *,
    max_retries: int = 3,
    retry_delay: float = 2.0,
) -> GitResult:
    """Fetch, rebase if behind, then push with retry on transient errors.

    Retries the push up to *max_retries* times with a fixed delay between
    attempts.  Only transient errors (network, auth timeout, remote
    unavailable) are retried; persistent failures like rejected non-fast-
    forward pushes fail immediately.

    No-ops cleanly when ``remote`` is not configured - local-only repos
    (the GUI-smoke playground, ephemeral test fixtures) used to log a noisy
    ``fatal: 'origin' does not appear to be a git repository`` per agent
    reap.  We now detect the missing remote and return a successful
    no-push :class:`GitResult` instead.

    Args:
        cwd: Repository root.
        branch: Branch name to push (``"master"`` is auto-corrected to ``"main"``).
        remote: Remote name (default "origin").
        max_retries: Number of push retry attempts on transient errors.
        retry_delay: Seconds to wait between push retries.

    Returns:
        GitResult from the push command.
    """

    # Guardrail: never push to "master" - auto-correct to "main".
    if branch == "master":
        logger.info("safe_push: correcting branch 'master' -> 'main'")
        branch = "main"

    # Local-only repo: no remote → no push.  Silent no-op so the spawner
    # merge loop doesn't spam ``fatal: 'origin' does not appear...`` per
    # agent reap.  Treat as a successful skip so callers don't log a
    # warning either.
    if not remote_exists(cwd, remote):
        logger.debug("safe_push: remote %r not configured at %s; skipping push", remote, cwd)
        return GitResult(returncode=0, stdout="", stderr=f"remote {remote!r} not configured; push skipped")

    fetch_result = fetch(cwd, remote)
    if not fetch_result.ok:
        logger.warning("git fetch failed: %s", fetch_result.stderr)

    behind_r = run_git(["rev-list", "--count", f"HEAD..{remote}/{branch}"], cwd)
    behind = int(behind_r.stdout.strip()) if behind_r.ok and behind_r.stdout.strip().isdigit() else 0

    if behind > 0:
        logger.info("Branch %s is %d commits behind %s/%s, rebasing", branch, behind, remote, branch)
        fail = _rebase_or_merge(cwd, remote, branch)
        if fail is not None:
            return fail

    return _push_with_retry(cwd, branch, remote, max_retries, retry_delay)


def _push_with_retry(
    cwd: Path,
    branch: str,
    remote: str,
    max_retries: int,
    retry_delay: float,
) -> GitResult:
    """Push to remote with retry on transient errors."""
    import time as _time

    push_result = GitResult(returncode=1, stdout="", stderr="no push attempted")
    for attempt in range(max_retries + 1):
        if attempt > 0:
            fetch(cwd, remote)
            _rebase_or_merge(cwd, remote, branch)
        push_result = run_git(["push", remote, branch], cwd, timeout=60)
        if push_result.ok:
            return push_result
        if not _is_transient_push_error(push_result.stderr) or attempt >= max_retries:
            if attempt > 0:
                logger.error("git push failed after %d attempts: %s", attempt + 1, push_result.stderr)
            return push_result
        logger.warning(
            "git push attempt %d/%d failed (transient): %s -- retrying in %.0fs",
            attempt + 1,
            max_retries + 1,
            push_result.stderr.strip(),
            retry_delay,
        )
        _time.sleep(retry_delay)
    return push_result


# ------------------------------------------------------------------
# Checkout / Revert
# ------------------------------------------------------------------


def checkout_discard(cwd: Path) -> GitResult:
    """Discard all unstaged changes (``git checkout -- .``)."""
    return run_git(["checkout", "--", "."], cwd)


def revert_commit(cwd: Path, commit_hash: str, *, no_commit: bool = True) -> GitResult:
    """Revert a commit.

    Args:
        cwd: Repository root.
        commit_hash: Hash to revert.
        no_commit: If True, stage the revert without committing.
    """
    cmd = ["revert"]
    if no_commit:
        cmd.append("--no-commit")
    cmd.append(commit_hash)
    return run_git(cmd, cwd, timeout=30)


# ------------------------------------------------------------------
# Tags
# ------------------------------------------------------------------


def tag(cwd: Path, name: str, message: str | None = None) -> GitResult:
    """Create a git tag.

    Args:
        cwd: Repository root.
        name: Tag name.
        message: Optional annotated tag message.
    """
    cmd = ["tag"]
    if message:
        cmd.extend(["-a", name, "-m", message])
    else:
        cmd.append(name)
    return run_git(cmd, cwd)


# Alias for discoverability
create_tag = tag


def list_tags(cwd: Path, pattern: str | None = None) -> list[str]:
    """Return tag names, optionally filtered by glob pattern.

    Args:
        cwd: Repository root.
        pattern: Optional glob (e.g. ``"v*"``).

    Returns:
        List of tag names, newest first.
    """
    cmd = ["tag", "-l", "--sort=-version:refname"]
    if pattern:
        cmd.append(pattern)
    r = run_git(cmd, cwd, timeout=10)
    if not r.ok or not r.stdout.strip():
        return []
    return [t.strip() for t in r.stdout.strip().splitlines() if t.strip()]


def _parse_latest_version(tags: list[str], tag_prefix: str) -> tuple[int, int, int, str | None]:
    """Extract the latest semver from tags.

    Args:
        tags: Tag names sorted by recency.
        tag_prefix: Prefix to strip (e.g. ``"v"``).

    Returns:
        (major, minor, patch, latest_tag) - tag is None if no valid tag found.
    """
    for t in tags:
        stripped = t.lstrip(tag_prefix)
        parts = stripped.split(".")
        if len(parts) >= 3 and all(p.isdigit() for p in parts[:3]):
            return int(parts[0]), int(parts[1]), int(parts[2]), t
    return 0, 0, 0, None


def _compute_bump(subjects: list[str]) -> str:
    """Determine semver bump level from conventional commit subjects.

    Args:
        subjects: Commit subject lines.

    Returns:
        One of ``"major"``, ``"minor"``, or ``"patch"``.
    """
    for subj in subjects:
        lower = subj.lower()
        if "breaking change" in lower or "!:" in subj:
            return "major"
    if any(subj.lower().startswith("feat") for subj in subjects):
        return "minor"
    return "patch"


def version_from_commits(cwd: Path, tag_prefix: str = "v") -> str:
    """Compute the next semantic version from conventional commits since the last tag.

    Scans commit messages between the latest ``v*`` tag and HEAD.  Bumps
    major on ``BREAKING CHANGE`` or ``!:`` in a subject, minor on ``feat``,
    patch on anything else (``fix``, ``refactor``, ``chore``, etc.).

    If no previous tag exists, starts from ``0.1.0``.

    Args:
        cwd: Repository root.
        tag_prefix: Prefix for version tags (default ``"v"``).

    Returns:
        Next version string *without* the prefix (e.g. ``"1.3.0"``).
    """
    tags = list_tags(cwd, pattern=f"{tag_prefix}*")
    major, minor, patch_v, latest_tag = _parse_latest_version(tags, tag_prefix)

    log_cmd = ["log", "--pretty=format:%s"]
    if latest_tag:
        log_cmd.insert(1, f"{latest_tag}..HEAD")
    log_r = run_git(log_cmd, cwd, timeout=10)

    has_commits = log_r.ok and bool(log_r.stdout.strip())
    subjects = log_r.stdout.strip().splitlines() if has_commits else []

    if subjects:
        bump = _compute_bump(subjects)
        if bump == "major":
            major += 1
            minor = 0
            patch_v = 0
        elif bump == "minor":
            minor += 1
            patch_v = 0
        else:
            patch_v += 1

    return f"{major}.{minor}.{patch_v}"


# ------------------------------------------------------------------
# Conventional commit helpers (private)
# ------------------------------------------------------------------


_NEW_FILE_RE = re.compile(r"^new file mode", re.MULTILINE)
_RENAME_RE = re.compile(r"^rename from", re.MULTILINE)
_DELETE_RE = re.compile(r"^deleted file mode", re.MULTILINE)


def _classify_change(staged_files: list[str], diff: str) -> str:
    """Classify the change type from file list and diff content."""
    all_tests = all("test" in f for f in staged_files)
    if all_tests:
        return "test"

    all_docs = all(f.endswith((".md", ".rst", ".txt")) or "docs/" in f for f in staged_files)
    if all_docs:
        return "docs"

    all_config = all(
        f.endswith((".toml", ".yaml", ".yml", ".json", ".cfg", ".ini")) or f in {"Makefile", "Dockerfile", ".gitignore"}
        for f in staged_files
    )
    if all_config:
        return "chore"

    # Check for new files (feat) vs modifications (refactor/fix)
    new_count = len(_NEW_FILE_RE.findall(diff))
    delete_count = len(_DELETE_RE.findall(diff))
    rename_count = len(_RENAME_RE.findall(diff))

    if new_count > 0 and new_count >= len(staged_files) // 2:
        return "feat"
    if delete_count > len(staged_files) // 2:
        return "refactor"
    if rename_count > 0:
        return "refactor"

    return "feat" if new_count > 0 else "refactor"


def _detect_scope(staged_files: list[str]) -> str:
    """Detect the primary scope from staged file paths."""
    if not staged_files:
        return "unknown"

    # Count directory occurrences to find the dominant scope
    dirs: Counter[str] = Counter()
    for f in staged_files:
        parts = Path(f).parts
        if len(parts) >= 3 and parts[0] == "src" and parts[1] == "bernstein":
            dirs[parts[2]] += 1
        elif parts and parts[0] == "tests":
            dirs["tests"] += 1
        elif parts:
            dirs[parts[0]] += 1

    if not dirs:
        return "unknown"

    top_result = dirs.most_common(1)
    if top_result:
        return top_result[0][0]
    return "unknown"


def _summarize_from_files(staged_files: list[str]) -> str:
    """Generate a short summary from changed file names."""
    if not staged_files:
        return "housekeeping"

    names = [Path(f).stem for f in staged_files[:3]]
    summary = ", ".join(names)
    if len(staged_files) > 3:
        summary += f" (+{len(staged_files) - 3} more)"
    return _truncate(summary, 60)


def _truncate(text: str, max_len: int) -> str:
    """Truncate text to max_len, adding ellipsis if needed."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _diff_stat_to_bullets(stat_output: str, max_bullets: int = 5) -> list[str]:
    """Convert ``git diff --stat`` output to markdown bullet points."""
    lines = stat_output.strip().splitlines()
    if not lines:
        return []

    bullets: list[str] = []
    for line in lines[:max_bullets]:
        line = line.strip()
        if line and "|" in line:
            # "path/to/file.py | 42 ++++---"
            parts = line.split("|", 1)
            fname = parts[0].strip()
            changes = parts[1].strip() if len(parts) > 1 else ""
            bullets.append(f"- {fname}: {changes}")
        elif line and "changed" in line:
            # Summary line: "3 files changed, 100 insertions(+), 20 deletions(-)"
            bullets.append(f"- {line}")

    return bullets
