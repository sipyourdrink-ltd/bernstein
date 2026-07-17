"""Canonical effective-settings hashing in settings_snapshot (#2550).

Two installs with identical effective settings must produce a byte-identical
canonical document and an equal hash, and a drifted setting must be nameable
as a diverging key - the inputs a run receipt and a replay divergence check
rely on.
"""

from __future__ import annotations

from datetime import UTC, datetime

from bernstein.cli.settings_snapshot import (
    SettingsSnapshot,
    SettingValue,
    canonical_settings_document,
    diverging_keys,
    effective_settings_hash,
    effective_settings_values,
)


def _snapshot(values: dict[str, object]) -> SettingsSnapshot:
    return SettingsSnapshot(
        captured_at=datetime(2026, 1, 1, tzinfo=UTC),
        settings={k: SettingValue(key=k, value=v, source="config") for k, v in values.items()},
    )


def test_canonical_document_is_key_order_independent() -> None:
    a = canonical_settings_document({"b": 2, "a": 1})
    b = canonical_settings_document({"a": 1, "b": 2})
    assert a == b
    assert effective_settings_hash({"b": 2, "a": 1}) == effective_settings_hash({"a": 1, "b": 2})


def test_effective_values_extracts_resolved_map() -> None:
    snap = _snapshot({"model": "claude", "budget": 42})
    assert effective_settings_values(snap) == {"model": "claude", "budget": 42}


def test_hash_changes_with_value_and_names_diverging_keys() -> None:
    recorded = {"model": "claude", "budget": 42, "server_url": "https://a"}
    current = {"model": "claude", "budget": 99, "server_url": "https://b"}
    assert effective_settings_hash(recorded) != effective_settings_hash(current)
    assert diverging_keys(recorded, current) == ["budget", "server_url"]


def test_missing_key_counts_as_divergence() -> None:
    assert diverging_keys({"a": 1, "b": 2}, {"a": 1}) == ["b"]
