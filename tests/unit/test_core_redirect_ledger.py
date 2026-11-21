"""Tests for the core compatibility redirect ledger."""

from __future__ import annotations

from importlib import import_module
from typing import cast

import bernstein.core as core_pkg
from bernstein.core.compat_redirect_ledger import (
    REDIRECT_LEDGER_POLICY,
    REVIEWED_REDIRECT_MAP_DIGEST,
    build_redirect_ledger,
    redirect_map_digest,
)

REDIRECT_MAP = cast(dict[str, str], core_pkg.__dict__["_REDIRECT_MAP"])


def test_redirect_map_matches_reviewed_ledger_digest() -> None:
    """Adding or changing a redirect requires a policy ledger review."""
    assert redirect_map_digest(REDIRECT_MAP) == REVIEWED_REDIRECT_MAP_DIGEST


def test_json_logging_imports_share_one_module() -> None:
    """Both historical paths must expose the module used by the CLI and server."""
    canonical = import_module("bernstein.core.observability.json_logging")
    assert import_module("bernstein.core.json_logging") is canonical
    assert import_module("bernstein.core.server.json_logging") is canonical


def test_redirect_ledger_covers_every_current_redirect() -> None:
    """Every redirected legacy import path must have ledger metadata."""
    ledger = build_redirect_ledger(REDIRECT_MAP)

    assert set(ledger) == set(REDIRECT_MAP)
    for old_path, entry in ledger.items():
        assert entry.old_path == f"bernstein.core.{old_path}"
        assert entry.new_path == REDIRECT_MAP[old_path]
        assert entry.owner == REDIRECT_LEDGER_POLICY.owner
        assert entry.first_release == REDIRECT_LEDGER_POLICY.first_release
        assert entry.removal_policy == REDIRECT_LEDGER_POLICY.removal_policy
