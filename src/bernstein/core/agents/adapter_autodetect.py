"""Adapter auto-detection (AGENT-015).

Scans PATH for known CLI tools and auto-registers discovered adapters
with the adapter registry.  This allows ``bernstein run`` to work
without explicit adapter configuration when tools are already installed.

Usage::

    results = scan_for_adapters()
    for r in results.found:
        print(f"Found {r.adapter_name} at {r.binary_path}")
    auto_register_adapters()
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Known CLI tool -> adapter mappings
# ---------------------------------------------------------------------------

# Maps binary names to their adapter registry names.
_KNOWN_BINARIES: dict[str, str] = {
    "claude": "claude",
    "codex": "codex",
    "gemini": "gemini",
    "aider": "aider",
    "amp": "amp",
    "qwen": "qwen",
    "cody": "cody",
    "cursor": "cursor",
    "cn": "continue",
    "goose": "goose",
    "kilo": "kilo",
    "kiro": "kiro",
    "ollama": "ollama",
    "opencode": "opencode",
}


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DetectedAdapter:
    """A CLI tool found on PATH.

    Attributes:
        adapter_name: Registry name for the adapter.
        binary_name: Name of the binary found.
        binary_path: Full path to the binary.
    """

    adapter_name: str
    binary_name: str
    binary_path: str


@dataclass
class ScanResult:
    """Result of scanning PATH for adapters.

    Attributes:
        found: Adapters found on PATH.
        missing: Binary names that were not found.
    """

    found: list[DetectedAdapter] = field(default_factory=list[DetectedAdapter])
    missing: list[str] = field(default_factory=list[str])


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


def scan_for_adapters(
    extra_binaries: dict[str, str] | None = None,
) -> ScanResult:
    """Scan PATH for known CLI agent binaries.

    Args:
        extra_binaries: Additional binary_name->adapter_name mappings
            to check beyond the built-in set.

    Returns:
        ScanResult with found and missing adapters.
    """
    binaries = dict(_KNOWN_BINARIES)
    if extra_binaries:
        binaries.update(extra_binaries)

    result = ScanResult()
    for binary_name, adapter_name in sorted(binaries.items()):
        path = shutil.which(binary_name)
        if path:
            result.found.append(
                DetectedAdapter(
                    adapter_name=adapter_name,
                    binary_name=binary_name,
                    binary_path=path,
                )
            )
            logger.debug("Auto-detect: found %s at %s", binary_name, path)
        else:
            result.missing.append(binary_name)

    logger.info(
        "Auto-detect: found %d/%d adapters on PATH",
        len(result.found),
        len(result.found) + len(result.missing),
    )
    return result


def auto_register_adapters(
    extra_binaries: dict[str, str] | None = None,
) -> ScanResult:
    """Scan PATH and register discovered adapters.

    Combines scanning with automatic registration in the adapter registry.
    Only registers adapters that are not already present.

    Args:
        extra_binaries: Additional binary_name->adapter_name mappings.

    Returns:
        ScanResult describing what was found.
    """
    from bernstein.adapters.registry import _ADAPTERS as adapters_map  # pyright: ignore[reportPrivateUsage]
    from bernstein.adapters.registry import get_adapter

    result = scan_for_adapters(extra_binaries=extra_binaries)

    for detected in result.found:
        if detected.adapter_name not in adapters_map:
            try:
                adapter = get_adapter(detected.adapter_name)
                logger.info(
                    "Auto-registered adapter: %s (%s)",
                    detected.adapter_name,
                    adapter.name(),
                )
            except ValueError:
                logger.debug(
                    "Adapter %s found on PATH but not in registry",
                    detected.adapter_name,
                )

    return result
