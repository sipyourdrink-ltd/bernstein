"""Derivation of the content-addressed context manifest (#3366).

What the agent was shown is an artifact here, not an implicit per-adapter
decision. These tests pin the two properties that make the artifact worth
anchoring later: the digest is a function of the declared path set and the bytes
behind it (never of the walk or the spelling), and a path the deriver cannot
resolve is recorded ``unmanifested`` with a reason code instead of vanishing.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from bernstein.core.agents.context_manifest import (
    CONTEXT_MANIFEST_VERSION,
    REASON_CODES,
    REASON_INVALID_PATH,
    REASON_MISSING,
    REASON_NOT_A_FILE,
    REASON_OUTSIDE_ROOT,
    REASON_UNREADABLE,
    derive_context_manifest,
    first_manifest_divergence,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A small tree with two declared files and one directory."""
    root = tmp_path / "repo"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "a.py").write_text("alpha\n", encoding="utf-8")
    (root / "src" / "pkg" / "b.py").write_text("bravo\n", encoding="utf-8")
    return root


def test_manifest_digest_is_a_function_of_the_bytes_not_the_walk(repo: Path) -> None:
    """Two derivations over the same tree produce byte-identical manifests."""
    declared = ["src/pkg/a.py", "src/pkg/b.py"]

    first = derive_context_manifest(repo_root=repo, declared_paths=declared)
    second = derive_context_manifest(repo_root=repo, declared_paths=declared)

    assert first.v == CONTEXT_MANIFEST_VERSION
    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.manifest_digest() == second.manifest_digest()
    assert first.manifest_digest().startswith("sha256:")
    assert [entry.digest for entry in first.entries] == [entry.digest for entry in second.entries]
    assert first_manifest_divergence(first, second) is None


def test_single_byte_edit_changes_the_digest_and_names_the_first_differing_entry(repo: Path) -> None:
    """A one-byte edit to a declared file moves the digest and is named."""
    declared = ["src/pkg/a.py", "src/pkg/b.py"]
    before = derive_context_manifest(repo_root=repo, declared_paths=declared)

    (repo / "src" / "pkg" / "b.py").write_text("bravA\n", encoding="utf-8")
    after = derive_context_manifest(repo_root=repo, declared_paths=declared)

    assert after.manifest_digest() != before.manifest_digest()
    divergence = first_manifest_divergence(before, after)
    assert divergence is not None
    assert divergence.index == 1
    assert divergence.path == "src/pkg/b.py"
    assert divergence.left is not None
    assert divergence.right is not None
    assert divergence.left.digest != divergence.right.digest


def test_missing_path_is_unmanifested_and_does_not_shrink_the_entry_count(repo: Path) -> None:
    """An unresolvable path keeps its position and records why."""
    declared = ["src/pkg/a.py", "src/pkg/gone.py", "src/pkg/b.py"]

    manifest = derive_context_manifest(repo_root=repo, declared_paths=declared)

    assert len(manifest.entries) == len(declared)
    middle = manifest.entries[1]
    assert middle.path == "src/pkg/gone.py"
    assert middle.unmanifested is True
    assert middle.reason == REASON_MISSING
    assert middle.reason in REASON_CODES
    assert middle.digest == ""
    assert manifest.unmanifested == (middle,)
    # The entries around it still carry bytes.
    assert manifest.entries[0].digest.startswith("sha256:")
    assert manifest.entries[2].digest.startswith("sha256:")


def test_directory_and_unreadable_entries_carry_distinct_reason_codes(repo: Path) -> None:
    """A directory and a file that cannot be opened are told apart."""
    locked = repo / "src" / "pkg" / "locked.py"
    locked.write_text("secret\n", encoding="utf-8")
    locked.chmod(stat.S_IWUSR)
    if os.access(locked, os.R_OK):  # pragma: no cover - root ignores the mode bits
        pytest.skip("this process can read a mode-0200 file; the unreadable case is unobservable")

    try:
        manifest = derive_context_manifest(repo_root=repo, declared_paths=["src/pkg", "src/pkg/locked.py"])
    finally:
        locked.chmod(stat.S_IRUSR | stat.S_IWUSR)

    assert manifest.entries[0].reason == REASON_NOT_A_FILE
    assert manifest.entries[1].reason == REASON_UNREADABLE
    assert all(entry.unmanifested for entry in manifest.entries)
    assert all(entry.digest == "" for entry in manifest.entries)


def test_path_escaping_the_repo_root_is_unmanifested_and_its_bytes_are_never_read(repo: Path, tmp_path: Path) -> None:
    """A declared path that leaves the root is refused, not content-addressed."""
    outside = tmp_path / "outside.txt"
    outside.write_text("not the agent's to see\n", encoding="utf-8")

    manifest = derive_context_manifest(
        repo_root=repo,
        declared_paths=["../outside.txt", "/etc/hostname", ""],
    )

    assert [entry.reason for entry in manifest.entries] == [
        REASON_INVALID_PATH,
        REASON_INVALID_PATH,
        REASON_INVALID_PATH,
    ]
    assert all(entry.unmanifested for entry in manifest.entries)
    body = manifest.canonical_bytes().decode("utf-8")
    assert "not the agent's to see" not in body
    assert "sha256:" not in body


def test_symlinked_entry_pointing_out_of_the_root_is_unmanifested(repo: Path, tmp_path: Path) -> None:
    """An ordinary-looking component that leaves the tree is refused."""
    outside = tmp_path / "outside.txt"
    outside.write_text("not the agent's to see\n", encoding="utf-8")
    (repo / "src" / "pkg" / "link.py").symlink_to(outside)

    manifest = derive_context_manifest(repo_root=repo, declared_paths=["src/pkg/link.py"])

    assert manifest.entries[0].unmanifested is True
    assert manifest.entries[0].reason == REASON_OUTSIDE_ROOT
    assert manifest.entries[0].digest == ""


def test_declared_order_is_preserved_and_duplicate_paths_collapse_to_one_entry(repo: Path) -> None:
    """The manifest is the declared order, deduplicated -- not a sorted walk."""
    manifest = derive_context_manifest(
        repo_root=repo,
        declared_paths=["src/pkg/b.py", "src/pkg/a.py", "src/pkg/b.py"],
    )

    assert [entry.path for entry in manifest.entries] == ["src/pkg/b.py", "src/pkg/a.py"]


def test_digest_is_independent_of_how_the_same_path_was_spelled(repo: Path) -> None:
    """Spelling a path differently is not a different context set."""
    plain = derive_context_manifest(repo_root=repo, declared_paths=["src/pkg/a.py"])
    dotted = derive_context_manifest(repo_root=repo, declared_paths=["./src/pkg/a.py"])
    windows = derive_context_manifest(repo_root=repo, declared_paths=["src\\pkg\\a.py"])
    redundant = derive_context_manifest(repo_root=repo, declared_paths=["src/pkg/a.py", "./src/pkg/a.py"])

    assert dotted.manifest_digest() == plain.manifest_digest()
    assert windows.manifest_digest() == plain.manifest_digest()
    assert redundant.manifest_digest() == plain.manifest_digest()
