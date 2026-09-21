"""Tests for the invoke method in GenericAdapter."""

from __future__ import annotations

import pytest

from bernstein.adapters.generic import GenericAdapter
from bernstein.core.models import ModelConfig


def test_generic_invoke_raises_not_implemented() -> None:
    """Calling invoke on GenericAdapter raises NotImplementedError with appropriate message."""
    adapter = GenericAdapter(cli_command="echo")
    with pytest.raises(
        NotImplementedError,
        match=r"GenericAdapter does not support direct model invocation\. "
              r"Use spawn\(\) for full agent processes instead\."
    ):
        adapter.invoke(
            model="test-model",
            input_text="test prompt",
            parameters={"temperature": 0.7}
        )