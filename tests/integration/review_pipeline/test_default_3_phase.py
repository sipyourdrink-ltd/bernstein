"""End-to-end test for the default 3-phase review pipeline.

The shipped ``templates/review/default-3-phase.yaml`` is loaded as-is and
run against a fixture diff with a stubbed LLM caller. We verify:

* The pipeline parses cleanly.
* All 7 agents (5 cheap + 1 senior + 1 final-gate) execute.
* Stage outputs propagate to the next stage's prompt.
* The final verdict matches the aggregator outcome.
* HMAC audit captures stage-level breakdown.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.quality.review_pipeline import (
    DiffSource,
    load_pipeline,
    run_pipeline_sync,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PIPELINE_PATH = _REPO_ROOT / "templates" / "review" / "default-3-phase.yaml"


@pytest.fixture()
def fixture_diff() -> DiffSource:
    return DiffSource(
        title="Fix off-by-one in tokenizer",
        description="Closes #123. Adjusts the tokenizer slice bounds.",
        diff="""\
diff --git a/tokenizer.py b/tokenizer.py
@@
- end = len(buf) - 1
+ end = len(buf)
""",
        pr_number=99,
    )


def _approve_text(role: str) -> str:
    return json.dumps({"verdict": "approve", "feedback": f"{role} ok", "issues": []})


def _reject_text(role: str) -> str:
    return json.dumps(
        {
            "verdict": "request_changes",
            "feedback": f"{role} concern",
            "issues": [f"{role} issue"],
        }
    )


def test_default_3_phase_all_approve(fixture_diff: DiffSource) -> None:
    pipeline = load_pipeline(_PIPELINE_PATH)
    seen_models: list[str] = []

    async def caller(*, prompt: str, model: str, **_: object) -> str:
        seen_models.append(model)
        return _approve_text(model)

    verdict = run_pipeline_sync(pipeline, fixture_diff, llm_caller=caller)
    # 5 cheap + 1 senior + 1 final-gate = 7 agents
    assert len(seen_models) == 7
    assert verdict.verdict == "approve"
    assert len(verdict.stages) == 3
    assert verdict.stages[0].total_count == 5
    assert verdict.stages[1].total_count == 1
    assert verdict.stages[2].total_count == 1


def test_default_3_phase_final_gate_blocks(fixture_diff: DiffSource) -> None:
    pipeline = load_pipeline(_PIPELINE_PATH)

    async def caller(*, prompt: str, model: str, **_: object) -> str:
        # Final-gate model rejects; everything else approves.
        if "claude-sonnet-4" in model:
            return _reject_text("gatekeeper")
        return _approve_text(model)

    verdict = run_pipeline_sync(pipeline, fixture_diff, llm_caller=caller)
    assert verdict.verdict == "request_changes"
    assert verdict.stages[-1].verdict == "request_changes"
    assert verdict.block_on_fail is True


def test_default_3_phase_propagates_findings_to_senior(fixture_diff: DiffSource) -> None:
    pipeline = load_pipeline(_PIPELINE_PATH)
    captured: list[str] = []

    async def caller(*, prompt: str, model: str, **_: object) -> str:
        captured.append(prompt)
        # First stage's lint role rejects so senior must see the finding.
        if "gemini-flash-1.5" in model and "lint issue" not in prompt:
            return _reject_text("lint")
        return _approve_text(model)

    run_pipeline_sync(pipeline, fixture_diff, llm_caller=caller)

    # Senior synthesis prompt is the 6th prompt sent (after 5 cheap agents).
    senior_prompt = captured[5]
    assert "Prior stage findings" in senior_prompt
    assert "cheap-verifiers" in senior_prompt


def test_default_3_phase_audit_breakdown(tmp_path: Path, fixture_diff: DiffSource) -> None:
    from bernstein.core.security.audit import AuditLog

    pipeline = load_pipeline(_PIPELINE_PATH)
    key_path = tmp_path / "k.key"
    key_path.write_text("00" * 32)
    key_path.chmod(0o600)
    audit = AuditLog(audit_dir=tmp_path / "audit", key_path=key_path)

    async def caller(*, prompt: str, model: str, **_: object) -> str:
        return _approve_text(model)

    run_pipeline_sync(pipeline, fixture_diff, llm_caller=caller, audit_log=audit)

    events: list[dict[str, Any]] = []
    for log_file in sorted((tmp_path / "audit").glob("*.jsonl")):
        for line in log_file.read_text().splitlines():
            line = line.strip()
            if line:
                events.append(json.loads(line))

    stages_logged = sorted(e["details"]["stage"] for e in events if e["event_type"] == "review_pipeline.stage")
    assert stages_logged == ["cheap-verifiers", "final_gate", "senior_synthesis"]
    # Verify HMAC chain integrity through verify().
    valid, errs = audit.verify()
    assert valid, errs
