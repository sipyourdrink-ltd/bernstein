"""Materialise a posted diff artifact as a git branch for the merge-preview gate (issue #6611)."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from bernstein.core.evidence.bundle import EvidenceStore
from bernstein.core.evidence.run_artifacts import read_artifact_rows, verify_run_artifacts
from bernstein.core.lineage.spine import content_hash_of

logger = logging.getLogger(__name__)


class RemoteDiffApplyError(RuntimeError):
    """A remote diff could not be applied."""

    def __init__(self, task_id: str, branch: str, detail: str) -> None:
        self.task_id = task_id
        self.branch = branch
        self.detail = detail
        message = f"task {task_id}: could not materialise remote diff on {branch}: {detail}"
        super().__init__(message)


def latest_verified_diff(sdd_dir: Path, task_id: str, *, hmac_key: bytes) -> tuple[str, str, int] | None:
    """Return the latest verified diff artifact for a given task, or None."""
    records = read_artifact_rows(sdd_dir, task_id)
    verified = {(r.key, r.version) for r in verify_run_artifacts(sdd_dir, task_id, hmac_key=hmac_key) if r.ok}

    # Pick the record with key == 'diff' and highest version whose (key, version) is in verified.
    # If none, return None.
    diff_record = None
    for r in records:
        if r.key == "diff" and (r.key, r.version) in verified:
            if diff_record is None or r.version > diff_record.version:
                diff_record = r

    if diff_record is None:
        return None

    # The payload for diff artifacts is now a dict with 'body' key.
    # It seems the test expects ArtifactPayload.report, which takes a body: str.
    # So we'll get the body from the payload directly.
    blob = EvidenceStore(sdd_dir / "evidence").get(diff_record.content_hash)
    if blob is None or content_hash_of(blob) != diff_record.content_hash:
        return None

    try:
        decoded = json.loads(blob.decode("utf-8"))
        # The prompt specifies the structure of the decoded blob as a dict with a 'body' key
        diff_text = decoded.get("body", "") if isinstance(decoded, dict) else ""
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("remote_diff_ingest: task=%s diff artifact blob is not valid JSON or unicode", task_id)
        return None

    if not diff_text:
        return None

    return (diff_text, diff_record.content_hash, diff_record.version)


def ingest_remote_diff(workdir: Path, sdd_dir: Path, task_id: str, *, hmac_key: bytes) -> str | None:
    """Materialise a posted diff artifact as a git branch."""
    found = latest_verified_diff(sdd_dir, task_id, hmac_key=hmac_key)
    if found is None:
        logger.warning("remote_diff_ingest: task=%s no verified diff artifact", task_id)
        return None

    diff_text, content_hash, version = found
    branch = f"agent/remote-{task_id}-v{version}"
    tmp = workdir / ".sdd" / "runtime" / "remote-ingest" / f"{task_id}-v{version}"

    tmp.parent.mkdir(parents=True, exist_ok=True)

    def _git(args: list[str], cwd: Path, timeout: int = 60) -> None:
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            raise RemoteDiffApplyError(task_id, branch, (result.stderr or result.stdout)[-800:])

    # Always ensure worktree is removed before attempting to add.
    # The requirement is `if tmp exists, run git -C workdir worktree remove --force tmp first.`
    # But for robustness, we can try to remove it always if it exists before trying to add it,
    # as leftover worktrees can cause issues.
    if tmp.exists():
        try:
            _git(["worktree", "remove", "--force", str(tmp)], cwd=workdir)
        except RemoteDiffApplyError as e:
            logger.debug("Failed to remove existing worktree %s before adding: %s", tmp, e.detail)

    try:
        # 1. worktree add --detach tmp HEAD, run in workdir.
        _git(["worktree", "add", "--detach", str(tmp), "HEAD"], cwd=workdir)

        # 2. write diff_text to tmp/'.remote.diff', run apply --index .remote.diff in tmp.
        diff_file = tmp / ".remote.diff"
        diff_file.write_text(diff_text)
        _git(["apply", "--index", str(diff_file)], cwd=tmp)

        # 3. unlink .remote.diff, then -c user.name=bernstein-remote-ingest -c user.email=fleet@bernstein.local commit -q -m 'task <task_id>: remote diff <content_hash>' in tmp.
        diff_file.unlink()
        _git(
            [
                "-c",
                "user.name=bernstein-remote-ingest",
                "-c",
                "user.email=fleet@bernstein.local",
                "commit",
                "-q",
                "-m",
                f"task {task_id}: remote diff {content_hash}",
            ],
            cwd=tmp,
        )

        # 4. branch -f <branch> HEAD in tmp.
        _git(["branch", "-f", branch, "HEAD"], cwd=tmp)

    finally:
        # finally: worktree remove --force tmp in workdir; ignore failure, log debug.
        try:
            _git(["worktree", "remove", "--force", str(tmp)], cwd=workdir)
        except RemoteDiffApplyError as e:
            logger.debug("Failed to remove worktree %s during cleanup: %s", tmp, e.detail)
        try:
            tmp.parent.rmdir()  # leave no empty remote-ingest directory behind
        except OSError:
            pass

    logger.info("remote_diff_ingest: task=%s branch=%s content_hash=%s", task_id, branch, content_hash)
    return branch


__all__ = ["RemoteDiffApplyError", "ingest_remote_diff", "latest_verified_diff"]
