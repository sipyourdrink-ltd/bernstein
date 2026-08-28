"""Tests for :mod:`bernstein.core.lineage.merge_provenance`.

Each test names the way the recording could be wrong rather than the
function it calls. The property under test throughout: after a CLI agent's
work is merged, the spine holds a row per landed path whose content hash a
third party can recompute from the repository alone.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bernstein.core.lineage.merge_provenance import (
    MERGE_STEP_PREFIX,
    MergedChangeUnreadable,
    changed_paths,
    collect_merged_artifacts,
    record_merge_artifacts,
)
from bernstein.core.lineage.spine import (
    JOURNAL_SEAL_STEP_PREFIX,
    LineageSpine,
    SpineStatus,
    content_hash_of,
)

_KEY = b"k" * 32


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo with one commit, standing in for the worktree root."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt")
    _git(root, "commit", "-qm", "seed")
    return root


def _land(repo: Path, changes: dict[str, str | None]) -> tuple[str, str]:
    """Apply *changes* as one commit; return ``(before_sha, after_sha)``."""
    before = _git(repo, "rev-parse", "HEAD")
    for path, content in changes.items():
        target = repo / path
        if content is None:
            target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "agent work")
    return before, _git(repo, "rev-parse", "HEAD")


def test_a_landed_file_gets_a_row_hashing_the_bytes_in_git(repo: Path, tmp_path: Path) -> None:
    """The recorded hash must equal the hash of the blob the repo holds.

    This is the whole point of recording at the merge rather than at write
    time: a third party recomputes the row from git without trusting us.
    """
    before, after = _land(repo, {"src/app.py": "print('x')\n"})
    lineage_root = tmp_path / "lineage"

    result = record_merge_artifacts(
        worktree_root=repo,
        before_sha=before,
        after_sha=after,
        actor="agent-1",
        lineage_root=lineage_root,
        run_id="run-1",
        hmac_key=_KEY,
    )

    assert result.recorded == ["src/app.py"]
    rows = list(LineageSpine(lineage_root, run_id="run-1", hmac_key=_KEY).iter_entries())
    assert len(rows) == 1
    blob = subprocess.run(
        ["git", "cat-file", "blob", f"{after}:src/app.py"],
        cwd=repo,
        capture_output=True,
        check=True,
    ).stdout
    assert rows[0].content_hash == content_hash_of(blob)


def test_a_deleted_path_is_recorded_not_dropped(repo: Path, tmp_path: Path) -> None:
    """A removal is work the agent did; a chain that omits it is partial."""
    before, after = _land(repo, {"seed.txt": None})
    lineage_root = tmp_path / "lineage"

    result = record_merge_artifacts(
        worktree_root=repo,
        before_sha=before,
        after_sha=after,
        actor="agent-1",
        lineage_root=lineage_root,
        run_id="run-1",
        hmac_key=_KEY,
    )

    assert result.recorded == ["seed.txt"]


def test_a_rename_records_both_paths_not_only_the_destination(repo: Path, tmp_path: Path) -> None:
    """Rename detection would hide the path the change removed."""
    before, after = _land(repo, {"seed.txt": None, "moved.txt": "seed\n"})

    statuses = dict((p, s) for s, p in changed_paths(repo, before, after))
    assert statuses == {"seed.txt": "D", "moved.txt": "A"}


def test_a_fast_forward_with_no_merge_commit_is_still_recorded(repo: Path, tmp_path: Path) -> None:
    """Reading ``before..after`` must not depend on a merge commit existing.

    Diffing a merge commit against its first parent returns nothing for a
    fast-forward, which would silently record no provenance for exactly
    the merges git chose to make cheap.
    """
    _git(repo, "checkout", "-q", "-b", "agent/x")
    (repo / "ff.txt").write_text("ff\n", encoding="utf-8")
    _git(repo, "add", "ff.txt")
    _git(repo, "commit", "-qm", "ff work")
    _git(repo, "checkout", "-q", "main")
    before = _git(repo, "rev-parse", "HEAD")
    _git(repo, "merge", "--ff-only", "-q", "agent/x")
    after = _git(repo, "rev-parse", "HEAD")

    assert _git(repo, "rev-list", "--merges", f"{before}..{after}") == ""
    assert [p for _, p in changed_paths(repo, before, after)] == ["ff.txt"]


def test_binary_content_is_hashed_from_raw_bytes(repo: Path, tmp_path: Path) -> None:
    """Decoding with ``errors="replace"`` would change a non-UTF-8 hash."""
    before = _git(repo, "rev-parse", "HEAD")
    raw = bytes([0x89, 0x50, 0x4E, 0x47, 0xFF, 0xFE, 0x00, 0x01])
    (repo / "logo.png").write_bytes(raw)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "binary")
    after = _git(repo, "rev-parse", "HEAD")

    artifacts, _, _ = collect_merged_artifacts(repo, before, after)

    assert [a.content for a in artifacts] == [raw]


def test_an_unreadable_diff_raises_instead_of_reporting_no_changes(repo: Path, tmp_path: Path) -> None:
    """An empty list means "changed nothing"; a failed read must not."""
    with pytest.raises(MergedChangeUnreadable):
        changed_paths(repo, "0" * 40, "1" * 40)


def test_recording_lifts_the_spine_out_of_the_seal_only_verdict(repo: Path, tmp_path: Path) -> None:
    """The defect this module exists to remove, asserted end to end.

    A spine holding only the journal seal verifies while recording nothing
    the run produced. With merge rows present it must no longer report
    ``SEAL_ONLY``.
    """
    lineage_root = tmp_path / "lineage"
    spine = LineageSpine(lineage_root, run_id="run-1", hmac_key=_KEY)
    spine.record_entry(
        artifact_path=".sdd/runs/run-1/journal.jsonl",
        content=b"{}",
        actor="orchestrator",
        step_id=f"{JOURNAL_SEAL_STEP_PREFIX}abc123",
        model="",
        timestamp=1,
    )
    assert spine.verify().status is SpineStatus.SEAL_ONLY

    before, after = _land(repo, {"src/app.py": "print('x')\n"})
    record_merge_artifacts(
        worktree_root=repo,
        before_sha=before,
        after_sha=after,
        actor="agent-1",
        lineage_root=lineage_root,
        run_id="run-1",
        hmac_key=_KEY,
    )

    verdict = LineageSpine(lineage_root, run_id="run-1", hmac_key=_KEY).verify()
    assert verdict.status is SpineStatus.OK


def test_a_mutated_merge_row_is_caught_and_localized(repo: Path, tmp_path: Path) -> None:
    """Artifact-row tamper localization, which the seal-only chain could
    never exercise: with one row there is nothing to localize to."""
    lineage_root = tmp_path / "lineage"
    before, after = _land(repo, {"a.py": "a\n", "b.py": "b\n", "c.py": "c\n"})
    record_merge_artifacts(
        worktree_root=repo,
        before_sha=before,
        after_sha=after,
        actor="agent-1",
        lineage_root=lineage_root,
        run_id="run-1",
        hmac_key=_KEY,
    )

    spine_path = lineage_root / "run-1" / "spine.jsonl"
    lines = spine_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    lines[1] = lines[1].replace('"actor":"agent-1"', '"actor":"agent-9"')
    spine_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    verdict = LineageSpine(lineage_root, run_id="run-1", hmac_key=_KEY).verify()
    assert verdict.status is not SpineStatus.OK
    # Localized to the mutated row: line 2 of three, named on its own and
    # without dragging the intact rows either side of it into the report.
    assert [e for e in verdict.errors if e.startswith("line 2:")]
    assert not [e for e in verdict.errors if e.startswith(("line 1:", "line 3:"))]


def test_every_row_names_the_merge_commit_it_came_from(repo: Path, tmp_path: Path) -> None:
    """Without the commit in ``step_id`` a row cannot be tied back to git."""
    lineage_root = tmp_path / "lineage"
    before, after = _land(repo, {"a.py": "a\n", "b.py": "b\n"})
    record_merge_artifacts(
        worktree_root=repo,
        before_sha=before,
        after_sha=after,
        actor="agent-1",
        lineage_root=lineage_root,
        run_id="run-1",
        hmac_key=_KEY,
    )

    rows = list(LineageSpine(lineage_root, run_id="run-1", hmac_key=_KEY).iter_entries())
    assert {r.step_id for r in rows} == {f"{MERGE_STEP_PREFIX}{after}"}
