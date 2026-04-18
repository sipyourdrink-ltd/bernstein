"""Regression guard for audit-166.

The ``bernstein.core.communication.discord_bot`` module was orphan
scaffolding with zero production importers (see
``.sdd/backlog/closed/audit-166-discord-bot-parallel-scaffolding.yaml``).

The real Discord integration lives in
``bernstein.core.trigger_sources.discord`` (Ed25519 verification +
payload normalisation) and ``bernstein.core.routes.discord`` (FastAPI
router wired into ``server_app``). This test ensures the orphan module
does not reappear accidentally.
"""

from __future__ import annotations

import importlib

import pytest


def test_discord_bot_module_removed() -> None:
    """Importing the removed scaffolding module must raise ``ModuleNotFoundError``."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("bernstein.core.communication.discord_bot")
