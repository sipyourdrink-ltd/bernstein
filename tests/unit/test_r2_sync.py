"""Tests for R2 workspace synchronization."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from bernstein.bridges.r2_sync import (
    R2Config,
    R2SyncError,
    R2WorkspaceSync,
    SyncManifest,
    SyncResult,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def r2_config() -> R2Config:
    """Create a default R2Config for tests."""
    return R2Config(account_id="test-account", api_token="test-token")


@pytest.fixture
def r2_sync(r2_config: R2Config) -> R2WorkspaceSync:
    """Create an R2WorkspaceSync instance for tests."""
    return R2WorkspaceSync(r2_config)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Create a temporary workspace with sample files."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hello')")
    (tmp_path / "src" / "util.py").write_text("def add(a, b): return a + b")
    (tmp_path / "README.md").write_text("# Test Project")
    (tmp_path / "config.yaml").write_text("key: value")
    return tmp_path


@pytest.fixture
def workspace_with_excludes(workspace: Path) -> Path:
    """Add files that should be excluded from sync."""
    # .git directory
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("[core]")
    (workspace / ".git" / "HEAD").write_text("ref: refs/heads/main")

    # __pycache__
    (workspace / "src" / "__pycache__").mkdir()
    (workspace / "src" / "__pycache__" / "main.cpython-312.pyc").write_bytes(b"\x00")

    # .pyc file at top level
    (workspace / "compiled.pyc").write_bytes(b"\x00")

    # node_modules
    (workspace / "node_modules").mkdir()
    (workspace / "node_modules" / "pkg").mkdir()
    (workspace / "node_modules" / "pkg" / "index.js").write_text("module.exports = {}")

    # .venv
    (workspace / ".venv").mkdir()
    (workspace / ".venv" / "bin").mkdir()
    (workspace / ".venv" / "bin" / "python").write_text("#!/usr/bin/env python")

    # .sdd/runtime
    (workspace / ".sdd").mkdir()
    (workspace / ".sdd" / "runtime").mkdir()
    (workspace / ".sdd" / "runtime" / "state.json").write_text("{}")
    (workspace / ".sdd" / "logs").mkdir()
    (workspace / ".sdd" / "logs" / "agent.log").write_text("log line")
    # .sdd/backlog should NOT be excluded
    (workspace / ".sdd" / "backlog").mkdir()
    (workspace / ".sdd" / "backlog" / "tasks.json").write_text("[]")

    return workspace


# ---------------------------------------------------------------------------
# R2Config tests
# ---------------------------------------------------------------------------


class TestR2Config:
    """Tests for R2Config dataclass."""

    def test_defaults(self) -> None:
        """R2Config provides sensible defaults."""
        config = R2Config(account_id="acct", api_token="tok")
        assert config.bucket_name == "bernstein-workspaces"
        assert config.max_file_size_mb == 50
        assert ".git" in config.exclude_patterns
        assert "__pycache__" in config.exclude_patterns

    def test_custom_values(self) -> None:
        """R2Config accepts custom values."""
        config = R2Config(
            account_id="custom-acct",
            api_token="custom-tok",
            bucket_name="my-bucket",
            max_file_size_mb=100,
            exclude_patterns=(".git", "*.log"),
        )
        assert config.bucket_name == "my-bucket"
        assert config.max_file_size_mb == 100
        assert config.exclude_patterns == (".git", "*.log")

    def test_frozen(self) -> None:
        """R2Config is immutable."""
        config = R2Config(account_id="acct", api_token="tok")
        with pytest.raises(AttributeError):
            config.bucket_name = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# SyncManifest tests
# ---------------------------------------------------------------------------


class TestSyncManifest:
    """Tests for SyncManifest serialization."""

    def test_to_dict(self) -> None:
        """Manifest serializes to dict correctly."""
        manifest = SyncManifest(
            workspace_id="ws-1",
            files={"a.py": "abc123", "b.py": "def456"},
            total_bytes=1024,
            file_count=2,
        )
        d = manifest.to_dict()
        assert d["workspace_id"] == "ws-1"
        assert d["files"] == {"a.py": "abc123", "b.py": "def456"}
        assert d["total_bytes"] == 1024
        assert d["file_count"] == 2

    def test_from_dict(self) -> None:
        """Manifest deserializes from dict correctly."""
        data = {
            "workspace_id": "ws-2",
            "files": {"x.py": "hash1"},
            "total_bytes": 512,
            "file_count": 1,
        }
        manifest = SyncManifest.from_dict(data)
        assert manifest.workspace_id == "ws-2"
        assert manifest.files == {"x.py": "hash1"}
        assert manifest.total_bytes == 512

    def test_roundtrip(self) -> None:
        """Manifest survives JSON roundtrip."""
        original = SyncManifest(
            workspace_id="ws-rt",
            files={"f.py": "aaa"},
            total_bytes=100,
            file_count=1,
        )
        restored = SyncManifest.from_dict(json.loads(json.dumps(original.to_dict())))
        assert restored == original


# ---------------------------------------------------------------------------
# SyncResult tests
# ---------------------------------------------------------------------------


class TestSyncResult:
    """Tests for SyncResult dataclass."""

    def test_defaults(self) -> None:
        """SyncResult has sensible defaults."""
        result = SyncResult(workspace_id="ws-1")
        assert result.files_uploaded == 0
        assert result.files_downloaded == 0
        assert result.bytes_transferred == 0
        assert result.files_changed == []

    def test_with_values(self) -> None:
        """SyncResult stores provided values."""
        result = SyncResult(
            workspace_id="ws-1",
            files_downloaded=3,
            bytes_transferred=4096,
            files_changed=["a.py", "b.py", "c.py"],
        )
        assert result.files_downloaded == 3
        assert len(result.files_changed) == 3


# ---------------------------------------------------------------------------
# _should_exclude tests
# ---------------------------------------------------------------------------


class TestShouldExclude:
    """Tests for workspace file exclusion logic."""

    def test_excludes_git(self, r2_sync: R2WorkspaceSync) -> None:
        """Files under .git are excluded."""
        assert r2_sync._should_exclude(Path(".git/config"))
        assert r2_sync._should_exclude(Path(".git/refs/heads/main"))

    def test_excludes_pycache(self, r2_sync: R2WorkspaceSync) -> None:
        """__pycache__ directories are excluded."""
        assert r2_sync._should_exclude(Path("src/__pycache__/mod.cpython-312.pyc"))

    def test_excludes_pyc_files(self, r2_sync: R2WorkspaceSync) -> None:
        """*.pyc files are excluded."""
        assert r2_sync._should_exclude(Path("compiled.pyc"))

    def test_excludes_node_modules(self, r2_sync: R2WorkspaceSync) -> None:
        """node_modules is excluded."""
        assert r2_sync._should_exclude(Path("node_modules/pkg/index.js"))

    def test_excludes_venv(self, r2_sync: R2WorkspaceSync) -> None:
        """.venv is excluded."""
        assert r2_sync._should_exclude(Path(".venv/bin/python"))

    def test_excludes_sdd_runtime(self, r2_sync: R2WorkspaceSync) -> None:
        """.sdd/runtime is excluded."""
        assert r2_sync._should_exclude(Path(".sdd/runtime/state.json"))

    def test_excludes_sdd_logs(self, r2_sync: R2WorkspaceSync) -> None:
        """.sdd/logs is excluded."""
        assert r2_sync._should_exclude(Path(".sdd/logs/agent.log"))

    def test_allows_normal_files(self, r2_sync: R2WorkspaceSync) -> None:
        """Normal source files are not excluded."""
        assert not r2_sync._should_exclude(Path("src/main.py"))
        assert not r2_sync._should_exclude(Path("README.md"))
        assert not r2_sync._should_exclude(Path("config.yaml"))

    def test_allows_sdd_backlog(self, r2_sync: R2WorkspaceSync) -> None:
        """.sdd/backlog is not excluded (only runtime and logs are)."""
        assert not r2_sync._should_exclude(Path(".sdd/backlog/tasks.json"))


# ---------------------------------------------------------------------------
# _hash_file tests
# ---------------------------------------------------------------------------


class TestHashFile:
    """Tests for SHA-256 file hashing."""

    def test_consistent_hash(self, tmp_path: Path) -> None:
        """Same content always produces same hash."""
        f = tmp_path / "test.txt"
        f.write_text("hello world")
        h1 = R2WorkspaceSync._hash_file(f)
        h2 = R2WorkspaceSync._hash_file(f)
        assert h1 == h2

    def test_correct_sha256(self, tmp_path: Path) -> None:
        """Hash matches hashlib.sha256 directly."""
        content = b"test content for hashing"
        f = tmp_path / "data.bin"
        f.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()
        assert R2WorkspaceSync._hash_file(f) == expected

    def test_different_content_different_hash(self, tmp_path: Path) -> None:
        """Different content produces different hashes."""
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("content a")
        f2.write_text("content b")
        assert R2WorkspaceSync._hash_file(f1) != R2WorkspaceSync._hash_file(f2)

    def test_empty_file(self, tmp_path: Path) -> None:
        """Empty file produces the known SHA-256 of empty bytes."""
        f = tmp_path / "empty"
        f.write_bytes(b"")
        expected = hashlib.sha256(b"").hexdigest()
        assert R2WorkspaceSync._hash_file(f) == expected


# ---------------------------------------------------------------------------
# _scan_workspace tests
# ---------------------------------------------------------------------------


class TestScanWorkspace:
    """Tests for workspace scanning."""

    def test_finds_all_files(self, r2_sync: R2WorkspaceSync, workspace: Path) -> None:
        """Scan finds all non-excluded files."""
        files = r2_sync._scan_workspace(workspace)
        assert "src/main.py" in files
        assert "src/util.py" in files
        assert "README.md" in files
        assert "config.yaml" in files

    def test_excludes_patterns(
        self,
        r2_sync: R2WorkspaceSync,
        workspace_with_excludes: Path,
    ) -> None:
        """Scan excludes configured patterns."""
        files = r2_sync._scan_workspace(workspace_with_excludes)
        # Should be present
        assert "src/main.py" in files
        assert ".sdd/backlog/tasks.json" in files
        # Should be excluded
        assert ".git/config" not in files
        assert "src/__pycache__/main.cpython-312.pyc" not in files
        assert "compiled.pyc" not in files
        assert "node_modules/pkg/index.js" not in files
        assert ".venv/bin/python" not in files
        assert ".sdd/runtime/state.json" not in files
        assert ".sdd/logs/agent.log" not in files

    def test_skips_oversized_files(self, tmp_path: Path) -> None:
        """Files exceeding max_file_size_mb are skipped."""
        config = R2Config(
            account_id="acct",
            api_token="tok",
            max_file_size_mb=0,  # 0 MB = skip everything
        )
        sync = R2WorkspaceSync(config)
        (tmp_path / "big.txt").write_text("x" * 100)
        files = sync._scan_workspace(tmp_path)
        assert len(files) == 0

    def test_returns_posix_paths(
        self,
        r2_sync: R2WorkspaceSync,
        workspace: Path,
    ) -> None:
        """All returned paths use POSIX separators."""
        files = r2_sync._scan_workspace(workspace)
        for path in files:
            assert "\\" not in path


# ---------------------------------------------------------------------------
# _build_zip tests
# ---------------------------------------------------------------------------


class TestBuildZip:
    """Tests for zip archive creation."""

    def test_creates_valid_zip(self, r2_sync: R2WorkspaceSync, workspace: Path) -> None:
        """Built zip is a valid zipfile."""
        zip_data = r2_sync._build_zip(workspace, ["src/main.py", "README.md"])
        assert zipfile.is_zipfile(io.BytesIO(zip_data))

    def test_contains_correct_files(
        self,
        r2_sync: R2WorkspaceSync,
        workspace: Path,
    ) -> None:
        """Zip contains exactly the requested files."""
        zip_data = r2_sync._build_zip(workspace, ["src/main.py", "config.yaml"])
        with zipfile.ZipFile(io.BytesIO(zip_data), "r") as zf:
            names = set(zf.namelist())
            assert names == {"src/main.py", "config.yaml"}

    def test_file_contents_preserved(
        self,
        r2_sync: R2WorkspaceSync,
        workspace: Path,
    ) -> None:
        """File contents in zip match originals."""
        zip_data = r2_sync._build_zip(workspace, ["src/main.py"])
        with zipfile.ZipFile(io.BytesIO(zip_data), "r") as zf:
            content = zf.read("src/main.py").decode()
            assert content == "print('hello')"

    def test_empty_file_list(self, r2_sync: R2WorkspaceSync, workspace: Path) -> None:
        """Empty file list produces a valid empty zip."""
        zip_data = r2_sync._build_zip(workspace, [])
        with zipfile.ZipFile(io.BytesIO(zip_data), "r") as zf:
            assert zf.namelist() == []

    def test_skips_missing_files(
        self,
        r2_sync: R2WorkspaceSync,
        workspace: Path,
    ) -> None:
        """Non-existent files are silently skipped."""
        zip_data = r2_sync._build_zip(workspace, ["nonexistent.py", "src/main.py"])
        with zipfile.ZipFile(io.BytesIO(zip_data), "r") as zf:
            assert "nonexistent.py" not in zf.namelist()
            assert "src/main.py" in zf.namelist()


# ---------------------------------------------------------------------------
# _r2_url tests
# ---------------------------------------------------------------------------


class TestR2Url:
    """Tests for R2 URL construction."""

    def test_with_key(self, r2_sync: R2WorkspaceSync) -> None:
        """URL includes account, bucket, and key."""
        url = r2_sync._r2_url("ws-1/manifest.json")
        assert url == ("https://test-account.r2.cloudflarestorage.com/bernstein-workspaces/ws-1/manifest.json")

    def test_without_key(self, r2_sync: R2WorkspaceSync) -> None:
        """Empty key gives bucket-level URL."""
        url = r2_sync._r2_url("")
        assert url == "https://test-account.r2.cloudflarestorage.com/bernstein-workspaces"

    def test_custom_bucket(self) -> None:
        """Custom bucket name is reflected in URL."""
        config = R2Config(
            account_id="acct",
            api_token="tok",
            bucket_name="custom-bucket",
        )
        sync = R2WorkspaceSync(config)
        url = sync._r2_url("key")
        assert "custom-bucket" in url


# ---------------------------------------------------------------------------
# Upload flow tests (mocked HTTP)
# ---------------------------------------------------------------------------


def _mock_response(status_code: int = 200, content: bytes = b"", text: str = "") -> httpx.Response:
    """Create a mock httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.content = content
    resp.text = text or content.decode("utf-8", errors="replace")
    return resp


class TestUpload:
    """Tests for the upload flow."""

    @pytest.mark.asyncio
    async def test_upload_creates_manifest(
        self,
        r2_sync: R2WorkspaceSync,
        workspace: Path,
    ) -> None:
        """Upload returns a manifest with all workspace files."""
        with patch.object(r2_sync, "_put_object", new_callable=AsyncMock) as mock_put:
            with patch.object(
                r2_sync,
                "_get_object",
                new_callable=AsyncMock,
                side_effect=R2SyncError("not found", status_code=404),
            ):
                manifest = await r2_sync.upload(workspace, "ws-test")

        assert manifest.workspace_id == "ws-test"
        assert manifest.file_count == 4
        assert "src/main.py" in manifest.files
        assert mock_put.call_count == 2  # zip + manifest

    @pytest.mark.asyncio
    async def test_upload_delta_skips_unchanged(
        self,
        r2_sync: R2WorkspaceSync,
        workspace: Path,
    ) -> None:
        """Upload skips files that match the existing manifest."""
        # Compute hashes for current files
        local_files = r2_sync._scan_workspace(workspace)
        existing_manifest = SyncManifest(
            workspace_id="ws-delta",
            files=local_files,  # All files already uploaded
            total_bytes=100,
            file_count=len(local_files),
        )

        put_calls: list[tuple[str, bytes]] = []

        async def _mock_put(key: str, data: bytes, content_type: str = "") -> None:
            put_calls.append((key, data))

        async def _mock_get(key: str) -> bytes:
            return json.dumps(existing_manifest.to_dict()).encode()

        with patch.object(r2_sync, "_put_object", side_effect=_mock_put):
            with patch.object(r2_sync, "_get_object", side_effect=_mock_get):
                await r2_sync.upload(workspace, "ws-delta")

        # Only manifest should be uploaded (no zip since nothing changed)
        assert len(put_calls) == 1
        assert "manifest.json" in put_calls[0][0]

    @pytest.mark.asyncio
    async def test_upload_r2_error(
        self,
        r2_sync: R2WorkspaceSync,
        workspace: Path,
    ) -> None:
        """Upload raises R2SyncError when R2 is unreachable."""
        with patch.object(
            r2_sync,
            "_get_object",
            new_callable=AsyncMock,
            side_effect=R2SyncError("not found"),
        ):
            with patch.object(
                r2_sync,
                "_put_object",
                new_callable=AsyncMock,
                side_effect=R2SyncError("connection refused"),
            ):
                with pytest.raises(R2SyncError, match="connection refused"):
                    await r2_sync.upload(workspace, "ws-err")


# ---------------------------------------------------------------------------
# Download flow tests (mocked HTTP)
# ---------------------------------------------------------------------------


class TestDownload:
    """Tests for the download flow."""

    @pytest.mark.asyncio
    async def test_download_changed_files(
        self,
        r2_sync: R2WorkspaceSync,
        workspace: Path,
    ) -> None:
        """Download extracts only changed files."""
        # Build a remote manifest with a modified file
        local_files = r2_sync._scan_workspace(workspace)
        remote_files = dict(local_files)
        remote_files["src/main.py"] = "different-hash"  # Simulate change

        remote_manifest = SyncManifest(
            workspace_id="ws-dl",
            files=remote_files,
            total_bytes=200,
            file_count=len(remote_files),
        )

        # Build a zip with the "changed" file
        new_content = b"print('updated')"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("src/main.py", new_content)
        zip_bytes = buf.getvalue()

        call_count = 0

        async def _mock_get(key: str) -> bytes:
            nonlocal call_count
            call_count += 1
            if "manifest.json" in key:
                return json.dumps(remote_manifest.to_dict()).encode()
            return zip_bytes

        with patch.object(r2_sync, "_get_object", side_effect=_mock_get):
            result = await r2_sync.download(workspace, "ws-dl")

        assert result.files_downloaded == 1
        assert "src/main.py" in result.files_changed
        assert (workspace / "src" / "main.py").read_bytes() == new_content

    @pytest.mark.asyncio
    async def test_download_no_changes(
        self,
        r2_sync: R2WorkspaceSync,
        workspace: Path,
    ) -> None:
        """Download with no changes transfers zero bytes."""
        local_files = r2_sync._scan_workspace(workspace)
        remote_manifest = SyncManifest(
            workspace_id="ws-nochange",
            files=local_files,
            total_bytes=100,
            file_count=len(local_files),
        )

        async def _mock_get(key: str) -> bytes:
            return json.dumps(remote_manifest.to_dict()).encode()

        with patch.object(r2_sync, "_get_object", side_effect=_mock_get):
            result = await r2_sync.download(workspace, "ws-nochange")

        assert result.files_downloaded == 0
        assert result.bytes_transferred == 0
        assert result.files_changed == []

    @pytest.mark.asyncio
    async def test_download_no_manifest(
        self,
        r2_sync: R2WorkspaceSync,
        workspace: Path,
    ) -> None:
        """Download raises error when manifest is missing."""
        with patch.object(
            r2_sync,
            "_get_object",
            new_callable=AsyncMock,
            side_effect=R2SyncError("not found", status_code=404),
        ):
            with pytest.raises(R2SyncError, match="No manifest found"):
                await r2_sync.download(workspace, "ws-missing")


# ---------------------------------------------------------------------------
# Cleanup tests
# ---------------------------------------------------------------------------


class TestCleanup:
    """Tests for workspace cleanup."""

    @pytest.mark.asyncio
    async def test_cleanup_deletes_all_objects(
        self,
        r2_sync: R2WorkspaceSync,
    ) -> None:
        """Cleanup deletes all objects with the workspace prefix."""
        deleted: list[str] = []

        async def _mock_list(prefix: str) -> list[str]:
            return [f"{prefix}manifest.json", f"{prefix}workspace.zip"]

        async def _mock_delete(key: str) -> None:
            deleted.append(key)

        with patch.object(r2_sync, "_list_objects", side_effect=_mock_list):
            with patch.object(r2_sync, "_delete_object", side_effect=_mock_delete):
                await r2_sync.cleanup("ws-clean")

        assert len(deleted) == 2
        assert "ws-clean/manifest.json" in deleted
        assert "ws-clean/workspace.zip" in deleted

    @pytest.mark.asyncio
    async def test_cleanup_empty_workspace(
        self,
        r2_sync: R2WorkspaceSync,
    ) -> None:
        """Cleanup of non-existent workspace is a no-op."""

        async def _mock_list(prefix: str) -> list[str]:
            return []

        with patch.object(r2_sync, "_list_objects", side_effect=_mock_list):
            await r2_sync.cleanup("ws-gone")


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Tests for error conditions."""

    def test_r2_sync_error_attributes(self) -> None:
        """R2SyncError stores workspace_id and status_code."""
        err = R2SyncError(
            "test error",
            workspace_id="ws-1",
            status_code=403,
        )
        assert str(err) == "test error"
        assert err.workspace_id == "ws-1"
        assert err.status_code == 403

    @pytest.mark.asyncio
    async def test_put_object_auth_failure(self, r2_sync: R2WorkspaceSync) -> None:
        """_put_object raises R2SyncError on 403."""
        mock_resp = _mock_response(status_code=403, text="Forbidden")

        async def _mock_put(*args: Any, **kwargs: Any) -> httpx.Response:
            return mock_resp

        with patch("bernstein.bridges.r2_sync.httpx.AsyncClient") as mock_client_cls:
            client_instance = AsyncMock()
            client_instance.put = _mock_put
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = client_instance

            with pytest.raises(R2SyncError, match="403"):
                await r2_sync._put_object("key", b"data")

    @pytest.mark.asyncio
    async def test_get_object_connection_error(self, r2_sync: R2WorkspaceSync) -> None:
        """_get_object raises R2SyncError on connection failure."""
        with patch("bernstein.bridges.r2_sync.httpx.AsyncClient") as mock_client_cls:
            client_instance = AsyncMock()
            client_instance.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = client_instance

            with pytest.raises(R2SyncError, match="Failed to download"):
                await r2_sync._get_object("key")


# ---------------------------------------------------------------------------
# Content-addressed dedup test
# ---------------------------------------------------------------------------


class TestContentAddressedDedup:
    """Tests for content-addressed deduplication."""

    @pytest.mark.asyncio
    async def test_unchanged_files_not_reuploaded(
        self,
        r2_sync: R2WorkspaceSync,
        workspace: Path,
    ) -> None:
        """Files with matching hashes in existing manifest are skipped."""
        local_files = r2_sync._scan_workspace(workspace)

        # Mark only one file as changed in existing manifest
        existing_files = dict(local_files)
        existing_files["src/main.py"] = "old-hash-that-differs"

        existing_manifest = SyncManifest(
            workspace_id="ws-dedup",
            files=existing_files,
            total_bytes=100,
            file_count=len(existing_files),
        )

        uploaded_zips: list[bytes] = []

        async def _mock_put(key: str, data: bytes, content_type: str = "") -> None:
            if "workspace.zip" in key:
                uploaded_zips.append(data)

        async def _mock_get(key: str) -> bytes:
            return json.dumps(existing_manifest.to_dict()).encode()

        with patch.object(r2_sync, "_put_object", side_effect=_mock_put):
            with patch.object(r2_sync, "_get_object", side_effect=_mock_get):
                await r2_sync.upload(workspace, "ws-dedup")

        # Only the changed file should be in the zip
        assert len(uploaded_zips) == 1
        with zipfile.ZipFile(io.BytesIO(uploaded_zips[0]), "r") as zf:
            names = zf.namelist()
            assert "src/main.py" in names
            # Other unchanged files should NOT be in the zip
            assert "README.md" not in names
            assert "config.yaml" not in names
