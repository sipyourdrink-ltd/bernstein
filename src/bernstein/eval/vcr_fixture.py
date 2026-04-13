"""VCR fixture pattern — dehydrate/hydrate deterministic test fixtures (T805).

Dehydration replaces environment-specific values with stable placeholders so
test recordings can be compared and replayed deterministically. Hydration
reverses the process, substituting actual values back in for replay.

Placeholders:
    - ``__CWD__`` — current working directory
    - ``__TIMESTAMP__`` — ISO-8601 timestamps
    - ``__UUID__`` — UUID patterns (with sequential numeric suffixes)
    - ``__HOME__`` — user home directory
    - ``__PID__`` — process ID
    - ``__PORT__`` — numeric port patterns in URLs

Usage:
    >>> vcr = VcrFixture(cwd="/tmp/proj")
    >>> vcr.dehydrate("Running /tmp/proj at 2026-04-03T10:00:00")
    'Running __CWD__ at __TIMESTAMP__'
    >>> vcr.hydrate("Running __CWD__ at __TIMESTAMP__")
    'Running /tmp/proj at 2026-04-03T00:00:00+00:00'
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

# ---------------------------------------------------------------------------
# Placeholder tokens
# ---------------------------------------------------------------------------

CWD_PLACEHOLDER = "__CWD__"
HOME_PLACEHOLDER = "__HOME__"
TIMESTAMP_PLACEHOLDER = "__TIMESTAMP__"
UUID_PLACEHOLDER = "__UUID__"
PID_PLACEHOLDER = "__PID__"
PORT_PLACEHOLDER = "__PORT__"

# ---------------------------------------------------------------------------
# Regex patterns — most specific first
# ---------------------------------------------------------------------------

# ISO-8601 timestamps: 2026-04-03T10:00:00Z, 2026-04-03T10:00:00.123+00:00, etc.
_TIMESTAMP_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?[Z+-]?(?:\d{2}:?\d{2})?",
)

# UUID patterns: 12345678-1234-1234-1234-123456789abc
_UUID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
)

# Short hex IDs (12-32 char hex strings not part of a path)
_SHORT_HEX_PATTERN = re.compile(
    r"(?<![/\\])[0-9a-fA-F]{12,32}(?![/\\w])",
)

# Port numbers in URL contexts (high ports only, not 80/443/8080)
_PORT_PATTERN = re.compile(
    r"https?://[^/:]+:(\d{2,5})",
)

# ---------------------------------------------------------------------------


@dataclass
class VcrMapping:
    """A single captured value to placeholder mapping."""

    placeholder: str
    original: str
    metadata: dict[str, Any] = field(default_factory=lambda: {})


@dataclass
class DehydrateResult:
    """Result of a dehydration operation."""

    dehydrated: str
    mappings: list[VcrMapping]
    cost_bytes: int


@dataclass
class HydrateResult:
    """Result of a hydration operation."""

    hydrated: str
    mappings_applied: int
    hypothetical_cost_bytes: int


class VcrFixture:
    """Dehydrate/hydrate strings for deterministic VCR-style test fixtures (T805).

    Args:
        cwd: Working directory to replace with ``__CWD__``.
            Defaults to ``str(Path.cwd())``.
        home: Home directory to replace with ``__HOME__``.
            Defaults to ``Path.home()``.
        pid: Process ID to replace with ``__PID__``. Defaults to current PID.
        fixed_timestamp: Fixed timestamp string for hydration replay.
            Uses current UTC time if not specified.
        seed: Optional seed for deterministic UUID generation on hydration.
    """

    def __init__(
        self,
        cwd: str | None = None,
        home: str | None = None,
        pid: int | None = None,
        fixed_timestamp: str | None = None,
        seed: int | None = None,
    ) -> None:
        self.cwd = cwd or str(Path.cwd())
        if self.cwd.endswith("/") and len(self.cwd) > 1:
            self.cwd = self.cwd.rstrip("/")
        self.home = home or str(Path.home())
        if self.home.endswith("/") and len(self.home) > 1:
            self.home = self.home.rstrip("/")
        self.pid = pid or 0
        self.fixed_timestamp = fixed_timestamp or datetime.now(UTC).isoformat()
        self.seed = seed

        # Track seen values for stable placeholder numbering
        self._uuid_counter: int = 0
        self._uuid_map: dict[str, str] = {}
        self._rehydrate_uuid_counter: int = 0

    # ------------------------------------------------------------------
    # Dehydration — real values to placeholders
    # ------------------------------------------------------------------

    def dehydrate(self, text: str) -> DehydrateResult:
        """Replace environment-specific values with stable placeholders.

        Args:
            text: Raw string containing timestamps, paths, UUIDs, etc.

        Returns:
            DehydrateResult with the normalized string and mapping records.
        """
        mappings: list[VcrMapping] = []
        result = text

        # Order matters: replace CWD/home before generic patterns
        result, cwd_mapping = self._replace_cwd(result)
        if cwd_mapping:
            mappings.append(cwd_mapping)

        result, home_mapping = self._replace_home(result)
        if home_mapping:
            mappings.append(home_mapping)

        result, pid_mapping = self._replace_pid(result)
        if pid_mapping:
            mappings.append(pid_mapping)

        result, port_mappings = self._replace_ports(result)
        mappings.extend(port_mappings)

        result, ts_mapping = self._replace_timestamp(result)
        if ts_mapping:
            mappings.append(ts_mapping)

        result, uuid_mappings = self._replace_uuids(result)
        mappings.extend(uuid_mappings)

        result, hex_mappings = self._replace_short_hex_ids(result)
        mappings.extend(hex_mappings)

        cost = len(result.encode("utf-8"))

        return DehydrateResult(
            dehydrated=result,
            mappings=mappings,
            cost_bytes=cost,
        )

    def dehydrate_value(self, value: Any) -> Any:  # pyright: ignore[reportUnknownVariableType]
        """Dehydrate a single value from a nested structure."""
        if isinstance(value, str):
            return self.dehydrate(value).dehydrated
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for k_str, v in cast("dict[str, Any]", value).items():
                result[k_str] = self.dehydrate_value(v)
            return result
        if isinstance(value, list):
            return [self.dehydrate_value(item) for item in cast("list[Any]", value)]
        return value

    # ------------------------------------------------------------------
    # Hydration — placeholders to reproducible values
    # ------------------------------------------------------------------

    def hydrate(self, text: str) -> HydrateResult:
        """Replace placeholders with deterministic reproducible values.

        Args:
            text: Dehydrated string containing ``__CWD__``, ``__UUID__``, etc.

        Returns:
            HydrateResult with the replayable string and cost metadata.
        """
        result = text

        result = result.replace(CWD_PLACEHOLDER, self.cwd)
        result = result.replace(HOME_PLACEHOLDER, self.home)
        result = result.replace(PID_PLACEHOLDER, str(self.pid))
        result = self._hydrate_ports(result)
        result = result.replace(TIMESTAMP_PLACEHOLDER, self.fixed_timestamp)

        # Replace __UUID__ with deterministic UUIDs based on seed
        result, uuid_count = self._hydrate_uuids(result)
        self._rehydrate_uuid_counter += uuid_count

        # Replace numbered UUIDs like __UUID_1__
        result = self._hydrate_numbered_uuids(result)

        hypothetical_cost = len(result.encode("utf-8"))

        return HydrateResult(
            hydrated=result,
            mappings_applied=text.count("__") - text.count("____"),
            hypothetical_cost_bytes=hypothetical_cost,
        )

    def hydrate_value(self, value: Any) -> Any:  # pyright: ignore[reportUnknownVariableType]
        """Hydrate a single value from a nested structure."""
        if isinstance(value, str):
            return self.hydrate(value).hydrated
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for k_str, v in cast("dict[str, Any]", value).items():
                result[k_str] = self.hydrate_value(v)
            return result
        if isinstance(value, list):
            items: list[Any] = []
            for item in cast("list[Any]", value):
                items.append(self.hydrate_value(item))
            return items
        return value

    # ------------------------------------------------------------------
    # Internal replacers
    # ------------------------------------------------------------------

    def _replace_cwd(self, text: str) -> tuple[str, VcrMapping | None]:
        if self.cwd in text:
            result = text.replace(self.cwd, CWD_PLACEHOLDER)
            return result, VcrMapping(CWD_PLACEHOLDER, self.cwd, {"type": "path"})
        return text, None

    def _replace_home(self, text: str) -> tuple[str, VcrMapping | None]:
        if self.home in text:
            result = text.replace(self.home, HOME_PLACEHOLDER)
            return result, VcrMapping(HOME_PLACEHOLDER, self.home, {"type": "path"})
        return text, None

    def _replace_pid(self, text: str) -> tuple[str, VcrMapping | None]:
        if self.pid and str(self.pid) in text:
            result = text.replace(str(self.pid), PID_PLACEHOLDER)
            return result, VcrMapping(PID_PLACEHOLDER, str(self.pid), {"type": "pid"})
        return text, None

    def _replace_ports(self, text: str) -> tuple[str, list[VcrMapping]]:
        """Replace port numbers in URL patterns with placeholders."""
        mappings: list[VcrMapping] = []

        def _port_replacer(match: re.Match[str]) -> str:
            port = match.group(1)
            port_num = int(port)
            # Skip well-known ports — only mask high ports
            if port_num < 1024 or port_num == 8080:
                return match.group(0)
            counter = len(mappings) + 1
            placeholder = f"{PORT_PLACEHOLDER}_{counter}" if counter > 1 else PORT_PLACEHOLDER
            mappings.append(VcrMapping(placeholder, port, {"type": "port"}))
            return match.group(0).replace(f":{port}", f":{placeholder}")

        result = _PORT_PATTERN.sub(_port_replacer, text)
        return result, mappings

    def _replace_timestamp(self, text: str) -> tuple[str, VcrMapping | None]:
        match = _TIMESTAMP_PATTERN.search(text)
        if match:
            original = match.group(0)
            result = _TIMESTAMP_PATTERN.sub(TIMESTAMP_PLACEHOLDER, text)
            return result, VcrMapping(TIMESTAMP_PLACEHOLDER, original, {"type": "timestamp"})
        return text, None

    def _replace_uuids(self, text: str) -> tuple[str, list[VcrMapping]]:
        """Replace UUIDs with numbered placeholders for stable mapping."""
        mappings: list[VcrMapping] = []

        def _uuid_replacer(match: re.Match[str]) -> str:
            original = match.group(0)
            if original not in self._uuid_map:
                self._uuid_map[original] = str(self._uuid_counter)
                self._uuid_counter += 1
            idx = self._uuid_map[original]
            placeholder = f"{UUID_PLACEHOLDER}_{idx}__" if idx else UUID_PLACEHOLDER
            mappings.append(VcrMapping(placeholder, original, {"type": "uuid"}))
            return placeholder

        result = _UUID_PATTERN.sub(_uuid_replacer, text)
        return result, mappings

    def _replace_short_hex_ids(self, text: str) -> tuple[str, list[VcrMapping]]:
        """Replace 12-32 char hex IDs that are not inside file paths."""
        mappings: list[VcrMapping] = []
        seen: dict[str, str] = {}

        def _hex_replacer(match: re.Match[str]) -> str:
            original = match.group(0)
            if original not in seen:
                seen[original] = f"__HEX_{len(seen)}__"
            placeholder = seen[original]
            mappings.append(
                VcrMapping(
                    placeholder,
                    original,
                    {
                        "type": "hex_id",
                        "hash": hashlib.sha1(original.encode("utf-8"), usedforsecurity=False).hexdigest()[:8],
                    },
                )
            )
            return placeholder

        result = _SHORT_HEX_PATTERN.sub(_hex_replacer, text)
        return result, mappings

    def _hydrate_ports(self, text: str) -> str:
        """Replace port placeholders with fixed test port numbers."""

        def _replacer(match: re.Match[str]) -> str:
            placeholder = match.group(1)
            if placeholder == PORT_PLACEHOLDER:
                return match.group(0).replace(f":{placeholder}", ":8052")
            # Handle numbered variants __PORT_1__ etc.
            port_num = 8052 + int(placeholder.split("_")[-1].rstrip("_"))
            return match.group(0).replace(f":{placeholder}", f":{port_num}")

        return re.sub(r":(__PORT__(?:_\d+)?__)", _replacer, text)

    def _hydrate_uuids(self, text: str) -> tuple[str, int]:
        """Replace all __UUID__ and numbered variants with seed-based UUIDs."""
        count = 0

        def _uuid_replacer(match: re.Match[str]) -> str:
            nonlocal count
            idx_str = match.group(1)
            idx = int(idx_str) if idx_str else count
            count += 1
            seed_val = (self.seed or 0) * 1000 + idx
            return str(uuid.UUID(int=seed_val % (2**128)))

        # Match __UUID__ alone and __UUID_N__ numbered variants
        result = re.sub(r"__UUID(?:_(\d+))?__", _uuid_replacer, text)
        return result, count

    def _hydrate_numbered_uuids(self, text: str) -> str:
        """Remaining numbered UUID placeholders like __UUID_1__."""

        def _replacer(match: re.Match[str]) -> str:
            idx = int(match.group(1))
            seed_val = (self.seed or 0) * 1000 + idx
            return str(uuid.UUID(int=seed_val % (2**128)))

        return re.sub(r"__UUID_(\d+)__", _replacer, text)

    # ------------------------------------------------------------------
    # File I/O helpers
    # ------------------------------------------------------------------

    def save_fixture(self, dehydrated: str | dict[str, Any], path: Path) -> None:
        """Persist a dehydrated fixture to disk.

        Args:
            dehydrated: Dehydrated string or dict (already processed).
            path: Output file path for the fixture.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(dehydrated, str):
            path.write_text(dehydrated, encoding="utf-8")
        else:
            path.write_text(json.dumps(dehydrated, indent=2), encoding="utf-8")

    def load_and_hydrate(self, path: Path) -> HydrateResult:
        """Load a dehydrated fixture from disk and rehydrate it.

        Args:
            path: Path to the dehydrated fixture file.

        Returns:
            HydrateResult with the replayable fixture.
        """
        content = path.read_text(encoding="utf-8")
        try:
            obj = json.loads(content)
            hydrated_obj = self.hydrate_value(obj)
            return HydrateResult(
                hydrated="",
                mappings_applied=0,
                hypothetical_cost_bytes=len(json.dumps(hydrated_obj).encode("utf-8")),
            )
        except ValueError:
            return self.hydrate(content)
