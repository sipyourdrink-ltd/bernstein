"""MicroVM sandbox backend with content-addressed snapshots (#2613).

The ``microvm`` backend is the first sandbox backend that isolates the
*kernel, network namespace, and process tree* at a hardware boundary,
not just the filesystem. It implements the existing
:class:`~bernstein.core.sandbox.backend.SandboxBackend` protocol against a
swappable :class:`~bernstein.core.sandbox.backends._vmmonitor.VMMonitor`
(Firecracker in production; a deterministic
:class:`~bernstein.core.sandbox.backends._vmmonitor.FakeMonitor` under
test), so every existing adapter works unchanged.

Its distinguishing feature is the snapshot contract. Where other backends
return a backend-private opaque handle (or raise ``NotImplementedError``),
:meth:`MicroVMSandboxSession.snapshot` freezes the workspace into a
*canonicalised image*, streams it into the content-addressed store
(``.sdd/cas``), and returns the image's **SHA-256 digest** as the snapshot
id. Two consequences fall out for free:

- ``resume(digest)`` reads the blob back by digest and verifies it before
  boot, so a tampered snapshot fails its CAS integrity check
  (:class:`~bernstein.core.persistence.cas_store.CASIntegrityError`)
  rather than booting corrupt state; and
- because the id *is* the content hash, two operators with the same base
  image resume byte-identical workspaces - the property the deterministic
  fork-and-race primitive (:mod:`bernstein.core.sandbox.fork_race`) builds
  its reproducible, attributable races on.

Host support (KVM, kernel/rootfs, ``firecracker`` binary) is enforced at
:meth:`create` time by the monitor's preflight. When it is absent the
backend raises
:class:`~bernstein.core.sandbox.backends._vmmonitor.MicroVMUnavailableError`
- it never silently degrades to a weaker isolation mode. The selector
therefore honours an explicit ``sandbox.backend: microvm`` with a loud
error on an unsupported host instead of a surprise worktree.

.. note::
   **The Firecracker VM boot path is experimental and not yet implemented.**
   This release ships the deterministic, fully-tested core - content-addressed
   snapshots, the fork-and-race primitive, and the signed selection receipt -
   plus the host preflight and no-silent-downgrade contract. The real guest
   boot + vsock-agent transport (which needs a KVM host and operator-supplied
   kernel/rootfs/agent, unbuildable on a host without ``/dev/kvm``) is a
   tracked follow-up. The snapshot/receipt machinery is validated
   host-independently over the ``FakeMonitor``.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.core.persistence.cas_store import CASStore
from bernstein.core.sandbox.backend import (
    ExecResult,
    SandboxCapability,
    SandboxSession,
)
from bernstein.core.sandbox.backends._vmmonitor import (
    SNAPSHOT_CONTENT_TYPE,
    FirecrackerMonitor,
    VMMonitor,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from bernstein.core.sandbox.manifest import WorkspaceManifest

_logger = logging.getLogger(__name__)

#: Directory (relative to the project root) that holds the content-addressed
#: store for VM snapshots.
_CAS_SUBDIR = Path(".sdd/cas")


def _default_cas_dir() -> Path:
    """Resolve the default CAS directory against the project root, not the CWD.

    ``.sdd`` is a project-rooted directory. Anchoring the default on the process
    working directory means the store follows the operator around: a run from a
    subdirectory silently creates a second store under that subdirectory. Walk up
    from the CWD to the nearest project marker (``.git`` or ``pyproject.toml``)
    and root the store there; fall back to the CWD when no marker is found. This
    only computes a path - it creates nothing (the store is built on first
    write).
    """
    start = Path.cwd()
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists() or (candidate / "pyproject.toml").exists():
            return candidate / _CAS_SUBDIR
    return start / _CAS_SUBDIR


class MicroVMProvisioningError(RuntimeError):
    """Raised when a booted guest cannot be provisioned into a usable state.

    Distinct from
    :class:`~bernstein.core.sandbox.backends._vmmonitor.MicroVMUnavailableError`
    (the *host* cannot run a microVM at all): here the guest booted but a
    provisioning step - a ``git clone``/``checkout`` returning non-zero, a
    file write failing - left the workspace invalid. The guest is always torn
    down before this propagates, so a failed :meth:`MicroVMSandboxBackend.create`
    never leaks a running monitor.
    """


def _default_monitor_factory(root: str) -> VMMonitor:
    """Production factory: a real Firecracker monitor (preflighted on boot)."""
    return FirecrackerMonitor(root=root)


def _require_ok(result: ExecResult, what: str, session_id: str) -> None:
    """Raise :class:`MicroVMProvisioningError` when a provisioning step failed."""
    if result.exit_code != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip()
        msg = f"{what} failed (exit {result.exit_code}) provisioning microVM session {session_id!r}: {stderr}"
        raise MicroVMProvisioningError(msg)


class MicroVMSandboxSession(SandboxSession):
    """An active microVM session backed by a :class:`VMMonitor`.

    File and exec I/O delegate straight to the monitor. :meth:`snapshot`
    is the interesting one: it content-addresses the workspace image into
    CAS and returns the digest.
    """

    backend_name = "microvm"

    def __init__(
        self,
        *,
        session_id: str,
        monitor: VMMonitor,
        cas: CASStore,
    ) -> None:
        self.session_id = session_id
        self.workdir = monitor.workdir
        self._monitor = monitor
        self._cas = cas
        self._closed = False

    async def read(self, path: str) -> bytes:
        return await self._monitor.read_file(path)

    async def write(self, path: str, data: bytes, *, mode: int = 0o644) -> None:
        await self._monitor.write_file(path, data, mode=mode)

    async def exec(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int | None = None,
        stdin: bytes | None = None,
    ) -> ExecResult:
        return await self._monitor.exec(
            cmd,
            cwd=cwd,
            env=env,
            timeout=timeout,
            stdin=stdin,
        )

    async def ls(self, path: str) -> list[str]:
        return await self._monitor.ls(path)

    async def snapshot(self) -> str:
        """Freeze the workspace to a content-addressed image; return its digest.

        The digest returned *is* ``sha256`` of the canonical image bytes,
        so it is a stable content address that :meth:`MicroVMSandboxBackend.resume`
        can boot from and that the fork-race receipt can pin.
        """
        image = await self._monitor.freeze_image()
        return self._cas.put(
            image,
            content_type=SNAPSHOT_CONTENT_TYPE,
            metadata={"backend": self.backend_name, "session_id": self.session_id},
        )

    async def shutdown(self) -> None:
        if self._closed:
            return
        # Set _closed only AFTER the monitor shutdown returns. Latching it before
        # the await would make a failed shutdown (monitor raised) look closed, so
        # the early-return above would swallow every retry and leave the guest
        # running - the session must stay tearable-down on retry.
        await self._monitor.shutdown()
        self._closed = True


class MicroVMSandboxBackend:
    """Provisions :class:`MicroVMSandboxSession` instances.

    Args:
        monitor_factory: Builds one :class:`VMMonitor` per session given
            the logical workspace root. Defaults to a real
            :class:`FirecrackerMonitor`; tests inject a
            :class:`FakeMonitor` factory.
        cas: Content-addressed store for snapshots. When omitted a store is
            built lazily on first write, rooted at ``.sdd/cas`` under the
            project root (never created merely by constructing the backend);
            tests inject a temp-dir store.
        cas_dir: Convenience alternative to *cas* - the directory to root
            a fresh :class:`CASStore` at (also built lazily on first write).
            Ignored when *cas* is given.
    """

    name = "microvm"
    capabilities = frozenset(
        {
            SandboxCapability.FILE_RW,
            SandboxCapability.EXEC,
            SandboxCapability.NETWORK,
            SandboxCapability.SNAPSHOT,
        },
    )

    def __init__(
        self,
        *,
        monitor_factory: Callable[[str], VMMonitor] | None = None,
        cas: CASStore | None = None,
        cas_dir: Path | None = None,
    ) -> None:
        self._monitor_factory = monitor_factory or _default_monitor_factory
        # Do NOT build the CASStore here. CASStore.__init__ creates its root
        # directory, and merely *constructing* a backend (as
        # registry.list_backends does when it materialises the whole catalogue)
        # must not touch the filesystem. Build it lazily on first use, and root
        # the default against the project root rather than the process CWD.
        self._cas: CASStore | None = cas
        self._cas_dir: Path = Path(cas_dir) if cas_dir is not None else _default_cas_dir()
        self._sessions: dict[str, MicroVMSandboxSession] = {}

    @property
    def cas(self) -> CASStore:
        """The content-addressed store backing this backend's snapshots.

        Built lazily on first access so that instantiating the backend creates
        no directories; the store (and its root) is created on first write.
        """
        if self._cas is None:
            self._cas = CASStore(self._cas_dir)
        return self._cas

    async def create(
        self,
        manifest: WorkspaceManifest,
        options: dict[str, Any] | None = None,
    ) -> MicroVMSandboxSession:
        """Provision a fresh microVM session from *manifest*.

        Host-side path construction comes only from *manifest* (a
        host-controlled value object), never from bytes read out of a
        guest - a restored guest cannot redirect where the backend writes.

        Raises:
            MicroVMUnavailableError: When the host cannot run a microVM
                (no KVM, missing binary/kernel/rootfs). The backend refuses
                rather than degrading to weaker isolation.
        """
        opts = options or {}
        session_id = str(opts.get("session_id") or f"microvm-{uuid.uuid4().hex}")
        monitor = self._monitor_factory(manifest.root)

        # boot() is inside the guard: a real monitor can allocate a process,
        # socket, or tap device and then raise partway through boot, and that
        # partially-started guest must still be torn down. Any post-boot
        # provisioning failure likewise tears the guest down - a half-
        # provisioned monitor is a resource leak, and a silently-ignored
        # non-zero git exit would hand back a session over an invalid
        # repository. BaseException so a cancellation also cleans up.
        try:
            await monitor.boot(base_env=manifest.env)
            if manifest.repo is not None:
                repo = manifest.repo
                clone = await monitor.exec(
                    ["git", "clone", "--no-hardlinks", "--quiet", repo.src_path, "."],
                )
                _require_ok(clone, f"git clone {repo.src_path!r}", session_id)
                checkout = await monitor.exec(["git", "checkout", "--quiet", repo.branch])
                _require_ok(checkout, f"git checkout {repo.branch!r}", session_id)

            for entry in manifest.files:
                await monitor.write_file(entry.path, entry.content, mode=entry.mode)
        except BaseException:
            # Tear the guest down, but never let a cleanup error mask the
            # original provisioning failure - log it so a genuine leaked guest
            # (process/socket/tap device) leaves a trace instead of vanishing.
            try:
                await monitor.shutdown()
            except Exception:
                _logger.warning(
                    "microVM shutdown failed during create() cleanup; a guest resource may have leaked (session %s)",
                    session_id,
                    exc_info=True,
                )
            raise

        session = MicroVMSandboxSession(
            session_id=session_id,
            monitor=monitor,
            cas=self.cas,
        )
        self._sessions[session_id] = session
        return session

    async def resume(self, snapshot_id: str) -> MicroVMSandboxSession:
        """Boot a fresh session from a content-addressed snapshot.

        The blob is read with integrity verification on, so a tampered
        snapshot raises
        :class:`~bernstein.core.persistence.cas_store.CASIntegrityError`
        before any guest state is booted.

        Raises:
            KeyError: When *snapshot_id* is not present in CAS.
            CASIntegrityError: When the stored bytes do not hash to
                *snapshot_id* (tampering / corruption).
        """
        image = self.cas.get(snapshot_id, verify=True)
        if image is None:
            msg = f"Unknown microvm snapshot digest: {snapshot_id}"
            raise KeyError(msg)

        monitor = self._monitor_factory("/workspace")
        # boot() and restore are both inside the guard: a boot that partially
        # allocates then raises, or a restore that raises (e.g. an unsafe member
        # in the image), must not leave the guest running.
        try:
            await monitor.boot(base_env={})
            await monitor.restore_image(image)
        except BaseException:
            try:
                await monitor.shutdown()
            except Exception:
                _logger.warning(
                    "microVM shutdown failed during resume() cleanup; a guest resource may have leaked (snapshot %s)",
                    snapshot_id,
                    exc_info=True,
                )
            raise

        session_id = f"microvm-resume-{snapshot_id[:12]}-{uuid.uuid4().hex[:8]}"
        session = MicroVMSandboxSession(
            session_id=session_id,
            monitor=monitor,
            cas=self.cas,
        )
        self._sessions[session_id] = session
        return session

    async def destroy(self, session: SandboxSession) -> None:
        """Tear down *session* and drop it from the tracking table.

        The tracking entry is dropped in a ``finally``: a failed
        ``session.shutdown()`` (monitor raised) must not strand the entry in the
        table, or a stranded session would read as live forever. The session
        itself stays tearable-down on retry - ``shutdown`` does not latch
        ``_closed`` on failure - so a caller can retry ``destroy`` after a
        transient monitor error.
        """
        try:
            await session.shutdown()
        finally:
            self._sessions.pop(session.session_id, None)


__all__ = [
    "MicroVMProvisioningError",
    "MicroVMSandboxBackend",
    "MicroVMSandboxSession",
]
