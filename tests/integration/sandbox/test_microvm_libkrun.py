"""Host-gated integration tests for the libkrun microVM monitor (#2971).

Mirrors the opt-in style of ``test_microvm_firecracker.py``. Two tiers:

- The refusal-invariant assertions run **everywhere**: on a host that cannot
  run a microVM, ``preflight()`` names what is missing and ``boot()`` raises,
  never degrading to a weaker isolation mode.
- The real boot/exec/file-IO/freeze round trip is **opt-in**. It boots actual
  virtual machines, so it is skipped unless
  ``BERNSTEIN_MICROVM_LIBKRUN_INTEGRATION=1`` *and* the monitor's own preflight
  reports nothing missing.

Provisioning the opt-in tier (see ``docs/architecture/sandbox.md`` for the full
write-up):

1. Install libkrun and libkrunfw. macOS/arm64:
   ``brew tap slp/krun && brew install libkrun``. Linux: the distribution
   packages, plus a readable+writable ``/dev/kvm``.
2. Build the launcher: ``bernstein sandbox microvm-launcher``. It compiles the
   packaged C source against the local libkrun and, on macOS, ad-hoc signs it
   with the hypervisor entitlements.
3. Point ``$BERNSTEIN_MICROVM_LIBKRUN_ROOTFS`` at a guest root filesystem
   *directory* providing ``/bin/sh`` and a ``mount`` that speaks virtiofs (an
   extracted Alpine ``minirootfs`` tarball is enough).

The tests do not provision anything themselves: ``preflight()`` is the contract,
and a test that silently fixed the host would hide exactly the failure this
backend exists to report.
"""

from __future__ import annotations

import os

import pytest

from bernstein.core.sandbox.backends._libkrun import LibkrunMonitor
from bernstein.core.sandbox.backends._vmmonitor import (
    FakeMonitor,
    MicroVMUnavailableError,
)

INTEGRATION_ENV = "BERNSTEIN_MICROVM_LIBKRUN_INTEGRATION"

_opt_in = pytest.mark.skipif(
    os.environ.get(INTEGRATION_ENV) != "1",
    reason=f"opt-in libkrun integration (boots real VMs); set {INTEGRATION_ENV}=1",
)


def _require_supported_host() -> LibkrunMonitor:
    monitor = LibkrunMonitor()
    missing = monitor.preflight()
    if missing:
        pytest.skip(f"host cannot run a libkrun guest: {'; '.join(missing)}")
    return monitor


# ---------------------------------------------------------------------------
# Runs everywhere: the no-silent-downgrade invariant
# ---------------------------------------------------------------------------


def test_preflight_reports_missing_preconditions() -> None:
    """preflight() is side-effect-free and enumerates what the host lacks."""
    missing = LibkrunMonitor().preflight()
    # On an unprovisioned host the list is non-empty; on a ready host it may be
    # empty. Either way the call must not raise and must not touch the host.
    assert isinstance(missing, list)
    assert all(isinstance(entry, str) for entry in missing)


@pytest.mark.asyncio
async def test_boot_refuses_on_unsupported_host() -> None:
    monitor = LibkrunMonitor()
    if not monitor.preflight():
        pytest.skip("host supports libkrun; unsupported-host refusal not exercisable")
    with pytest.raises(MicroVMUnavailableError):
        await monitor.boot(base_env={})


# ---------------------------------------------------------------------------
# Opt-in: a real guest
# ---------------------------------------------------------------------------


@_opt_in
@pytest.mark.asyncio
async def test_real_guest_roundtrip() -> None:
    """boot -> exec -> write/read -> ls against a real libkrun guest."""
    monitor = _require_supported_host()
    await monitor.boot(base_env={"BERNSTEIN_TEST": "1"})
    try:
        await monitor.write_file("input.txt", b"payload from host\n")

        # The guest really executes: it must see the host-written file through
        # the virtiofs share, and its streams must come back separated.
        result = await monitor.exec(["/bin/sh", "-c", "uname -s; cat input.txt; echo oops >&2"])
        assert result.exit_code == 0
        assert result.stdout == b"Linux\npayload from host\n"
        assert b"oops" in result.stderr

        # A guest write must be visible on the host side of the same share.
        written = await monitor.exec(["/bin/sh", "-c", "echo from-guest > output.txt"])
        assert written.exit_code == 0
        assert await monitor.read_file("output.txt") == b"from-guest\n"
        assert "output.txt" in await monitor.ls(monitor.workdir)
    finally:
        await monitor.shutdown()


@_opt_in
@pytest.mark.asyncio
async def test_real_guest_runs_a_separate_kernel() -> None:
    """The boundary is a real one: the guest is not this host's kernel."""
    monitor = _require_supported_host()
    await monitor.boot(base_env={})
    try:
        result = await monitor.exec(["/bin/sh", "-c", "uname -sr"])
        assert result.exit_code == 0
        assert result.stdout.startswith(b"Linux ")
    finally:
        await monitor.shutdown()


@_opt_in
@pytest.mark.asyncio
async def test_real_guest_exit_codes_are_not_confused_with_libkrun_failures() -> None:
    """A guest command returning 127 is a result, not a VM failure.

    This is the collision the adapter exists to disambiguate: 125/126/127 are
    libkrun's own failure codes *and* legitimate guest exit codes.
    """
    monitor = _require_supported_host()
    await monitor.boot(base_env={})
    try:
        for code in (0, 1, 42, 125, 126, 127):
            result = await monitor.exec(["/bin/sh", "-c", f"exit {code}"])
            assert result.exit_code == code, f"guest exit {code} was not reported faithfully"
    finally:
        await monitor.shutdown()


@_opt_in
@pytest.mark.asyncio
async def test_real_guest_receives_stdin_and_the_session_environment() -> None:
    monitor = _require_supported_host()
    await monitor.boot(base_env={"FROM_BASE": "base-value"})
    try:
        piped = await monitor.exec(["/bin/sh", "-c", "cat"], stdin=b"piped\n")
        assert piped.exit_code == 0
        assert piped.stdout == b"piped\n"

        env = await monitor.exec(
            ["/bin/sh", "-c", "echo $FROM_BASE $FROM_CALL"],
            env={"FROM_CALL": "call-value"},
        )
        assert env.stdout == b"base-value call-value\n"
    finally:
        await monitor.shutdown()


@_opt_in
@pytest.mark.asyncio
async def test_real_freeze_matches_fakemonitor_byte_for_byte() -> None:
    """The acceptance criterion: identical inputs, identical content address.

    The workspace is built *inside the guest* here, then frozen on the host;
    ``FakeMonitor`` builds the same tree locally. Identical bytes prove the
    canonical image is a pure function of the tree, independent of which
    monitor produced it.
    """
    monitor = _require_supported_host()
    await monitor.boot(base_env={})
    fake = FakeMonitor()
    await fake.boot(base_env={})
    try:
        script = "mkdir -p sub && printf alpha > top.txt && printf beta > sub/nested.txt"
        result = await monitor.exec(["/bin/sh", "-c", script])
        assert result.exit_code == 0

        await fake.write_file("top.txt", b"alpha")
        await fake.write_file("sub/nested.txt", b"beta")

        assert await monitor.freeze_image() == await fake.freeze_image()
    finally:
        await monitor.shutdown()
        await fake.shutdown()


@_opt_in
@pytest.mark.asyncio
async def test_real_restore_repopulates_a_guest_workspace() -> None:
    """A frozen image restores into a fresh session and the guest can read it."""
    monitor = _require_supported_host()
    await monitor.boot(base_env={})
    try:
        await monitor.write_file("state.txt", b"captured\n")
        image = await monitor.freeze_image()
    finally:
        await monitor.shutdown()

    restored = _require_supported_host()
    await restored.boot(base_env={})
    try:
        await restored.restore_image(image)
        result = await restored.exec(["/bin/sh", "-c", "cat state.txt"])
        assert result.exit_code == 0
        assert result.stdout == b"captured\n"
        assert await restored.freeze_image() == image
    finally:
        await restored.shutdown()


@_opt_in
@pytest.mark.asyncio
async def test_real_backend_roundtrip_through_the_seam(tmp_path) -> None:
    """The backend above the seam drives the real monitor unchanged."""
    from bernstein.core.persistence.cas_store import CASStore
    from bernstein.core.sandbox.backends.microvm import MicroVMSandboxBackend
    from bernstein.core.sandbox.manifest import WorkspaceManifest

    _require_supported_host()
    backend = MicroVMSandboxBackend(
        monitor_factory=lambda root: LibkrunMonitor(root=root),
        cas=CASStore(tmp_path / "cas"),
    )
    session = await backend.create(WorkspaceManifest(root="/workspace"))
    try:
        await session.write("state.txt", b"captured")
        digest = await session.snapshot()
        assert digest
    finally:
        await backend.destroy(session)

    resumed = await backend.resume(digest)
    try:
        assert await resumed.read("state.txt") == b"captured"
    finally:
        await backend.destroy(resumed)
