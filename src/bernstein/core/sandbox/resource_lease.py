"""Tag-filtered resource leases: one atomic claim primitive (#5128).

Before this module the only lock in the tree was
:func:`bernstein.cli.commands.worktrees_cmd.lock_gc` -- the right *shape*
(``O_EXCL`` acquire, staleness reclamation, release-on-raise) hardcoded to one
file, so every other claimable thing (a sandbox slot, a worktree, a device) was
either unlocked or would need its own bespoke copy of that shape.

The shape is generalised here along four axes:

* **Tags.** Resources declare a many-to-many tag set; a claim is a tag filter
  resolved *and* locked in one transaction, so no candidate can be taken
  between the resolve and the lock.
* **Distinct failures.** A filter that matches no declared resource raises
  :class:`NoMatchingResourceError`; a filter whose matches are all held raises
  :class:`NoFreeResourceError`. "Nothing like that exists" and "they are all
  busy" are different operator problems and must not share an exception.
* **TTL and owner.** Every lease records the session identity that took it and
  an absolute expiry; :meth:`Lease.keepalive` pushes the expiry out, and an
  expired lease is reclaimable by the next claimant so a killed holder cannot
  wedge a resource forever.
* **Release.** A lease is a context manager, and an interpreter-exit hook
  releases whatever the process still holds -- logging, never raising, because
  a failed release must not turn a clean shutdown into a traceback.

Staleness follows ``lock_gc``'s precedent exactly: a payload that is missing or
mid-write is *not* stale, so a lease another process created between its
``O_EXCL`` and its write is never stolen. A holder's pid is only consulted when
the lease was taken on this host.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import logging
import os
import re
import socket
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable

logger = logging.getLogger(__name__)

#: Default lease lifetime. Long enough for a real sandbox task, short enough
#: that a killed holder does not park a resource for a working day.
DEFAULT_LEASE_TTL_S = 900.0

#: Directory, relative to the store root, holding one file per active lease.
LEASE_DIR_RELNAME = "leases"

#: Wire-format version stamped into every lease file.
LEASE_SCHEMA_VERSION = 1

#: Resource ids and lock names are used verbatim as filenames, so they are
#: restricted to a conservative, path-separator-free alphabet.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_LEASE_SUFFIX = ".lease.json"


class ResourceLeaseError(RuntimeError):
    """Base for every claim failure."""


class LeaseConflictError(ResourceLeaseError):
    """Raised when a specific resource or named lock is already held.

    Attributes:
        name: The resource id / lock name that is held.
        holder: The recorded lease payload of the current holder, or ``None``
            when it could not be read.
    """

    def __init__(self, message: str, *, name: str = "", holder: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.name = name
        self.holder = holder


class NoMatchingResourceError(ResourceLeaseError):
    """Raised when the tag filter matches no declared resource at all."""


class NoFreeResourceError(ResourceLeaseError):
    """Raised when every resource matching the tag filter is currently held."""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def _validate_name(name: str, *, what: str) -> str:
    if not _NAME_RE.match(name):
        raise ValueError(f"invalid {what} {name!r}: expected [A-Za-z0-9][A-Za-z0-9._-]{{0,127}}")
    return name


@dataclass(frozen=True)
class ResourceDeclaration:
    """A claimable thing and the tags it carries.

    Tags are many-to-many: one resource carries several, and one tag names
    several resources. A claim filter is satisfied when the resource's tags are
    a superset of the requested ones.
    """

    resource_id: str
    tags: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        _validate_name(self.resource_id, what="resource id")
        object.__setattr__(self, "tags", frozenset(str(t) for t in self.tags))

    def matches(self, wanted: frozenset[str]) -> bool:
        """True when this resource carries every tag in *wanted*."""
        return wanted <= self.tags


class ResourceRegistry:
    """An ordered set of :class:`ResourceDeclaration` to resolve filters against.

    The registry is pure data: it never touches the lease store, so resolving a
    filter is deterministic and two operators with the same declarations get
    the same candidate order.
    """

    def __init__(self, resources: Iterable[ResourceDeclaration]) -> None:
        by_id: dict[str, ResourceDeclaration] = {}
        for resource in resources:
            if resource.resource_id in by_id:
                raise ValueError(f"duplicate resource id {resource.resource_id!r}")
            by_id[resource.resource_id] = resource
        self._by_id = by_id

    def __len__(self) -> int:
        return len(self._by_id)

    @property
    def resource_ids(self) -> list[str]:
        """Every declared resource id, sorted."""
        return sorted(self._by_id)

    def get(self, resource_id: str) -> ResourceDeclaration | None:
        """Return the declaration for *resource_id*, or ``None``."""
        return self._by_id.get(resource_id)

    def matching(self, tags: Iterable[str]) -> list[ResourceDeclaration]:
        """Return, sorted by resource id, every resource carrying all *tags*."""
        wanted = frozenset(str(t) for t in tags)
        return [self._by_id[rid] for rid in sorted(self._by_id) if self._by_id[rid].matches(wanted)]


# ---------------------------------------------------------------------------
# Identity + staleness
# ---------------------------------------------------------------------------


def _session_identity() -> str:
    """Return the install identity that owns leases taken by this process.

    Imported lazily so a claim never drags the identity module into a process
    that only needed a lock.
    """
    try:
        from bernstein.core.identity.install_rev import get_install_rev

        rev = get_install_rev()
    except Exception:  # pragma: no cover - identity is best-effort here
        logger.debug("lease owner: install identity unavailable, falling back to pid", exc_info=True)
        return ""
    return rev or ""


def default_owner() -> str:
    """Owner string recorded on a lease: the session identity, else the pid."""
    return _session_identity() or f"pid-{os.getpid()}"


def _hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:  # pragma: no cover - hostname lookup effectively never fails
        return ""


def _read_lease(path: Path) -> dict[str, Any] | None:
    """Return the recorded lease payload at *path*, or ``None`` when unreadable."""
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return cast("dict[str, Any]", data) if isinstance(data, dict) else None


def _holder_process_alive(meta: dict[str, Any]) -> bool:
    """True when the recorded holder is a live process on this host.

    A lease recorded on another host is treated as alive: this primitive is
    local, and guessing about a remote pid would reclaim a resource somebody
    else is still using.
    """
    if meta.get("host") and meta.get("host") != _hostname():
        return True
    pid = meta.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return True
    from bernstein.core.orchestration.process_utils import is_process_alive

    return is_process_alive(pid)


def _lease_is_stale(meta: dict[str, Any] | None) -> bool:
    """True when a recorded lease may be reclaimed by another claimant.

    An unreadable / mid-write payload is NOT stale, mirroring ``lock_gc``: a
    lease another process just created, between its ``O_EXCL`` and its write, is
    never stolen. A fully written payload is stale when its TTL has passed or
    when its holder process is gone.
    """
    if not isinstance(meta, dict):
        return False
    expires_at = meta.get("expires_at")
    if isinstance(expires_at, (int, float)) and time.time() > float(expires_at):
        return True
    return not _holder_process_alive(meta)


# ---------------------------------------------------------------------------
# Lease
# ---------------------------------------------------------------------------


@dataclass
class Lease:
    """A held claim on one resource, released on context exit or process exit."""

    resource_id: str
    lease_id: str
    owner: str
    pid: int
    host: str
    acquired_at: float
    expires_at: float
    path: Path
    ttl_s: float = DEFAULT_LEASE_TTL_S
    released: bool = field(default=False)

    def payload(self) -> dict[str, Any]:
        """Canonical on-disk view of this lease."""
        return {
            "schema_version": LEASE_SCHEMA_VERSION,
            "resource_id": self.resource_id,
            "lease_id": self.lease_id,
            "owner": self.owner,
            "pid": self.pid,
            "host": self.host,
            "acquired_at": self.acquired_at,
            "expires_at": self.expires_at,
        }

    @property
    def expired(self) -> bool:
        """True once the recorded TTL has passed."""
        return time.time() > self.expires_at

    def _write(self) -> None:
        """Replace the lease file atomically with the current payload."""
        tmp = self.path.with_name(f"{self.path.name}.{self.lease_id}.tmp")
        tmp.write_text(json.dumps(self.payload(), sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    def keepalive(self, ttl_s: float | None = None) -> float:
        """Extend the lease by *ttl_s* (default: its original TTL).

        Raises:
            LeaseConflictError: when the lease was already reclaimed, so the
                caller learns it no longer owns the resource instead of
                silently overwriting the new holder's record.

        Returns:
            The new absolute expiry.
        """
        if self.released:
            raise LeaseConflictError(f"lease on {self.resource_id!r} was already released", name=self.resource_id)
        recorded = _read_lease(self.path)
        if recorded is None or recorded.get("lease_id") != self.lease_id:
            raise LeaseConflictError(
                f"lease on {self.resource_id!r} is no longer held by this process",
                name=self.resource_id,
                holder=recorded,
            )
        self.ttl_s = float(ttl_s) if ttl_s is not None else self.ttl_s
        self.expires_at = time.time() + self.ttl_s
        self._write()
        return self.expires_at

    def release(self) -> None:
        """Release the lease. Never raises.

        Only a file this lease still owns is unlinked: once another claimant
        has reclaimed an expired lease, releasing the stale handle must not
        delete the new holder's record.
        """
        if self.released:
            return
        self.released = True
        _forget_lease(self)
        recorded = _read_lease(self.path)
        if recorded is not None and recorded.get("lease_id") != self.lease_id:
            logger.debug("lease release: %s already reclaimed by another holder", self.path)
            return
        try:
            self.path.unlink()
        except OSError:
            logger.debug("lease release: %s already removed", self.path)

    def __enter__(self) -> Lease:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.release()


# ---------------------------------------------------------------------------
# Process-exit hook
# ---------------------------------------------------------------------------

_HELD: dict[int, Lease] = {}
_atexit_registered = False


def _remember_lease(lease: Lease) -> None:
    global _atexit_registered
    _HELD[id(lease)] = lease
    if not _atexit_registered:
        atexit.register(release_all_held)
        _atexit_registered = True


def _forget_lease(lease: Lease) -> None:
    _HELD.pop(id(lease), None)


def release_all_held() -> None:
    """Release every lease this process still holds, logging on failure.

    Registered with :mod:`atexit` on the first claim so a process that exits
    without releasing does not park its resources until their TTL. It never
    raises: a failed release during interpreter shutdown must not become a
    traceback on an otherwise clean exit.
    """
    for lease in list(_HELD.values()):
        try:
            lease.release()
        except Exception:  # pragma: no cover - release() is already total
            logger.warning("lease release on exit failed for %s", lease.resource_id, exc_info=True)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class LeaseStore:
    """One file per active lease under ``<root>/leases/``.

    A file-per-lease scheme, not an event ledger: the acquire is a single
    ``O_EXCL`` ``open`` -- the same atomicity ``lock_gc`` already relies on --
    so two claimants racing for one resource are decided by the kernel with no
    read-modify-write window, and listing the active leases is listing a
    directory.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    @property
    def leases_dir(self) -> Path:
        """Directory holding one file per active lease."""
        return self.root / LEASE_DIR_RELNAME

    def path_for(self, name: str) -> Path:
        """Lease-file path for resource / lock *name*."""
        return self.leases_dir / f"{_validate_name(name, what='resource id')}{_LEASE_SUFFIX}"

    def active(self) -> list[dict[str, Any]]:
        """Return the recorded payload of every readable lease, sorted by id."""
        try:
            entries = sorted(self.leases_dir.glob(f"*{_LEASE_SUFFIX}"))
        except OSError:
            return []
        rows = [meta for path in entries if (meta := _read_lease(path)) is not None]
        return sorted(rows, key=lambda m: str(m.get("resource_id", "")))

    def _open_exclusive(self, name: str, path: Path) -> int:
        """``O_EXCL`` open of *path*, reclaiming a provably stale lease once."""
        try:
            return os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            meta = _read_lease(path)
            if not _lease_is_stale(meta):
                owner = f" held by {meta['owner']}" if isinstance(meta, dict) and meta.get("owner") else ""
                raise LeaseConflictError(
                    f"resource {name!r} is already leased ({path}{owner})", name=name, holder=meta
                ) from exc
            logger.warning("Reclaiming expired lease %s (previous holder gone or TTL passed): %s", path, meta)
            # A competing claimant may win the race and re-create the lease;
            # the retry below resolves that case.
            with contextlib.suppress(OSError):
                path.unlink()
            try:
                return os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError as retry_exc:
                raise LeaseConflictError(
                    f"resource {name!r} is already leased ({path})", name=name, holder=_read_lease(path)
                ) from retry_exc

    def acquire(self, name: str, *, owner: str | None = None, ttl_s: float = DEFAULT_LEASE_TTL_S) -> Lease:
        """Take a lease on *name*, or raise :class:`LeaseConflictError`.

        Args:
            name: Resource id or arbitrary lock name.
            owner: Lease owner; defaults to the session identity.
            ttl_s: Lifetime in seconds. Once passed, another claimant may
                reclaim the lease even if this process never released it.
        """
        path = self.path_for(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = self._open_exclusive(name, path)

        now = time.time()
        lease = Lease(
            resource_id=name,
            lease_id=uuid.uuid4().hex,
            owner=owner or default_owner(),
            pid=os.getpid(),
            host=_hostname(),
            acquired_at=now,
            expires_at=now + float(ttl_s),
            path=path,
            ttl_s=float(ttl_s),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(lease.payload(), sort_keys=True))
        except BaseException:
            with contextlib.suppress(OSError):
                path.unlink()
            raise
        _remember_lease(lease)
        return lease


# ---------------------------------------------------------------------------
# Claim
# ---------------------------------------------------------------------------


def claim(
    registry: ResourceRegistry,
    tags: Iterable[str],
    *,
    store: LeaseStore,
    owner: str | None = None,
    ttl_s: float = DEFAULT_LEASE_TTL_S,
) -> Lease:
    """Resolve *tags* against *registry* and lock the first free match.

    The resolve and the lock are one transaction from the caller's side: each
    candidate is attempted with an ``O_EXCL`` acquire and a candidate taken by
    a competing claimant simply moves the search on, so no window exists in
    which a resolved-but-unlocked resource can be handed to two callers.

    Raises:
        NoMatchingResourceError: no declared resource carries all of *tags*.
        NoFreeResourceError: every match is currently held.
    """
    candidates = registry.matching(tags)
    if not candidates:
        raise NoMatchingResourceError(f"no resource declares tags {sorted(str(t) for t in tags)}")
    conflicts: list[str] = []
    for candidate in candidates:
        try:
            return store.acquire(candidate.resource_id, owner=owner, ttl_s=ttl_s)
        except LeaseConflictError as exc:
            conflicts.append(candidate.resource_id)
            logger.debug("claim: %s unavailable (%s)", candidate.resource_id, exc)
    raise NoFreeResourceError(
        f"all {len(conflicts)} resources matching {sorted(str(t) for t in tags)} are leased: {conflicts}"
    )


@contextlib.contextmanager
def named_lock(
    store: LeaseStore,
    name: str,
    *,
    owner: str | None = None,
    ttl_s: float = DEFAULT_LEASE_TTL_S,
) -> Generator[Lease]:
    """Hold an arbitrary-name lock for the duration of the block.

    The same primitive as :func:`claim` without a registry, for serialising
    work that is not a declared resource. Released on exit even when the body
    raises.

    Raises:
        LeaseConflictError: the lock is held by a live, unexpired owner.
    """
    lease = store.acquire(name, owner=owner, ttl_s=ttl_s)
    try:
        yield lease
    finally:
        lease.release()


__all__ = [
    "DEFAULT_LEASE_TTL_S",
    "LEASE_SCHEMA_VERSION",
    "Lease",
    "LeaseConflictError",
    "LeaseStore",
    "NoFreeResourceError",
    "NoMatchingResourceError",
    "ResourceDeclaration",
    "ResourceLeaseError",
    "ResourceRegistry",
    "claim",
    "default_owner",
    "named_lock",
    "release_all_held",
]
