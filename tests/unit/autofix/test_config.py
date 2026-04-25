"""Unit tests for the autofix TOML config loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.autofix.config import (
    DEFAULT_COST_CAP_USD,
    DEFAULT_LABEL,
    DEFAULT_LOG_BYTE_BUDGET,
    DEFAULT_POLL_INTERVAL_SECONDS,
    load_config,
)


def test_missing_file_returns_empty_config(tmp_path: Path) -> None:
    """A missing file is not an error; it just yields no repos."""
    config = load_config(tmp_path / "nonexistent.toml")
    assert config.repos == ()
    assert config.poll_interval_seconds == DEFAULT_POLL_INTERVAL_SECONDS
    assert config.log_byte_budget == DEFAULT_LOG_BYTE_BUDGET


def test_valid_toml_parses_into_typed_config(tmp_path: Path) -> None:
    """A well-formed file populates every documented field."""
    cfg_path = tmp_path / "autofix.toml"
    cfg_path.write_text(
        "poll_interval_seconds = 30\n"
        "log_byte_budget = 8192\n"
        "\n"
        "[[repo]]\n"
        'name = "foo/bar"\n'
        "cost_cap_usd = 1.5\n"
        "allow_force_push = true\n"
        "\n"
        "[[repo]]\n"
        'name = "baz/qux"\n',
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    assert cfg.poll_interval_seconds == 30
    assert cfg.log_byte_budget == 8192
    assert len(cfg.repos) == 2

    foo = cfg.repo("foo/bar")
    assert foo is not None
    assert foo.cost_cap_usd == pytest.approx(1.5)
    assert foo.allow_force_push is True
    assert foo.label == DEFAULT_LABEL

    baz = cfg.repo("baz/qux")
    assert baz is not None
    assert baz.cost_cap_usd == DEFAULT_COST_CAP_USD
    assert baz.allow_force_push is False


def test_malformed_toml_raises_value_error(tmp_path: Path) -> None:
    """Operators get a ValueError, not a stack trace, on bad TOML."""
    cfg_path = tmp_path / "autofix.toml"
    cfg_path.write_text("this is = = bad", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(cfg_path)


def test_repo_without_name_is_rejected(tmp_path: Path) -> None:
    """Every [[repo]] entry must declare a non-empty name."""
    cfg_path = tmp_path / "autofix.toml"
    cfg_path.write_text("[[repo]]\nname = ''\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(cfg_path)


def test_negative_cost_cap_is_clamped_to_zero(tmp_path: Path) -> None:
    """Negative cost caps coerce to ``0`` (unlimited) so we never under-budget."""
    cfg_path = tmp_path / "autofix.toml"
    cfg_path.write_text(
        "[[repo]]\nname='a/b'\ncost_cap_usd = -10\n",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    repo = cfg.repo("a/b")
    assert repo is not None
    assert repo.cost_cap_usd == 0.0


def test_invalid_poll_interval_falls_back_to_default(tmp_path: Path) -> None:
    """A zero or negative poll interval is ignored in favour of the default."""
    cfg_path = tmp_path / "autofix.toml"
    cfg_path.write_text("poll_interval_seconds = 0\n", encoding="utf-8")
    cfg = load_config(cfg_path)
    assert cfg.poll_interval_seconds == DEFAULT_POLL_INTERVAL_SECONDS
