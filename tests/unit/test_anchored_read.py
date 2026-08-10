"""Tests for bernstein.core.persistence.anchored_read - per-component opens."""

from __future__ import annotations

import errno
import os
import sys
from typing import TYPE_CHECKING

import pytest

from bernstein.core.persistence.anchored_read import (
    ANCHORED_OPEN_SUPPORTED,
    open_anchored,
)

if TYPE_CHECKING:
    from pathlib import Path


def _read_all(fd: int) -> bytes:
    with os.fdopen(fd, "rb") as handle:
        return handle.read()


def _open_fd_count() -> int:
    """Count this process's open descriptors, for leak assertions.

    ``/proc/self/fd`` on Linux, ``/dev/fd`` on macOS and the BSDs; -1 where
    neither exists, which the caller turns into a skip.
    """
    for fd_dir in ("/proc/self/fd", "/dev/fd"):
        if os.path.isdir(fd_dir):
            return len(os.listdir(fd_dir))
    return -1


class TestComponentValidation:
    def test_requires_at_least_one_component(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="at least one path component"):
            open_anchored(tmp_path, flags=os.O_RDONLY)

    @pytest.mark.parametrize("component", ["", ".", "..", "a/b", "sub/"])
    def test_rejects_anything_that_is_not_a_single_name(
        self,
        tmp_path: Path,
        component: str,
    ) -> None:
        """Traversal is refused as a programming error rather than resolved. A
        component that could contain a separator would let the caller walk back
        out of the root the walk exists to anchor on."""
        with pytest.raises(ValueError, match="single names"):
            open_anchored(tmp_path, component, flags=os.O_RDONLY)


class TestAnchoredOpen:
    def test_reads_a_file_below_the_root(self, tmp_path: Path) -> None:
        (tmp_path / "ab").mkdir()
        (tmp_path / "ab" / "blob").write_bytes(b"payload")
        assert _read_all(open_anchored(tmp_path, "ab", "blob", flags=os.O_RDONLY)) == b"payload"

    def test_missing_final_component_raises_enoent(self, tmp_path: Path) -> None:
        (tmp_path / "ab").mkdir()
        with pytest.raises(FileNotFoundError):
            open_anchored(tmp_path, "ab", "blob", flags=os.O_RDONLY)

    def test_missing_intermediate_component_raises_enoent(self, tmp_path: Path) -> None:
        """A caller that treats FileNotFoundError as a miss has to see one from
        an absent parent too, or a missing shard becomes a hard error."""
        with pytest.raises(FileNotFoundError):
            open_anchored(tmp_path, "ab", "blob", flags=os.O_RDONLY)


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX symlink semantics")
@pytest.mark.skipif(not ANCHORED_OPEN_SUPPORTED, reason="needs dir_fd and O_NOFOLLOW")
class TestSymlinkRefusal:
    def test_refuses_a_symlinked_final_component(self, tmp_path: Path) -> None:
        (tmp_path / "ab").mkdir()
        target = tmp_path / "elsewhere"
        target.write_bytes(b"attacker bytes")
        (tmp_path / "ab" / "blob").symlink_to(target)
        with pytest.raises(OSError) as excinfo:
            open_anchored(tmp_path, "ab", "blob", flags=os.O_RDONLY)
        assert excinfo.value.errno == errno.ELOOP

    def test_refuses_a_symlinked_intermediate_component(self, tmp_path: Path) -> None:
        """The component O_NOFOLLOW alone never covered.

        The errno is platform-dependent and deliberately not pinned: Linux
        reports ELOOP for a refused symlink, while macOS and the BSDs report
        ENOTDIR when O_DIRECTORY is also set. Both mean the link was not
        traversed - the target here *is* a directory, so ENOTDIR can only come
        from refusing the link itself. What must hold everywhere is that the
        failure is not a FileNotFoundError, since callers read that as absence.
        """
        decoy = tmp_path / "decoy"
        decoy.mkdir()
        (decoy / "blob").write_bytes(b"attacker bytes")
        (tmp_path / "ab").symlink_to(decoy, target_is_directory=True)
        with pytest.raises(OSError) as excinfo:
            open_anchored(tmp_path, "ab", "blob", flags=os.O_RDONLY)
        assert not isinstance(excinfo.value, FileNotFoundError)
        assert excinfo.value.errno in {errno.ELOOP, errno.ENOTDIR}

    def test_follows_a_symlinked_root(self, tmp_path: Path) -> None:
        """The root is the trust anchor and is opened normally - a symlinked
        root is where the operator chose to put the store."""
        real = tmp_path / "real"
        (real / "ab").mkdir(parents=True)
        (real / "ab" / "blob").write_bytes(b"payload")
        linked = tmp_path / "linked"
        linked.symlink_to(real, target_is_directory=True)
        assert _read_all(open_anchored(linked, "ab", "blob", flags=os.O_RDONLY)) == b"payload"

    def test_a_file_in_place_of_a_directory_is_enotdir(self, tmp_path: Path) -> None:
        (tmp_path / "ab").write_bytes(b"not a directory")
        with pytest.raises(NotADirectoryError):
            open_anchored(tmp_path, "ab", "blob", flags=os.O_RDONLY)

    def test_refused_walk_leaks_no_descriptors(self, tmp_path: Path) -> None:
        """Every intermediate descriptor is closed on the way out, including the
        one held when the refusal fires. A walk that leaks here would exhaust
        the table under exactly the condition it is meant to survive."""
        before = _open_fd_count()
        if before < 0:
            pytest.skip("no /proc/self/fd on this platform")
        decoy = tmp_path / "decoy"
        decoy.mkdir()
        (tmp_path / "ab").symlink_to(decoy, target_is_directory=True)
        for _ in range(50):
            with pytest.raises(OSError):
                open_anchored(tmp_path, "ab", "blob", flags=os.O_RDONLY)
        assert _open_fd_count() == before
