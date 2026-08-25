"""Reviewer-prompt ruleset tests (issue #4481).

AC4 -- guard rules are visible to the reviewer prompt, so an unattended pass
stops re-reporting a finding the operator already rejected.
AC5 -- with no ruleset the prompt is byte-identical to what the pipeline sent
before the ruleset existed.
"""

from __future__ import annotations

import json
from typing import Any

from bernstein.core.quality.review_pipeline import (
    AgentSpec,
    DiffSource,
    ReviewPipeline,
    StageSpec,
    run_pipeline_sync,
)
from bernstein.core.quality.review_pipeline.ruleset import EMPTY_RULESET, parse_ruleset

_RULES = """## Raise

- Bare `except:` that swallows the traceback.

## Guard

- Do not re-report `assert` in tests as a security finding.
"""

_PIPELINE = ReviewPipeline(stages=[StageSpec(name="s", agents=[AgentSpec(role="r", model="m")])])


def _capturing_caller() -> Any:
    prompts: list[str] = []

    async def caller(*, prompt: str, model: str, **_: object) -> str:
        prompts.append(prompt)
        return json.dumps({"verdict": "approve", "feedback": "ok", "issues": []})

    caller.prompts = prompts  # type: ignore[attr-defined]
    return caller


def _run(ruleset: object) -> str:
    caller = _capturing_caller()
    run_pipeline_sync(
        _PIPELINE,
        DiffSource(title="t", description="d", diff="+ x"),
        llm_caller=caller,
        ruleset=ruleset,
    )
    return str(caller.prompts[0])


def test_reviewer_prompt_carries_the_guard_rules() -> None:
    prompt = _run(parse_ruleset(_RULES))

    assert "Do not re-report `assert` in tests as a security finding." in prompt
    assert "Bare `except:` that swallows the traceback." in prompt


def test_reviewer_prompt_is_unchanged_when_the_ruleset_is_empty() -> None:
    assert _run(EMPTY_RULESET) == _run(None)
