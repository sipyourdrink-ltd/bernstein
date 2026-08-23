"""Tests for bernstein.core.persistence.cas_store - content-addressable storage."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import sys
import threading
from typing import TYPE_CHECKING

import pytest

from bernstein.core.persistence.cas_store import (
    CASEntry,
    CASIntegrityError,
    CASStats,
    CASStore,
    put_file,
    put_text,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def cas(tmp_path: Path) -> CASStore:
    """Return a CASStore rooted in a temporary directory."""
    return CASStore(tmp_path / "cas")


# ---------------------------------------------------------------------------
# CASEntry dataclass
# ---------------------------------------------------------------------------


class TestCASEntry:
    def test_frozen(self) -> None:
        entry = CASEntry(digest="abc", size_bytes=10, created_at=0.0, content_type="text/plain")
        with pytest.raises(AttributeError):
            entry.digest = "xyz"  # type: ignore[misc]

    def test_default_metadata_empty(self) -> None:
        entry = CASEntry(digest="abc", size_bytes=0, created_at=0.0, content_type="text/plain")
        assert entry.metadata == {}

    def test_metadata_preserved(self) -> None:
        meta = {"author": "agent-1", "task_id": "t-42"}
        entry = CASEntry(
            digest="abc",
            size_bytes=0,
            created_at=0.0,
            content_type="text/plain",
            metadata=meta,
        )
        assert entry.metadata == meta


# ---------------------------------------------------------------------------
# CASStats dataclass
# ---------------------------------------------------------------------------


class TestCASStats:
    def test_frozen(self) -> None:
        stats = CASStats(total_entries=1, total_bytes=100, dedup_saves=0)
        with pytest.raises(AttributeError):
            stats.total_entries = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# CASStore - put / get / has
# ---------------------------------------------------------------------------


class TestCASStorePutGet:
    def test_put_returns_sha256_hex(self, cas: CASStore) -> None:
        digest = cas.put(b"hello")
        expected = hashlib.sha256(b"hello").hexdigest()
        assert digest == expected

    def test_get_returns_content(self, cas: CASStore) -> None:
        digest = cas.put(b"world")
        assert cas.get(digest) == b"world"

    def test_get_missing_returns_none(self, cas: CASStore) -> None:
        assert cas.get("0" * 64) is None

    def test_has_true_after_put(self, cas: CASStore) -> None:
        digest = cas.put(b"data")
        assert cas.has(digest) is True

    def test_has_false_for_unknown(self, cas: CASStore) -> None:
        assert cas.has("f" * 64) is False

    def test_put_empty_bytes(self, cas: CASStore) -> None:
        digest = cas.put(b"")
        assert cas.get(digest) == b""

    def test_put_large_content(self, cas: CASStore) -> None:
        data = b"x" * 1_000_000
        digest = cas.put(data)
        assert cas.get(digest) == data


# ---------------------------------------------------------------------------
# CASStore - read-path integrity verification
# ---------------------------------------------------------------------------


class TestCASStoreIntegrity:
    def _corrupt_blob(self, cas: CASStore, digest: str) -> None:
        """Flip one byte of the on-disk blob for *digest* in place."""
        blob = cas.root / digest[:2] / digest
        data = bytearray(blob.read_bytes())
        data[0] ^= 0xFF
        blob.write_bytes(bytes(data))

    def test_get_raises_on_corrupted_blob(self, cas: CASStore) -> None:
        digest = cas.put(b"authentic content")
        self._corrupt_blob(cas, digest)
        with pytest.raises(CASIntegrityError) as exc_info:
            cas.get(digest)
        # The error must name the requested key so a caller can act on it.
        assert digest in str(exc_info.value)

    def test_get_corruption_error_carries_expected_and_actual(self, cas: CASStore) -> None:
        digest = cas.put(b"authentic content")
        self._corrupt_blob(cas, digest)
        corrupted = (cas.root / digest[:2] / digest).read_bytes()
        actual = hashlib.sha256(corrupted).hexdigest()
        with pytest.raises(CASIntegrityError) as exc_info:
            cas.get(digest)
        assert exc_info.value.expected == digest
        assert exc_info.value.actual == actual

    def test_get_does_not_mask_corruption_as_miss(self, cas: CASStore) -> None:
        # A corrupted blob must surface as an integrity error, never as None
        # (which would let tampering look like a cache miss).
        digest = cas.put(b"authentic content")
        self._corrupt_blob(cas, digest)
        with pytest.raises(CASIntegrityError):
            cas.get(digest)

    def test_intact_round_trip_passes_verification(self, cas: CASStore) -> None:
        digest = cas.put(b"authentic content")
        assert cas.get(digest) == b"authentic content"

    def test_verify_off_returns_corrupted_bytes(self, cas: CASStore) -> None:
        # The opt-out is explicit and bypasses the rehash for callers that
        # have already verified upstream or read enormous blobs in tight loops.
        digest = cas.put(b"authentic content")
        self._corrupt_blob(cas, digest)
        corrupted = (cas.root / digest[:2] / digest).read_bytes()
        assert cas.get(digest, verify=False) == corrupted

    def test_missing_blob_still_returns_none_with_verify_on(self, cas: CASStore) -> None:
        assert cas.get("0" * 64, verify=True) is None


# ---------------------------------------------------------------------------
# CASStore - deduplication
# ---------------------------------------------------------------------------


class TestCASStoreDedup:
    def test_duplicate_put_returns_same_digest(self, cas: CASStore) -> None:
        d1 = cas.put(b"same")
        d2 = cas.put(b"same")
        assert d1 == d2

    def test_dedup_increments_counter(self, cas: CASStore) -> None:
        cas.put(b"dup")
        cas.put(b"dup")
        cas.put(b"dup")
        assert cas.stats().dedup_saves == 2

    def test_different_content_different_digest(self, cas: CASStore) -> None:
        d1 = cas.put(b"alpha")
        d2 = cas.put(b"beta")
        assert d1 != d2


# ---------------------------------------------------------------------------
# CASStore - sharding layout
# ---------------------------------------------------------------------------


class TestCASStoreSharding:
    def test_blob_stored_in_shard_directory(self, cas: CASStore) -> None:
        digest = cas.put(b"shard-test")
        shard = cas.root / digest[:2]
        assert shard.is_dir()
        assert (shard / digest).exists()

    def test_meta_sidecar_exists(self, cas: CASStore) -> None:
        digest = cas.put(b"meta-test", content_type="text/plain")
        meta_path = cas.root / digest[:2] / f"{digest}.meta.json"
        assert meta_path.exists()
        data = json.loads(meta_path.read_text())
        assert data["digest"] == digest
        assert data["content_type"] == "text/plain"


# ---------------------------------------------------------------------------
# CASStore - get_entry
# ---------------------------------------------------------------------------


class TestCASStoreGetEntry:
    def test_get_entry_returns_cas_entry(self, cas: CASStore) -> None:
        digest = cas.put(b"entry-test", content_type="application/json", metadata={"k": "v"})
        entry = cas.get_entry(digest)
        assert entry is not None
        assert entry.digest == digest
        assert entry.size_bytes == len(b"entry-test")
        assert entry.content_type == "application/json"
        assert entry.metadata == {"k": "v"}

    def test_get_entry_missing_returns_none(self, cas: CASStore) -> None:
        assert cas.get_entry("0" * 64) is None


# ---------------------------------------------------------------------------
# CASStore - delete
# ---------------------------------------------------------------------------


class TestCASStoreDelete:
    def test_delete_removes_blob_and_meta(self, cas: CASStore) -> None:
        digest = cas.put(b"delete-me")
        assert cas.delete(digest) is True
        assert cas.has(digest) is False
        assert cas.get(digest) is None

    def test_delete_missing_returns_false(self, cas: CASStore) -> None:
        assert cas.delete("0" * 64) is False

    def test_delete_cleans_empty_shard(self, cas: CASStore) -> None:
        digest = cas.put(b"only-blob")
        shard = cas.root / digest[:2]
        cas.delete(digest)
        # Shard directory should be removed when empty.
        assert not shard.exists()

    def test_delete_preserves_shard_with_siblings(self, cas: CASStore) -> None:
        # Put two blobs that share the same shard prefix.
        d1 = cas.put(b"blob-a")
        prefix = d1[:2]
        # Create a dummy sibling in the same shard.
        shard = cas.root / prefix
        dummy = shard / "dummy.txt"
        dummy.write_text("keep me")
        cas.delete(d1)
        assert shard.exists()  # Shard kept because it's not empty.


# ---------------------------------------------------------------------------
# CASStore - list_entries
# ---------------------------------------------------------------------------


class TestCASStoreListEntries:
    def test_empty_store(self, cas: CASStore) -> None:
        assert cas.list_entries() == []

    def test_lists_all_entries(self, cas: CASStore) -> None:
        cas.put(b"one")
        cas.put(b"two")
        cas.put(b"three")
        entries = cas.list_entries()
        assert len(entries) == 3

    def test_sorted_by_created_at(self, cas: CASStore) -> None:
        cas.put(b"first")
        cas.put(b"second")
        entries = cas.list_entries()
        assert entries[0].created_at <= entries[1].created_at


# ---------------------------------------------------------------------------
# CASStore - stats
# ---------------------------------------------------------------------------


class TestCASStoreStats:
    def test_empty_store_stats(self, cas: CASStore) -> None:
        s = cas.stats()
        assert s.total_entries == 0
        assert s.total_bytes == 0
        assert s.dedup_saves == 0

    def test_stats_after_puts(self, cas: CASStore) -> None:
        cas.put(b"aaa")
        cas.put(b"bbbbb")
        s = cas.stats()
        assert s.total_entries == 2
        assert s.total_bytes == 8  # 3 + 5

    def test_stats_includes_dedup(self, cas: CASStore) -> None:
        cas.put(b"same")
        cas.put(b"same")
        s = cas.stats()
        assert s.total_entries == 1
        assert s.dedup_saves == 1


# ---------------------------------------------------------------------------
# put_file helper
# ---------------------------------------------------------------------------


class TestPutFile:
    def test_stores_file_content(self, cas: CASStore, tmp_path: Path) -> None:
        f = tmp_path / "source.py"
        f.write_text("print('hello')\n")
        digest = put_file(cas, f)
        stored = cas.get(digest)
        assert stored == f.read_bytes()

    def test_records_source_file_in_metadata(self, cas: CASStore, tmp_path: Path) -> None:
        f = tmp_path / "data.json"
        f.write_text("{}")
        digest = put_file(cas, f)
        entry = cas.get_entry(digest)
        assert entry is not None
        assert entry.metadata["source_file"] == str(f)

    def test_infers_content_type_from_suffix(self, cas: CASStore, tmp_path: Path) -> None:
        f = tmp_path / "module.py"
        f.write_text("x = 1")
        digest = put_file(cas, f)
        entry = cas.get_entry(digest)
        assert entry is not None
        assert entry.content_type == "text/x-python"

    def test_unknown_suffix_uses_octet_stream(self, cas: CASStore, tmp_path: Path) -> None:
        f = tmp_path / "data.xyz"
        f.write_bytes(b"\x00\x01")
        digest = put_file(cas, f)
        entry = cas.get_entry(digest)
        assert entry is not None
        assert entry.content_type == "application/octet-stream"

    def test_missing_file_raises(self, cas: CASStore, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            put_file(cas, tmp_path / "no-such-file.txt")

    def test_custom_metadata_merged(self, cas: CASStore, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("text")
        digest = put_file(cas, f, metadata={"agent": "qa"})
        entry = cas.get_entry(digest)
        assert entry is not None
        assert entry.metadata["agent"] == "qa"
        assert "source_file" in entry.metadata


# ---------------------------------------------------------------------------
# put_text helper
# ---------------------------------------------------------------------------


class TestPutText:
    def test_stores_utf8_text(self, cas: CASStore) -> None:
        digest = put_text(cas, "hello world")
        assert cas.get(digest) == b"hello world"

    def test_content_type_is_text_plain(self, cas: CASStore) -> None:
        digest = put_text(cas, "txt")
        entry = cas.get_entry(digest)
        assert entry is not None
        assert entry.content_type == "text/plain"

    def test_unicode_text(self, cas: CASStore) -> None:
        text = "Bernstein \u266b orchestrates agents"
        digest = put_text(cas, text)
        assert cas.get(digest) == text.encode("utf-8")

    def test_metadata_passed_through(self, cas: CASStore) -> None:
        digest = put_text(cas, "annotated", metadata={"tag": "review"})
        entry = cas.get_entry(digest)
        assert entry is not None
        assert entry.metadata == {"tag": "review"}


# ---------------------------------------------------------------------------
# CASStore - constructor
# ---------------------------------------------------------------------------


class TestCASStoreInit:
    def test_creates_root_directory(self, tmp_path: Path) -> None:
        root = tmp_path / "new" / "cas"
        CASStore(root)
        assert root.is_dir()

    def test_root_property(self, tmp_path: Path) -> None:
        root = tmp_path / "cas"
        store = CASStore(root)
        assert store.root == root


# ---------------------------------------------------------------------------
# CASStore.get - symlink safety (O_NOFOLLOW, race-free)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX symlink semantics")
class TestCASStoreSymlinkSafety:
    def test_get_entry_refuses_to_follow_a_symlinked_sidecar(self, cas: CASStore, tmp_path: Path) -> None:
        """Metadata lookup must not follow a sidecar replaced by a link."""
        digest = cas.put(b"authentic blob")
        sidecar = cas.root / digest[:2] / f"{digest}.meta.json"
        target = tmp_path / "attacker-meta.json"
        target.write_text(sidecar.read_text())
        sidecar.unlink()
        sidecar.symlink_to(target)

        with pytest.raises(OSError) as excinfo:
            cas.get_entry(digest)
        assert not isinstance(excinfo.value, FileNotFoundError)

    def test_get_entry_refuses_to_follow_a_symlinked_shard_directory(
        self,
        cas: CASStore,
        tmp_path: Path,
    ) -> None:
        """Metadata lookup must anchor every component below the CAS root."""
        digest = cas.put(b"authentic blob")
        shard = cas.root / digest[:2]
        sidecar = shard / f"{digest}.meta.json"
        decoy = tmp_path / "attacker-shard"
        decoy.mkdir()
        (decoy / sidecar.name).write_text(sidecar.read_text())
        shutil.rmtree(shard)
        shard.symlink_to(decoy, target_is_directory=True)

        with pytest.raises(OSError) as excinfo:
            cas.get_entry(digest)
        assert not isinstance(excinfo.value, FileNotFoundError)
        assert excinfo.value.errno in {errno.ELOOP, errno.ENOTDIR}

    def test_get_refuses_to_follow_a_symlinked_blob(self, cas: CASStore, tmp_path: Path) -> None:
        """A blob path replaced by a symlink must be rejected by the read itself
        (O_NOFOLLOW), atomically - never followed to its target. This is the
        race-free replacement for an is_symlink() pre-check, which would leave a
        TOCTOU window (swap in the symlink between check and read)."""
        digest = cas.put(b"authentic blob")
        blob = cas.blob_path(digest)
        # Repoint the blob at an attacker-controlled file with different content.
        target = tmp_path / "attacker-target"
        target.write_bytes(b"attacker bytes")
        blob.unlink()
        blob.symlink_to(target)
        # OSError (ELOOP) from O_NOFOLLOW refusing the symlink.
        with pytest.raises(OSError):
            cas.get(digest, verify=True)

    def test_get_returns_none_for_a_genuinely_absent_blob(self, cas: CASStore) -> None:
        """A blob that was never stored still reads as None (absent), so the
        O_NOFOLLOW change does not turn a normal miss into an error."""
        missing = hashlib.sha256(b"never stored").hexdigest()
        assert cas.get(missing) is None

    def test_get_refuses_to_follow_a_symlinked_shard_directory(
        self,
        cas: CASStore,
        tmp_path: Path,
    ) -> None:
        """O_NOFOLLOW on the blob open guards the final component only, so the
        shard directory above it was still followed (#3561). The anchored walk
        refuses a link at that component too, and the refusal must arrive as an
        OSError - never as bytes, and never as a miss."""
        digest = cas.put(b"authentic blob")
        shard = cas.root / digest[:2]
        # A shard directory the attacker controls, holding a blob at the same
        # name whose bytes are not the ones the digest promises.
        decoy = tmp_path / "attacker-shard"
        decoy.mkdir()
        (decoy / digest).write_bytes(b"attacker bytes")
        shutil.rmtree(shard)
        shard.symlink_to(decoy, target_is_directory=True)

        # The errno is platform-dependent (ELOOP on Linux, ENOTDIR on macOS and
        # the BSDs, which check directory-ness before reporting the refusal);
        # both mean the link was not traversed. What has to hold everywhere is
        # that no bytes come back and the failure is not FileNotFoundError.
        with pytest.raises(OSError) as excinfo:
            cas.get(digest, verify=True)
        assert not isinstance(excinfo.value, FileNotFoundError)
        assert excinfo.value.errno in {errno.ELOOP, errno.ENOTDIR}

    def test_symlinked_shard_is_not_reported_as_absent(
        self,
        cas: CASStore,
        tmp_path: Path,
    ) -> None:
        """The refusal must not be widened into a cache miss. `receipt verify`
        splits absent (an operational event) from unreadable (a property of this
        host); returning None here would file a refused symlink under the wrong
        one and clear the blob of suspicion it has not earned."""
        digest = cas.put(b"authentic blob")
        shard = cas.root / digest[:2]
        empty = tmp_path / "empty-shard"
        empty.mkdir()
        shutil.rmtree(shard)
        shard.symlink_to(empty, target_is_directory=True)

        # The link resolves to a directory with no blob in it, so following it
        # would surface ENOENT and read as absent. The refusal must land on the
        # shard component, before the blob name is ever looked up - which is
        # what makes this test distinct from the one above: here the wrong
        # answer is available and quiet.
        with pytest.raises(OSError) as excinfo:
            cas.get(digest, verify=True)
        assert not isinstance(excinfo.value, FileNotFoundError)
        assert excinfo.value.errno in {errno.ELOOP, errno.ENOTDIR}

    def test_get_returns_none_when_the_shard_directory_is_missing(
        self,
        cas: CASStore,
    ) -> None:
        """A shard directory that was never created is an ordinary miss, not a
        reader-side failure - the walk adds a component that can raise ENOENT,
        and that has to keep reading as absent."""
        digest = cas.put(b"blob in its own shard")
        shutil.rmtree(cas.root / digest[:2])
        assert cas.get(digest) is None

    def test_get_refuses_a_fifo_at_the_blob_path_without_stalling(self, cas: CASStore) -> None:
        """The reader stall #3561 is about does not need a symlink: a FIFO at
        the blob's own name blocks a plain open until a writer appears, and
        O_NOFOLLOW never had anything to say about it. The read must refuse the
        type instead of waiting - so this asserts on a worker thread and fails
        on the timeout, because the regression here is a hang, not an error."""
        digest = cas.put(b"authentic blob")
        blob = cas.blob_path(digest)
        blob.unlink()
        os.mkfifo(blob)

        finished = threading.Event()
        result: list[BaseException | None] = []

        def attempt() -> None:
            try:
                cas.get(digest, verify=True)
                result.append(None)
            except BaseException as exc:
                result.append(exc)
            finished.set()

        threading.Thread(target=attempt, daemon=True).start()
        assert finished.wait(timeout=10), "CASStore.get stalled on a FIFO blob"
        # OSError, never None: a refused type is a reader-side failure the
        # verifier reports as unreadable, not a blob that is simply absent.
        assert isinstance(result[0], OSError)
        assert not isinstance(result[0], FileNotFoundError)

    def test_a_symlinked_store_root_is_still_readable(self, tmp_path: Path) -> None:
        """Pointing the store root at another volume is operator configuration,
        not an attack: the walk anchors on the root and only refuses links
        *below* it. Refusing a symlinked root would break working installs to
        defend against someone who already controls where the operator put it."""
        real_root = tmp_path / "real-cas"
        real_root.mkdir()
        linked_root = tmp_path / "linked-cas"
        linked_root.symlink_to(real_root, target_is_directory=True)

        store = CASStore(linked_root)
        digest = store.put(b"blob under a symlinked root")
        assert store.get(digest) == b"blob under a symlinked root"
