"""Unit tests for the chat allow-list loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.chat.permissions import AllowList, load_allow_list


def test_allow_list_accepts_known_user() -> None:
    """is_allowed matches on stringified user ids."""
    allow = AllowList(users={"12345"})
    assert allow.is_allowed("12345") is True
    assert allow.is_allowed(12345) is True


def test_allow_list_rejects_unknown_user() -> None:
    """Anything not explicitly allowed must be rejected."""
    allow = AllowList(users={"12345"})
    assert allow.is_allowed("99999") is False


def test_empty_allow_list_denies_everyone() -> None:
    """Default-deny: an empty list must reject every user."""
    allow = AllowList()
    assert allow.is_allowed("anyone") is False


def test_load_from_yaml(tmp_path: Path) -> None:
    """Loader reads chat.allowed_users from yaml."""
    cfg = tmp_path / "bernstein.yaml"
    cfg.write_text(
        "chat:\n  allowed_users:\n    - '12345'\n    - 67890\n",
        encoding="utf-8",
    )
    allow = load_allow_list(cfg)
    assert allow.is_allowed("12345") is True
    assert allow.is_allowed("67890") is True


def test_load_merges_cli_override(tmp_path: Path) -> None:
    """CLI override appends to yaml-provided users."""
    cfg = tmp_path / "bernstein.yaml"
    cfg.write_text("chat:\n  allowed_users: ['yaml-user']\n", encoding="utf-8")
    allow = load_allow_list(cfg, cli_override=["cli-user"])
    assert allow.is_allowed("yaml-user") is True
    assert allow.is_allowed("cli-user") is True


def test_load_merges_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``$BERNSTEIN_CHAT_ALLOW`` contributes comma-separated ids."""
    monkeypatch.setenv("BERNSTEIN_CHAT_ALLOW", "env-one, env-two ,")
    allow = load_allow_list(tmp_path / "missing.yaml")
    assert allow.is_allowed("env-one") is True
    assert allow.is_allowed("env-two") is True
    assert allow.is_allowed("env-three") is False


def test_missing_config_returns_empty_allow_list(tmp_path: Path) -> None:
    """A missing file is not an error -- it simply yields a deny-all list."""
    allow = load_allow_list(tmp_path / "no-such-file.yaml")
    assert allow.is_allowed("anyone") is False
