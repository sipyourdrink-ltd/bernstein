"""Claude Code hooks must POST to the run's server, not the 8052 default.

Regression for 2026-09-02: `_inject_hooks_config` is called without a
server_url, so its hard-coded default won on every run and each hook hit
whatever else held 8052 - which answered 401.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bernstein.adapters.claude import ClaudeCodeAdapter


def _hook_blob(workdir: Path) -> str:
    return json.dumps(json.loads((workdir / ".claude" / "settings.local.json").read_text()))


def test_hooks_use_the_env_server_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_SERVER_URL", "http://127.0.0.1:51814")
    ClaudeCodeAdapter._inject_hooks_config(tmp_path, "adversary-75e81717")
    blob = _hook_blob(tmp_path)
    assert "127.0.0.1:51814/hooks/adversary-75e81717" in blob
    assert "8052" not in blob


def test_trailing_slash_does_not_double_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_SERVER_URL", "http://127.0.0.1:51814/")
    ClaudeCodeAdapter._inject_hooks_config(tmp_path, "s1")
    assert "51814/hooks/s1" in _hook_blob(tmp_path)


def test_explicit_argument_still_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_SERVER_URL", "http://127.0.0.1:51814")
    ClaudeCodeAdapter._inject_hooks_config(tmp_path, "s1", "http://10.0.0.5:9000")
    assert "10.0.0.5:9000/hooks/s1" in _hook_blob(tmp_path)


def test_default_is_unchanged_without_the_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BERNSTEIN_SERVER_URL", raising=False)
    ClaudeCodeAdapter._inject_hooks_config(tmp_path, "s1")
    assert "127.0.0.1:8052/hooks/s1" in _hook_blob(tmp_path)
