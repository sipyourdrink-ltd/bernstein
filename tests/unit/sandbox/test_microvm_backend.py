"""Unit tests for the microVM sandbox backend (#2613).

The backend runs the full :class:`SandboxBackendConformance` suite over a
:class:`FakeMonitor` (which really executes commands and really freezes the
workspace, so these are honest exercises of the snapshot pipeline, not mock
echoes). Backend-specific tests then cover the content-addressed snapshot
contract: digest determinism, tamper rejection on resume, the strict
no-silent-downgrade preflight, and path-traversal hardening of the image
format.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from bernstein.core.persistence.cas_store import CASIntegrityError, CASStore
from bernstein.core.sandbox.backend import ExecResult, SandboxCapability
from bernstein.core.sandbox.backends._vmmonitor import (
    FakeMonitor,
    MicroVMUnavailableError,
    canonical_workspace_image,
    extract_workspace_image,
)
from bernstein.core.sandbox.backends.microvm import (
    MicroVMProvisioningError,
    MicroVMSandboxBackend,
)
from bernstein.core.sandbox.conformance import SandboxBackendConformance
from bernstein.core.sandbox.manifest import FileEntry, GitRepoEntry, WorkspaceManifest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping
    from pathlib import Path


def _backend(tmp_path: Path) -> MicroVMSandboxBackend:
    return MicroVMSandboxBackend(
        monitor_factory=lambda root: FakeMonitor(root=root),
        cas=CASStore(tmp_path / "cas"),
    )


class TestMicroVMConformance(SandboxBackendConformance):
    """Run the full backend conformance suite over the FakeMonitor."""

    @pytest_asyncio.fixture
    async def backend(self, tmp_path: Path) -> AsyncIterator[MicroVMSandboxBackend]:
        yield _backend(tmp_path)

    @pytest.fixture
    def manifest(self) -> WorkspaceManifest:
        return WorkspaceManifest(root="/workspace", env={"LC_ALL": "C"}, timeout_seconds=60)


def test_backend_declares_hardware_boundary_capabilities(tmp_path: Path) -> None:
    # Inject a tmp_path-rooted CAS so the test never creates a repo-local
    # .sdd/cas directory as a side effect of constructing the backend.
    backend = MicroVMSandboxBackend(cas=CASStore(tmp_path / "cas"))
    assert backend.capabilities == frozenset(
        {
            SandboxCapability.FILE_RW,
            SandboxCapability.EXEC,
            SandboxCapability.NETWORK,
            SandboxCapability.SNAPSHOT,
        },
    )


@pytest.mark.asyncio
async def test_snapshot_digest_is_deterministic(tmp_path: Path) -> None:
    """Two identical workspaces snapshot to the same content address."""
    backend = _backend(tmp_path)
    manifest = WorkspaceManifest(root="/workspace", files=(FileEntry(path="a.txt", content=b"hi"),))
    s1 = await backend.create(manifest)
    await s1.write("state.txt", b"same")
    d1 = await s1.snapshot()
    s2 = await backend.create(manifest)
    await s2.write("state.txt", b"same")
    d2 = await s2.snapshot()
    assert d1 == d2
    await backend.destroy(s1)
    await backend.destroy(s2)


@pytest.mark.asyncio
async def test_resume_rejects_tampered_snapshot(tmp_path: Path) -> None:
    cas = CASStore(tmp_path / "cas")
    backend = MicroVMSandboxBackend(monitor_factory=lambda root: FakeMonitor(root=root), cas=cas)
    session = await backend.create(WorkspaceManifest(root="/workspace"))
    await session.write("state.txt", b"captured")
    digest = await session.snapshot()
    await backend.destroy(session)

    blob = cas.root / digest[:2] / digest
    corrupt = bytearray(blob.read_bytes())
    corrupt[5] ^= 0xFF
    blob.write_bytes(bytes(corrupt))

    with pytest.raises(CASIntegrityError):
        await backend.resume(digest)


@pytest.mark.asyncio
async def test_resume_unknown_digest_raises(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    with pytest.raises(KeyError):
        await backend.resume("0" * 64)


class _CloneFailsMonitor:
    """A VMMonitor stub whose ``git clone`` fails, to exercise create() cleanup.

    Structurally satisfies the (runtime-checkable) ``VMMonitor`` protocol; only
    the methods create() touches are meaningful. Records ``shutdown`` calls so a
    test can assert the guest is always torn down on a provisioning failure.
    """

    def __init__(self, root: str) -> None:
        self._root = root
        self.shutdown_calls = 0

    @property
    def workdir(self) -> str:
        return self._root

    async def boot(self, *, base_env: Mapping[str, str]) -> None:
        return None

    async def exec(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int | None = None,
        stdin: bytes | None = None,
    ) -> ExecResult:
        return ExecResult(exit_code=1, stdout=b"", stderr=b"fatal: repo not found", duration_seconds=0.0)

    async def write_file(self, path: str, data: bytes, *, mode: int = 0o644) -> None:
        return None

    async def read_file(self, path: str) -> bytes:
        return b""

    async def ls(self, path: str) -> list[str]:
        return []

    async def freeze_image(self) -> bytes:
        return b""

    async def restore_image(self, image: bytes) -> None:
        return None

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


class _BootFailsMonitor:
    """A VMMonitor stub that 'allocates' during boot and then raises.

    Mirrors a real monitor that starts a process/socket/tap device and then
    fails partway through boot - the partially-started guest must still be torn
    down. Records ``shutdown`` calls.
    """

    def __init__(self, root: str) -> None:
        self._root = root
        self.shutdown_calls = 0

    @property
    def workdir(self) -> str:
        return self._root

    async def boot(self, *, base_env: Mapping[str, str]) -> None:
        raise RuntimeError("boot exploded after allocating")

    async def exec(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int | None = None,
        stdin: bytes | None = None,
    ) -> ExecResult:
        raise AssertionError("unreachable: boot failed first")

    async def write_file(self, path: str, data: bytes, *, mode: int = 0o644) -> None:
        return None

    async def read_file(self, path: str) -> bytes:
        return b""

    async def ls(self, path: str) -> list[str]:
        return []

    async def freeze_image(self) -> bytes:
        return b""

    async def restore_image(self, image: bytes) -> None:
        return None

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


@pytest.mark.asyncio
async def test_create_tears_down_guest_when_boot_fails(tmp_path: Path) -> None:
    """A boot that raises after allocating must be shut down and register no session."""
    monitors: list[_BootFailsMonitor] = []

    def factory(root: str) -> _BootFailsMonitor:
        monitor = _BootFailsMonitor(root)
        monitors.append(monitor)
        return monitor

    backend = MicroVMSandboxBackend(monitor_factory=factory, cas=CASStore(tmp_path / "cas"))
    with pytest.raises(RuntimeError, match="boot exploded"):
        await backend.create(WorkspaceManifest(root="/workspace"))

    assert monitors and monitors[0].shutdown_calls == 1
    assert backend._sessions == {}


@pytest.mark.asyncio
async def test_resume_tears_down_guest_when_boot_fails(tmp_path: Path) -> None:
    """resume() likewise tears down a guest whose boot fails after allocating."""
    cas = CASStore(tmp_path / "cas")
    # Produce a real snapshot so resume() gets past its CAS lookup.
    good = MicroVMSandboxBackend(monitor_factory=lambda root: FakeMonitor(root=root), cas=cas)
    session = await good.create(WorkspaceManifest(root="/workspace"))
    await session.write("x.txt", b"data")
    digest = await session.snapshot()
    await good.destroy(session)

    monitors: list[_BootFailsMonitor] = []

    def factory(root: str) -> _BootFailsMonitor:
        monitor = _BootFailsMonitor(root)
        monitors.append(monitor)
        return monitor

    backend = MicroVMSandboxBackend(monitor_factory=factory, cas=cas)
    with pytest.raises(RuntimeError, match="boot exploded"):
        await backend.resume(digest)

    assert monitors and monitors[0].shutdown_calls == 1
    assert backend._sessions == {}


@pytest.mark.asyncio
async def test_create_tears_down_guest_when_clone_fails(tmp_path: Path) -> None:
    """A non-zero ``git clone`` fails loudly and never leaks the booted monitor."""
    monitors: list[_CloneFailsMonitor] = []

    def factory(root: str) -> _CloneFailsMonitor:
        monitor = _CloneFailsMonitor(root)
        monitors.append(monitor)
        return monitor

    backend = MicroVMSandboxBackend(monitor_factory=factory, cas=CASStore(tmp_path / "cas"))
    manifest = WorkspaceManifest(
        root="/workspace",
        repo=GitRepoEntry(src_path=str(tmp_path / "missing-repo"), branch="main"),
    )

    with pytest.raises(MicroVMProvisioningError):
        await backend.create(manifest)

    assert monitors, "monitor factory was never invoked"
    assert monitors[0].shutdown_calls == 1
    # A failed create must not register a session in the tracking table.
    assert backend._sessions == {}


@pytest.mark.asyncio
async def test_default_backend_refuses_without_kvm(tmp_path: Path) -> None:
    """The production backend never silently degrades: it raises on an unsupported host.

    On a KVM host this test's premise does not hold, so it is skipped.
    """
    backend = MicroVMSandboxBackend(cas=CASStore(tmp_path / "cas"))
    from bernstein.core.sandbox.backends._vmmonitor import FirecrackerMonitor

    if not FirecrackerMonitor().preflight():
        pytest.skip("host actually supports Firecracker; no-downgrade path not exercisable here")
    with pytest.raises(MicroVMUnavailableError):
        await backend.create(WorkspaceManifest(root="/workspace"))


def test_canonical_image_roundtrip_and_traversal_guard(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "f.txt").write_bytes(b"data")
    (src / "top.txt").write_bytes(b"top")
    image = canonical_workspace_image(src)
    # deterministic: same tree -> same bytes
    assert image == canonical_workspace_image(src)

    dest = tmp_path / "dest"
    extract_workspace_image(image, dest)
    assert (dest / "sub" / "f.txt").read_bytes() == b"data"
    assert (dest / "top.txt").read_bytes() == b"top"

    # A hostile image with a traversal member is rejected.
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        info = tarfile.TarInfo(name="../escape.txt")
        payload = b"x"
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    with pytest.raises(MicroVMUnavailableError):
        extract_workspace_image(buf.getvalue(), tmp_path / "dest2")


def test_extract_rejects_symlink_then_child_traversal(tmp_path: Path) -> None:
    """A symlink escaping root, followed by a child written through it, is refused.

    This is the TOCTOU a name-only pre-check misses: ``link -> ../../outside``
    passes a name check, then ``link/pwned`` writes outside root once the link
    exists. The stdlib ``data`` filter validates links at extraction time.
    """
    import io
    import tarfile

    outside = tmp_path / "outside"
    outside.mkdir()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        link = tarfile.TarInfo(name="link")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../outside"
        tar.addfile(link)
        child = tarfile.TarInfo(name="link/pwned.txt")
        payload = b"escaped"
        child.size = len(payload)
        tar.addfile(child, io.BytesIO(payload))

    with pytest.raises(MicroVMUnavailableError):
        extract_workspace_image(buf.getvalue(), tmp_path / "dest3")
    assert not (outside / "pwned.txt").exists()


def test_extract_refuses_on_unpatched_cpython(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Extraction is refused when the tarfile 'data' filter predates its CVE fix.

    The ``data`` filter is the only defence for an untrusted guest image, so on
    a build where it can be bypassed the backend must refuse rather than extract
    behind a broken guarantee.
    """
    from bernstein.core.sandbox.backends import _vmmonitor

    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"hi")
    image = canonical_workspace_image(src)

    monkeypatch.setattr(_vmmonitor, "_tarfile_data_filter_is_patched", lambda: False)
    with pytest.raises(MicroVMUnavailableError, match="CVE-2025-4517"):
        extract_workspace_image(image, tmp_path / "dest-unpatched")


def test_extract_guard_runs_before_clearing_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """On an unpatched interpreter the refusal fires before *root* is emptied.

    A doomed extraction must never destroy an already-populated destination -
    the guard is checked before any filesystem mutation.
    """
    from bernstein.core.sandbox.backends import _vmmonitor

    dest = tmp_path / "populated"
    dest.mkdir()
    (dest / "keep.txt").write_bytes(b"precious")

    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"hi")
    image = canonical_workspace_image(src)

    monkeypatch.setattr(_vmmonitor, "_tarfile_data_filter_is_patched", lambda: False)
    with pytest.raises(MicroVMUnavailableError, match="CVE-2025-4517"):
        extract_workspace_image(image, dest)
    assert (dest / "keep.txt").read_bytes() == b"precious"


@pytest.mark.raw_tarfile_filter
@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ((3, 11, 13, "final", 0), False),  # below the >=3.12 project floor -> refuse
        ((3, 12, 10, "final", 0), False),
        ((3, 12, 11, "final", 0), True),
        ((3, 13, 3, "final", 0), False),
        ((3, 13, 4, "final", 0), True),
        ((3, 14, 0, "alpha", 7), False),  # 3.14 fix did not land until b3
        ((3, 14, 0, "beta", 2), False),
        ((3, 14, 0, "beta", 3), True),
        ((3, 14, 0, "final", 0), True),
        ((3, 15, 0, "final", 0), True),
    ],
)
def test_tarfile_data_filter_version_matrix(
    monkeypatch: pytest.MonkeyPatch,
    version: tuple[int | str, ...],
    expected: bool,
) -> None:
    from bernstein.core.sandbox.backends import _vmmonitor

    monkeypatch.setattr(_vmmonitor.sys, "version_info", version)
    assert _vmmonitor._tarfile_data_filter_is_patched() is expected


def test_snapshot_digest_is_host_home_agnostic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The canonical image is a pure function of workspace CONTENT - it must not
    change with host state like ``$HOME``.

    Guards against the v3.7.1-class bug where a receipt hash silently depended on
    ``$HOME`` through a redaction helper, so an honest ledger verified on one
    host and failed on another.
    """
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "b.txt").write_bytes(b"nested")
    (src / "a.txt").write_bytes(b"deliverable")

    monkeypatch.setenv("HOME", "/home/alice")
    monkeypatch.setenv("USER", "alice")
    image_alice = canonical_workspace_image(src)

    monkeypatch.setenv("HOME", "/home/bob")
    monkeypatch.setenv("USER", "bob")
    image_bob = canonical_workspace_image(src)

    assert image_alice == image_bob  # same content -> same bytes -> same digest


def test_extract_rejects_absolute_symlink(tmp_path: Path) -> None:
    """A symlink whose target is an absolute path outside root is refused."""
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        link = tarfile.TarInfo(name="evil")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tar.addfile(link)

    with pytest.raises(MicroVMUnavailableError):
        extract_workspace_image(buf.getvalue(), tmp_path / "dest4")


# ---------------------------------------------------------------------------
# #2707 item 1: no filesystem side effect on construction; project-rooted default
# ---------------------------------------------------------------------------


def test_constructing_backend_creates_no_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Merely instantiating the backend must write nothing to disk.

    The microVM backend used to build its ``CASStore`` eagerly in ``__init__``,
    and ``CASStore.__init__`` creates its root directory - so constructing the
    backend (as ``registry.list_backends`` does for the whole catalogue) created
    ``.sdd/cas`` in whatever directory the operator happened to run from. The
    store must instead be built on first write.
    """
    empty = tmp_path / "empty_cwd"
    empty.mkdir()
    monkeypatch.chdir(empty)

    backend = MicroVMSandboxBackend()  # no cas, no cas_dir -> default location

    assert backend.name == "microvm"
    # Nothing was created under the CWD as a side effect of construction.
    assert list(empty.iterdir()) == []


def test_default_cas_dir_resolves_against_project_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default CAS location is anchored on the project root, not the CWD.

    ``.sdd`` is a project-rooted directory. Anchoring the default on the process
    working directory means a subdirectory invocation silently creates a second
    store. Resolving against the nearest project marker makes the location a
    function of the project, not of where the process was launched.
    """
    from bernstein.core.sandbox.backends import microvm

    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    sub = root / "pkg" / "deep"
    sub.mkdir(parents=True)

    # Resolve to an absolute path *while in each CWD* - a CWD-relative default
    # would only diverge once anchored, and deferring .resolve() past the second
    # chdir would hide the bug by anchoring both against the same final CWD.
    monkeypatch.chdir(sub)
    from_sub = microvm._default_cas_dir()
    from_sub_abs = from_sub.resolve()
    monkeypatch.chdir(root)
    from_root_abs = microvm._default_cas_dir().resolve()

    # A project-rooted default is absolute and identical from any subdirectory.
    assert from_sub.is_absolute()
    assert from_sub_abs == from_root_abs
    assert from_sub_abs == (root / ".sdd" / "cas").resolve()
    # Resolving the path created nothing.
    assert not (root / ".sdd").exists()


# ---------------------------------------------------------------------------
# #2702 items 3, 4, 6: shutdown/destroy lifecycle under a failing monitor
# ---------------------------------------------------------------------------


class _FlakyShutdownMonitor:
    """A VMMonitor stub whose ``shutdown`` fails the first ``fail_times`` calls.

    ``boot`` succeeds so ``create()`` registers a real session; ``shutdown``
    then raises until it has been called ``fail_times`` times, so a test can
    prove a failed teardown is retryable (``_closed`` is not latched) and that
    the backend still drops the tracking entry.
    """

    def __init__(self, root: str, *, fail_times: int = 1) -> None:
        self._root = root
        self._fail_times = fail_times
        self.shutdown_calls = 0

    @property
    def workdir(self) -> str:
        return self._root

    async def boot(self, *, base_env: Mapping[str, str]) -> None:
        return None

    async def exec(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int | None = None,
        stdin: bytes | None = None,
    ) -> ExecResult:  # pragma: no cover - unused by these tests
        return ExecResult(exit_code=0, stdout=b"", stderr=b"", duration_seconds=0.0)

    async def write_file(self, path: str, data: bytes, *, mode: int = 0o644) -> None:
        return None

    async def read_file(self, path: str) -> bytes:  # pragma: no cover
        return b""

    async def ls(self, path: str) -> list[str]:  # pragma: no cover
        return []

    async def freeze_image(self) -> bytes:  # pragma: no cover
        return b""

    async def restore_image(self, image: bytes) -> None:  # pragma: no cover
        return None

    async def shutdown(self) -> None:
        self.shutdown_calls += 1
        if self.shutdown_calls <= self._fail_times:
            raise RuntimeError("monitor shutdown boom")


@pytest.mark.asyncio
async def test_failed_monitor_shutdown_is_not_latched_and_pops_session(tmp_path: Path) -> None:
    """A session whose ``monitor.shutdown()`` raised must remain tearable-down.

    Guards three lifecycle rules at once:
      * ``MicroVMSandboxSession.shutdown`` sets ``_closed`` only *after* the
        monitor shutdown returns, so a failed shutdown does not latch the
        session shut (#2702 item 3).
      * ``MicroVMSandboxBackend.destroy`` pops the tracking entry in a
        ``finally``, so a failed shutdown does not strand it (#2702 item 4).
      * A retry actually re-attempts the monitor shutdown and succeeds
        (#2702 item 6).
    """
    monitors: list[_FlakyShutdownMonitor] = []

    def factory(root: str) -> _FlakyShutdownMonitor:
        monitor = _FlakyShutdownMonitor(root, fail_times=1)
        monitors.append(monitor)
        return monitor

    backend = MicroVMSandboxBackend(monitor_factory=factory, cas=CASStore(tmp_path / "cas"))
    session = await backend.create(WorkspaceManifest(root="/workspace"))
    sid = session.session_id
    assert sid in backend._sessions

    # First teardown: the monitor shutdown raises.
    with pytest.raises(RuntimeError, match="monitor shutdown boom"):
        await backend.destroy(session)

    # The entry is popped despite the failure (AC 4), and the session is not
    # latched shut (AC 3/6), so it can be torn down again.
    assert sid not in backend._sessions
    assert session._closed is False
    assert monitors[0].shutdown_calls == 1

    # Retry: the monitor shutdown is actually re-attempted and now succeeds.
    await backend.destroy(session)
    assert session._closed is True
    assert monitors[0].shutdown_calls == 2
