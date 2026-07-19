"""MicroVM monitor shim for the ``microvm`` sandbox backend (#2613).

The :class:`MicroVMSandboxBackend` never talks to a hypervisor directly.
It drives a :class:`VMMonitor` - a small, swappable adapter that owns one
guest's lifecycle (boot, exec, file I/O, freeze-to-image, restore,
shutdown). Two monitors ship here:

- :class:`FirecrackerMonitor` - the real adapter. It performs a strict
  host preflight (KVM device, ``firecracker`` binary, a configured kernel
  + rootfs) and raises :class:`MicroVMUnavailableError` the moment any
  precondition is missing. It never silently degrades into a weaker
  isolation mode - an operator who asked for a hardware boundary gets an
  error, not a surprise.
- :class:`FakeMonitor` - a deterministic, host-portable stand-in used by
  the unit + acceptance tests (mirroring how the Docker backend mocks its
  daemon). It runs commands as ordinary local subprocesses inside a
  private temp directory and freezes that directory into a *canonicalised
  workspace image*. Because it really executes and really captures the
  resulting filesystem, its snapshots are honest disk images - not canned
  bytes - so the determinism/tamper acceptance test exercises the actual
  content-addressing and signing substrate rather than a mock's echo.

Snapshots are **full canonicalised workspace images**, not memory dumps.
A memory snapshot can never be byte-reproducible (kernel timers, entropy
pool, page/ASLR ordering), which would make the "same race twice ->
identical signed receipt" guarantee impossible. A canonicalised tar of the
guest filesystem (sorted paths, zeroed mtimes/uids, normalised modes) is
reproducible given identical file contents, and its ``sha256`` *is* the
snapshot id the CAS store addresses. Full images are self-contained, so
``resume(digest)`` needs no base and cannot be confused about which base
it forked from. Disk-*delta* images (smaller, base-relative) are a future
optimisation tracked separately; correctness does not depend on them.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import operator
import os
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from bernstein.core.sandbox.backend import ExecResult

if TYPE_CHECKING:
    from collections.abc import Mapping


class MicroVMUnavailableError(RuntimeError):
    """Raised when a microVM cannot be provisioned on this host.

    The message enumerates every missing precondition (KVM, binary,
    kernel image) so the operator can fix the host. Callers must treat
    this as a hard failure - the microvm backend deliberately does *not*
    fall back to a weaker isolation mode, because a silent downgrade from
    a hardware boundary to a bare worktree is exactly the security
    regression an operator running untrusted code cannot tolerate.
    """


# ---------------------------------------------------------------------------
# Canonical workspace-image helpers (the content-addressed snapshot format)
# ---------------------------------------------------------------------------

#: Content-type recorded in CAS metadata for a workspace-image snapshot.
SNAPSHOT_CONTENT_TYPE = "application/x-bernstein-vm-snapshot+tar"


def _tarfile_data_filter_is_patched() -> bool:
    """Whether this interpreter's tarfile ``data`` filter includes the June-2025 fix.

    The stdlib ``data`` filter (:func:`extract_workspace_image`'s only defence
    against a hostile image) had a family of extraction-escape bypasses
    (CVE-2024-12718 / CVE-2025-4138 / CVE-2025-4330 / CVE-2025-4435 /
    CVE-2025-4517) fixed in CPython 3.12.11, 3.13.4, and 3.14.0b3 (the 3.14
    fix did not land until beta 3, so 3.14.0a* / b1 / b2 are still vulnerable).
    On an unpatched build the filter can be circumvented, so relying on it there
    is a false sense of safety. The project floor is ``>=3.12``, which still
    admits vulnerable 3.12.0-3.12.10 / 3.13.0-3.13.3 builds - hence this
    runtime check rather than a project-wide ``requires-python`` bump.
    """
    v = sys.version_info
    if v[:2] == (3, 12):
        return v >= (3, 12, 11)
    if v[:2] == (3, 13):
        return v >= (3, 13, 4)
    if v[:2] == (3, 14):
        # version_info compares releaselevel lexically (alpha < beta <
        # candidate < final), so this admits 3.14.0b3+ and every final/rc.
        return v >= (3, 14, 0, "beta", 3)
    return v[:2] > (3, 14)


def canonical_workspace_image(root: Path) -> bytes:
    """Freeze the directory tree at *root* into deterministic tar bytes.

    Determinism is the whole point: two calls over byte-identical file
    trees must produce byte-identical output so ``sha256`` of the result
    is a stable content address. To get there we strip every source of
    run-to-run variance a normal tar would embed:

    - entries are emitted in sorted path order (not readdir order);
    - mtime is zeroed; uid/gid/uname/gname are zeroed/emptied;
    - modes are normalised to ``0o755`` for dirs/executables and
      ``0o644`` otherwise (guest-arbitrary permission bits do not affect
      the deliverable and would otherwise poison the digest);
    - the fixed ``USTAR`` format is used so no PAX timestamp headers leak.

    Symlinks are captured as symlinks; special files are skipped (a guest
    should not be smuggling device nodes into a content-addressed image).

    Args:
        root: Directory whose contents become the image.

    Returns:
        Deterministic tar bytes suitable for ``CASStore.put``.
    """
    root = root.resolve()
    entries: list[tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        base = Path(dirpath)
        for name in sorted(dirnames):
            full = base / name
            arc = full.relative_to(root).as_posix()
            entries.append((arc, full))
        for name in sorted(filenames):
            full = base / name
            arc = full.relative_to(root).as_posix()
            entries.append((arc, full))
    entries.sort(key=operator.itemgetter(0))

    buf = io.BytesIO()
    # Fixed format + no compression: gzip would embed an mtime and OS byte.
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for arc, full in entries:
            info = tarfile.TarInfo(name=arc)
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            if full.is_symlink():
                info.type = tarfile.SYMTYPE
                info.linkname = os.readlink(full)
                info.mode = 0o777
                tar.addfile(info)
            elif full.is_dir():
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            elif full.is_file():
                data = full.read_bytes()
                info.type = tarfile.REGTYPE
                info.size = len(data)
                info.mode = 0o755 if os.access(full, os.X_OK) else 0o644
                tar.addfile(info, io.BytesIO(data))
            # else: skip fifos/sockets/devices - not part of a deliverable.
    return buf.getvalue()


def extract_workspace_image(image: bytes, root: Path) -> None:
    """Restore a :func:`canonical_workspace_image` blob into *root*.

    *root* is emptied first so the restored tree equals the image exactly
    (no stray files leaking in from a reused directory). A snapshot image is
    guest-derived and therefore untrusted even though its bytes verified
    against the CAS digest - content-addressing proves *integrity*, not
    *benignity*. Extraction is hardened against path traversal and link
    escapes via the stdlib ``data`` filter (see below).

    Args:
        image: Tar bytes produced by :func:`canonical_workspace_image`.
        root: Destination directory (created if absent, cleared if present).

    Raises:
        MicroVMUnavailableError: When a member is unsafe (absolute path,
            ``..`` traversal, or a sym/hardlink escaping *root*).
    """
    root = root.resolve()

    # Refuse BEFORE any filesystem mutation. The stdlib ``data`` filter is the
    # *only* thing standing between this untrusted, guest-derived image and an
    # extraction escape, and on a CPython build predating the CVE-2025-4517-
    # family fix it can be bypassed - so refuse rather than extract behind a
    # defence that does not hold. Checking here (not after clearing *root*)
    # means a doomed extraction never destroys an already-populated workspace.
    if not _tarfile_data_filter_is_patched():
        version = ".".join(str(part) for part in sys.version_info[:3])
        msg = (
            f"Refusing to extract an untrusted snapshot image on CPython {version}: "
            "its tarfile 'data' filter predates the CVE-2025-4517-family fix "
            "(upgrade to >=3.12.11 / >=3.13.4)."
        )
        raise MicroVMUnavailableError(msg)

    if root.exists():
        for child in root.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
    else:
        root.mkdir(parents=True, exist_ok=True)

    # The stdlib ``data`` filter (PEP 706, py3.12+) does the right
    # member-by-member checks *at extraction time*: it refuses absolute paths,
    # ``..`` traversal, and links (symlink OR hardlink) whose target escapes
    # the destination. That closes the symlink-then-child TOCTOU a name-only
    # pre-check misses - a hostile ``dir -> ../../outside`` followed by
    # ``dir/file`` is resolved against the real link at write time and refused.
    with tarfile.open(fileobj=io.BytesIO(image), mode="r:") as tar:
        try:
            tar.extractall(root, filter="data")
        except tarfile.FilterError as exc:
            msg = f"Refusing unsafe member in snapshot image: {exc}"
            raise MicroVMUnavailableError(msg) from exc


# ---------------------------------------------------------------------------
# Monitor protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class VMMonitor(Protocol):
    """One guest's lifecycle, abstracted away from the hypervisor.

    The backend depends only on this surface, so Firecracker, a future
    Cloud Hypervisor adapter, and the test :class:`FakeMonitor` are fully
    interchangeable. All methods are async to match
    :class:`~bernstein.core.sandbox.backend.SandboxSession`.
    """

    @property
    def workdir(self) -> str:
        """Absolute path of the workspace root inside the guest."""
        ...

    async def boot(self, *, base_env: Mapping[str, str]) -> None:
        """Start the guest and prepare an empty workspace root."""
        ...

    async def exec(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int | None = None,
        stdin: bytes | None = None,
    ) -> ExecResult:
        """Run *cmd* inside the guest and capture its result."""
        ...

    async def read_file(self, path: str) -> bytes:
        """Read a file from the guest workspace."""
        ...

    async def write_file(self, path: str, data: bytes, *, mode: int = 0o644) -> None:
        """Write *data* to *path* in the guest workspace."""
        ...

    async def ls(self, path: str) -> list[str]:
        """List directory entry names in the guest workspace, sorted."""
        ...

    async def freeze_image(self) -> bytes:
        """Return a canonicalised, content-addressable image of the workspace."""
        ...

    async def restore_image(self, image: bytes) -> None:
        """Boot/populate the guest workspace from a frozen image."""
        ...

    async def shutdown(self) -> None:
        """Tear the guest down. Idempotent."""
        ...


# ---------------------------------------------------------------------------
# FakeMonitor - deterministic local stand-in used by tests
# ---------------------------------------------------------------------------


class FakeMonitor:
    """A host-portable, deterministic :class:`VMMonitor` for tests.

    It is *not* a mock that returns canned bytes. It really executes
    commands (as local subprocesses) inside a private temp directory and
    really freezes that directory into a canonical image. That honesty is
    what lets the acceptance test prove determinism of the actual snapshot
    /CAS/receipt pipeline instead of proving a mock echoes itself.

    The only thing it does *not* provide is the kernel/network/PID
    hardware boundary - that is :class:`FirecrackerMonitor`'s job and is
    validated by the KVM-gated integration test. Everything the
    determinism guarantee depends on lives above the boundary and is fully
    exercised here.
    """

    def __init__(self, *, root: str = "/workspace") -> None:
        self._logical_root = root
        # Resolve symlinks up front (macOS /var -> /private/var) so the
        # containment check in _resolve compares like with like; otherwise a
        # resolved target legitimately under the temp dir looks like an escape.
        self._host_dir = Path(tempfile.mkdtemp(prefix="bernstein-microvm-fake-")).resolve()
        self._base_env: dict[str, str] = {}
        self._closed = False

    @property
    def workdir(self) -> str:
        return self._logical_root

    # -- path safety -------------------------------------------------------

    def _resolve(self, path: str) -> Path:
        """Map a guest path to a host path pinned under the temp dir."""
        rel = path
        if Path(path).is_absolute():
            # Treat the logical root as the guest FS root for absolute paths.
            rel = os.path.relpath(path, self._logical_root)
        target = (self._host_dir / rel).resolve()
        if target != self._host_dir and self._host_dir not in target.parents:
            msg = f"Path {path!r} escapes the guest workspace"
            raise ValueError(msg)
        return target

    # -- lifecycle ---------------------------------------------------------

    async def boot(self, *, base_env: Mapping[str, str]) -> None:
        self._base_env = dict(base_env)
        self._host_dir.mkdir(parents=True, exist_ok=True)

    async def exec(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int | None = None,
        stdin: bytes | None = None,
    ) -> ExecResult:
        run_env = os.environ.copy()
        run_env.update(self._base_env)
        if env:
            run_env.update(env)
        workdir = self._resolve(cwd) if cwd else self._host_dir

        loop = asyncio.get_running_loop()
        start = loop.time()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(workdir),
            env=run_env,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=stdin),
                timeout=timeout,
            )
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            msg = f"Command timed out after {timeout}s: {cmd!r}"
            raise TimeoutError(msg) from exc
        duration = loop.time() - start
        return ExecResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
        )

    async def read_file(self, path: str) -> bytes:
        target = self._resolve(path)
        if not target.exists():
            msg = f"No such file in guest: {path}"
            raise FileNotFoundError(msg)
        return target.read_bytes()

    async def write_file(self, path: str, data: bytes, *, mode: int = 0o644) -> None:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        with contextlib.suppress(OSError):
            target.chmod(mode)

    async def ls(self, path: str) -> list[str]:
        target = self._resolve(path)
        if not target.is_dir():
            msg = f"Not a directory in guest: {path}"
            raise NotADirectoryError(msg)
        return sorted(p.name for p in target.iterdir())

    async def freeze_image(self) -> bytes:
        return canonical_workspace_image(self._host_dir)

    async def restore_image(self, image: bytes) -> None:
        self._host_dir.mkdir(parents=True, exist_ok=True)
        extract_workspace_image(image, self._host_dir)

    async def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        shutil.rmtree(self._host_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# FirecrackerMonitor - the real adapter (strict preflight, no silent degrade)
# ---------------------------------------------------------------------------


class FirecrackerMonitor:
    """Real microVM monitor driving a Firecracker guest.

    **Experimental - the guest boot path is not yet implemented.** This
    release ships the host preflight and the strict no-silent-downgrade
    contract; the full Firecracker lifecycle (API socket -> boot-source ->
    drives -> network -> ``InstanceStart`` -> in-guest vsock agent for
    exec/file-IO/snapshot) is a tracked follow-up that requires a
    KVM-capable Linux host plus an operator-supplied guest kernel, rootfs,
    and agent - none of which can be provisioned or validated on a host
    without ``/dev/kvm`` (e.g. macOS or CI runners without nested virt).

    Construction is cheap and never boots a VM. :meth:`preflight` reports
    the missing host preconditions side-effect-free. :meth:`boot` runs it
    and, on a supported host, still raises :class:`MicroVMUnavailableError`
    because the boot lifecycle is unimplemented - it never pretends to have
    booted, and it never degrades to a weaker isolation mode. The
    determinism / content-addressing / receipt machinery this backend feeds
    is validated host-independently over :class:`FakeMonitor`; the real
    boot is covered by the opt-in KVM-gated integration test once the
    follow-up lands.
    """

    #: Env var pointing at a guest kernel image (vmlinux).
    KERNEL_ENV = "BERNSTEIN_MICROVM_KERNEL"
    #: Env var pointing at a guest rootfs (ext4) image.
    ROOTFS_ENV = "BERNSTEIN_MICROVM_ROOTFS"

    def __init__(
        self,
        *,
        root: str = "/workspace",
        firecracker_bin: str = "firecracker",
        kernel_image: str | None = None,
        rootfs_image: str | None = None,
    ) -> None:
        self._logical_root = root
        self._firecracker_bin = firecracker_bin
        self._kernel_image = kernel_image or os.environ.get(self.KERNEL_ENV)
        self._rootfs_image = rootfs_image or os.environ.get(self.ROOTFS_ENV)

    @property
    def workdir(self) -> str:
        return self._logical_root

    def preflight(self) -> list[str]:
        """Return the list of missing host preconditions (empty == ready).

        Checks are cheap and side-effect-free so the selector/backend can
        probe support before committing to a boot.
        """
        missing: list[str] = []
        if sys.platform != "linux":
            missing.append(f"host OS is {sys.platform!r}, Firecracker requires Linux/KVM")
        if not Path("/dev/kvm").exists():
            missing.append("/dev/kvm not present (no hardware virtualization / KVM)")
        elif not os.access("/dev/kvm", os.R_OK | os.W_OK):
            missing.append("/dev/kvm is not readable+writable by this user")
        if shutil.which(self._firecracker_bin) is None:
            missing.append(f"{self._firecracker_bin!r} binary not found on PATH")
        if not self._kernel_image or not Path(self._kernel_image).exists():
            missing.append(f"guest kernel image missing (set ${self.KERNEL_ENV})")
        if not self._rootfs_image or not Path(self._rootfs_image).exists():
            missing.append(f"guest rootfs image missing (set ${self.ROOTFS_ENV})")
        return missing

    def _require_available(self) -> None:
        missing = self.preflight()
        if missing:
            detail = "; ".join(missing)
            msg = f"MicroVM (Firecracker) unavailable on this host: {detail}"
            raise MicroVMUnavailableError(msg)

    async def boot(self, *, base_env: Mapping[str, str]) -> None:
        # Preflight first: on an unsupported host this is where an explicit
        # microvm request fails loudly (never a silent worktree downgrade).
        self._require_available()
        # On a supported host we still refuse: the full Firecracker boot
        # lifecycle (API socket -> boot-source -> drives -> network ->
        # InstanceStart -> in-guest vsock agent) is not implemented in this
        # release. We raise rather than pretend to have booted.
        raise MicroVMUnavailableError(
            "Firecracker guest boot is not yet implemented in this release "
            "(experimental adapter). The deterministic snapshot / fork-race / "
            "receipt machinery is complete; the VM boot + guest-agent transport "
            "is a tracked follow-up requiring a KVM host. This build refuses "
            "rather than silently degrading isolation.",
        )

    async def exec(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int | None = None,
        stdin: bytes | None = None,
    ) -> ExecResult:
        self._require_available()
        raise MicroVMUnavailableError("Firecracker guest exec path not provisioned on this host")

    async def read_file(self, path: str) -> bytes:
        self._require_available()
        raise MicroVMUnavailableError("Firecracker guest I/O not provisioned on this host")

    async def write_file(self, path: str, data: bytes, *, mode: int = 0o644) -> None:
        self._require_available()
        raise MicroVMUnavailableError("Firecracker guest I/O not provisioned on this host")

    async def ls(self, path: str) -> list[str]:
        self._require_available()
        raise MicroVMUnavailableError("Firecracker guest I/O not provisioned on this host")

    async def freeze_image(self) -> bytes:
        self._require_available()
        raise MicroVMUnavailableError("Firecracker guest snapshot not provisioned on this host")

    async def restore_image(self, image: bytes) -> None:
        self._require_available()
        raise MicroVMUnavailableError("Firecracker guest restore not provisioned on this host")

    async def shutdown(self) -> None:
        # Idempotent: nothing to tear down until the boot lifecycle lands.
        return


__all__ = [
    "SNAPSHOT_CONTENT_TYPE",
    "FakeMonitor",
    "FirecrackerMonitor",
    "MicroVMUnavailableError",
    "VMMonitor",
    "canonical_workspace_image",
    "extract_workspace_image",
]
