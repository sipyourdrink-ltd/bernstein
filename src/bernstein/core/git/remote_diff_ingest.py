"""Materialise a posted diff artifact as a git branch for the merge-preview gate (issue #6611)."""

from __future__ import annotations

import contextlib
import json
import logging
import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
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
    """Return ``(diff_text, content_hash, version)`` of the newest verified ``diff`` artifact, or None."""
    found = latest_verified_record(sdd_dir, task_id, "diff", hmac_key=hmac_key)
    if found is None:
        return None
    decoded, content_hash, version = found
    diff_text = decoded.get("body", "") if isinstance(decoded, dict) else ""
    if not isinstance(diff_text, str) or not diff_text:
        return None
    return diff_text, content_hash, version


def latest_verified_record(sdd_dir: Path, task_id: str, key: str, *, hmac_key: bytes) -> tuple[Any, str, int] | None:
    """Return ``(decoded_content, content_hash, version)`` of the newest verified artifact under ``key``.

    The blob is re-hashed against the journal row before it is decoded, so a
    tampered or missing blob reads as "no artifact" rather than as content.
    """
    records = read_artifact_rows(sdd_dir, task_id)
    verified = {(r.key, r.version) for r in verify_run_artifacts(sdd_dir, task_id, hmac_key=hmac_key) if r.ok}
    record = None
    for r in records:
        if r.key == key and (r.key, r.version) in verified and (record is None or r.version > record.version):
            record = r
    if record is None:
        return None
    blob = EvidenceStore(sdd_dir / "evidence").get(record.content_hash)
    if blob is None or content_hash_of(blob) != record.content_hash:
        return None
    try:
        decoded = json.loads(blob.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("remote_diff_ingest: task=%s artifact %r blob is not valid JSON", task_id, key)
        return None
    return decoded, record.content_hash, record.version


def _materialise_branch(
    workdir: Path, task_id: str, version: int, content_hash: str, write: Callable[[Path], None]
) -> str:
    """Run ``write`` inside a detached throwaway worktree at HEAD and publish the result as a branch."""
    branch = f"agent/remote-{task_id}-v{version}"
    tmp = workdir / ".sdd" / "runtime" / "remote-ingest" / f"{task_id}-v{version}"
    tmp.parent.mkdir(parents=True, exist_ok=True)

    def _git(args: list[str], cwd: Path, timeout: int = 60) -> None:
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            raise RemoteDiffApplyError(task_id, branch, (result.stderr or result.stdout)[-800:])

    if tmp.exists():
        try:
            _git(["worktree", "remove", "--force", str(tmp)], cwd=workdir)
        except RemoteDiffApplyError as e:
            logger.debug("Failed to remove existing worktree %s before adding: %s", tmp, e.detail)
    try:
        _git(["worktree", "add", "--detach", str(tmp), "HEAD"], cwd=workdir)
        write(tmp)
        _git(["add", "-A"], cwd=tmp)
        _git(
            [
                "-c",
                "user.name=bernstein-remote-ingest",
                "-c",
                "user.email=fleet@bernstein.local",
                "commit",
                "-q",
                "-m",
                f"task {task_id}: remote artifact {content_hash}",
            ],
            cwd=tmp,
        )
        _git(["branch", "-f", branch, "HEAD"], cwd=tmp)
    finally:
        try:
            _git(["worktree", "remove", "--force", str(tmp)], cwd=workdir)
        except RemoteDiffApplyError as e:
            logger.debug("Failed to remove worktree %s during cleanup: %s", tmp, e.detail)
        with contextlib.suppress(OSError):
            tmp.parent.rmdir()  # leave no empty remote-ingest directory behind
    logger.info("remote_diff_ingest: task=%s branch=%s content_hash=%s", task_id, branch, content_hash)
    return branch


def ingest_remote_artifact(workdir: Path, sdd_dir: Path, task: Any, *, hmac_key: bytes) -> str | None:
    """Materialise a posted artifact-mode output at the task's declared ``output_path`` on a branch.

    ``task`` is an artifact-mode :class:`~bernstein.core.models.Task`; the
    artifact is looked up under key ``artifact`` (the default slot) and written
    as its ``body`` text, or as canonical JSON when the content has no body.
    """
    from bernstein.core.tasks.artifact_completion import artifact_output_path

    found = latest_verified_record(sdd_dir, task.id, "artifact", hmac_key=hmac_key)
    if found is None:
        logger.warning("remote_diff_ingest: task=%s no verified 'artifact' artifact", task.id)
        return None
    decoded, content_hash, version = found
    relpath = artifact_output_path(task)
    if isinstance(decoded, dict) and isinstance(decoded.get("body"), str):
        payload = decoded["body"].encode("utf-8")
    else:
        payload = json.dumps(decoded, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def _write(tmp: Path) -> None:
        target = tmp / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)

    return _materialise_branch(workdir, task.id, version, content_hash, _write)


def ingest_remote_diff(workdir: Path, sdd_dir: Path, task_id: str, *, hmac_key: bytes) -> str | None:
    """Materialise a posted ``diff`` artifact as the branch ``agent/remote-<task>-v<n>``.

    Returns None when no verified diff exists; raises
    :class:`RemoteDiffApplyError` when the diff does not apply onto HEAD.
    """
    found = latest_verified_diff(sdd_dir, task_id, hmac_key=hmac_key)
    if found is None:
        logger.warning("remote_diff_ingest: task=%s no verified 'diff' artifact", task_id)
        return None
    diff_text, content_hash, version = found

    def _apply(tmp: Path) -> None:
        diff_file = tmp / ".remote.diff"
        diff_file.write_text(diff_text)
        try:
            result = subprocess.run(
                ["git", "apply", "--index", str(diff_file)], cwd=tmp, capture_output=True, text=True, timeout=60
            )
        finally:
            diff_file.unlink(missing_ok=True)
        if result.returncode != 0:
            raise RemoteDiffApplyError(
                task_id, f"agent/remote-{task_id}-v{version}", (result.stderr or result.stdout)[-800:]
            )

    return _materialise_branch(workdir, task_id, version, content_hash, _apply)


__all__ = [
    "RemoteDiffApplyError",
    "ingest_remote_artifact",
    "ingest_remote_diff",
    "latest_verified_diff",
    "latest_verified_record",
]
