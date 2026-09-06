"""Backward-compatibility shim and test helper for snapshot allowlists (#5552, #5503)."""

from __future__ import annotations

from bernstein.testing.ratchet import (
    _branch_changed_files,
    assert_ratchet_matches,
    format_snapshot_snippet,
)

__all__ = [
    "_branch_changed_files",
    "assert_ratchet_matches",
    "format_snapshot_snippet",
]
