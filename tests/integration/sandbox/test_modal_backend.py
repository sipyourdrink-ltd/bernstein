"""Integration tests for the Modal sandbox backend (oai-002).

Gated by ``MODAL_TOKEN_ID``; without credentials the suite skips. We
do not start a live Modal sandbox in this ticket — that requires a
paid account. The test here is a smoke test that the SDK loads and
the backend advertises its capabilities correctly.
"""

from __future__ import annotations

import importlib.util
import os

import pytest

from bernstein.core.sandbox import SandboxCapability


def _modal_ready() -> bool:
    if importlib.util.find_spec("modal") is None:
        return False
    return bool(os.environ.get("MODAL_TOKEN_ID"))


pytestmark = pytest.mark.skipif(
    not _modal_ready(),
    reason="Modal SDK not installed or MODAL_TOKEN_ID not set",
)


def test_modal_backend_declares_gpu_and_snapshot() -> None:
    from bernstein.core.sandbox.backends.modal import ModalSandboxBackend

    backend = ModalSandboxBackend()
    assert SandboxCapability.GPU in backend.capabilities
    assert SandboxCapability.SNAPSHOT in backend.capabilities
    assert SandboxCapability.FILE_RW in backend.capabilities
    assert SandboxCapability.EXEC in backend.capabilities
