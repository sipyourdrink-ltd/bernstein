"""Regression tests: the canonical workspace image is a pure function of the tree (#2704).

``canonical_workspace_image`` produces the *content address* a microVM
snapshot is stored and resumed by, so its bytes must depend only on the tree -
not on the process that reads it, nor on where the workspace root lives on this
host. These tests pin the five deviations #2704 catalogued. Each one varies the
AXIS the defect rode on (process identity, permission bits, special-file
presence, host path prefix, path length) and asserts the digest behaves, rather
than asserting a single canned vector.

The sibling ``test_microvm_backend.py`` owns the extract-hardening and
end-to-end backend tests; this file owns the freeze-side purity contract.
"""

from __future__ import annotations

import hashlib
import io
import os
import stat
import tarfile
from typing import TYPE_CHECKING

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.orchestration.best_of_n import CandidateResult
from bernstein.core.persistence.cas_store import CASStore
from bernstein.core.sandbox.backends._vmmonitor import (
    SPECIAL_FILES_MANIFEST,
    FakeMonitor,
    canonical_workspace_image,
    extract_workspace_image,
)
from bernstein.core.sandbox.backends.microvm import MicroVMSandboxBackend
from bernstein.core.sandbox.fork_race import fork_race
from bernstein.core.sandbox.manifest import FileEntry, WorkspaceManifest

if TYPE_CHECKING:
    from pathlib import Path


def _digest(image: bytes) -> str:
    return hashlib.sha256(image).hexdigest()


# ---------------------------------------------------------------------------
# Defect 1: the digest must not depend on the calling process
# ---------------------------------------------------------------------------


def test_digest_is_independent_of_process_identity_axis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Varying the ``os.access(X_OK)`` answer must not move the digest.

    ``os.access(X_OK)`` folds in euid/root/ACLs/``noexec`` - process identity
    that is nowhere in the tree. We cannot flip the real euid inside a test, so
    we vary the exact signal the old code consulted: monkeypatch ``os.access``
    to answer "yes to everything" then "no to everything". A digest that is a
    pure function of the tree is identical under both.
    """
    src = tmp_path / "src"
    src.mkdir()
    exe = src / "run.sh"
    exe.write_bytes(b"#!/bin/sh\n")
    exe.chmod(0o755)
    (src / "data.txt").write_bytes(b"payload")
    (src / "data.txt").chmod(0o644)

    monkeypatch.setattr(os, "access", lambda *a, **k: True)
    image_yes = canonical_workspace_image(src)
    monkeypatch.setattr(os, "access", lambda *a, **k: False)
    image_no = canonical_workspace_image(src)

    assert image_yes == image_no

    # And we still capture the tree's real executable bit, so the fix is not a
    # degenerate "hardcode one mode for everything" (which would also be
    # process-independent but wrong): flipping the file's real x bit must move
    # the digest.
    exe.chmod(0o644)
    assert canonical_workspace_image(src) != image_yes


# ---------------------------------------------------------------------------
# Defect 3: 0o600 and 0o644 must not collide, and restore must not widen
# ---------------------------------------------------------------------------


def test_private_and_public_modes_have_distinct_digests(tmp_path: Path) -> None:
    """A 0o600 file and a 0o644 file with identical bytes must differ in digest."""
    private = tmp_path / "priv"
    private.mkdir()
    (private / "secret").write_bytes(b"token")
    (private / "secret").chmod(0o600)

    public = tmp_path / "pub"
    public.mkdir()
    (public / "secret").write_bytes(b"token")
    (public / "secret").chmod(0o644)

    assert canonical_workspace_image(private) != canonical_workspace_image(public)


def test_restore_does_not_widen_a_private_mode(tmp_path: Path) -> None:
    """Restoring a 0o600 file must not widen it to a world-readable 0o644."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "id_ed25519").write_bytes(b"PRIVATE KEY")
    (src / "id_ed25519").chmod(0o600)

    image = canonical_workspace_image(src)
    dest = tmp_path / "dest"
    extract_workspace_image(image, dest)

    restored_mode = stat.S_IMODE((dest / "id_ed25519").stat().st_mode)
    assert restored_mode == 0o600
    # The private file must not have gained any group/other permission.
    assert restored_mode & 0o077 == 0


# ---------------------------------------------------------------------------
# Defect 4: a special file's presence must move the digest
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="platform has no FIFOs")
def test_special_file_presence_changes_the_digest(tmp_path: Path) -> None:
    """A workspace with a FIFO must not share a digest with one without it."""
    without = tmp_path / "without"
    without.mkdir()
    (without / "keep.txt").write_bytes(b"data")

    with_fifo = tmp_path / "with"
    with_fifo.mkdir()
    (with_fifo / "keep.txt").write_bytes(b"data")
    os.mkfifo(with_fifo / "pipe")

    assert canonical_workspace_image(with_fifo) != canonical_workspace_image(without)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="platform has no FIFOs")
def test_special_file_is_recorded_but_not_materialised_on_restore(tmp_path: Path) -> None:
    """The FIFO's presence is recorded, but restore does not recreate a device node."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "keep.txt").write_bytes(b"data")
    os.mkfifo(src / "pipe")

    image = canonical_workspace_image(src)
    dest = tmp_path / "dest"
    extract_workspace_image(image, dest)

    # The real file survived, the device node was NOT recreated, and the
    # manifest records the dropped special file's presence and kind.
    assert (dest / "keep.txt").read_bytes() == b"data"
    assert not (dest / "pipe").exists()
    manifest = (dest / SPECIAL_FILES_MANIFEST).read_text()
    assert "pipe\tfifo" in manifest


# ---------------------------------------------------------------------------
# Defect 2: symlink targets must round-trip host-independently
# ---------------------------------------------------------------------------


def _tree_with_symlink(root: Path, *, absolute: bool) -> None:
    root.mkdir(parents=True)
    (root / "target.txt").write_bytes(b"pointee")
    link = root / "link"
    if absolute:
        link.symlink_to(root / "target.txt")  # absolute host path
    else:
        link.symlink_to("target.txt")  # relative, in-tree


def test_absolute_symlink_digest_is_host_path_independent(tmp_path: Path) -> None:
    """The same logical workspace under two different roots must hash identically.

    An absolute symlink target embeds the host path prefix; two operators whose
    workspaces live at different paths would otherwise get different content
    addresses for byte-identical trees.
    """
    root_a = tmp_path / "hostA" / "deep" / "ws"
    root_b = tmp_path / "B" / "ws"
    _tree_with_symlink(root_a, absolute=True)
    _tree_with_symlink(root_b, absolute=True)

    assert canonical_workspace_image(root_a) == canonical_workspace_image(root_b)


def test_relative_and_absolute_symlinks_round_trip(tmp_path: Path) -> None:
    """freeze -> extract -> freeze is a fixed point for both symlink flavours."""
    for absolute in (True, False):
        root = tmp_path / f"src-{absolute}"
        _tree_with_symlink(root, absolute=absolute)
        image = canonical_workspace_image(root)

        dest = tmp_path / f"dest-{absolute}"
        extract_workspace_image(image, dest)
        # The restored link resolves to the in-tree pointee ...
        assert (dest / "link").is_symlink()
        assert (dest / "link").read_bytes() == b"pointee"
        # ... and re-freezing the restored tree reproduces the exact digest.
        assert canonical_workspace_image(dest) == image


# ---------------------------------------------------------------------------
# Defect 5: a single long filename must not abort the fork-race
# ---------------------------------------------------------------------------

_LONG_NAME = "z" * 150  # > USTAR's 100-char name field, no '/' for prefix split


def test_long_filename_snapshots_deterministically(tmp_path: Path) -> None:
    """A path over the USTAR limit is canonicalised (not a ValueError) and is stable."""
    src = tmp_path / "src"
    src.mkdir()
    (src / _LONG_NAME).write_bytes(b"content")

    image = canonical_workspace_image(src)
    assert image == canonical_workspace_image(src)  # deterministic

    # The long-name image still extracts through the hardened restore path.
    dest = tmp_path / "dest"
    extract_workspace_image(image, dest)
    assert (dest / _LONG_NAME).read_bytes() == b"content"


def test_long_filename_image_is_process_independent(tmp_path: Path) -> None:
    """The fallback format must not leak wall-clock/pid: same bytes across builds."""
    src = tmp_path / "src"
    src.mkdir()
    (src / _LONG_NAME).write_bytes(b"content")
    first = canonical_workspace_image(src)
    second = canonical_workspace_image(src)
    assert _digest(first) == _digest(second)


def _backend(tmp_path: Path) -> MicroVMSandboxBackend:
    return MicroVMSandboxBackend(
        monitor_factory=lambda root: FakeMonitor(root=root),
        cas=CASStore(tmp_path / "cas"),
    )


async def _base_snapshot(backend: MicroVMSandboxBackend) -> str:
    manifest = WorkspaceManifest(root="/workspace", files=(FileEntry(path="base.txt", content=b"BASE"),))
    session = await backend.create(manifest)
    digest = await session.snapshot()
    await backend.destroy(session)
    return digest


async def _one_long_name_candidate(session: object, index: int) -> CandidateResult:
    # Candidate 0 writes a path that overruns the USTAR limit; the others are
    # ordinary. Before the fix, candidate 0's freeze raised a bare ValueError
    # that asyncio.gather surfaced, aborting the whole race.
    if index == 0:
        await session.write(_LONG_NAME, b"long")  # type: ignore[attr-defined]
    else:
        await session.write(f"cand{index}.txt", f"work-{index}".encode())  # type: ignore[attr-defined]
    return CandidateResult(
        task_id=f"candidate-{index}",
        tests_passing=(index % 2 == 0),
        lint_score=max(0.0, 1.0 - 0.1 * index),
    )


@pytest.mark.asyncio
async def test_long_filename_does_not_abort_the_fork_race(tmp_path: Path) -> None:
    """One candidate writing a long filename must not take down the whole race."""
    backend = _backend(tmp_path)
    key = Ed25519PrivateKey.generate()
    base = await _base_snapshot(backend)

    receipt = await fork_race(
        backend=backend,
        base_snapshot_digest=base,
        run_candidate=_one_long_name_candidate,
        k=3,
        signing_key=key,
    )

    # The race completed: all three candidates produced a terminal snapshot and
    # a winner was selected (rather than the long-name candidate aborting it).
    assert receipt.winner_task_id in {f"candidate-{i}" for i in range(3)}
    assert len(receipt.loser_snapshot_digests) == 2


# ---------------------------------------------------------------------------
# Reserved-name collision must be refused diagnosably, not silently collapsed
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="platform has no FIFOs")
def test_reserved_manifest_name_collision_is_refused(tmp_path: Path) -> None:
    """A real file at the reserved manifest name plus a special file is refused."""
    from bernstein.core.sandbox.backends._vmmonitor import WorkspaceImageError

    src = tmp_path / "src"
    src.mkdir()
    (src / SPECIAL_FILES_MANIFEST).write_bytes(b"user data")
    os.mkfifo(src / "pipe")

    with pytest.raises(WorkspaceImageError, match="reserved"):
        canonical_workspace_image(src)

    # Without the special file there is no ambiguity: the user file is fine.
    (src / "pipe").unlink()
    image = canonical_workspace_image(src)
    with tarfile.open(fileobj=io.BytesIO(image), mode="r:") as tar:
        assert SPECIAL_FILES_MANIFEST in tar.getnames()
