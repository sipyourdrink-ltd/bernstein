"""Clean-room cleanup: worktrees are removed when the gate phase refuses.

Every volunteer run ends in one of two places: a signed bundle or a refusal.
Either way, the worktree the runner built is gone.  A refusal that leaves a
worktree behind is the same leak as a success that does -- a directory on a
donor's disk that nobody asked for and that nobody will remove.

The seam is :func:`~bernstein.core.volunteer.task_finish.clean_room`, called
by :func:`~bernstein.core.volunteer.task_finish.finish_volunteer_task` after
scope enforcement and gate re-runs.  It is also called directly by a caller
that receives a refusal before :func:`finish_volunteer_task` is reached, so the
same guarantee holds for every refusal path.

The scope tests in ``test_task_finish.py`` cover the scope and gate enforcement
itself.  These tests cover only the cleanup guarantee.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from bernstein.core.volunteer.manifest import load_manifest
from bernstein.core.volunteer.runner import TaskDiff, WallClockBudget
from bernstein.core.volunteer.task_finish import (
    clean_room,
    finish_volunteer_task,
)

# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------

_GIT_IDENTITY = ["-c", "user.name=fixture", "-c", "user.email=fixture@invalid"]


def _fixture_repo(tmp_path: Path) -> Path:
    """A real git repository with one commit, used as the clone source."""
    repo = tmp_path / "fixture-repo"
    repo.mkdir(parents=True)
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "src" / "pkg" / "mod.py").write_text("# original\n", encoding="utf-8")
    subprocess.run(["git", "init", "--initial-branch=main", "-q"], cwd=repo, check=True)
    subprocess.run(["git", *_GIT_IDENTITY, "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", *_GIT_IDENTITY, "commit", "-qm", "fixture"], cwd=repo, check=True)
    return repo


def _worktree(repo: Path, session: str) -> Path:
    """Create a real git worktree for *session*, mirroring what the runner does.

    Uses *repo* (a real git repository) as the worktree manager's repo root.
    The worktree ends up at ``repo/.sdd/worktrees/{session}``.
    """
    wt_path = repo / ".sdd" / "worktrees" / session
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "--no-checkout", f".sdd/worktrees/{session}", "HEAD"],
        cwd=repo,
        check=True,
    )
    # Write a file so the worktree is "real"
    (wt_path / "src" / "pkg").mkdir(parents=True, exist_ok=True)
    (wt_path / "src" / "pkg" / "mod.py").write_text("# edited\n", encoding="utf-8")
    return wt_path


def _manifest() -> object:
    return load_manifest(
        json.dumps(
            {
                "version": 1,
                "license": "Apache-2.0",
                "gates": [["false"]],  # always fails
                "sandbox": "container",
                "max_wall_clock_minutes": 5,
            }
        )
    )


# --------------------------------------------------------------------------
# clean_room itself
# --------------------------------------------------------------------------


def test_clean_room_removes_the_worktree_from_a_task_diff(tmp_path: Path) -> None:
    """The worktree is gone after clean_room is called."""
    repo = _fixture_repo(tmp_path)
    session = "session-clean-room-unit"
    worktree_path = _worktree(repo, session)

    assert worktree_path.is_dir(), "precondition: worktree exists"

    task_diff = TaskDiff(
        diff="diff --git a/src/pkg/mod.py b/src/pkg/mod.py\n--- a/src/pkg/mod.py\n+++ b/src/pkg/mod.py\n@@ -1 +1 @@\n-old\n+new\n",
        worktree_path=worktree_path,
        base_commit="a" * 40,
        manifest_sha256="b" * 64,
        profile_digest="c" * 64,
        wall_clock={},
        budget=WallClockBudget.start(300),
    )

    clean_room(task_diff)

    assert not worktree_path.exists(), "clean_room must remove the worktree"
    # The parent run/ directory may still exist; only the worktree itself is gone
    assert not (worktree_path.parent).exists() or True  # don't assert on parent


def test_clean_room_is_idempotent(tmp_path: Path) -> None:
    """Calling it twice on the same diff is not an error."""
    repo = _fixture_repo(tmp_path)
    session = "session-clean-room-idempotent"
    worktree_path = _worktree(repo, session)

    task_diff = TaskDiff(
        diff="diff --git a/src/pkg/mod.py b/src/pkg/mod.py\n--- a/src/pkg/mod.py\n+++ b/src/pkg/mod.py\n@@ -1 +1 @@\n-old\n+new\n",
        worktree_path=worktree_path,
        base_commit="a" * 40,
        manifest_sha256="b" * 64,
        profile_digest="c" * 64,
        wall_clock={},
        budget=WallClockBudget.start(300),
    )

    clean_room(task_diff)  # first call: removes the worktree
    clean_room(task_diff)  # second call: must not raise


# --------------------------------------------------------------------------
# finish_volunteer_task cleans up on refusal
# --------------------------------------------------------------------------


def test_clean_room_cleans_up_worktree_even_on_gate_failure(tmp_path: Path) -> None:
    """A gate refusal must not leave the worktree on disk.

    The worktree is the runner's output.  It is not the program's output --
    the output is the refusal record or the signed bundle.  Leaving the
    worktree behind after a refusal is a resource leak on a donor's machine,
    and a refusal that cleans up is indistinguishable from a success that does.
    """
    repo = _fixture_repo(tmp_path)
    session = "session-gate-failure"
    worktree_path = _worktree(repo, session)

    from bernstein.core.security.result_receipt_bundle import GENESIS_ANCHOR, ChainLink, TaskRef
    from bernstein.core.volunteer.sandbox_profile import build_volunteer_profile
    from bernstein.core.volunteer.task_finish import TaskProvenance

    manifest = _manifest()
    profile = build_volunteer_profile(
        manifest,
        available_backends=["container"],
        donor_accepts_plain_container=True,
    )

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.from_private_bytes(bytes([1]) * 32)

    result = finish_volunteer_task(
        patch=(
            "diff --git a/src/pkg/mod.py b/src/pkg/mod.py\n"
            "--- a/src/pkg/mod.py\n"
            "+++ b/src/pkg/mod.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        ),
        manifest=manifest,
        profile=profile,
        workspace=worktree_path,  # worktree IS the workspace here
        provenance=TaskProvenance(
            task=TaskRef(repo="example/project", commit_sha="0" * 40, issue_number=1),
            adapter_id="adapter.mock.v1",
            model_id="mock-model",
            selection_receipt="sel-receipt",
            chain=ChainLink(anchor=GENESIS_ANCHOR, length=1),
        ),
        signing_key=key,
        gate_env={},
        created_at="2026-08-17T00:00:00Z",
    )

    # The function must return a refusal (gate [[false]] always fails)
    assert not hasattr(result, "bundle"), "gate failure must produce a refusal, not a bundle"

    # The worktree is gone even though the gate refused
    assert not worktree_path.exists(), "finish_volunteer_task must remove the worktree even when gates fail"


def test_clean_room_cleans_up_worktree_even_on_scope_refusal(tmp_path: Path) -> None:
    """A scope refusal must also remove the worktree."""
    repo = _fixture_repo(tmp_path)
    session = "session-scope-refusal"
    worktree_path = _worktree(repo, session)

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from bernstein.core.security.result_receipt_bundle import GENESIS_ANCHOR, ChainLink, TaskRef
    from bernstein.core.volunteer.sandbox_profile import build_volunteer_profile
    from bernstein.core.volunteer.task_finish import TaskProvenance

    key = Ed25519PrivateKey.from_private_bytes(bytes([2]) * 32)
    manifest = load_manifest(
        json.dumps(
            {
                "version": 1,
                "license": "Apache-2.0",
                "gates": [["true"]],  # passes, but scope fails
                "allowed_paths": ["docs/**"],  # src/** is not admitted
                "sandbox": "container",
                "max_wall_clock_minutes": 5,
            }
        )
    )
    profile = build_volunteer_profile(
        manifest,
        available_backends=["container"],
        donor_accepts_plain_container=True,
    )

    result = finish_volunteer_task(
        patch=(
            "diff --git a/src/pkg/mod.py b/src/pkg/mod.py\n"
            "--- a/src/pkg/mod.py\n"
            "+++ b/src/pkg/mod.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        ),
        manifest=manifest,
        profile=profile,
        workspace=worktree_path,
        provenance=TaskProvenance(
            task=TaskRef(repo="example/project", commit_sha="0" * 40, issue_number=1),
            adapter_id="adapter.mock.v1",
            model_id="mock-model",
            selection_receipt="sel-receipt",
            chain=ChainLink(anchor=GENESIS_ANCHOR, length=1),
        ),
        signing_key=key,
        gate_env={},
        created_at="2026-08-17T00:00:00Z",
    )

    assert not hasattr(result, "bundle"), "scope refusal must produce a refusal, not a bundle"
    assert not worktree_path.exists(), (
        "finish_volunteer_task must remove the worktree even when scope enforcement refuses"
    )


def test_clean_room_cleans_up_worktree_on_profile_mismatch(tmp_path: Path) -> None:
    """A profile/manifest mismatch refusal also removes the worktree."""
    repo = _fixture_repo(tmp_path)
    session = "session-profile-mismatch"
    worktree_path = _worktree(repo, session)

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from bernstein.core.security.result_receipt_bundle import GENESIS_ANCHOR, ChainLink, TaskRef
    from bernstein.core.volunteer.sandbox_profile import build_volunteer_profile
    from bernstein.core.volunteer.task_finish import TaskProvenance

    key = Ed25519PrivateKey.from_private_bytes(bytes([3]) * 32)

    # Two different manifests so their digests differ
    manifest_a = load_manifest(
        json.dumps(
            {
                "version": 1,
                "license": "Apache-2.0",
                "gates": [["true"]],
                "sandbox": "container",
                "max_wall_clock_minutes": 5,
            }
        )
    )
    manifest_b = load_manifest(
        json.dumps(
            {
                "version": 1,
                "license": "Apache-2.0",
                "gates": [["true"]],
                "sandbox": "container",
                "max_wall_clock_minutes": 3,  # different ceiling
            }
        )
    )

    # Profile derived from manifest_b, enforced against manifest_a
    profile_b = build_volunteer_profile(
        manifest_b,
        available_backends=["container"],
        donor_accepts_plain_container=True,
    )

    result = finish_volunteer_task(
        patch=(
            "diff --git a/src/pkg/mod.py b/src/pkg/mod.py\n"
            "--- a/src/pkg/mod.py\n"
            "+++ b/src/pkg/mod.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        ),
        manifest=manifest_a,  # different from the one profile_b was built from
        profile=profile_b,
        workspace=worktree_path,
        provenance=TaskProvenance(
            task=TaskRef(repo="example/project", commit_sha="0" * 40, issue_number=1),
            adapter_id="adapter.mock.v1",
            model_id="mock-model",
            selection_receipt="sel-receipt",
            chain=ChainLink(anchor=GENESIS_ANCHOR, length=1),
        ),
        signing_key=key,
        gate_env={},
        created_at="2026-08-17T00:00:00Z",
    )

    assert not hasattr(result, "bundle"), "profile mismatch must produce a refusal"
    assert not worktree_path.exists(), "finish_volunteer_task must remove the worktree even on profile mismatch"
