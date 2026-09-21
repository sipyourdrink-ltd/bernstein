"""Tests for the invoke method in the base CLIAdapter."""

from __future__ import annotations

import pytest

from bernstein.adapters.base import CLIAdapter, SpawnResult

if __name__ == "__main__":
    from pathlib import Path
    from bernstein.core.models import ModelConfig


class MinimalAdapter(CLIAdapter):
    """A minimal adapter that does not override invoke."""

    def name(self) -> str:
        return "minimal"

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
        timeout_seconds: int = 1800,
        task_scope: str = "medium",
        budget_multiplier: float = 1.0,
        system_addendum: str = "",
        multimodal_context: Any | None = None,
    ) -> SpawnResult:
        raise NotImplementedError("spawn not implemented for test")


def test_base_invoke_raises_not_implemented() -> None:
    """Calling invoke on a base adapter subclass that doesn't override it raises NotImplementedError."""
    adapter = MinimalAdapter()
    with pytest.raises(NotImplementedError, match=r".*\.invoke\(\) must be implemented by subclasses"):
        adapter.invoke(model="test-model", input_text="test prompt")