"""Salvage uncommitted agent work before worktree cleanup (audit-088).

When an agent fails, is killed, or its worktree is otherwise reaped while it
still has dirty state, ``git worktree remove --force`` silently discards the
work.  Diffs and untracked files are lost — not even ``git reflog`` can
recover them because no ref ever pointed at the work.

This module adds a lightweight pre-cleanup step that captures any dirty
state into a durable location *before* the worktree is removed:

1. ``salvage/<session-id>`` branch created in the repo with a WIP commit
   containing every tracked modification + untracked file (except
   ``.gitignore`` matches).  Preferred path — the branch is easy to inspect
   with ``git log`` / ``git diff`` and ``git checkout``.
2. If the branch cannot be pushed to ``origin`` (no remote, network error,
   etc.) a filesystem fallback writes the diff + untracked file inventory
   to ``.sdd/runtime/salvage/<session-id>-<timestamp>/``.

The functions here are intentionally best-effort: any exception raised by
a salvage step is logged and swallowed so that cleanup itself never fails
because of a salvage failure — we'd rather lose the salvage than block the
orchestrator.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bernstein.core.git.git_basic import run_git

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_SALVAGE_DIR_REL = ".sdd/runtime/salvage"
_SALVAGE_BRANCH_PREFIX = "salvage/"
_SALVAGE_TIMEOUT_S = 30


@dataclass(frozen=True)
class SalvageResult:
    """Outcome of a salvage attempt.

    Attributes:
        salvaged: True if anything was salvaged (dirty state was present
            and was successfully captured in at least one location).
        had_changes: True if the worktree had uncommitted or untracked
            changes at the time of salvage.  ``salvaged`` implies
            ``had_changes`` but not vice-versa (dirty state could fail to
            be captured anywhere).
        branch: Name of the salvage branch created (if any).
        branch_pushed: True if the salvage branch was successfully pushed
            to the remote.
        patch_path: Filesystem path to the saved diff, if the patch
            fallback was used.  ``None`` when only the branch path was
            used.
        untracked_files: List of untracked file paths that were captured
            (purely informational).
        errors: Any non-fatal errors that occurred during salvage.
    """

    salvaged: bool
    had_changes: bool
    branch: str | None = None
    branch_pushed: bool = False
    patch_path: Path | None = None
    untracked_files: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _collect_status(worktree_path: Path) -> tuple[bool, list[str]]:
    """Return ``(has_changes, untracked_files)`` for the worktree.

    Uses ``git status --porcelain`` which already honours ``.gitignore``
    — ignored files do not appear in the output.

    Args:
        worktree_path: Path to the worktree directory.

    Returns:
        Tuple of (has_changes, untracked_files).  ``has_changes`` is True
        when the output is non-empty.  ``untracked_files`` lists paths
        prefixed with ``??`` in the porcelain output.
    """
    result = run_git(["status", "--porcelain"], worktree_path, timeout=_SALVAGE_TIMEOUT_S)
    if not result.ok:
        return False, []
    untracked: list[str] = []
    has_any = False
    for raw in result.stdout.splitlines():
        if not raw.strip():
            continue
        has_any = True
        if raw.startswith("?? "):
            untracked.append(raw[3:].strip())
    return has_any, untracked


def _salvage_dir(repo_root: Path, session_id: str, ts: int) -> Path:
    """Return (and create) the per-session salvage directory.

    Layout: ``.sdd/runtime/salvage/<session-id>-<timestamp>/``.
    """
    base = repo_root / _SALVAGE_DIR_REL
    out = base / f"{session_id}-{ts}"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _write_patch_fallback(
    worktree_path: Path,
    repo_root: Path,
    session_id: str,
    untracked_files: list[str],
    ts: int,
    diff_text: str,
) -> tuple[Path | None, list[str]]:
    """Dump the worktree's dirty state to a filesystem salvage directory.

    Produces two artefacts inside ``.sdd/runtime/salvage/<id>-<ts>/``:

    - ``diff.patch``: pre-captured ``git diff HEAD`` (tracked modifications
      only).  Must be captured *before* the branch path runs because
      ``_try_salvage_branch`` commits the work and advances HEAD.
    - ``untracked.json``: list of untracked file paths *and* their raw
      bytes base64-encoded so the content is recoverable without needing
      the original worktree.

    Args:
        worktree_path: Path to the dirty worktree.
        repo_root: Repository root used to resolve the salvage directory.
        session_id: Agent session identifier.
        untracked_files: Untracked paths previously collected from
            ``git status --porcelain``.
        ts: Unix timestamp used in the salvage directory name.
        diff_text: Pre-captured diff contents (``git diff HEAD``).

    Returns:
        Tuple of (salvage directory path, errors).  The path is ``None``
        if nothing was saved.
    """
    errors: list[str] = []
    out_dir = _salvage_dir(repo_root, session_id, ts)

    # 1. Tracked diff — pre-captured so it reflects the worktree state *before*
    #    the branch path committed the changes.
    patch_path = out_dir / "diff.patch"
    try:
        patch_path.write_text(diff_text, encoding="utf-8")
    except OSError as exc:
        errors.append(f"write diff.patch: {exc}")
        return None, errors

    # 2. Untracked files — dump name + base64 bytes so binary blobs survive.
    import base64

    untracked_payload: list[dict[str, str]] = []
    for rel in untracked_files:
        src = worktree_path / rel
        if not src.is_file():
            continue
        try:
            raw = src.read_bytes()
        except OSError as exc:
            errors.append(f"read untracked {rel}: {exc}")
            continue
        untracked_payload.append(
            {
                "path": rel,
                "base64": base64.b64encode(raw).decode("ascii"),
            }
        )
    try:
        (out_dir / "untracked.json").write_text(
            json.dumps({"session_id": session_id, "files": untracked_payload}, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        errors.append(f"write untracked.json: {exc}")

    # 3. Human-readable marker so operators can spot the salvage at a glance.
    try:
        (out_dir / "README.txt").write_text(
            "Salvaged uncommitted worktree state (audit-088).\n"
            f"session_id: {session_id}\n"
            f"timestamp: {ts}\n"
            f"untracked_count: {len(untracked_payload)}\n"
            "\n"
            "Restore with:\n"
            "  git apply diff.patch\n"
            '  python -c \'import json,base64,os;d=json.load(open("untracked.json"));\\\n'
            '    [open(f["path"],"wb").write(base64.b64decode(f["base64"])) for f in d["files"]]\'\n',
            encoding="utf-8",
        )
    except OSError as exc:
        errors.append(f"write README.txt: {exc}")

    return out_dir, errors


def _try_salvage_branch(
    worktree_path: Path,
    repo_root: Path,
    session_id: str,
) -> tuple[str | None, bool, list[str]]:
    """Attempt to capture dirty state as a ``salvage/<id>`` branch.

    Strategy:
    1. ``git add -A`` inside the worktree so tracked edits + new files land
       on the index.  ``.gitignore`` matches are skipped by ``git add`` —
       no explicit filter needed.
    2. ``git commit -m "WIP: salvage <id>"`` (allowed-empty so we still
       leave an audit marker when the diff is trivial).
    3. Rename the current branch ref to ``salvage/<id>`` via
       ``git branch -M`` so the worktree's agent branch becomes the
       salvage branch (the worktree is about to be removed anyway).
    4. Push ``salvage/<id>`` to the remote (best-effort).

    Args:
        worktree_path: Path to the dirty worktree.
        repo_root: Repository root (used to resolve the main repo for
            remote queries if needed — currently unused but kept for
            future extensions).
        session_id: Agent session identifier.

    Returns:
        Tuple of (branch name, pushed, errors).  ``branch`` is ``None``
        if the branch could not be created for any reason.
    """
    del repo_root  # reserved for future use
    errors: list[str] = []
    branch = f"{_SALVAGE_BRANCH_PREFIX}{session_id}"

    # 1. Stage everything (including untracked files, skipping .gitignore).
    add_r = run_git(["add", "-A"], worktree_path, timeout=_SALVAGE_TIMEOUT_S)
    if not add_r.ok:
        errors.append(f"git add -A: {add_r.stderr.strip()}")
        return None, False, errors

    # 2. Commit.  --allow-empty so we leave a breadcrumb even when the diff
    #    is purely whitespace or already staged-and-reverted.
    msg = f"WIP: salvage {session_id} (audit-088)"
    commit_r = run_git(
        [
            "-c",
            "user.email=bernstein@salvage.local",
            "-c",
            "user.name=bernstein-salvage",
            "commit",
            "--allow-empty",
            "-m",
            msg,
        ],
        worktree_path,
        timeout=_SALVAGE_TIMEOUT_S,
    )
    if not commit_r.ok:
        errors.append(f"git commit: {commit_r.stderr.strip()}")
        return None, False, errors

    # 3. Rename current branch to salvage/<id>.
    rename_r = run_git(["branch", "-M", branch], worktree_path, timeout=_SALVAGE_TIMEOUT_S)
    if not rename_r.ok:
        errors.append(f"git branch -M {branch}: {rename_r.stderr.strip()}")
        return None, False, errors

    # 4. Push best-effort.
    push_r = run_git(
        ["push", "--set-upstream", "origin", branch],
        worktree_path,
        timeout=60,
    )
    pushed = push_r.ok
    if not pushed:
        errors.append(f"git push origin {branch}: {push_r.stderr.strip()}")

    return branch, pushed, errors


def salvage_worktree(
    repo_root: Path,
    worktree_path: Path,
    session_id: str,
    *,
    push: bool = True,
) -> SalvageResult:
    """Capture any uncommitted work from *worktree_path* before it is removed.

    The preferred strategy is a salvage branch — it's the easiest to
    inspect (``git log salvage/<id>``, ``git diff main...salvage/<id>``).
    When the branch cannot be pushed to the remote we still keep the
    local ref, *and* we always write a filesystem patch as a belt-and-
    braces fallback so the salvage survives even if the local ref is
    garbage-collected.

    Args:
        repo_root: Repository root (used to locate ``.sdd/runtime/salvage``).
        worktree_path: Path to the worktree about to be cleaned up.
        session_id: Agent session identifier (used for branch + directory
            names).
        push: When False, skip the ``git push`` step (useful for tests
            and offline runs).

    Returns:
        SalvageResult describing what was saved.  Callers should log the
        return value so the operator can find the salvage later.
    """
    errors: list[str] = []

    if not worktree_path.exists():
        return SalvageResult(salvaged=False, had_changes=False, errors=["worktree path does not exist"])

    # Quick status probe — if the worktree is clean we exit immediately with no
    # side effects.  This is the common case for successful agent runs.
    try:
        has_changes, untracked = _collect_status(worktree_path)
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("salvage: status probe failed for %s: %s", session_id, exc)
        return SalvageResult(salvaged=False, had_changes=False, errors=[f"status probe: {exc}"])

    if not has_changes:
        return SalvageResult(salvaged=False, had_changes=False)

    ts = int(time.time())
    branch: str | None = None
    pushed = False
    patch_path: Path | None = None

    # 0. Capture the diff BEFORE the branch path — ``_try_salvage_branch``
    #    commits the dirty state, which advances HEAD and makes ``git diff HEAD``
    #    empty.  We still want the fs fallback to contain the real diff.
    diff_r = run_git(["diff", "HEAD"], worktree_path, timeout=_SALVAGE_TIMEOUT_S)
    diff_text = diff_r.stdout if diff_r.ok else ""
    if not diff_r.ok:
        errors.append(f"git diff HEAD (pre-capture): {diff_r.stderr.strip()}")

    # 1. Try the branch path first (preferred).
    try:
        branch, pushed, branch_errors = _try_salvage_branch(worktree_path, repo_root, session_id)
        errors.extend(branch_errors)
        if not push and branch is not None:
            # Caller asked us not to push — reset the flag so the result is accurate.
            pushed = False
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("salvage: branch path failed for %s: %s", session_id, exc)
        errors.append(f"branch path: {exc}")

    # 2. Always write the filesystem patch fallback.  Cheap insurance — even
    #    a successful branch push can be overwritten or force-deleted later.
    try:
        patch_path, patch_errors = _write_patch_fallback(
            worktree_path,
            repo_root,
            session_id,
            untracked,
            ts,
            diff_text,
        )
        errors.extend(patch_errors)
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("salvage: patch fallback failed for %s: %s", session_id, exc)
        errors.append(f"patch fallback: {exc}")

    salvaged = branch is not None or patch_path is not None

    if salvaged:
        logger.warning(
            "Salvaged uncommitted work for session %s: branch=%s pushed=%s patch=%s untracked=%d",
            session_id,
            branch,
            pushed,
            patch_path,
            len(untracked),
        )
    else:
        logger.error(
            "Failed to salvage uncommitted work for session %s — changes may be lost. errors=%s",
            session_id,
            errors,
        )

    return SalvageResult(
        salvaged=salvaged,
        had_changes=True,
        branch=branch,
        branch_pushed=pushed,
        patch_path=patch_path,
        untracked_files=list(untracked),
        errors=errors,
    )
