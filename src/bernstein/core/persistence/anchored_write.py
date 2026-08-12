"""Create directories and open files below a trusted root without following a link.

This is the write-side counterpart to :mod:`anchored_read`, and it exists for
the same reason.  A caller that validates a directory and then writes to it by
re-deriving the path from a string has two different things: the directory it
checked, and the directory the string names when the write happens.  Those are
the same object only while nothing replaces a component in between, which is
not something the caller can arrange.  Anchoring removes the second derivation
-- there is one walk, each component opened relative to a descriptor for its
parent with ``O_NOFOLLOW``, so refusing a link is a property of the open rather
than of a check that preceded it.

Creating a directory needs one step reading does not.  ``os.mkdir`` with the
name already taken raises ``EEXIST`` and says nothing about *what* took it, so
``exist_ok`` semantics built on that alone accept a symlink pointing anywhere.
:func:`mkdir_anchored` therefore always re-opens the component it just created
or found, with ``O_NOFOLLOW | O_DIRECTORY``, whatever ``mkdir`` did.  A link at
the name fails that open; which errno says so is the platform's call, since
``O_NOFOLLOW`` and ``O_DIRECTORY`` disagree about what is wrong first -- Linux
reports ``ELOOP`` and macOS ``ENOTDIR``.  Callers should treat the refusal, not
the errno, as the contract.

The **root is opened normally**, deliberately, matching :mod:`anchored_read`.
Pointing a store's root somewhere else -- ``.sdd`` symlinked onto a larger
volume -- is ordinary operator configuration, and refusing it would break
working installations.  Everything below the root is store-managed layout,
where a link is never something the store itself wrote.

The walk needs ``os.open`` and ``os.mkdir`` to accept ``dir_fd`` **and** the
platform to define ``O_NOFOLLOW``.  Windows has none of them, so there it
degrades to plain ``Path.mkdir`` and a single open of the joined path.  Be
precise about what that fallback is: it refuses **nothing**, at the final
component or above it.  It is not a weaker guard; it is the absence of one, and
a Windows junction is followed there exactly as it was before this module
existed.  Saying otherwise in a docstring would be the only thing worse than
the gap itself.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from typing import IO, TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

__all__ = [
    "ANCHORED_WRITE_SUPPORTED",
    "AnchoredDir",
    "anchored_append",
    "anchored_write_text",
    "mkdir_anchored",
    "open_anchored_write",
]


# Every capability is required, not just ``dir_fd``.  The walk refuses a link
# only because ``O_NOFOLLOW`` is among its flags, and it creates directories
# only because ``os.mkdir`` accepts ``dir_fd``; selecting on a subset would, on
# a platform offering one without the others, take the branch that cannot
# refuse anything.  Mirrors ``ANCHORED_OPEN_SUPPORTED`` in ``anchored_read``.
ANCHORED_WRITE_SUPPORTED: bool = (
    os.open in os.supports_dir_fd and os.mkdir in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW")
)


def _validate_components(components: tuple[str, ...]) -> None:
    """Refuse anything that is not a single path component name.

    A separator, ``.`` or ``..`` here is a programming error rather than
    something to resolve: the whole point of the walk is that each step is one
    name, so a caller that packs a path into one component has silently opted
    back into the derivation this module removes.
    """
    separators = tuple(sep for sep in (os.sep, os.altsep) if sep)
    for component in components:
        if component in {"", ".", ".."} or any(sep in component for sep in separators):
            msg = f"anchored components must be single names, got {component!r}"
            raise ValueError(msg)


@dataclass(frozen=True)
class AnchoredDir:
    """A directory named as a trusted root plus the components below it.

    Carrying the parts rather than a joined path is the point: a consumer can
    hold this across a buffer or a queue and the walk still happens at the
    moment of the write, not at the moment the value was built.

    Attributes:
        root: Trusted anchor.  Opened without ``O_NOFOLLOW`` -- a symlinked
            root is operator configuration, not something to refuse.
        parts: Component names below *root*, outermost first.  Each is refused
            if it turns out to be a link.
    """

    root: Path
    parts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Refuse non-component parts at construction rather than at the write."""
        _validate_components(self.parts)

    @property
    def path(self) -> Path:
        """Return the joined path.

        For logging, size checks and rotation only.  Writing through this
        re-introduces exactly the derivation the type exists to avoid, so
        callers that write must go through :func:`open_anchored_write` or
        :func:`anchored_append`.
        """
        return self.root.joinpath(*self.parts)

    def child(self, *names: str) -> AnchoredDir:
        """Return a descendant anchored at the same root."""
        return AnchoredDir(root=self.root, parts=(*self.parts, *names))


def mkdir_anchored(directory: AnchoredDir, *, exist_ok: bool = True) -> None:
    """Create *directory* and every missing component above it.

    Each component is created relative to a descriptor for its parent and then
    re-opened with ``O_NOFOLLOW | O_DIRECTORY``.  The re-open is what carries
    the guarantee: ``mkdir`` reports ``EEXIST`` for a symlink and a directory
    alike, so accepting its verdict would accept a link planted at the name.

    Args:
        directory: Anchored directory to create.  The root must already exist
            and is never created here -- creating an anchor would mean
            trusting a location nothing has vouched for.
        exist_ok: Whether an existing directory is acceptable.  A component
            that already exists as a *directory* is always traversed; this
            governs only whether the final component may already be present.

    Raises:
        OSError: As the underlying calls do.  ``ELOOP`` or ``ENOTDIR`` -- see
            the module docstring on why either can mean the same thing -- means
            a component was refused as a symlink or a non-directory.
            ``EEXIST`` means the final component existed with *exist_ok* false.
    """
    if not ANCHORED_WRITE_SUPPORTED:
        # No capability to anchor with; this is the behaviour that predates the
        # module, kept deliberately rather than dressed up as a check.
        directory.path.mkdir(parents=True, exist_ok=exist_ok)
        return

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    parent_fd = os.open(directory.root, os.O_RDONLY | directory_flag)
    try:
        last_index = len(directory.parts) - 1
        for index, component in enumerate(directory.parts):
            try:
                os.mkdir(component, dir_fd=parent_fd)
            except FileExistsError:
                # Intermediate components are allowed to exist unconditionally
                # -- they are layout, not the thing being created.  Whether the
                # final one may is the caller's call, but the re-open below
                # still runs either way, so an existing *link* is refused here
                # even when an existing directory would have been fine.
                if index == last_index and not exist_ok:
                    raise
            child_fd = os.open(
                component,
                os.O_RDONLY | nofollow | directory_flag,
                dir_fd=parent_fd,
            )
            # Take ownership before closing the old descriptor: closing first
            # would, if that close raised, leak the descriptor just opened.
            # Close errors on a read-only directory descriptor carry nothing a
            # caller could act on, so they are dropped.
            stale_fd, parent_fd = parent_fd, child_fd
            with contextlib.suppress(OSError):
                os.close(stale_fd)
    finally:
        with contextlib.suppress(OSError):
            os.close(parent_fd)


def open_anchored_write(directory: AnchoredDir, name: str, *, flags: int) -> int:
    """Open *name* inside *directory* for writing, refusing linked components.

    Args:
        directory: Anchored parent directory.  It is walked, not created; call
            :func:`mkdir_anchored` first when it may be missing.
        name: Final component.  A single name, never a path.
        flags: Open flags, typically ``os.O_WRONLY | os.O_CREAT | os.O_APPEND``.
            ``O_NOFOLLOW`` is added where the platform defines it.

    Returns:
        An open file descriptor.  The caller owns it and must close it.

    Raises:
        ValueError: If *name* is not a single component name.
        OSError: As :func:`os.open` does.  ``ELOOP`` (or ``ENOTDIR`` for an
            intermediate component on some platforms) means one was a symlink
            and the open refused to follow it.
    """
    _validate_components((name,))
    nofollow = getattr(os, "O_NOFOLLOW", 0)

    if not ANCHORED_WRITE_SUPPORTED:
        return os.open(directory.path / name, flags | nofollow, 0o644)

    directory_flag = getattr(os, "O_DIRECTORY", 0)
    parent_fd = os.open(directory.root, os.O_RDONLY | directory_flag)
    try:
        for component in directory.parts:
            child_fd = os.open(
                component,
                os.O_RDONLY | nofollow | directory_flag,
                dir_fd=parent_fd,
            )
            stale_fd, parent_fd = parent_fd, child_fd
            with contextlib.suppress(OSError):
                os.close(stale_fd)
        return os.open(name, flags | nofollow, 0o644, dir_fd=parent_fd)
    finally:
        with contextlib.suppress(OSError):
            os.close(parent_fd)


def anchored_write_text(
    directory: AnchoredDir,
    name: str,
    text: str,
    *,
    encoding: str = "utf-8",
) -> Path:
    """Write *text* to *name* inside *directory*, replacing any current content.

    The anchored counterpart to ``Path.write_text``.  Truncation happens on the
    descriptor the walk produced, so a link planted at *name* is refused rather
    than followed and overwritten.

    Args:
        directory: Anchored parent directory, already created.
        name: File to write.  Created if absent, truncated if present.
        text: Content to write.
        encoding: Text encoding.  Defaults to UTF-8.

    Returns:
        The joined path that was written, for logging and for callers that
        report where a file landed.

    Raises:
        OSError: As :func:`open_anchored_write` does.
    """
    fd = open_anchored_write(directory, name, flags=os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    try:
        handle: IO[str] = os.fdopen(fd, "w", encoding=encoding)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        raise
    with handle:
        handle.write(text)
    return directory.path / name


@contextlib.contextmanager
def anchored_append(
    directory: AnchoredDir,
    name: str,
    *,
    encoding: str = "utf-8",
    fsync: bool = False,
) -> Iterator[IO[str]]:
    """Yield a text handle appending to *name* inside *directory*.

    Mirrors the shape of :func:`~bernstein.core.persistence.durable_write.fsynced_write`
    so an appender switching to an anchored write keeps its structure, and adds
    the walk that helper cannot do from a path alone.

    Args:
        directory: Anchored parent directory, already created.
        name: File to append to.  Created if absent.
        encoding: Text encoding.  Defaults to UTF-8, as every JSONL appender
            in the tree uses.
        fsync: Whether to fsync on clean exit.  On an exception the fsync is
            skipped -- a partial write cannot be promised durable -- but the
            handle is closed regardless.

    Yields:
        The open text-mode handle.  This helper owns flush, fsync and close.

    Raises:
        OSError: As :func:`open_anchored_write` does.
    """
    fd = open_anchored_write(directory, name, flags=os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    try:
        handle: IO[str] = os.fdopen(fd, "a", encoding=encoding)
    except BaseException:
        # ``fdopen`` takes ownership only on success; without this the
        # descriptor leaks on the failure path.
        with contextlib.suppress(OSError):
            os.close(fd)
        raise
    try:
        yield handle
        handle.flush()
        if fsync:
            os.fsync(handle.fileno())
    finally:
        handle.close()
