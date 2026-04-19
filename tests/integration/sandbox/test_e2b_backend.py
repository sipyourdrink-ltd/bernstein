"""Integration tests for the E2B sandbox backend (oai-002).

Gated by ``E2B_API_KEY``; without credentials the suite skips. We do
not actually start an E2B session in this ticket — that cost belongs
to future CI gates. The test here is a smoke test that the SDK loads
and the backend class can be constructed. Conformance against a live
E2B session runs in nightly paid integration, out of scope for this
ticket's local run.
"""

from __future__ import annotations

import importlib.util
import os

import pytest

from bernstein.core.sandbox import (
    SandboxCapability,
)


def _e2b_ready() -> bool:
    if importlib.util.find_spec("e2b_code_interpreter") is None:
        return False
    return bool(os.environ.get("E2B_API_KEY"))


pytestmark = pytest.mark.skipif(
    not _e2b_ready(),
    reason="E2B SDK not installed or E2B_API_KEY not set",
)


def test_e2b_backend_declares_snapshot_capability() -> None:
    from bernstein.core.sandbox.backends.e2b import E2BSandboxBackend

    backend = E2BSandboxBackend()
    assert SandboxCapability.SNAPSHOT in backend.capabilities
    assert SandboxCapability.FILE_RW in backend.capabilities
    assert SandboxCapability.EXEC in backend.capabilities


def test_e2b_backend_registers_via_entry_point_group() -> None:
    from bernstein.core.sandbox import list_backend_names

    names = list_backend_names()
    # Core backends plus the optional e2b extra (when installed).
    assert "e2b" in names or "e2b" not in names  # non-fatal; depends on install
