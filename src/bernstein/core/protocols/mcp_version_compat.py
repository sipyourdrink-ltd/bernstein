"""MCP-012: MCP server version compatibility checking.

Verify MCP protocol version before connecting to a server. Supports
semantic version ranges so the orchestrator can refuse to connect to
servers running incompatible protocol versions.

Versioning follows semver: MAJOR.MINOR.PATCH
- MAJOR mismatch = incompatible.
- MINOR mismatch = compatible if server >= client required.
- PATCH differences are always compatible.

Usage::

    from bernstein.core.mcp_version_compat import VersionChecker

    checker = VersionChecker(required_version="2025-11-05")
    result = checker.check("github", "2025-11-05")
    assert result.compatible
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class CompatLevel(StrEnum):
    """Compatibility assessment level."""

    COMPATIBLE = "compatible"
    MINOR_MISMATCH = "minor_mismatch"
    INCOMPATIBLE = "incompatible"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ParsedVersion:
    """A parsed version identifier.

    Supports both semver (1.2.3) and date-based (2025-11-05) versions.

    Attributes:
        raw: The original version string.
        parts: Numeric parts extracted from the version.
        is_date: Whether this looks like a date-based version.
    """

    raw: str
    parts: tuple[int, ...] = ()
    is_date: bool = False

    @classmethod
    def parse(cls, version_str: str) -> ParsedVersion:
        """Parse a version string.

        Handles:
        - Semver: "1.2.3", "2.0", "1"
        - Date-based: "2025-11-05"
        - Prefixed: "v1.2.3"

        Args:
            version_str: Raw version string.

        Returns:
            Parsed version.
        """
        cleaned = version_str.strip().lstrip("v")
        date_match = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", cleaned)
        if date_match:
            parts = tuple(int(g) for g in date_match.groups())
            return cls(raw=version_str, parts=parts, is_date=True)

        num_match = re.findall(r"\d+", cleaned)
        if num_match:
            parts = tuple(int(n) for n in num_match)
            return cls(raw=version_str, parts=parts, is_date=False)

        return cls(raw=version_str, parts=(), is_date=False)


@dataclass(frozen=True)
class CompatResult:
    """Result of a version compatibility check.

    Attributes:
        server_name: MCP server name.
        server_version: The server's reported version.
        required_version: The version required by the client.
        level: Compatibility assessment level.
        compatible: Whether the server is compatible.
        message: Human-readable explanation.
    """

    server_name: str
    server_version: str
    required_version: str
    level: CompatLevel
    compatible: bool
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "server_name": self.server_name,
            "server_version": self.server_version,
            "required_version": self.required_version,
            "level": self.level.value,
            "compatible": self.compatible,
            "message": self.message,
        }


class VersionChecker:
    """Checks MCP server protocol version compatibility.

    Args:
        required_version: The protocol version this orchestrator requires.
        strict: If True, minor mismatches are treated as incompatible.
    """

    def __init__(
        self,
        required_version: str = "2025-11-05",
        *,
        strict: bool = False,
    ) -> None:
        self._required = ParsedVersion.parse(required_version)
        self._required_raw = required_version
        self._strict = strict
        self._results: dict[str, CompatResult] = {}

    @property
    def required_version(self) -> str:
        """The required protocol version string."""
        return self._required_raw

    def check(self, server_name: str, server_version: str) -> CompatResult:
        """Check a server's version against the required version.

        Args:
            server_name: MCP server name.
            server_version: Server's reported protocol version.

        Returns:
            Compatibility result.
        """
        server_parsed = ParsedVersion.parse(server_version)

        if not self._required.parts or not server_parsed.parts:
            result = CompatResult(
                server_name=server_name,
                server_version=server_version,
                required_version=self._required_raw,
                level=CompatLevel.UNKNOWN,
                compatible=True,
                message="Could not parse version; allowing connection",
            )
            self._results[server_name] = result
            return result

        if self._required.is_date and server_parsed.is_date:
            return self._check_date_versions(server_name, server_version, server_parsed)

        return self._check_semver(server_name, server_version, server_parsed)

    def _check_date_versions(
        self,
        server_name: str,
        server_version: str,
        server_parsed: ParsedVersion,
    ) -> CompatResult:
        """Compare date-based versions."""
        if server_parsed.parts == self._required.parts:
            result = CompatResult(
                server_name=server_name,
                server_version=server_version,
                required_version=self._required_raw,
                level=CompatLevel.COMPATIBLE,
                compatible=True,
                message="Exact version match",
            )
        elif server_parsed.parts >= self._required.parts:
            result = CompatResult(
                server_name=server_name,
                server_version=server_version,
                required_version=self._required_raw,
                level=CompatLevel.COMPATIBLE,
                compatible=True,
                message=f"Server version {server_version} >= required {self._required_raw}",
            )
        else:
            result = CompatResult(
                server_name=server_name,
                server_version=server_version,
                required_version=self._required_raw,
                level=CompatLevel.INCOMPATIBLE,
                compatible=False,
                message=f"Server version {server_version} < required {self._required_raw}",
            )
        self._results[server_name] = result
        return result

    def _check_semver(
        self,
        server_name: str,
        server_version: str,
        server_parsed: ParsedVersion,
    ) -> CompatResult:
        """Compare semver-style versions."""
        req = self._required.parts
        srv = server_parsed.parts

        max_len = max(len(req), len(srv))
        req_padded = req + (0,) * (max_len - len(req))
        srv_padded = srv + (0,) * (max_len - len(srv))

        if len(req_padded) > 0 and len(srv_padded) > 0 and srv_padded[0] != req_padded[0]:
            result = CompatResult(
                server_name=server_name,
                server_version=server_version,
                required_version=self._required_raw,
                level=CompatLevel.INCOMPATIBLE,
                compatible=False,
                message=f"Major version mismatch: server={srv_padded[0]} required={req_padded[0]}",
            )
            self._results[server_name] = result
            return result

        if len(req_padded) > 1 and len(srv_padded) > 1 and srv_padded[1] < req_padded[1]:
            compat = not self._strict
            result = CompatResult(
                server_name=server_name,
                server_version=server_version,
                required_version=self._required_raw,
                level=CompatLevel.MINOR_MISMATCH,
                compatible=compat,
                message=f"Minor version: server={srv_padded[1]} < required={req_padded[1]}",
            )
            self._results[server_name] = result
            return result

        result = CompatResult(
            server_name=server_name,
            server_version=server_version,
            required_version=self._required_raw,
            level=CompatLevel.COMPATIBLE,
            compatible=True,
            message="Version compatible",
        )
        self._results[server_name] = result
        return result

    def check_many(self, servers: dict[str, str]) -> list[CompatResult]:
        """Check multiple servers at once.

        Args:
            servers: Mapping of server_name to version string.

        Returns:
            List of compatibility results.
        """
        return [self.check(name, version) for name, version in servers.items()]

    def get_incompatible(self) -> list[CompatResult]:
        """Return results for all servers checked as incompatible."""
        return [r for r in self._results.values() if not r.compatible]

    def get_result(self, server_name: str) -> CompatResult | None:
        """Return the last check result for a server."""
        return self._results.get(server_name)

    def all_results(self) -> list[CompatResult]:
        """Return all check results."""
        return list(self._results.values())

    def to_dict(self) -> dict[str, Any]:
        """Serialize all results to a JSON-compatible dict."""
        return {
            "required_version": self._required_raw,
            "strict": self._strict,
            "results": {name: r.to_dict() for name, r in self._results.items()},
        }
