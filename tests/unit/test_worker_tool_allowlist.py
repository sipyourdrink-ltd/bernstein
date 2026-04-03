"""Tests for worker tool allowlist per spawn (T578 / T427)."""

from __future__ import annotations

import os
from unittest.mock import patch

from bernstein.core.spawner import (
    build_tool_allowlist_env,
    check_tool_allowed,
    parse_tool_allowlist_env,
)


class TestBuildToolAllowlistEnv:
    def test_builds_env_with_comma_separated_tools(self) -> None:
        env = build_tool_allowlist_env(["read_file", "bash"])
        assert env == {"BERNSTEIN_TOOL_ALLOWLIST": "read_file,bash"}

    def test_empty_list(self) -> None:
        env = build_tool_allowlist_env([])
        # Empty list means no allowlist env (all tools allowed by default)
        assert env == {}

    def test_single_tool(self) -> None:
        env = build_tool_allowlist_env(["bash"])
        assert env == {"BERNSTEIN_TOOL_ALLOWLIST": "bash"}


class TestParseToolAllowlistEnv:
    def test_parses_comma_separated_tools(self) -> None:
        with patch.dict(os.environ, {"BERNSTEIN_TOOL_ALLOWLIST": "read_file,bash,grep"}, clear=False):
            result = parse_tool_allowlist_env()
        assert result == ["read_file", "bash", "grep"]

    def test_returns_none_when_empty(self) -> None:
        with patch.dict(os.environ, {"BERNSTEIN_TOOL_ALLOWLIST": ""}, clear=False):
            result = parse_tool_allowlist_env()
        assert result is None

    def test_returns_none_when_not_set(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BERNSTEIN_TOOL_ALLOWLIST", None)
            result = parse_tool_allowlist_env()
        assert result is None

    def test_strips_whitespace(self) -> None:
        with patch.dict(os.environ, {"BERNSTEIN_TOOL_ALLOWLIST": "  read_file , bash "}, clear=False):
            result = parse_tool_allowlist_env()
        assert result == ["read_file", "bash"]


class TestCheckToolAllowed:
    def test_allows_when_no_allowlist(self) -> None:
        assert check_tool_allowed("bash", None) is True
        assert check_tool_allowed("any_tool", None) is True

    def test_allows_when_in_allowlist(self) -> None:
        assert check_tool_allowed("bash", ["bash", "read_file"]) is True

    def test_denies_when_not_in_allowlist(self) -> None:
        assert check_tool_allowed("delete_file", ["read_file", "grep"]) is False

    def test_empty_allowlist_denies_all(self) -> None:
        assert check_tool_allowed("bash", []) is False
        assert check_tool_allowed("read_file", []) is False

    def test_case_sensitive(self) -> None:
        allowlist = ["read_file"]
        assert check_tool_allowed("read_file", allowlist) is True
        assert check_tool_allowed("Read_File", allowlist) is False
