"""Adapters for different CLI agents (Claude Code, Codex, Gemini, etc.) and scanner tools."""

from .scanner import (
    DeterminismTier,
    OutputFormat,
    ScannerAdapter,
    ScannerCategory,
    ScanResult,
    ScanScope,
)
from .scanner_finding import Finding
from .scanner_registry import (
    get_scanner,
    iter_scanner_specs,
    register_scanner,
    scanner_name_for_provider,
)

__all__ = [
    "DeterminismTier",
    "Finding",
    "OutputFormat",
    "ScanResult",
    "ScanScope",
    "ScannerAdapter",
    "ScannerCategory",
    "get_scanner",
    "iter_scanner_specs",
    "register_scanner",
    "scanner_name_for_provider",
]
