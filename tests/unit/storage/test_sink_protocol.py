"""Unit tests for :class:`ArtifactSink` + conformance suite base (oai-003).

The :class:`ArtifactSinkConformance` suite is parametrized: every
first-party sink gets its fixture here. Cloud sinks (S3/GCS/Azure/R2)
run the same suite in ``tests/integration/storage/`` behind gating
skipifs - they are excluded from the unit suite because they need
emulators or real credentials.
"""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from bernstein.core.storage import (
    ArtifactSink,
    ArtifactSinkConformance,
    LocalFsSink,
)
from bernstein.core.storage.sink import join_keys, normalise_key

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


def test_storage_imports_without_pytest() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.modules['pytest'] = None; from bernstein.core.storage import LocalFsSink",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


class TestLocalFsSinkConformance(ArtifactSinkConformance):
    """Runs the shared conformance suite against LocalFsSink."""

    # Override the default 1 MB payload with 128 KB so the
    # unit suite stays fast - large_payload_roundtrip still exercises
    # the chunked-write path via atomic_write.
    large_payload_bytes = 128 * 1024

    @pytest_asyncio.fixture
    async def sink(self, tmp_path: Path) -> AsyncIterator[ArtifactSink]:
        """Yield a LocalFsSink rooted at a fresh tmp_path."""
        s = LocalFsSink(tmp_path)
        try:
            yield s
        finally:
            await s.close()


# ---------------------------------------------------------------------------
# Key helpers
# ---------------------------------------------------------------------------


def test_normalise_key_strips_leading_slash() -> None:
    assert normalise_key("/runtime/wal/r1.jsonl") == "runtime/wal/r1.jsonl"


def test_normalise_key_rejects_empty() -> None:
    with pytest.raises(ValueError):
        normalise_key("")


def test_normalise_key_rejects_dotdot() -> None:
    with pytest.raises(ValueError):
        normalise_key("runtime/../etc/passwd")


def test_normalise_key_rejects_only_slashes() -> None:
    with pytest.raises(ValueError):
        normalise_key("///")


def test_join_keys_basic() -> None:
    assert join_keys("runtime", "wal", "r1.jsonl") == "runtime/wal/r1.jsonl"


def test_join_keys_skips_empty_parts() -> None:
    assert join_keys("runtime", "", "state.json") == "runtime/state.json"


def test_join_keys_strips_embedded_slashes() -> None:
    assert join_keys("/runtime/", "/wal/") == "runtime/wal"


def test_join_keys_rejects_all_empty() -> None:
    with pytest.raises(ValueError):
        join_keys("", "/", "")
