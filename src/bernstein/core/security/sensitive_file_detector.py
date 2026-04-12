"""SEC-017: Sensitive file detection and handling.

Auto-detect certificates, keys, config files with secrets.  Prevent
accidental commits by scanning file paths and content for known
sensitive patterns.

Usage::

    from bernstein.core.sensitive_file_detector import (
        SensitiveFileDetector,
        SensitiveFileConfig,
        DetectionResult,
    )

    detector = SensitiveFileDetector()
    result = detector.scan_path("config/.env.production")
    if result.is_sensitive:
        print(f"Sensitive file: {result.reason}")
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


class SensitiveCategory(StrEnum):
    """Categories of sensitive file detections."""

    PRIVATE_KEY = "private_key"
    CERTIFICATE = "certificate"
    ENVIRONMENT_FILE = "environment_file"
    CREDENTIALS = "credentials"
    CONFIG_SECRET = "config_secret"
    TOKEN_FILE = "token_file"
    SSH_KEY = "ssh_key"
    CLOUD_CREDENTIALS = "cloud_credentials"


class DetectionConfidence(StrEnum):
    """Confidence level of a sensitive file detection."""

    HIGH = "high"  # Definite match (e.g. .pem file with key header)
    MEDIUM = "medium"  # Likely match (e.g. .env file)
    LOW = "low"  # Possible match (e.g. config.yaml with "password" key)


@dataclass(frozen=True)
class DetectionResult:
    """Result of scanning a single file for sensitive content.

    Attributes:
        path: The file path that was scanned.
        is_sensitive: Whether the file was detected as sensitive.
        category: Category of sensitivity.
        confidence: Confidence of the detection.
        reason: Why the file was flagged.
        line_number: Line number where the sensitive content was found (0 if path-based).
    """

    path: str
    is_sensitive: bool
    category: SensitiveCategory | None = None
    confidence: DetectionConfidence | None = None
    reason: str = ""
    line_number: int = 0


@dataclass(frozen=True)
class ScanSummary:
    """Summary of scanning multiple files.

    Attributes:
        total_scanned: Number of files scanned.
        sensitive_count: Number of files detected as sensitive.
        results: Individual detection results for sensitive files.
        safe_for_commit: Whether all files are safe to commit.
    """

    total_scanned: int
    sensitive_count: int
    results: tuple[DetectionResult, ...]
    safe_for_commit: bool


# ---------------------------------------------------------------------------
# Path-based patterns
# ---------------------------------------------------------------------------

_SENSITIVE_PATH_PATTERNS: tuple[tuple[re.Pattern[str], SensitiveCategory, DetectionConfidence], ...] = (
    # Private keys
    (re.compile(r".*\.pem$", re.IGNORECASE), SensitiveCategory.PRIVATE_KEY, DetectionConfidence.HIGH),
    (re.compile(r".*\.key$", re.IGNORECASE), SensitiveCategory.PRIVATE_KEY, DetectionConfidence.HIGH),
    (re.compile(r".*\.p12$", re.IGNORECASE), SensitiveCategory.PRIVATE_KEY, DetectionConfidence.HIGH),
    (re.compile(r".*\.pfx$", re.IGNORECASE), SensitiveCategory.PRIVATE_KEY, DetectionConfidence.HIGH),
    (re.compile(r".*\.jks$", re.IGNORECASE), SensitiveCategory.PRIVATE_KEY, DetectionConfidence.HIGH),
    # Certificates
    (re.compile(r".*\.crt$", re.IGNORECASE), SensitiveCategory.CERTIFICATE, DetectionConfidence.MEDIUM),
    (re.compile(r".*\.cer$", re.IGNORECASE), SensitiveCategory.CERTIFICATE, DetectionConfidence.MEDIUM),
    (re.compile(r".*\.der$", re.IGNORECASE), SensitiveCategory.CERTIFICATE, DetectionConfidence.MEDIUM),
    # Environment files
    (re.compile(r"(^|/)\.env(\..+)?$"), SensitiveCategory.ENVIRONMENT_FILE, DetectionConfidence.HIGH),
    (re.compile(r"(^|/)\.env\.local$"), SensitiveCategory.ENVIRONMENT_FILE, DetectionConfidence.HIGH),
    (re.compile(r"(^|/)\.env\.production$"), SensitiveCategory.ENVIRONMENT_FILE, DetectionConfidence.HIGH),
    # Credential files
    (re.compile(r"(^|/)credentials\.json$"), SensitiveCategory.CREDENTIALS, DetectionConfidence.HIGH),
    (
        re.compile(r"(^|/)service[-_]?account.*\.json$", re.IGNORECASE),
        SensitiveCategory.CLOUD_CREDENTIALS,
        DetectionConfidence.HIGH,
    ),
    (re.compile(r"(^|/)\.htpasswd$"), SensitiveCategory.CREDENTIALS, DetectionConfidence.HIGH),
    (re.compile(r"(^|/)\.netrc$"), SensitiveCategory.CREDENTIALS, DetectionConfidence.HIGH),
    # SSH keys
    (re.compile(r"(^|/)id_(rsa|dsa|ecdsa|ed25519)$"), SensitiveCategory.SSH_KEY, DetectionConfidence.HIGH),
    (re.compile(r"(^|/)id_(rsa|dsa|ecdsa|ed25519)\.pub$"), SensitiveCategory.SSH_KEY, DetectionConfidence.MEDIUM),
    # Token files
    (re.compile(r"(^|/)\.token$"), SensitiveCategory.TOKEN_FILE, DetectionConfidence.HIGH),
    (re.compile(r"(^|/)token\.json$"), SensitiveCategory.TOKEN_FILE, DetectionConfidence.HIGH),
    # Cloud credentials
    (re.compile(r"(^|/)\.aws/credentials$"), SensitiveCategory.CLOUD_CREDENTIALS, DetectionConfidence.HIGH),
    (re.compile(r"(^|/)\.gcp/.*\.json$"), SensitiveCategory.CLOUD_CREDENTIALS, DetectionConfidence.HIGH),
    (re.compile(r"(^|/)\.azure/.*\.json$"), SensitiveCategory.CLOUD_CREDENTIALS, DetectionConfidence.HIGH),
    (re.compile(r"(^|/)kubeconfig$"), SensitiveCategory.CLOUD_CREDENTIALS, DetectionConfidence.HIGH),
    (re.compile(r"(^|/)\.kube/config$"), SensitiveCategory.CLOUD_CREDENTIALS, DetectionConfidence.HIGH),
)

# ---------------------------------------------------------------------------
# Content-based patterns
# ---------------------------------------------------------------------------

_SENSITIVE_CONTENT_PATTERNS: tuple[tuple[re.Pattern[str], SensitiveCategory, DetectionConfidence], ...] = (
    # Private key headers
    (re.compile(r"-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----"), SensitiveCategory.PRIVATE_KEY, DetectionConfidence.HIGH),
    (re.compile(r"-----BEGIN\s+EC\s+PRIVATE\s+KEY-----"), SensitiveCategory.PRIVATE_KEY, DetectionConfidence.HIGH),
    (
        re.compile(r"-----BEGIN\s+ENCRYPTED\s+PRIVATE\s+KEY-----"),
        SensitiveCategory.PRIVATE_KEY,
        DetectionConfidence.HIGH,
    ),
    (re.compile(r"-----BEGIN\s+OPENSSH\s+PRIVATE\s+KEY-----"), SensitiveCategory.SSH_KEY, DetectionConfidence.HIGH),
    # API key patterns
    (re.compile(r"sk-ant-[a-zA-Z0-9\-_]{20,}"), SensitiveCategory.TOKEN_FILE, DetectionConfidence.HIGH),
    (re.compile(r"sk-[a-zA-Z0-9]{20,}"), SensitiveCategory.TOKEN_FILE, DetectionConfidence.MEDIUM),
    (re.compile(r"AIza[a-zA-Z0-9\-_]{35}"), SensitiveCategory.TOKEN_FILE, DetectionConfidence.HIGH),
    (re.compile(r"gsk_[a-zA-Z0-9]{20,}"), SensitiveCategory.TOKEN_FILE, DetectionConfidence.HIGH),
    # Generic secret patterns in config
    (
        re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key)\s*[:=]\s*['\"]?[a-zA-Z0-9+/=_\-]{16,}"),
        SensitiveCategory.CONFIG_SECRET,
        DetectionConfidence.MEDIUM,
    ),
    # AWS keys
    (re.compile(r"AKIA[0-9A-Z]{16}"), SensitiveCategory.CLOUD_CREDENTIALS, DetectionConfidence.HIGH),
)


@dataclass
class SensitiveFileConfig:
    """Configuration for the sensitive file detector.

    Attributes:
        extra_path_patterns: Additional path patterns to consider sensitive.
        extra_content_patterns: Additional content patterns to scan for.
        ignore_paths: Path patterns to ignore (e.g. test fixtures).
        scan_content: Whether to scan file content (slower but more thorough).
        max_file_size_bytes: Skip content scanning for files larger than this.
    """

    extra_path_patterns: list[tuple[str, SensitiveCategory, DetectionConfidence]] = field(
        default_factory=list[tuple[str, SensitiveCategory, DetectionConfidence]],
    )
    extra_content_patterns: list[tuple[str, SensitiveCategory, DetectionConfidence]] = field(
        default_factory=list[tuple[str, SensitiveCategory, DetectionConfidence]],
    )
    ignore_paths: list[str] = field(default_factory=list[str])
    scan_content: bool = True
    max_file_size_bytes: int = 1_000_000  # 1 MB


class SensitiveFileDetector:
    """Detects sensitive files by path patterns and content scanning.

    Args:
        config: Detection configuration.
    """

    def __init__(self, config: SensitiveFileConfig | None = None) -> None:
        self._config = config or SensitiveFileConfig()
        self._extra_path_patterns: list[tuple[re.Pattern[str], SensitiveCategory, DetectionConfidence]] = [
            (re.compile(p), cat, conf) for p, cat, conf in self._config.extra_path_patterns
        ]
        self._extra_content_patterns: list[tuple[re.Pattern[str], SensitiveCategory, DetectionConfidence]] = [
            (re.compile(p), cat, conf) for p, cat, conf in self._config.extra_content_patterns
        ]

    def _is_ignored(self, path: str) -> bool:
        """Check if a path should be ignored.

        Args:
            path: The file path to check.

        Returns:
            True if the path matches an ignore pattern.
        """
        return any(pattern in path for pattern in self._config.ignore_paths)

    def scan_path(self, path: str) -> DetectionResult:
        """Scan a file path (without reading content) for sensitive patterns.

        Args:
            path: The file path to scan.

        Returns:
            Detection result for the path.
        """
        if self._is_ignored(path):
            return DetectionResult(path=path, is_sensitive=False, reason="Ignored by config")

        # Check built-in patterns
        for pattern, category, confidence in _SENSITIVE_PATH_PATTERNS:
            if pattern.search(path):
                return DetectionResult(
                    path=path,
                    is_sensitive=True,
                    category=category,
                    confidence=confidence,
                    reason=f"Path matches sensitive pattern: {pattern.pattern}",
                )

        # Check extra patterns
        for pattern, category, confidence in self._extra_path_patterns:
            if pattern.search(path):
                return DetectionResult(
                    path=path,
                    is_sensitive=True,
                    category=category,
                    confidence=confidence,
                    reason=f"Path matches custom pattern: {pattern.pattern}",
                )

        return DetectionResult(path=path, is_sensitive=False, reason="No sensitive patterns matched")

    def scan_content(self, path: str, content: str) -> DetectionResult:
        """Scan file content for sensitive patterns.

        Args:
            path: The file path (for reporting).
            content: The file content to scan.

        Returns:
            Detection result.  Returns the first match found.
        """
        if self._is_ignored(path):
            return DetectionResult(path=path, is_sensitive=False, reason="Ignored by config")

        lines = content.splitlines()

        # Check built-in content patterns
        for pattern, category, confidence in _SENSITIVE_CONTENT_PATTERNS:
            for line_num, line in enumerate(lines, 1):
                if pattern.search(line):
                    return DetectionResult(
                        path=path,
                        is_sensitive=True,
                        category=category,
                        confidence=confidence,
                        reason=f"Content matches sensitive pattern: {pattern.pattern}",
                        line_number=line_num,
                    )

        # Check extra content patterns
        for pattern, category, confidence in self._extra_content_patterns:
            for line_num, line in enumerate(lines, 1):
                if pattern.search(line):
                    return DetectionResult(
                        path=path,
                        is_sensitive=True,
                        category=category,
                        confidence=confidence,
                        reason=f"Content matches custom pattern: {pattern.pattern}",
                        line_number=line_num,
                    )

        return DetectionResult(path=path, is_sensitive=False, reason="No sensitive content found")

    def scan_file(self, filepath: Path) -> DetectionResult:
        """Scan a file by both path and content.

        Args:
            filepath: Path to the file to scan.

        Returns:
            Detection result.  Path check is done first, then content.
        """
        path_str = str(filepath)

        # Path-based check first (fast)
        path_result = self.scan_path(path_str)
        if path_result.is_sensitive:
            return path_result

        # Content-based check (slower)
        if not self._config.scan_content:
            return DetectionResult(
                path=path_str,
                is_sensitive=False,
                reason="Content scanning disabled",
            )

        if not filepath.is_file():
            return DetectionResult(
                path=path_str,
                is_sensitive=False,
                reason="Not a file",
            )

        try:
            size = filepath.stat().st_size
            if size > self._config.max_file_size_bytes:
                return DetectionResult(
                    path=path_str,
                    is_sensitive=False,
                    reason=f"File too large ({size} bytes) for content scanning",
                )
            content = filepath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return DetectionResult(
                path=path_str,
                is_sensitive=False,
                reason="Could not read file",
            )

        return self.scan_content(path_str, content)

    def scan_paths(self, paths: list[str]) -> ScanSummary:
        """Scan multiple file paths and return a summary.

        Args:
            paths: List of file paths to scan.

        Returns:
            Summary of the scan results.
        """
        sensitive_results: list[DetectionResult] = []

        for path in paths:
            result = self.scan_path(path)
            if result.is_sensitive:
                sensitive_results.append(result)

        return ScanSummary(
            total_scanned=len(paths),
            sensitive_count=len(sensitive_results),
            results=tuple(sensitive_results),
            safe_for_commit=len(sensitive_results) == 0,
        )

    def scan_directory(self, directory: Path, recursive: bool = True) -> ScanSummary:
        """Scan all files in a directory.

        Args:
            directory: Directory to scan.
            recursive: Whether to scan recursively.

        Returns:
            Summary of the scan results.
        """
        sensitive_results: list[DetectionResult] = []
        total = 0

        pattern = "**/*" if recursive else "*"
        for filepath in directory.glob(pattern):
            if filepath.is_file():
                total += 1
                result = self.scan_file(filepath)
                if result.is_sensitive:
                    sensitive_results.append(result)

        return ScanSummary(
            total_scanned=total,
            sensitive_count=len(sensitive_results),
            results=tuple(sensitive_results),
            safe_for_commit=len(sensitive_results) == 0,
        )
