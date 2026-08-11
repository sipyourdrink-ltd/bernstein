"""Open a file below a trusted root without following a symlink on the way.

``O_NOFOLLOW`` refuses a symlink at the **final** path component only.  A
store that shards its files into subdirectories therefore keeps an opening: a
symlink planted at a parent component -- ``.sdd/cas/ab/`` for a CAS blob --
is still followed, and the read lands wherever it points.

:func:`open_anchored` closes that by walking the path one component at a
time.  Each component is opened relative to a descriptor for its parent, with
``O_NOFOLLOW`` among the flags, so the refusal is a property of the open
itself.  Nothing re-derives a path between the check and the use, because
there is no check -- the same reasoning that put ``O_NOFOLLOW`` on the blob
open in the first place, extended to the components above it.

The **root is opened normally**, deliberately.  Pointing a store's root
somewhere else -- ``.sdd/cas`` symlinked onto a larger volume -- is ordinary
operator configuration, and refusing it would break working installations to
defend against an attacker who, by definition, already controls the location
the operator chose.  Everything below the root is store-managed layout, where
a symlink is never something the store itself wrote.

The walk needs ``os.open`` to accept ``dir_fd`` **and** the platform to define
``O_NOFOLLOW``.  Windows has neither, so there it degrades to a single open of
the joined path -- today's behaviour, no worse -- rather than to a
``is_symlink()`` pre-check, which would trade an atomic guarantee for a race
window and call it an improvement.  Be precise about what that fallback is:
with ``O_NOFOLLOW`` undefined it refuses **nothing**, on the final component
or above it.  It is not a weaker guard; it is the absence of one, and a
Windows junction is followed there exactly as it was before.

Refusing symlinks is also not the whole of the problem it belongs to.  Nothing
about a link is required to plant a FIFO or a device at a name and stall the
reader that opens it, so ``require_regular_file`` covers that separately, by
opening non-blocking and rejecting the type on the descriptor.
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["ANCHORED_OPEN_SUPPORTED", "open_anchored"]


# Both capabilities are required, not just ``dir_fd``: the anchored open
# refuses a symlink only because ``O_NOFOLLOW`` is among its flags, so
# selecting the walk on ``dir_fd`` alone would, on a platform offering one
# without the other, take the branch that cannot refuse anything.  This mirrors
# the reasoning in ``core.security.sigstore_attestation``.
ANCHORED_OPEN_SUPPORTED: bool = os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW")


def _reject_irregular(fd: int, name: str) -> None:
    """Close *fd* and raise unless it is a regular file.

    A symlink refusal says nothing about what the open landed on when no link
    was involved at all.  A FIFO, device or directory planted at the name is a
    different way to reach the same end -- a reader that stalls or reads
    something that is not a stored blob -- so the type is checked on the
    descriptor already open, never by stat-ing the path again.
    """
    try:
        mode = os.fstat(fd).st_mode
    except OSError:
        os.close(fd)
        raise
    if not stat.S_ISREG(mode):
        os.close(fd)
        msg = f"not a regular file: {name!r}"
        raise OSError(errno.EINVAL, msg)


def open_anchored(
    root: Path,
    *components: str,
    flags: int,
    require_regular_file: bool = False,
) -> int:
    """Open ``root`` joined with *components*, refusing symlinked components.

    Args:
        root: Trusted anchor directory.  Opened without ``O_NOFOLLOW``: a
            symlinked root is operator configuration, not an attack.
        *components: Path components below *root*, outermost first.  Each is a
            single name; passing a separator, ``.`` or ``..`` is a programming
            error and is refused rather than resolved.
        flags: Open flags for the final component.  ``O_NOFOLLOW`` is added
            where the platform defines it; the caller supplies the rest
            (typically ``os.O_RDONLY``).
        require_regular_file: Refuse anything that is not a regular file.
            Adds ``O_NONBLOCK`` to the open so a FIFO cannot stall it, then
            rejects on the descriptor.  Stores that only ever write regular
            files should set this; it is off by default because a caller that
            legitimately opens a directory or device would otherwise break.

    Returns:
        An open file descriptor for the final component.  The caller owns it
        and must close it.

    Raises:
        ValueError: If no components are given, or one is not a single
            component name.
        OSError: As :func:`os.open` does.  Three errno values carry meaning for
            callers: ``ENOENT`` (``FileNotFoundError``) means a component is
            genuinely missing, ``ELOOP`` means one was a symlink and the open
            refused to follow it, and ``EINVAL`` means *require_regular_file*
            rejected the type.  Callers that treat a missing file as an
            ordinary miss must not widen that to ``OSError``, or a refused
            symlink would be reported as absence.
    """
    if not components:
        msg = "open_anchored requires at least one path component below the root"
        raise ValueError(msg)
    separators = tuple(sep for sep in (os.sep, os.altsep) if sep)
    for component in components:
        if component in {"", ".", ".."} or any(sep in component for sep in separators):
            msg = f"open_anchored components must be single names, got {component!r}"
            raise ValueError(msg)

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if require_regular_file:
        # ``O_NONBLOCK`` has to be on the open itself.  A FIFO planted at the
        # final component blocks *inside* ``os.open`` until a writer appears,
        # so an ``fstat`` afterwards would never be reached -- the check has to
        # be reachable before it can reject anything.  On a regular file the
        # flag changes nothing.
        flags |= getattr(os, "O_NONBLOCK", 0)

    if not ANCHORED_OPEN_SUPPORTED:
        # Windows and anything else missing either capability.  One open of the
        # joined path, with no symlink refusal anywhere: ``O_NOFOLLOW`` is not
        # defined here, so ``nofollow`` is 0 and this branch protects no
        # component, parent or final.  That is the behaviour that predates this
        # function, kept deliberately -- see the module docstring.
        fd = os.open(root.joinpath(*components), flags | nofollow)
        if require_regular_file:
            _reject_irregular(fd, components[-1])
        return fd

    directory_flag = getattr(os, "O_DIRECTORY", 0)
    parent_fd = os.open(root, os.O_RDONLY | directory_flag)
    try:
        for component in components[:-1]:
            # ``O_DIRECTORY`` makes a non-directory component fail as ENOTDIR
            # here rather than at the next open, so the errno names what is
            # actually wrong.  It is also why an intermediate component needs
            # no ``O_NONBLOCK``: a FIFO planted as a shard directory is
            # rejected on type before the blocking open handler ever runs, so
            # only the final component -- opened without ``O_DIRECTORY``,
            # because a blob is a file -- can stall.
            child_fd = os.open(
                component,
                os.O_RDONLY | nofollow | directory_flag,
                dir_fd=parent_fd,
            )
            # Move ownership to the child *before* closing the old descriptor.
            # Closing first and assigning after would, if that close raised,
            # leak the descriptor just opened and leave the cleanup below
            # closing one that already failed. Close errors on a read-only
            # directory descriptor carry no information a caller could act on
            # -- there is nothing buffered to lose -- so they are dropped
            # rather than aborting a walk that has otherwise succeeded.
            stale_fd, parent_fd = parent_fd, child_fd
            with contextlib.suppress(OSError):
                os.close(stale_fd)
        fd = os.open(components[-1], flags | nofollow, dir_fd=parent_fd)
    finally:
        with contextlib.suppress(OSError):
            os.close(parent_fd)
    if require_regular_file:
        _reject_irregular(fd, components[-1])
    return fd
