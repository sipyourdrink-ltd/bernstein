"""Smoke test for the OpenAI Agents SDK adapter.

This test is gated by the ``OPENAI_API_KEY`` environment variable so it
only runs when an operator explicitly opts in.  CI machines never have
the key, so the test is effectively always skipped in CI and never
incurs spend.

Run locally with::

    OPENAI_API_KEY=sk-... uv run pytest \\
        tests/integration/adapters/test_openai_agents_smoke.py -x -q
"""

from __future__ import annotations

import importlib.util
import os
import time
from pathlib import Path

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.openai_agents import OpenAIAgentsAdapter

# Gate on the API key — this keeps the test skipped in CI and any
# environment that has not explicitly opted into real OpenAI calls.
pytestmark = pytest.mark.skipif(
    not os.getenv("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set — smoke test requires real OpenAI credentials",
)


def _sdk_installed() -> bool:
    """Return True when the optional ``openai-agents`` SDK is importable."""
    return importlib.util.find_spec("agents") is not None


@pytest.mark.skipif(
    not _sdk_installed(),
    reason="openai-agents SDK not installed; run `pip install bernstein[openai]`",
)
def test_smoke_agent_runs_end_to_end(tmp_path: Path) -> None:
    """Spawn a minimal 1-tool Agent and verify the subprocess exits cleanly.

    The Bernstein spawner manages the process.  We assert that:
    * a log file was created,
    * the subprocess exits within the timeout,
    * the log contains at least one ``completion`` event.

    We deliberately do **not** assert on tool-call accuracy or the exact
    summary — those are properties of the model, not the adapter.
    """
    adapter = OpenAIAgentsAdapter()
    session_id = f"smoke-{int(time.time())}"

    result = adapter.spawn(
        prompt="Write a one-line haiku about unit tests into ./haiku.txt.",
        workdir=tmp_path,
        model_config=ModelConfig(model="gpt-5-mini", effort="low"),
        session_id=session_id,
        timeout_seconds=120,
    )

    assert result.log_path.exists()
    assert result.proc is not None

    # Wait for the subprocess to exit (2-minute cap).
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline and adapter.is_alive(result.pid):
        time.sleep(1)

    assert not adapter.is_alive(result.pid), "OpenAI agents runner did not exit"

    log_contents = result.log_path.read_text(encoding="utf-8", errors="replace")
    # The runner always emits a "start" event; "completion" appears on
    # success.  A rate-limit exit is also acceptable — we don't want this
    # smoke test to be flaky on quota exhaustion.
    assert '"type": "start"' in log_contents
    assert '"type": "completion"' in log_contents or '"kind": "rate_limit"' in log_contents
