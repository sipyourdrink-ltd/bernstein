"""Artifact provenance for the work a CLI agent lands through a merge.

A CLI adapter spawns a subprocess that writes files directly in its
worktree, so those writes never cross :func:`record_artifact_write` -- the
single in-process write boundary. What was left on that path was a spine
holding one row, the run's own journal seal: a chain that verifies while
recording nothing the run produced (issue #2789 traced this; its
``SEAL_ONLY`` verdict names the condition but does not fill the chain).

This module fills it at the point where agent work is accepted into the
repository. Recording there rather than at write time buys a property
write-time interception cannot offer: the recorded content is the blob as
it landed, so anyone holding the repository can recompute every row's
content hash from git alone, without trusting the recorder or replaying
the agent. A row that disagrees with the repository is detectable by a
third party.

The change is read as ``before..after`` around the merge rather than from
the merge commit's parents, so a fast-forward -- which produces no merge
commit at all -- is recorded the same way as a true merge.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

#: ``step_id`` prefix for rows recording work landed by a merge. Mirrors
#: ``JOURNAL_SEAL_STEP_PREFIX`` so a reader can tell at a glance which rows
#: describe produced artifacts and which are the run's internal seal.
MERGE_STEP_PREFIX = "merge:"

#: Largest blob recorded inline. Content is hashed from bytes held in
#: memory, and an agent-produced source file is orders of magnitude below
#: this. A blob above the cap is skipped and counted rather than recorded
#: with substituted content: a row whose hash covers something other than
#: the file it names would verify while describing nothing.
MAX_BLOB_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class MergedArtifact:
    """One path the merge changed, with the bytes that landed."""

    path: str
    content: bytes
    deleted: bool


@dataclass
class MergeProvenanceResult:
    """What recording produced, for logging and for tests to assert on."""

    recorded: list[str] = field(default_factory=list[str])
    skipped_oversize: list[str] = field(default_factory=list[str])
    unreadable: list[str] = field(default_factory=list[str])

    @property
    def total_seen(self) -> int:
        return len(self.recorded) + len(self.skipped_oversize) + len(self.unreadable)


class MergedChangeUnreadable(RuntimeError):
    """The set of paths the merge landed could not be read.

    Raised rather than collapsed into an empty list, for the reason
    ``spawner_merge._incoming_files`` gives: an empty list is a merge that
    changed nothing, and a read that failed must not be indistinguishable
    from it. Here the consequence is milder but the same in kind -- a
    silent empty read would leave a seal-only spine while reporting
    success, which is the exact defect this module exists to remove.
    """


def _git_bytes(args: list[str], cwd: Path, *, timeout: int = 30) -> bytes:
    """Run git and return raw stdout.

    ``run_git`` decodes with ``errors="replace"``, which is correct for
    the text it is used for and wrong here: content is hashed, so a
    replacement character would change the hash of any non-UTF-8 blob.
    """
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        msg = f"git {' '.join(args)} exited {result.returncode} ({detail})"
        raise MergedChangeUnreadable(msg)
    return result.stdout


def changed_paths(worktree_root: Path, before_sha: str, after_sha: str) -> list[tuple[str, str]]:
    """Return ``(status, path)`` for every path the merge changed.

    ``--no-renames`` for the reason the merge gate uses it: rename
    detection reports only a rename's destination, so the path a rename
    removed would go unrecorded. A rename is two paths changing and both
    are named.

    Raises:
        MergedChangeUnreadable: The diff could not be read.
    """
    raw = _git_bytes(
        ["diff", "--name-status", "--no-renames", "-z", f"{before_sha}..{after_sha}"],
        worktree_root,
    )
    # ``-z`` because a path may contain a newline; git would otherwise
    # quote and escape it, and the recorded path must be the real one.
    fields = raw.decode("utf-8", "surrogateescape").split("\0")
    out: list[tuple[str, str]] = []
    i = 0
    while i + 1 < len(fields):
        status = fields[i].strip()
        path = fields[i + 1]
        if status and path:
            out.append((status[:1], path))
        i += 2
    return out


def collect_merged_artifacts(
    worktree_root: Path,
    before_sha: str,
    after_sha: str,
) -> tuple[list[MergedArtifact], list[str], list[str]]:
    """Read the bytes that landed for every path the merge changed.

    Returns:
        ``(artifacts, skipped_oversize, unreadable)``. A deletion is an
        artifact with empty content and ``deleted=True``: the run changed
        that path, and a chain that omits removals records only half of
        what the agent did.

    Raises:
        MergedChangeUnreadable: The path list could not be read.
    """
    artifacts: list[MergedArtifact] = []
    oversize: list[str] = []
    unreadable: list[str] = []

    for status, path in changed_paths(worktree_root, before_sha, after_sha):
        if status == "D":
            artifacts.append(MergedArtifact(path=path, content=b"", deleted=True))
            continue
        try:
            size_raw = _git_bytes(["cat-file", "-s", f"{after_sha}:{path}"], worktree_root)
            if int(size_raw.decode().strip()) > MAX_BLOB_BYTES:
                oversize.append(path)
                continue
            content = _git_bytes(["cat-file", "blob", f"{after_sha}:{path}"], worktree_root)
        except (MergedChangeUnreadable, ValueError, subprocess.SubprocessError, OSError) as exc:
            # One unreadable blob must not cost the provenance of every
            # other path in the same merge.
            logger.warning("merge provenance: could not read %s at %s: %s", path, after_sha[:12], exc)
            unreadable.append(path)
            continue
        artifacts.append(MergedArtifact(path=path, content=content, deleted=False))

    return artifacts, oversize, unreadable


def record_merge_artifacts(
    *,
    worktree_root: Path,
    before_sha: str,
    after_sha: str,
    actor: str,
    lineage_root: Path,
    run_id: str,
    hmac_key: bytes,
    model: str = "",
) -> MergeProvenanceResult:
    """Record one spine row per path a merge landed.

    Each row's ``step_id`` carries the merge commit, so a verifier can tie
    the row to the object in git whose blob it hashes.

    Raises:
        MergedChangeUnreadable: The path list could not be read. Callers
            landing a merge must catch this: the merge is already durable
            in git and the rows are re-derivable from it, so a provenance
            failure must be loud but must not undo work that landed.
    """
    from bernstein.adapters.base import record_artifact_write

    artifacts, oversize, unreadable = collect_merged_artifacts(worktree_root, before_sha, after_sha)
    result = MergeProvenanceResult(skipped_oversize=oversize, unreadable=unreadable)

    for artifact in artifacts:
        record_artifact_write(
            artifact_path=artifact.path,
            content=artifact.content,
            actor=actor,
            step_id=f"{MERGE_STEP_PREFIX}{after_sha}",
            model=model,
            lineage_root=lineage_root,
            run_id=run_id,
            hmac_key=hmac_key,
        )
        result.recorded.append(artifact.path)

    if oversize:
        logger.warning(
            "merge provenance: %d path(s) above %d bytes recorded no row: %s",
            len(oversize),
            MAX_BLOB_BYTES,
            ", ".join(oversize[:5]),
        )
    return result


__all__ = [
    "MAX_BLOB_BYTES",
    "MERGE_STEP_PREFIX",
    "MergeProvenanceResult",
    "MergedArtifact",
    "MergedChangeUnreadable",
    "changed_paths",
    "collect_merged_artifacts",
    "record_merge_artifacts",
]
