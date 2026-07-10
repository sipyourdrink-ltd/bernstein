"""AC1 for checkpointed retries (#2359): warm costs fewer input tokens than cold.

A cold retry replays the full task prompt (description, context, prior-failure
digest). A warm retry resumes the native session, so the only new input is the
templated corrective instruction. On the same fixture, per adapter, the warm
retry prompt must estimate to measurably fewer input tokens than the cold one.

Adapters without the capability must receive the full cold prompt unchanged
(AC4: no behavior change).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.adapters._contract import (
    CHECKPOINT_RETRY_CAPABILITY_MATRIX,
    CheckpointRetryCapability,
)
from bernstein.core.tasks.checkpoint_retry import (
    CheckpointRef,
    RetryMode,
    build_retry_prompt,
    decide_retry,
)
from bernstein.core.tokens.token_estimation import estimate_tokens_for_text

# One shared fixture: a realistically-sized cold re-prompt (description plus
# accumulated context) and one failed-gate output for the corrective template.
_COLD_PROMPT = (
    "# Task: implement the frobnicator endpoint\n\n"
    "## Description\n" + ("The endpoint must validate payloads and stream results. " * 120) + "\n\n"
    "## Repository context\n" + ("src/module.py: helper documentation and invariants. " * 80) + "\n\n"
    "## Previous attempt failed\n" + ("Traceback (most recent call last): assertion failed in gate. " * 40)
)
_GATE_NAME = "pytest"
_GATE_OUTPUT = "FAILED tests/test_endpoint.py::test_stream - assert status == 200"

_WARM_CAPABLE = sorted(
    name
    for name, capability in CHECKPOINT_RETRY_CAPABILITY_MATRIX.items()
    if capability is not CheckpointRetryCapability.NONE
)
_COLD_ONLY = sorted(
    name
    for name, capability in CHECKPOINT_RETRY_CAPABILITY_MATRIX.items()
    if capability is CheckpointRetryCapability.NONE
)


def _ref(adapter: str, tmp_path: Path) -> CheckpointRef:
    return CheckpointRef(
        task_id="t1",
        adapter=adapter,
        session_id="sess-abc",
        workspace_hash="ws-hash",
        worktree_path=str(tmp_path),
        journal_index=0,
        event_hash="a" * 64,
    )


@pytest.mark.parametrize("adapter", _WARM_CAPABLE)
def test_warm_retry_costs_fewer_input_tokens_than_cold(adapter: str, tmp_path: Path) -> None:
    decision = decide_retry(
        task_id="t1",
        requested_mode="warm",
        checkpoint=_ref(adapter, tmp_path),
        actual_workspace_hash="ws-hash",
        gate_name=_GATE_NAME,
        gate_output=_GATE_OUTPUT,
    )
    assert decision.effective_mode is RetryMode.WARM
    warm_tokens = estimate_tokens_for_text(build_retry_prompt(decision, cold_prompt=_COLD_PROMPT))
    cold_tokens = estimate_tokens_for_text(_COLD_PROMPT)
    assert warm_tokens < cold_tokens, adapter
    # "Measurably fewer": on this fixture the warm prompt is the corrective
    # instruction only, at most half of the cold re-prompt.
    assert warm_tokens <= cold_tokens / 2, (adapter, warm_tokens, cold_tokens)


@pytest.mark.parametrize("adapter", _COLD_ONLY)
def test_cold_only_adapter_receives_full_prompt_unchanged(adapter: str, tmp_path: Path) -> None:
    decision = decide_retry(
        task_id="t1",
        requested_mode="warm",
        checkpoint=_ref(adapter, tmp_path),
        actual_workspace_hash="ws-hash",
        gate_name=_GATE_NAME,
        gate_output=_GATE_OUTPUT,
    )
    assert decision.effective_mode is RetryMode.COLD
    assert build_retry_prompt(decision, cold_prompt=_COLD_PROMPT) == _COLD_PROMPT
