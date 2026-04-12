"""Tests for contextual_tips — tips catalog, cooldown, persistence, and display."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from bernstein.tui.contextual_tips import (
    COOLDOWN_SECONDS,
    TipEntry,
    TipsCatalog,
    TipState,
    show_tip,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tip_dir(tmp_path: Path) -> Path:
    """Create a .sdd/tips/ directory under tmp_path."""
    d = tmp_path / ".sdd" / "tips"
    d.mkdir(parents=True)
    return d


@pytest.fixture()
def catalog_paths(tip_dir: Path) -> tuple[Path, Path]:
    """Return (catalog_path, active_path) inside the tip_dir."""
    return tip_dir / "catalog.json", tip_dir / "active.json"


def _write_catalog(path: Path, tips: list[dict[str, str]]) -> None:
    """Write a list of tip dicts to a JSON catalog file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tips), encoding="utf-8")


# ---------------------------------------------------------------------------
# TestTipEntry
# ---------------------------------------------------------------------------


class TestTipEntry:
    def test_to_dict_roundtrips(self) -> None:
        entry = TipEntry(category="general", tip="hello")
        result = TipEntry.from_dict(cast("dict[str, object]", entry.to_dict()))
        assert result.category == "general"
        assert result.tip == "hello"

    def test_from_dict_missing_fields(self) -> None:
        entry = TipEntry.from_dict(cast("dict[str, object]", {}))
        assert entry.category == "general"
        assert entry.tip == ""


# ---------------------------------------------------------------------------
# TestTipState
# ---------------------------------------------------------------------------


class TestTipState:
    def test_to_dict_roundtrips(self) -> None:
        state = TipState(last_seen={"tip_a": 100.0})
        result = TipState.from_dict(cast("dict[str, object]", state.to_dict()))
        assert result.last_seen == {"tip_a": 100.0}

    def test_from_dict_empty(self) -> None:
        assert TipState.from_dict(cast("dict[str, object]", {})).last_seen == {}


# ---------------------------------------------------------------------------
# TestTipsCatalog
# ---------------------------------------------------------------------------


class TestTipsCatalog:
    def test_default_tips_when_no_catalog_file(self, catalog_paths: tuple[Path, Path]) -> None:
        catalog_path, active_path = catalog_paths
        cat = TipsCatalog(catalog_path=catalog_path, active_path=active_path)
        # Should load defaults (~9 tips)
        assert len(cat.get_all_tips()) > 0

    def test_load_custom_catalog(self, tmp_path: Path) -> None:
        catalog_path = tmp_path / "catalog.json"
        active_path = tmp_path / "active.json"
        _write_catalog(
            catalog_path,
            [
                {"category": "custom", "tip": "custom tip"},
                {"category": "general", "tip": "general custom"},
            ],
        )
        cat = TipsCatalog(catalog_path=catalog_path, active_path=active_path)
        tips = cat.get_all_tips()
        assert len(tips) == 2
        assert cat.get_categories() == ["custom", "general"]

    def test_get_tip_returns_random(self, tmp_path: Path) -> None:
        catalog_path = tmp_path / "catalog.json"
        active_path = tmp_path / "active.json"
        _write_catalog(
            catalog_path,
            [
                {"category": "test", "tip": "tip A"},
                {"category": "test", "tip": "tip B"},
            ],
        )
        cat = TipsCatalog(catalog_path=catalog_path, active_path=active_path)
        result = cat.get_tip(category="test", now=0.0)
        assert result in ("tip A", "tip B")

    def test_get_tip_cooldown(self, tmp_path: Path) -> None:
        catalog_path = tmp_path / "catalog.json"
        active_path = tmp_path / "active.json"
        _write_catalog(
            catalog_path,
            [
                {"category": "test", "tip": "only tip"},
            ],
        )
        cat = TipsCatalog(catalog_path=catalog_path, active_path=active_path)

        result1 = cat.get_tip(category="test", now=1000.0)
        assert result1 == "only tip"

        # Within cooldown — should be None
        result2 = cat.get_tip(category="test", now=1000.0 + COOLDOWN_SECONDS - 1)
        assert result2 is None

    def test_get_tip_cooldown_expires(self, tmp_path: Path) -> None:
        catalog_path = tmp_path / "catalog.json"
        active_path = tmp_path / "active.json"
        _write_catalog(
            catalog_path,
            [
                {"category": "test", "tip": "only tip"},
            ],
        )
        cat = TipsCatalog(catalog_path=catalog_path, active_path=active_path)

        cat.get_tip(category="test", now=1000.0)
        # After cooldown — should return tip again
        result = cat.get_tip(category="test", now=1000.0 + COOLDOWN_SECONDS)
        assert result == "only tip"

    def test_get_tip_unknown_category_returns_none(self, tmp_path: Path) -> None:
        catalog_path = tmp_path / "catalog.json"
        active_path = tmp_path / "active.json"
        _write_catalog(catalog_path, [{"category": "test", "tip": "tip"}])
        cat = TipsCatalog(catalog_path=catalog_path, active_path=active_path)
        assert cat.get_tip(category="nonexistent") is None

    def test_add_tip(self, tmp_path: Path) -> None:
        catalog_path = tmp_path / "catalog.json"
        active_path = tmp_path / "active.json"
        _write_catalog(catalog_path, [])
        cat = TipsCatalog(catalog_path=catalog_path, active_path=active_path)
        cat.add_tip("new", "brand new tip")
        assert any(t.tip == "brand new tip" and t.category == "new" for t in cat.get_all_tips())

    def test_add_tip_dedup(self, tmp_path: Path) -> None:
        catalog_path = tmp_path / "catalog.json"
        active_path = tmp_path / "active.json"
        _write_catalog(catalog_path, [{"category": "x", "tip": "same"}])
        cat = TipsCatalog(catalog_path=catalog_path, active_path=active_path)
        cat.add_tip("x", "same")
        assert len(cat.get_all_tips("x")) == 1

    def test_persistence_state_reloaded(self, tmp_path: Path) -> None:
        catalog_path = tmp_path / "catalog.json"
        active_path = tmp_path / "active.json"
        _write_catalog(catalog_path, [{"category": "test", "tip": "persisted"}])

        cat1 = TipsCatalog(catalog_path=catalog_path, active_path=active_path)
        cat1.get_tip(category="test", now=500.0)

        cat2 = TipsCatalog(catalog_path=catalog_path, active_path=active_path)
        # Should still be in cooldown since state was persisted
        assert cat2.get_tip(category="test", now=500.0 + COOLDOWN_SECONDS - 10) is None


# ---------------------------------------------------------------------------
# TestShowTip
# ---------------------------------------------------------------------------


class TestShowTip:
    def test_show_tip_returns_tip_text(self, tmp_path: Path) -> None:
        catalog_path = tmp_path / "catalog.json"
        active_path = tmp_path / "active.json"
        _write_catalog(catalog_path, [{"category": "test", "tip": "visible tip"}])
        cat = TipsCatalog(catalog_path=catalog_path, active_path=active_path)

        result = show_tip(catalog=cat, category="test", now=0.0)
        assert result == "visible tip"

    def test_show_tip_returns_none_when_cooldown(self, tmp_path: Path) -> None:
        catalog_path = tmp_path / "catalog.json"
        active_path = tmp_path / "active.json"
        _write_catalog(catalog_path, [{"category": "test", "tip": "single"}])
        cat = TipsCatalog(catalog_path=catalog_path, active_path=active_path)
        # First call consumes the tip
        show_tip(catalog=cat, category="test", now=0.0)
        # Second call within cooldown returns None
        result = show_tip(catalog=cat, category="test", now=10.0)
        assert result is None
