"""Unit tests for _infer_adapter_name_for_provider fallback behavior when
the current adapter is not registered in the provider alias registry.

The changed behavior (PR #5348): when no registry match is found and
``registry_name_for(self._adapter)`` returns None (the adapter is not in the
registry), a warning is logged before returning self._adapter.name().
Previously the ``or`` shortcut silently fell through without distinguishing
"registry returned None" from "fallback succeeded".
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bernstein.core.spawner import AgentSpawner


def _make_spawner(tmp_path: Path) -> AgentSpawner:
    adapter = MagicMock()
    adapter.name.return_value = "unregistered-adapter"
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir(parents=True, exist_ok=True)
    return AgentSpawner(adapter, templates_dir, tmp_path)


def test_unregistered_adapter_fallback_returns_adapter_name_and_logs_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """When registry_name_for returns None (adapter not registered), the
    fallback still returns self._adapter.name() but now also emits a warning
    so operators can distinguish 'no registry entry' from 'no match at all'."""
    spawner = _make_spawner(tmp_path)
    result = spawner._infer_adapter_name_for_provider(
        "unknown-provider", "unknown-model"
    )
    assert result == "unregistered-adapter"
    assert any(
        "no registry match" in record.message and "not registered" in record.message
        for record in caplog.records
    ), "Expected a warning about the adapter not being registered in the registry"


