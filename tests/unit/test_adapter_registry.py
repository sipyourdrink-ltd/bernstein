"""Unit tests for bernstein.adapters.registry."""

from __future__ import annotations

import pytest

from bernstein.adapters.claude import ClaudeCodeAdapter
from bernstein.adapters.codex import CodexAdapter
from bernstein.adapters.gemini import GeminiAdapter
from bernstein.adapters.generic import GenericAdapter
from bernstein.adapters.qwen import QwenAdapter
from bernstein.adapters.registry import _ADAPTERS, get_adapter, register_adapter


def teardown_function() -> None:
    """Remove any test-registered adapters after each test."""
    _ADAPTERS.pop("custom", None)
    _ADAPTERS.pop("instance_adapter", None)


def test_get_claude_adapter() -> None:
    adapter = get_adapter("claude")
    assert isinstance(adapter, ClaudeCodeAdapter)


def test_get_codex_adapter() -> None:
    adapter = get_adapter("codex")
    assert isinstance(adapter, CodexAdapter)


def test_get_gemini_adapter() -> None:
    adapter = get_adapter("gemini")
    assert isinstance(adapter, GeminiAdapter)


def test_get_qwen_adapter() -> None:
    adapter = get_adapter("qwen")
    assert isinstance(adapter, QwenAdapter)


def test_get_generic_adapter() -> None:
    adapter = get_adapter("generic")
    assert isinstance(adapter, GenericAdapter)


def test_get_unknown_adapter_raises() -> None:
    with pytest.raises(ValueError, match="Unknown adapter 'foobar'"):
        get_adapter("foobar")


def test_get_unknown_adapter_lists_available() -> None:
    with pytest.raises(ValueError, match="Available:"):
        get_adapter("foobar")


def test_register_and_get_custom_class_adapter() -> None:
    class CustomAdapter(ClaudeCodeAdapter):
        pass

    register_adapter("custom", CustomAdapter)
    adapter = get_adapter("custom")
    assert isinstance(adapter, CustomAdapter)


def test_register_and_get_instance_adapter() -> None:
    instance = GenericAdapter(cli_command="my-cli", display_name="My CLI")
    register_adapter("instance_adapter", instance)
    result = get_adapter("instance_adapter")
    assert result is instance
