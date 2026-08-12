"""Tests for bernstein.core.persistence.anchored_write - anchored creates and writes.

The interesting cases are the ones a path-based write cannot distinguish: a
component that is a symlink rather than a directory, and a component replaced
between the moment the directory was created and the moment the file was
opened. The replacement tests drive that interleaving through a patched seam
rather than through a thread race, so a failure means the guard is missing
rather than that the machine was slow.
"""

from __future__ import annotations

import errno
import os
from typing import TYPE_CHECKING

import pytest

from bernstein.core.persistence import anchored_write
from bernstein.core.persistence.anchored_write import (
    ANCHORED_WRITE_SUPPORTED,
    AnchoredDir,
    anchored_append,
    anchored_write_text,
    mkdir_anchored,
    open_anchored_write,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.ci

needs_anchoring = pytest.mark.skipif(not ANCHORED_WRITE_SUPPORTED, reason="needs dir_fd and O_NOFOLLOW")


class TestComponentValidation:
    """A component is one name, never a path."""

    @pytest.mark.parametrize("component", ["", ".", "..", "a/b", f"a{os.sep}b"])
    def test_non_component_is_refused_at_construction(self, component: str, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="single names"):
            AnchoredDir(root=tmp_path, parts=(component,))

    def test_non_component_filename_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="single names"):
            open_anchored_write(AnchoredDir(root=tmp_path), "a/b", flags=os.O_WRONLY | os.O_CREAT)


class TestHappyPath:
    """The ordinary case still works, on every platform."""

    def test_mkdir_creates_the_whole_chain(self, tmp_path: Path) -> None:
        mkdir_anchored(AnchoredDir(root=tmp_path, parts=("a", "b", "c")))
        assert (tmp_path / "a" / "b" / "c").is_dir()

    def test_mkdir_is_idempotent(self, tmp_path: Path) -> None:
        target = AnchoredDir(root=tmp_path, parts=("a", "b"))
        mkdir_anchored(target)
        mkdir_anchored(target)
        assert (tmp_path / "a" / "b").is_dir()

    def test_mkdir_without_exist_ok_refuses_an_existing_final_component(self, tmp_path: Path) -> None:
        target = AnchoredDir(root=tmp_path, parts=("a", "b"))
        mkdir_anchored(target)
        with pytest.raises(FileExistsError):
            mkdir_anchored(target, exist_ok=False)

    def test_append_creates_then_appends(self, tmp_path: Path) -> None:
        target = AnchoredDir(root=tmp_path, parts=("d",))
        mkdir_anchored(target)
        for line in ("one\n", "two\n"):
            with anchored_append(target, "log.jsonl") as handle:
                handle.write(line)
        assert (tmp_path / "d" / "log.jsonl").read_text() == "one\ntwo\n"

    def test_write_text_replaces_content(self, tmp_path: Path) -> None:
        target = AnchoredDir(root=tmp_path)
        anchored_write_text(target, "f.json", "first")
        written = anchored_write_text(target, "f.json", "second")
        assert written.read_text() == "second"
        assert written == tmp_path / "f.json"

    def test_a_symlinked_root_is_accepted(self, tmp_path: Path) -> None:
        """Pointing a store's root elsewhere is operator configuration."""
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)

        mkdir_anchored(AnchoredDir(root=link, parts=("sub",)))
        with anchored_append(AnchoredDir(root=link, parts=("sub",)), "f") as handle:
            handle.write("x")
        assert (real / "sub" / "f").read_text() == "x"


@needs_anchoring
class TestLinkedComponentsAreRefused:
    """Everything below the root is store-managed layout; a link is not ours."""

    def test_mkdir_refuses_a_symlinked_component(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (tmp_path / "a").symlink_to(outside, target_is_directory=True)

        with pytest.raises(OSError) as excinfo:
            mkdir_anchored(AnchoredDir(root=tmp_path, parts=("a", "b")))
        # Which of the two arrives is the platform's call: opening a symlink
        # with ``O_NOFOLLOW | O_DIRECTORY`` is ELOOP on Linux and ENOTDIR on
        # macOS, since there the missing directory is what fails first. The
        # refusal is the contract; the errno is not.
        assert excinfo.value.errno in {errno.ELOOP, errno.ENOTDIR}
        assert not (outside / "b").exists()

    def test_mkdir_refuses_a_symlinked_final_component(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (tmp_path / "a").symlink_to(outside, target_is_directory=True)

        with pytest.raises(OSError):
            mkdir_anchored(AnchoredDir(root=tmp_path, parts=("a",)))

    def test_mkdir_refuses_a_non_directory_component(self, tmp_path: Path) -> None:
        (tmp_path / "a").write_text("not a directory")
        with pytest.raises(OSError):
            mkdir_anchored(AnchoredDir(root=tmp_path, parts=("a", "b")))

    def test_append_refuses_a_symlinked_parent(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (tmp_path / "a").symlink_to(outside, target_is_directory=True)

        with pytest.raises(OSError):
            with anchored_append(AnchoredDir(root=tmp_path, parts=("a",)), "f") as handle:
                handle.write("x")
        assert not (outside / "f").exists()

    def test_append_refuses_a_symlinked_file(self, tmp_path: Path) -> None:
        target = tmp_path / "elsewhere.jsonl"
        target.write_text("")
        (tmp_path / "log.jsonl").symlink_to(target)

        with pytest.raises(OSError):
            with anchored_append(AnchoredDir(root=tmp_path), "log.jsonl") as handle:
                handle.write("x")
        assert target.read_text() == ""

    def test_write_text_refuses_a_symlinked_file(self, tmp_path: Path) -> None:
        target = tmp_path / "elsewhere.json"
        target.write_text("original")
        (tmp_path / "f.json").symlink_to(target)

        with pytest.raises(OSError):
            anchored_write_text(AnchoredDir(root=tmp_path), "f.json", "replacement")
        assert target.read_text() == "original"


@needs_anchoring
class TestReplacementMidOperation:
    """A component swapped after it was created is still refused.

    The swap is driven from a patched ``os.mkdir`` / ``os.open`` rather than
    from a competing thread: the point is that the guard holds for a given
    interleaving, and a scheduled race would only ever sample interleavings.
    """

    def test_component_replaced_between_mkdir_and_open(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        real_mkdir = os.mkdir
        swapped = False

        def swapping_mkdir(path: object, *args: object, **kwargs: object) -> None:
            nonlocal swapped
            real_mkdir(path, *args, **kwargs)  # type: ignore[arg-type]
            if not swapped and path == "a":
                # The directory now exists and has been vouched for by nothing
                # yet. Replace it before the walk re-opens it.
                swapped = True
                dir_fd = kwargs.get("dir_fd")
                os.rmdir("a", dir_fd=dir_fd)  # type: ignore[arg-type]
                os.symlink(outside, "a", dir_fd=dir_fd)  # type: ignore[arg-type]

        monkeypatch.setattr(anchored_write.os, "mkdir", swapping_mkdir)

        with pytest.raises(OSError):
            mkdir_anchored(AnchoredDir(root=tmp_path, parts=("a", "b")))
        assert swapped, "the swap never ran; the test proved nothing"
        assert not (outside / "b").exists()

    def test_directory_replaced_between_creation_and_file_open(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        target = AnchoredDir(root=tmp_path, parts=("a",))
        mkdir_anchored(target)

        # Between the layout being created and the append happening, the
        # tenant directory is replaced by a link. A caller holding a joined
        # path would write through it; the walk refuses it.
        (tmp_path / "a").rmdir()
        (tmp_path / "a").symlink_to(outside, target_is_directory=True)

        with pytest.raises(OSError):
            with anchored_append(target, "tasks.jsonl") as handle:
                handle.write("{}\n")
        assert not (outside / "tasks.jsonl").exists()

    def test_file_replaced_between_rotation_and_append(self, tmp_path: Path) -> None:
        """A link planted at the filename is refused even after the dir is fine."""
        target = AnchoredDir(root=tmp_path, parts=("a",))
        mkdir_anchored(target)
        with anchored_append(target, "log.jsonl") as handle:
            handle.write("first\n")

        outside = tmp_path / "outside.jsonl"
        outside.write_text("")
        (tmp_path / "a" / "log.jsonl").unlink()
        (tmp_path / "a" / "log.jsonl").symlink_to(outside)

        with pytest.raises(OSError):
            with anchored_append(target, "log.jsonl") as handle:
                handle.write("second\n")
        assert outside.read_text() == ""


class TestUnsupportedPlatformIsNotDressedUp:
    """The fallback refuses nothing, and the module says so.

    Asserting this keeps a later change from quietly adding an ``is_symlink``
    pre-check to the fallback and calling the gap closed: that would trade an
    atomic guarantee for a race window, which is not an improvement and should
    not be able to arrive without this test being updated deliberately.
    """

    def test_support_flag_requires_every_capability(self) -> None:
        expected = os.open in os.supports_dir_fd and os.mkdir in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW")
        assert ANCHORED_WRITE_SUPPORTED is expected

    def test_fallback_is_documented_as_absent_rather_than_weak(self) -> None:
        doc = anchored_write.__doc__ or ""
        assert "refuses **nothing**" in doc
        assert "absence of one" in doc
