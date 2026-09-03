"""Unit tests for _infer_adapter_name_for_provider fallback behavior when
the current adapter is not registered in the provider alias registry.

The changed behavior (PR #5348): when no registry match is found and
``registry_name_for(self._adapter)`` returns None (the adapter is not in the
registry), a warning is logged before returning self._adapter.name().
Previously the ``or`` shortcut silently fell through without distinguishing
"registry returned None" from "fallback succeeded".

The structural swap from ``self._adapter.name()`` to
``registry_name_for(self._adapter)`` also changes the registered path: when
the adapter IS in the registry, the fallback must be the registry key, not
the adapter's self-reported name. The two can diverge (a single adapter class
registered under a second key, or an adapter whose public name differs from
its registry key) -- covering that divergence is what this module guards.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bernstein.core.spawner import AgentSpawner


def _make_spawner(tmp_path: Path, adapter_name: str = "unregistered-adapter") -> AgentSpawner:
    adapter = MagicMock()
    adapter.name.return_value = adapter_name
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


def test_registered_adapter_fallback_uses_registry_name_not_self_name(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """When the adapter IS registered, the fallback must be the registry key,
    not the adapter's self-reported name. Structural swap from
    self._adapter.name() to registry_name_for(self._adapter) -- a registered
    adapter whose public name and registry key differ would otherwise be
    misrouted to the public name on the fallback path.

    Patches registry_name_for (the function the swap now calls) to return a
    distinct registry key; if the spawner short-circuits to self._adapter.name()
    on the registered path, this test fails.
    """
    custom_key = "pr1-fallback-registry-key"
    spawner = _make_spawner(tmp_path, adapter_name="publicly-renamed-adapter")
    with patch(
        "bernstein.core.agents.spawner_core.registry_name_for",
        return_value=custom_key,
    ):
        result = spawner._infer_adapter_name_for_provider(
            "totally-unknown-provider", "totally-unknown-model"
        )
    assert result == custom_key, (
        f"expected fallback to registry key {custom_key!r}, got {result!r}; "
        "if the spawner returned the public name, registry_name_for was "
        "not consulted on the registered-adapter path"
    )
    assert not any(
        "not registered" in record.message for record in caplog.records
    ), "no 'not registered' warning expected when the adapter IS registered"

