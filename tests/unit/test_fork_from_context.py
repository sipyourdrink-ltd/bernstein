"""Tests for the forked-agent pattern in quality_gates (fork_from_context)."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
from bernstein.core.models import Complexity, Scope, Task
from bernstein.core.quality_gates import (
    _FORK_CONTEXT_MAX_CHARS,
    IntentVerificationConfig,
    _verify_intent_async,
)


def _make_task(*, id: str = "T-fork-001") -> Task:
    return Task(
        id=id,
        title="Add login feature",
        description="Implement user login with JWT.",
        role="backend",
        scope=Scope.SMALL,
        complexity=Complexity.LOW,
    )


class TestForkFromContext:
    """Verify that fork_from_context prepends parent context to the prompt."""

    @pytest.mark.asyncio
    async def test_prompt_includes_parent_context(self, tmp_path: Path) -> None:
        """When fork_from_context is set, the LLM receives the context prefix."""
        parent_ctx = "User is authenticated. JWT secret is in env."
        config = IntentVerificationConfig(
            enabled=True,
            fork_from_context=parent_ctx,
        )
        task = _make_task()

        captured_prompts: list[str] = []

        async def fake_call_llm(**kwargs: object) -> str:
            await asyncio.sleep(0)  # Async interface requirement
            captured_prompts.append(str(kwargs.get("prompt", "")))
            return '{"verdict": "yes", "reason": "All good."}'

        with (
            patch(
                "bernstein.core.quality_gates._get_intent_diff",
                return_value="diff output",
            ),
            patch(
                "bernstein.core.llm.call_llm",
                side_effect=fake_call_llm,
            ),
        ):
            await _verify_intent_async(task, tmp_path, config)

        assert len(captured_prompts) == 1
        prompt = captured_prompts[0]
        assert "## Agent Session Context" in prompt
        assert parent_ctx in prompt
        # Verification body should also be present
        assert "## Original Task" in prompt

    @pytest.mark.asyncio
    async def test_no_context_prefix_without_fork(self, tmp_path: Path) -> None:
        """Without fork_from_context, the prompt does NOT contain the header."""
        config = IntentVerificationConfig(enabled=True)
        task = _make_task()

        captured_prompts: list[str] = []

        async def fake_call_llm(**kwargs: object) -> str:
            await asyncio.sleep(0)  # Async interface requirement
            captured_prompts.append(str(kwargs.get("prompt", "")))
            return '{"verdict": "yes", "reason": "Fine."}'

        with (
            patch(
                "bernstein.core.quality_gates._get_intent_diff",
                return_value="some diff",
            ),
            patch(
                "bernstein.core.llm.call_llm",
                side_effect=fake_call_llm,
            ),
        ):
            await _verify_intent_async(task, tmp_path, config)

        assert len(captured_prompts) == 1
        assert "## Agent Session Context" not in captured_prompts[0]

    @pytest.mark.asyncio
    async def test_context_truncated_to_max_chars(self, tmp_path: Path) -> None:
        """fork_from_context is truncated to _FORK_CONTEXT_MAX_CHARS."""
        long_ctx = "x" * (_FORK_CONTEXT_MAX_CHARS + 500)
        config = IntentVerificationConfig(
            enabled=True,
            fork_from_context=long_ctx,
        )
        task = _make_task()

        captured_prompts: list[str] = []

        async def fake_call_llm(**kwargs: object) -> str:
            await asyncio.sleep(0)  # Async interface requirement
            captured_prompts.append(str(kwargs.get("prompt", "")))
            return '{"verdict": "yes", "reason": "ok"}'

        with (
            patch(
                "bernstein.core.quality_gates._get_intent_diff",
                return_value="diff",
            ),
            patch(
                "bernstein.core.llm.call_llm",
                side_effect=fake_call_llm,
            ),
        ):
            await _verify_intent_async(task, tmp_path, config)

        prompt = captured_prompts[0]
        # The truncated context must appear, but not the full oversized string
        assert "x" * _FORK_CONTEXT_MAX_CHARS in prompt
        assert "x" * (_FORK_CONTEXT_MAX_CHARS + 1) not in prompt

    def test_config_default_fork_is_none(self) -> None:
        """fork_from_context defaults to None."""
        cfg = IntentVerificationConfig()
        assert cfg.fork_from_context is None

    def test_config_accepts_fork_context(self) -> None:
        """IntentVerificationConfig accepts a non-None fork_from_context."""
        ctx = "some parent context"
        cfg = IntentVerificationConfig(fork_from_context=ctx)
        assert cfg.fork_from_context == ctx
