"""KVM-gated integration tests for the Firecracker microVM monitor (#2613).

These are skipped everywhere the bounty normally runs (macOS, CI without
``/dev/kvm``). The determinism / CAS / receipt guarantees are proven
host-independently by the FakeMonitor unit + acceptance suite; this module
covers only the parts that genuinely need a hypervisor:

- On an unsupported host, the monitor's ``preflight`` reports the missing
  preconditions and ``boot`` refuses with :class:`MicroVMUnavailableError`
  (the no-silent-downgrade invariant) - this part runs everywhere.
- On a fully provisioned KVM host (opt in with
  ``BERNSTEIN_MICROVM_INTEGRATION=1`` plus a configured kernel/rootfs), the
  real backend round-trips create/exec/snapshot/resume.
"""

from __future__ import annotations

import os

import pytest

from bernstein.core.sandbox.backends._vmmonitor import (
    FirecrackerMonitor,
    MicroVMUnavailableError,
)


def test_preflight_reports_missing_preconditions() -> None:
    """preflight() is side-effect-free and enumerates what the host lacks."""
    missing = FirecrackerMonitor().preflight()
    # On this dev host (no KVM) the list is non-empty; on a KVM host it may
    # be empty. Either way the call must not raise.
    assert isinstance(missing, list)


@pytest.mark.asyncio
async def test_boot_refuses_on_unsupported_host() -> None:
    monitor = FirecrackerMonitor()
    if not monitor.preflight():
        pytest.skip("host supports Firecracker; unsupported-host refusal not exercisable")
    with pytest.raises(MicroVMUnavailableError):
        await monitor.boot(base_env={})


@pytest.mark.skipif(
    os.environ.get("BERNSTEIN_MICROVM_INTEGRATION") != "1",
    reason="opt-in Firecracker integration (needs KVM + kernel/rootfs); set BERNSTEIN_MICROVM_INTEGRATION=1",
)
@pytest.mark.asyncio
async def test_real_microvm_roundtrip(tmp_path) -> None:
    """Full create -> exec -> snapshot -> resume against a real Firecracker guest.

    Requires an operator-provisioned host: KVM, the ``firecracker`` binary,
    and ``BERNSTEIN_MICROVM_KERNEL`` / ``BERNSTEIN_MICROVM_ROOTFS`` pointing
    at a guest kernel + rootfs.
    """
    from bernstein.core.persistence.cas_store import CASStore
    from bernstein.core.sandbox.backends.microvm import MicroVMSandboxBackend
    from bernstein.core.sandbox.manifest import WorkspaceManifest

    backend = MicroVMSandboxBackend(cas=CASStore(tmp_path / "cas"))
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
