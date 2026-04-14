"""R2 workspace synchronization for cloud agent execution.

Uploads workspace files to Cloudflare R2 before agent spawn,
downloads modified files after agent completion. Uses content-addressed
storage to minimize transfer size.
"""

from __future__ import annotations

import fnmatch
import hashlib
import io
import json
import logging
import zipfile
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


def _default_exclude_patterns() -> tuple[str, ...]:
    """Default patterns for workspace file exclusion."""
    return (
        ".git",
        "__pycache__",
        "node_modules",
        ".venv",
        "*.pyc",
        ".sdd/runtime",
        ".sdd/logs",
    )


@dataclass(frozen=True)
class R2Config:
    """Configuration for R2 workspace sync.

    Attributes:
        account_id: Cloudflare account identifier.
        api_token: API token with R2 read/write permissions.
        bucket_name: R2 bucket name for workspace storage.
        max_file_size_mb: Maximum individual file size in megabytes.
        exclude_patterns: Glob/path patterns to skip during sync.
    """

    account_id: str
    api_token: str
    bucket_name: str = "bernstein-workspaces"
    max_file_size_mb: int = 50
    exclude_patterns: tuple[str, ...] = (
        ".git",
        "__pycache__",
        "node_modules",
        ".venv",
        "*.pyc",
        ".sdd/runtime",
        ".sdd/logs",
    )


@dataclass(frozen=True)
class SyncManifest:
    """Manifest of files in a workspace snapshot.

    Attributes:
        workspace_id: Unique identifier for this workspace.
        files: Mapping of relative path to SHA-256 content hash.
        total_bytes: Total size of all files in bytes.
        file_count: Number of files in the manifest.
    """

    workspace_id: str
    files: dict[str, str]  # relative_path -> content_hash
    total_bytes: int = 0
    file_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize manifest to a JSON-compatible dictionary."""
        return {
            "workspace_id": self.workspace_id,
            "files": self.files,
            "total_bytes": self.total_bytes,
            "file_count": self.file_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SyncManifest:
        """Deserialize manifest from a dictionary.

        Args:
            data: Dictionary with manifest fields.

        Returns:
            Reconstructed SyncManifest.
        """
        return cls(
            workspace_id=data["workspace_id"],
            files=data["files"],
            total_bytes=data.get("total_bytes", 0),
            file_count=data.get("file_count", 0),
        )


@dataclass(frozen=True)
class SyncResult:
    """Result of a sync operation.

    Attributes:
        workspace_id: Unique identifier for this workspace.
        files_uploaded: Number of files uploaded.
        files_downloaded: Number of files downloaded.
        bytes_transferred: Total bytes transferred.
        files_changed: List of relative paths that changed.
    """

    workspace_id: str
    files_uploaded: int = 0
    files_downloaded: int = 0
    bytes_transferred: int = 0
    files_changed: list[str] = field(default_factory=list)


class R2SyncError(Exception):
    """Raised when an R2 sync operation fails.

    Attributes:
        workspace_id: The workspace the error relates to, if applicable.
        status_code: HTTP status code from R2, if applicable.
    """

    def __init__(
        self,
        message: str,
        *,
        workspace_id: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.workspace_id = workspace_id
        self.status_code = status_code


class R2WorkspaceSync:
    """Sync local workspace to/from Cloudflare R2.

    Uses content-addressed storage (SHA-256 hashes) so unchanged files
    are never re-uploaded. Each workspace snapshot gets a manifest that
    maps relative paths to content hashes.

    Usage::

        sync = R2WorkspaceSync(R2Config(account_id="...", api_token="..."))
        # Upload before agent runs
        manifest = await sync.upload(workdir, workspace_id="task-123")
        # Download after agent completes
        result = await sync.download(workdir, workspace_id="task-123")
    """

    def __init__(self, config: R2Config) -> None:
        self._config = config

    async def upload(self, workdir: Path, workspace_id: str) -> SyncManifest:
        """Upload workspace files to R2 bucket.

        Creates a zip archive of the workspace (excluding configured patterns)
        and uploads it to R2 with a manifest for delta syncing.

        Args:
            workdir: Local workspace directory to upload.
            workspace_id: Unique identifier for this workspace snapshot.

        Returns:
            Manifest describing the uploaded files.

        Raises:
            R2SyncError: If upload fails.
        """
        local_files = self._scan_workspace(workdir)

        # Try to fetch existing manifest for delta detection
        existing_hashes: dict[str, str] = {}
        try:
            raw = await self._get_object(f"{workspace_id}/manifest.json")
            existing_manifest = SyncManifest.from_dict(json.loads(raw))
            existing_hashes = existing_manifest.files
        except R2SyncError:
            pass  # No existing manifest — upload everything

        # Determine which files need uploading (new or changed)
        files_to_upload = [
            rel_path for rel_path, content_hash in local_files.items() if existing_hashes.get(rel_path) != content_hash
        ]

        bytes_transferred = 0
        if files_to_upload:
            zip_data = self._build_zip(workdir, files_to_upload)
            bytes_transferred = len(zip_data)
            await self._put_object(
                f"{workspace_id}/workspace.zip",
                zip_data,
                content_type="application/zip",
            )

        # Compute totals
        total_bytes = sum(
            (workdir / rel_path).stat().st_size for rel_path in local_files if (workdir / rel_path).exists()
        )

        manifest = SyncManifest(
            workspace_id=workspace_id,
            files=local_files,
            total_bytes=total_bytes,
            file_count=len(local_files),
        )

        await self._put_object(
            f"{workspace_id}/manifest.json",
            json.dumps(manifest.to_dict()).encode(),
            content_type="application/json",
        )

        logger.info(
            "Uploaded workspace %s: %d files (%d changed), %d bytes",
            workspace_id,
            len(local_files),
            len(files_to_upload),
            bytes_transferred,
        )

        return manifest

    async def download(self, workdir: Path, workspace_id: str) -> SyncResult:
        """Download modified files from R2 back to local workspace.

        Compares remote manifest against local state and downloads only
        changed files.

        Args:
            workdir: Local workspace directory to download into.
            workspace_id: Unique identifier for this workspace snapshot.

        Returns:
            Result describing what was downloaded.

        Raises:
            R2SyncError: If download fails.
        """
        # Get remote manifest
        try:
            raw = await self._get_object(f"{workspace_id}/manifest.json")
        except R2SyncError as exc:
            raise R2SyncError(
                f"No manifest found for workspace {workspace_id}",
                workspace_id=workspace_id,
            ) from exc

        remote_manifest = SyncManifest.from_dict(json.loads(raw))

        # Compare against local file hashes
        local_files = self._scan_workspace(workdir)
        changed_files = [
            rel_path
            for rel_path, content_hash in remote_manifest.files.items()
            if local_files.get(rel_path) != content_hash
        ]

        bytes_transferred = 0
        if changed_files:
            zip_data = await self._get_object(f"{workspace_id}/workspace.zip")
            bytes_transferred = len(zip_data)

            with zipfile.ZipFile(io.BytesIO(zip_data), "r") as zf:
                for rel_path in changed_files:
                    if rel_path in zf.namelist():
                        target = workdir / rel_path
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(zf.read(rel_path))

        logger.info(
            "Downloaded workspace %s: %d files changed, %d bytes",
            workspace_id,
            len(changed_files),
            bytes_transferred,
        )

        return SyncResult(
            workspace_id=workspace_id,
            files_downloaded=len(changed_files),
            bytes_transferred=bytes_transferred,
            files_changed=changed_files,
        )

    async def cleanup(self, workspace_id: str) -> None:
        """Delete workspace files from R2 after task completion.

        Args:
            workspace_id: Identifier of the workspace to remove.

        Raises:
            R2SyncError: If deletion fails.
        """
        objects = await self._list_objects(prefix=f"{workspace_id}/")
        for key in objects:
            await self._delete_object(key)
        logger.info("Cleaned up workspace %s: deleted %d objects", workspace_id, len(objects))

    def _scan_workspace(self, workdir: Path) -> dict[str, str]:
        """Scan workspace and compute content hashes, respecting exclude patterns.

        Args:
            workdir: Root directory to scan.

        Returns:
            Mapping of relative path (POSIX format) to SHA-256 hex digest.
        """
        result: dict[str, str] = {}
        max_bytes = self._config.max_file_size_mb * 1024 * 1024

        for path in sorted(workdir.rglob("*")):
            if not path.is_file():
                continue
            if self._should_exclude(path.relative_to(workdir)):
                continue
            try:
                if path.stat().st_size > max_bytes:
                    logger.debug("Skipping oversized file: %s", path)
                    continue
                rel = path.relative_to(workdir).as_posix()
                result[rel] = self._hash_file(path)
            except OSError:
                logger.debug("Skipping unreadable file: %s", path)

        return result

    def _should_exclude(self, rel_path: Path) -> bool:
        """Check if a relative path matches any exclude pattern.

        Args:
            rel_path: Path relative to workspace root.

        Returns:
            True if the path should be excluded.
        """
        rel_str = rel_path.as_posix()
        for pattern in self._config.exclude_patterns:
            # Check each path component for directory-style patterns
            for part in rel_path.parts:
                if fnmatch.fnmatch(part, pattern):
                    return True
            # Check the full relative path
            if fnmatch.fnmatch(rel_str, pattern):
                return True
            # Check if pattern matches as a path prefix
            if rel_str.startswith(pattern + "/") or rel_str == pattern:
                return True
        return False

    @staticmethod
    def _hash_file(path: Path) -> str:
        """Compute SHA-256 hash of file contents.

        Args:
            path: Absolute path to the file.

        Returns:
            Hex-encoded SHA-256 digest.
        """
        h = hashlib.sha256()
        with path.open("rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    def _build_zip(self, workdir: Path, files: list[str]) -> bytes:
        """Create zip archive of specified files.

        Args:
            workdir: Root directory containing the files.
            files: List of relative paths to include.

        Returns:
            Zip archive as bytes.
        """
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel_path in files:
                abs_path = workdir / rel_path
                if abs_path.is_file():
                    zf.write(abs_path, arcname=rel_path)
        return buf.getvalue()

    async def _put_object(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> None:
        """Upload object to R2 via S3-compatible API.

        Args:
            key: Object key within the bucket.
            data: Raw bytes to upload.
            content_type: MIME type for the object.

        Raises:
            R2SyncError: If the upload fails.
        """
        url = self._r2_url(key)
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.put(
                    url,
                    content=data,
                    headers={
                        "Authorization": f"Bearer {self._config.api_token}",
                        "Content-Type": content_type,
                    },
                    timeout=60.0,
                )
            except httpx.HTTPError as exc:
                raise R2SyncError(f"Failed to upload {key}: {exc}") from exc

            if resp.status_code >= 400:
                raise R2SyncError(
                    f"R2 PUT {key} returned {resp.status_code}: {resp.text}",
                    status_code=resp.status_code,
                )

    async def _get_object(self, key: str) -> bytes:
        """Download object from R2.

        Args:
            key: Object key within the bucket.

        Returns:
            Raw object bytes.

        Raises:
            R2SyncError: If the download fails.
        """
        url = self._r2_url(key)
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {self._config.api_token}"},
                    timeout=60.0,
                )
            except httpx.HTTPError as exc:
                raise R2SyncError(f"Failed to download {key}: {exc}") from exc

            if resp.status_code >= 400:
                raise R2SyncError(
                    f"R2 GET {key} returned {resp.status_code}: {resp.text}",
                    status_code=resp.status_code,
                )

        return resp.content

    async def _delete_object(self, key: str) -> None:
        """Delete object from R2.

        Args:
            key: Object key within the bucket.

        Raises:
            R2SyncError: If the deletion fails.
        """
        url = self._r2_url(key)
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.delete(
                    url,
                    headers={"Authorization": f"Bearer {self._config.api_token}"},
                    timeout=30.0,
                )
            except httpx.HTTPError as exc:
                raise R2SyncError(f"Failed to delete {key}: {exc}") from exc

            if resp.status_code >= 400:
                raise R2SyncError(
                    f"R2 DELETE {key} returned {resp.status_code}: {resp.text}",
                    status_code=resp.status_code,
                )

    async def _list_objects(self, prefix: str) -> list[str]:
        """List objects in R2 with given prefix.

        Args:
            prefix: Key prefix to filter by.

        Returns:
            List of matching object keys.

        Raises:
            R2SyncError: If the listing fails.
        """
        url = self._r2_url("")
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(
                    url,
                    params={"prefix": prefix, "list-type": "2"},
                    headers={"Authorization": f"Bearer {self._config.api_token}"},
                    timeout=30.0,
                )
            except httpx.HTTPError as exc:
                raise R2SyncError(f"Failed to list objects with prefix {prefix}: {exc}") from exc

            if resp.status_code >= 400:
                raise R2SyncError(
                    f"R2 LIST returned {resp.status_code}: {resp.text}",
                    status_code=resp.status_code,
                )

        # Parse S3 XML response — extract <Key> elements
        import xml.etree.ElementTree as ET

        keys: list[str] = []
        try:
            root = ET.fromstring(resp.text)
            # S3 ListObjectsV2 uses namespace
            ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
            for contents in root.findall(".//s3:Contents/s3:Key", ns):
                if contents.text:
                    keys.append(contents.text)
            # Also try without namespace (R2 may omit it)
            if not keys:
                for contents in root.findall(".//Contents/Key"):
                    if contents.text:
                        keys.append(contents.text)
        except ET.ParseError:
            logger.warning("Failed to parse R2 list response")

        return keys

    def _r2_url(self, key: str) -> str:
        """Build R2 S3-compatible API URL for a key.

        Args:
            key: Object key (may be empty for bucket-level operations).

        Returns:
            Full URL for the R2 S3-compatible endpoint.
        """
        account_id = self._config.account_id
        bucket = self._config.bucket_name
        base = f"https://{account_id}.r2.cloudflarestorage.com/{bucket}"
        if key:
            return f"{base}/{key}"
        return base
