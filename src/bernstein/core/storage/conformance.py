"""Protocol conformance helpers for :class:`ArtifactSink` implementations.

A single reusable test base lives here so backend-specific test
modules only have to supply a fixture that yields a sink instance.
This mirrors
:mod:`bernstein.core.sandbox.conformance` so plugin authors familiar
with sandbox backends find the storage surface predictable.

Subclass it like::

    class TestLocalFsConformance(ArtifactSinkConformance):
        @pytest.fixture
        async def sink(self, tmp_path):
            yield LocalFsSink(tmp_path)

Run-time notes:

- Tests use ``@pytest.mark.asyncio`` so they slot into the existing
  pytest-asyncio setup.
- ``concurrent`` tests spawn ``asyncio.gather``-style bursts; cloud
  sinks running against local emulators should still complete inside
  a few seconds.
- ``large_payload`` is 1 MB by default — large enough to exercise
  chunked upload paths on cloud sinks but small enough to keep CI
  fast. Sinks needing more aggressive coverage override
  :attr:`ArtifactSinkConformance.large_payload_bytes`.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from bernstein.core.storage.sink import ArtifactSink


class ArtifactSinkConformance:
    """Reusable conformance suite for any :class:`ArtifactSink`.

    Subclasses supply an ``sink`` pytest fixture yielding a ready sink.
    """

    #: Override in a subclass to change the "large payload" size.
    large_payload_bytes: int = 1024 * 1024

    @pytest.mark.asyncio
    async def test_read_write_roundtrip_binary(
        self,
        sink: ArtifactSink,
    ) -> None:
        """Roundtrip arbitrary binary bytes."""
        payload = bytes(range(256)) * 8
        await sink.write("conformance/binary.bin", payload)
        got = await sink.read("conformance/binary.bin")
        assert got == payload

    @pytest.mark.asyncio
    async def test_read_write_roundtrip_utf8(
        self,
        sink: ArtifactSink,
    ) -> None:
        """Roundtrip UTF-8 text encoded as bytes."""
        payload = "hello world — conformance ✓".encode()
        await sink.write("conformance/utf8.txt", payload)
        got = await sink.read("conformance/utf8.txt")
        assert got == payload

    @pytest.mark.asyncio
    async def test_empty_payload_roundtrip(
        self,
        sink: ArtifactSink,
    ) -> None:
        """Empty writes are legal and readable."""
        await sink.write("conformance/empty.txt", b"")
        got = await sink.read("conformance/empty.txt")
        assert got == b""

    @pytest.mark.asyncio
    async def test_large_payload_roundtrip(
        self,
        sink: ArtifactSink,
    ) -> None:
        """Large payloads survive intact."""
        payload = b"x" * self.large_payload_bytes
        await sink.write("conformance/large.bin", payload)
        got = await sink.read("conformance/large.bin")
        assert got == payload

    @pytest.mark.asyncio
    async def test_list_with_prefix(
        self,
        sink: ArtifactSink,
    ) -> None:
        """``list`` returns keys with the matching prefix."""
        await sink.write("conformance/list/a.txt", b"a")
        await sink.write("conformance/list/b.txt", b"b")
        await sink.write("conformance/other.txt", b"c")
        keys = await sink.list("conformance/list")
        assert "conformance/list/a.txt" in keys
        assert "conformance/list/b.txt" in keys
        assert "conformance/other.txt" not in keys

    @pytest.mark.asyncio
    async def test_list_many_keys_paginates(
        self,
        sink: ArtifactSink,
    ) -> None:
        """Listing more than one page returns every key.

        S3 default page size is 1000 — we use 50 writes here because
        the conformance suite also runs on emulators where larger
        payloads are slower. Pagination support is still exercised on
        sinks that set their page size to 10 or below.
        """
        for i in range(50):
            await sink.write(f"conformance/many/{i:03d}.txt", str(i).encode())
        keys = await sink.list("conformance/many")
        assert len(keys) >= 50

    @pytest.mark.asyncio
    async def test_exists_reports_correctly(
        self,
        sink: ArtifactSink,
    ) -> None:
        """``exists`` returns True/False."""
        assert await sink.exists("conformance/missing.txt") is False
        await sink.write("conformance/present.txt", b"hi")
        assert await sink.exists("conformance/present.txt") is True

    @pytest.mark.asyncio
    async def test_delete_removes_key(
        self,
        sink: ArtifactSink,
    ) -> None:
        """``delete`` removes the key and subsequent read raises."""
        await sink.write("conformance/delete.txt", b"bye")
        await sink.delete("conformance/delete.txt")
        with pytest.raises(FileNotFoundError):
            await sink.read("conformance/delete.txt")

    @pytest.mark.asyncio
    async def test_read_missing_raises_file_not_found(
        self,
        sink: ArtifactSink,
    ) -> None:
        """Reading a missing key raises :class:`FileNotFoundError`."""
        with pytest.raises(FileNotFoundError):
            await sink.read("conformance/does-not-exist.txt")

    @pytest.mark.asyncio
    async def test_stat_returns_size_and_mtime(
        self,
        sink: ArtifactSink,
    ) -> None:
        """``stat`` reports size and mtime after a write."""
        payload = b"abc123"
        await sink.write("conformance/stat.txt", payload)
        st = await sink.stat("conformance/stat.txt")
        assert st.size_bytes == len(payload)
        assert st.last_modified_unix >= 0

    @pytest.mark.asyncio
    async def test_stat_missing_raises_file_not_found(
        self,
        sink: ArtifactSink,
    ) -> None:
        """``stat`` on a missing key raises :class:`FileNotFoundError`."""
        with pytest.raises(FileNotFoundError):
            await sink.stat("conformance/missing-stat.txt")

    @pytest.mark.asyncio
    async def test_concurrent_writes_do_not_interfere(
        self,
        sink: ArtifactSink,
    ) -> None:
        """Concurrent writes to different keys land correctly."""

        async def _write(i: int) -> None:
            await sink.write(
                f"conformance/concurrent/{i:02d}.txt",
                str(i).encode(),
            )

        await asyncio.gather(*(_write(i) for i in range(20)))
        for i in range(20):
            got = await sink.read(f"conformance/concurrent/{i:02d}.txt")
            assert got == str(i).encode()

    @pytest.mark.asyncio
    async def test_name_is_nonempty(
        self,
        sink: ArtifactSink,
    ) -> None:
        """Every sink must advertise a non-empty canonical name."""
        assert isinstance(sink.name, str) and sink.name


__all__ = ["ArtifactSinkConformance"]
