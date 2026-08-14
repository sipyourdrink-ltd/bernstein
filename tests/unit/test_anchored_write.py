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
import stat
from pathlib import Path

import pytest

from bernstein.core.persistence import anchored_write
from bernstein.core.persistence.anchored_write import (
    ANCHORED_ROTATE_SUPPORTED,
    ANCHORED_WRITE_SUPPORTED,
    AnchoredDir,
    anchored_append,
    anchored_write_text,
    mkdir_anchored,
    open_anchored_write,
    rotate_anchored,
)

pytestmark = pytest.mark.ci

needs_anchoring = pytest.mark.skipif(not ANCHORED_WRITE_SUPPORTED, reason="needs dir_fd and O_NOFOLLOW")

# `os.open`'s mode argument is a POSIX mode. Windows does not apply it, and
# there is no ACL fallback here to apply instead, so the owner-only guarantee
# is a POSIX one and the assertion that checks it says so rather than failing
# on a platform the guarantee was never claimed for.
posix_modes_only = pytest.mark.skipif(os.name != "posix", reason="0o600 is a POSIX mode; Windows ignores it")


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


# --- rotation ---------------------------------------------------------------
#
# Rotation is the operation with the teeth: it stats, unlinks and renames.
# Running it on a derived path and anchoring only the append that follows
# protects the harmless half - a link planted at a managed parent has already
# redirected every rename and deletion by the time the append refuses anything.


def _fill(path: Path, size: int) -> None:
    path.write_text("x" * size, encoding="utf-8")


def test_rotate_leaves_a_file_under_the_threshold_alone(tmp_path: Path) -> None:
    target = tmp_path / "metrics.jsonl"
    _fill(target, 10)

    assert rotate_anchored(AnchoredDir(root=tmp_path), "metrics.jsonl", max_bytes=100) is False
    assert target.read_text(encoding="utf-8") == "x" * 10
    assert not (tmp_path / "metrics.jsonl.1").exists()


def test_rotate_shifts_backups_within_the_retention_limit(tmp_path: Path) -> None:
    _fill(tmp_path / "metrics.jsonl", 200)
    _fill(tmp_path / "metrics.jsonl.1", 1)
    _fill(tmp_path / "metrics.jsonl.2", 2)

    assert rotate_anchored(AnchoredDir(root=tmp_path), "metrics.jsonl", max_bytes=100, max_backups=2) is True

    assert not (tmp_path / "metrics.jsonl").exists()
    assert (tmp_path / "metrics.jsonl.1").stat().st_size == 200
    # `.1` shifted down to `.2`; the old `.2` was at the limit and was dropped.
    assert (tmp_path / "metrics.jsonl.2").stat().st_size == 1
    assert not (tmp_path / "metrics.jsonl.3").exists()


def test_rotate_missing_file_is_not_an_error(tmp_path: Path) -> None:
    assert rotate_anchored(AnchoredDir(root=tmp_path), "absent.jsonl", max_bytes=1) is False


@pytest.mark.parametrize("name", ["a/b.jsonl", "..", ".", ""])
def test_rotate_refuses_a_name_that_is_not_one_component(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="single names"):
        rotate_anchored(AnchoredDir(root=tmp_path), name, max_bytes=1)


@needs_anchoring
def test_rotate_refuses_a_symlinked_parent_and_touches_nothing_outside(tmp_path: Path) -> None:
    """The case the derived path could not decide: the parent is a link.

    `outside/metrics.jsonl` is over the threshold and would be renamed if the
    link were followed. Nothing in `outside` may move.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    _fill(outside / "metrics.jsonl", 500)

    root = tmp_path / "root"
    root.mkdir()
    (root / "metrics").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError) as excinfo:
        rotate_anchored(AnchoredDir(root=root, parts=("metrics",)), "metrics.jsonl", max_bytes=100)

    assert excinfo.value.errno in {errno.ELOOP, errno.ENOTDIR}
    assert (outside / "metrics.jsonl").stat().st_size == 500
    assert not (outside / "metrics.jsonl.1").exists()


@needs_anchoring
def test_rotate_does_not_follow_a_link_planted_at_the_file_itself(tmp_path: Path) -> None:
    """A link at the name has no size of its own worth rotating.

    Stat'ing through it would measure - and then rename - a file the layout
    does not own.
    """
    outside = tmp_path / "outside.jsonl"
    _fill(outside, 500)
    (tmp_path / "metrics.jsonl").symlink_to(outside)

    assert rotate_anchored(AnchoredDir(root=tmp_path), "metrics.jsonl", max_bytes=100) is False
    assert outside.stat().st_size == 500
    assert (tmp_path / "metrics.jsonl").is_symlink()


def test_rotation_capability_covers_every_call_rotation_makes() -> None:
    """A partial platform must take the fallback, not fail halfway through.

    `ANCHORED_WRITE_SUPPORTED` answers for `os.open` and `os.mkdir`. Rotation
    also stats, unlinks and renames through a descriptor, and a platform with
    the first pair but not the second would enter the anchored branch and raise
    `NotImplementedError` mid-rotation - after the earlier steps had already
    run.
    """
    if not ANCHORED_ROTATE_SUPPORTED:
        pytest.skip("platform takes the path-based fallback")

    assert ANCHORED_WRITE_SUPPORTED
    for fn in (os.stat, os.unlink, os.rename):
        assert fn in os.supports_dir_fd, f"{fn.__name__} must accept dir_fd for rotation to anchor"


def test_rotation_falls_back_when_the_platform_cannot_anchor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With the capability off, rotation still happens - by path."""
    monkeypatch.setattr(anchored_write, "ANCHORED_ROTATE_SUPPORTED", False)
    target = tmp_path / "metrics.jsonl"
    target.write_text("x" * 500, encoding="utf-8")

    assert rotate_anchored(AnchoredDir(root=tmp_path), "metrics.jsonl", max_bytes=100) is True
    assert (tmp_path / "metrics.jsonl.1").stat().st_size == 500


# --- the degraded path, pinned -----------------------------------------------
#
# `ANCHORED_WRITE_SUPPORTED` is one flag over two capabilities: `O_NOFOLLOW`
# and `dir_fd` on open and mkdir. A platform can have the first without the
# second, and then `open_anchored_write` loses its walk but keeps the flag on
# the final open. That is a narrower refusal, not an absent one, and the two
# tests below say which half survives so nobody has to infer it from the flag.


@pytest.mark.skipif(not getattr(os, "O_NOFOLLOW", 0), reason="needs O_NOFOLLOW")
def test_degraded_open_still_refuses_a_linked_final_component(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the walk, the flag on the last open is all that is left."""
    monkeypatch.setattr(anchored_write, "ANCHORED_WRITE_SUPPORTED", False)
    outside = tmp_path / "outside.jsonl"
    outside.write_text("kept\n", encoding="utf-8")
    (tmp_path / "metrics.jsonl").symlink_to(outside)

    with pytest.raises(OSError) as excinfo:
        fd = open_anchored_write(
            AnchoredDir(root=tmp_path),
            "metrics.jsonl",
            flags=os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        )
        os.close(fd)

    assert excinfo.value.errno in {errno.ELOOP, errno.ENOTDIR}
    assert outside.read_text(encoding="utf-8") == "kept\n"


def test_degraded_open_follows_a_linked_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The half that is genuinely lost, written down rather than discovered."""
    monkeypatch.setattr(anchored_write, "ANCHORED_WRITE_SUPPORTED", False)
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "metrics").symlink_to(outside)

    fd = open_anchored_write(
        AnchoredDir(root=root, parts=("metrics",)),
        "usage.jsonl",
        flags=os.O_WRONLY | os.O_CREAT,
    )
    os.close(fd)

    assert (outside / "usage.jsonl").exists()


# --- what O_NOFOLLOW does not cover ------------------------------------------


def test_capability_predicate_requires_o_directory() -> None:
    """A walk that cannot say "directory" is not a walk over directories.

    Without `O_DIRECTORY` a component that turned out to be a regular file
    would be opened and handed on as the next parent, so the flag belongs in
    the predicate next to the two that were already there.
    """
    every_capability = (
        os.open in os.supports_dir_fd
        and os.mkdir in os.supports_dir_fd
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
    )
    assert ANCHORED_WRITE_SUPPORTED is every_capability


@needs_anchoring
def test_walk_refuses_a_regular_file_where_a_directory_belongs(tmp_path: Path) -> None:
    (tmp_path / "a").write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(OSError) as excinfo:
        fd = open_anchored_write(
            AnchoredDir(root=tmp_path, parts=("a",)),
            "f.jsonl",
            flags=os.O_WRONLY | os.O_CREAT,
        )
        os.close(fd)

    assert excinfo.value.errno == errno.ENOTDIR


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs mkfifo")
@needs_anchoring
def test_write_refuses_a_fifo_instead_of_blocking_on_it(tmp_path: Path) -> None:
    """A FIFO is not a symlink, so `O_NOFOLLOW` lets it through.

    Reader-less, the non-blocking open fails outright (`ENXIO`); with a reader
    attached it opens and the `fstat` guard is what turns it away. Both are the
    same requirement seen from two sides: a worker must not park on a name an
    attacker chose.
    """
    fifo = tmp_path / "metrics.jsonl"
    os.mkfifo(fifo)
    target = AnchoredDir(root=tmp_path)

    with pytest.raises(OSError) as excinfo:
        fd = open_anchored_write(target, "metrics.jsonl", flags=os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        os.close(fd)
    assert excinfo.value.errno == errno.ENXIO

    reader_fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        with pytest.raises(OSError) as excinfo:
            fd = open_anchored_write(target, "metrics.jsonl", flags=os.O_WRONLY | os.O_CREAT | os.O_APPEND)
            os.close(fd)
        assert excinfo.value.errno == errno.EPERM
    finally:
        os.close(reader_fd)


def test_ordinary_write_still_returns_a_blocking_descriptor(tmp_path: Path) -> None:
    """The non-blocking open is a probe, not the mode the caller is handed."""
    fd = open_anchored_write(AnchoredDir(root=tmp_path), "m.jsonl", flags=os.O_WRONLY | os.O_CREAT)
    try:
        assert os.get_blocking(fd) is True
    finally:
        os.close(fd)


# --- the anchor is a directory, not a directory name -------------------------


def test_relative_root_is_pinned_at_construction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`AnchoredDir` is built once and walked later, possibly elsewhere.

    Deferring the walk is the design; deferring resolution of the anchor too
    would mean a value built under one working directory and flushed under
    another wrote into a different tree with no error to show for it.
    """
    (tmp_path / "here").mkdir()
    (tmp_path / "elsewhere" / "here").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    target = AnchoredDir(root=Path("here"))
    assert target.root == tmp_path / "here"

    monkeypatch.chdir(tmp_path / "elsewhere")
    fd = open_anchored_write(target, "m.jsonl", flags=os.O_WRONLY | os.O_CREAT)
    os.close(fd)

    assert (tmp_path / "here" / "m.jsonl").exists()
    assert not (tmp_path / "elsewhere" / "here" / "m.jsonl").exists()


@posix_modes_only
def test_created_files_are_owner_only(tmp_path: Path) -> None:
    """Containing the write and then leaving the file readable answers half of it."""
    fd = open_anchored_write(AnchoredDir(root=tmp_path), "state.json", flags=os.O_WRONLY | os.O_CREAT)
    os.close(fd)

    assert stat.S_IMODE((tmp_path / "state.json").stat().st_mode) == 0o600
