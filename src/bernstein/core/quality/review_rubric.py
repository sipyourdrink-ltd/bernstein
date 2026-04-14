"""Multi-dimensional code review scoring rubric for agent-produced diffs.

Scores every agent-produced diff on five dimensions:
  - style_compliance   (0-10)
  - correctness        (0-10)
  - performance_impact (0-10)
  - security           (0-10)
  - maintainability    (0-10)

Aggregates these into a composite score (0-10). Tasks below the configured
threshold trigger automatic rework or human review.

This is distinct from quality_score.py which provides a single binary
pass/fail score from gate results — the rubric provides nuanced, per-dimension
feedback from an LLM reviewer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.core.defaults import GATE

if TYPE_CHECKING:
    from bernstein.core.models import Task

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "google/gemini-flash-1.5"
_DEFAULT_PROVIDER = "openrouter"
_MAX_DIFF_CHARS = GATE.review_max_diff_chars
_MAX_TOKENS = GATE.review_max_tokens

# Composite score weights (must sum to 1.0)
_DIMENSION_WEIGHTS: dict[str, float] = {
    "style_compliance": 0.15,
    "correctness": 0.35,
    "performance_impact": 0.15,
    "security": 0.25,
    "maintainability": 0.10,
}

_PROMPT_TEMPLATE = """\
You are a senior software engineer performing a structured code review. \
Evaluate the following diff on five dimensions. Each dimension is scored \
0-10 where 10 is perfect and 0 is unacceptable.

## Task Description
**Title:** {title}
**Description:**
{description}

## Git Diff
```diff
{diff}
```

## Scoring Dimensions
- **style_compliance** (0-10): Adherence to PEP 8, project conventions, naming, \
formatting, docstrings. 10 = perfectly clean. 0 = egregiously violates style.
- **correctness** (0-10): Does the code correctly implement the described \
requirement? Are there logic errors, off-by-ones, or missing edge cases? \
10 = fully correct. 0 = broken.
- **performance_impact** (0-10): Does the change introduce unnecessary \
complexity, N+1 queries, inefficient algorithms, or memory leaks? \
10 = no regression or net improvement. 0 = severe performance regression.
- **security** (0-10): Does the diff introduce injection vectors, secret \
exposure, improper auth, or other OWASP Top 10 issues? \
10 = no security concerns. 0 = critical vulnerability introduced.
- **maintainability** (0-10): Is the code readable, modular, and easy to \
change? Does it add technical debt? 10 = highly maintainable. \
0 = impossible to maintain.

## Output Format
Respond with ONLY a JSON object with exactly these keys:
{{
  "style_compliance": <int 0-10>,
  "correctness": <int 0-10>,
  "performance_impact": <int 0-10>,
  "security": <int 0-10>,
  "maintainability": <int 0-10>,
  "feedback": {{
    "style_compliance": "<one sentence>",
    "correctness": "<one sentence>",
    "performance_impact": "<one sentence>",
    "security": "<one sentence>",
    "maintainability": "<one sentence>"
  }},
  "summary": "<two sentence overall assessment>"
}}

Output ONLY the JSON. No markdown fences. No extra text.
"""


@dataclass(frozen=True)
class ReviewRubricConfig:
    """Configuration for the multi-dimensional review rubric gate.

    Attributes:
        enabled: Master switch — when False, the gate does not run.
        model: LLM model for review scoring.
        provider: LLM provider key.
        max_diff_chars: Truncate diff at this length for cost control.
        max_tokens: Token cap for LLM response.
        composite_threshold: Minimum composite score (0.0-10.0) to pass.
            Tasks scoring below this trigger rework or human review.
        block_below_threshold: When True, tasks below the threshold are
            blocked (require rework). When False, they emit a warning only.
        rework_threshold: Composite score below which automatic rework is
            triggered (if supported by orchestrator). Must be <= composite_threshold.
    """

    enabled: bool = False
    model: str = _DEFAULT_MODEL
    provider: str = _DEFAULT_PROVIDER
    max_diff_chars: int = _MAX_DIFF_CHARS
    max_tokens: int = _MAX_TOKENS
    composite_threshold: float = 6.0
    block_below_threshold: bool = True
    rework_threshold: float = 4.0


@dataclass
class DimensionScore:
    """Score and feedback for one rubric dimension.

    Attributes:
        name: Dimension name (e.g. "correctness").
        score: Numeric score 0-10.
        feedback: One-sentence explanation.
    """

    name: str
    score: int
    feedback: str


@dataclass
class RubricResult:
    """Full multi-dimensional review rubric result.

    Attributes:
        composite: Weighted composite score 0.0-10.0.
        dimensions: Per-dimension scores and feedback.
        summary: Overall qualitative assessment.
        passed: True if composite >= composite_threshold.
        blocked: True if the gate blocks task completion.
        needs_rework: True if composite < rework_threshold.
        detail: Human-readable one-liner for gate reports.
        raw_response: Raw LLM JSON string for debugging.
        errors: Any errors encountered during scoring.
    """

    composite: float
    dimensions: list[DimensionScore]
    summary: str
    passed: bool
    blocked: bool
    needs_rework: bool
    detail: str
    raw_response: str = ""
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        return {
            "composite": self.composite,
            "dimensions": {d.name: {"score": d.score, "feedback": d.feedback} for d in self.dimensions},
            "summary": self.summary,
            "passed": self.passed,
            "blocked": self.blocked,
            "needs_rework": self.needs_rework,
        }


def _compute_composite(scores: dict[str, int]) -> float:
    """Compute a weighted composite score from per-dimension integer scores."""
    weight_sum = sum(_DIMENSION_WEIGHTS.get(dim, 0.0) for dim in scores)
    if math.isclose(weight_sum, 0.0, abs_tol=1e-9):
        return 0.0
    weighted = sum(scores.get(dim, 0) * w for dim, w in _DIMENSION_WEIGHTS.items() if dim in scores)
    return round(weighted / weight_sum, 2)


def _parse_rubric_response(raw: str) -> tuple[dict[str, int], dict[str, str], str]:
    """Parse LLM JSON response into (scores, feedbacks, summary).

    Returns default-zero dicts on parse failure.
    """
    # Strip markdown fences if LLM added them despite instructions
    clean = re.sub(r"^```(?:json)?\s*\n", "", raw.strip(), flags=re.MULTILINE)
    clean = re.sub(r"\n```\s*$", "", clean.strip(), flags=re.MULTILINE)

    try:
        data: dict[str, Any] = json.loads(clean)
    except json.JSONDecodeError:
        # Try extracting JSON from inside the text
        m = re.search(r"\{.*\}", clean, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group())
            except json.JSONDecodeError:
                return {}, {}, ""
        else:
            return {}, {}, ""

    dimensions = list(_DIMENSION_WEIGHTS.keys())
    scores: dict[str, int] = {}
    for dim in dimensions:
        raw_val = data.get(dim)
        if isinstance(raw_val, (int, float)):
            scores[dim] = max(0, min(10, int(raw_val)))
        else:
            scores[dim] = 5  # neutral default on parse failure

    feedback_raw = data.get("feedback", {})
    feedbacks: dict[str, str] = {}
    for dim in dimensions:
        feedbacks[dim] = str(feedback_raw.get(dim, "")) if isinstance(feedback_raw, dict) else ""

    summary = str(data.get("summary", ""))
    return scores, feedbacks, summary


async def _fetch_diff(run_dir: Path) -> tuple[str, list[str]]:
    """Fetch the Python diff from git. Returns (diff_text, errors)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "diff",
            "HEAD~1",
            "--",
            "*.py",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=run_dir,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        diff = stdout.decode() if stdout else ""
        if not diff.strip():
            proc = await asyncio.create_subprocess_exec(
                "git",
                "diff",
                "--cached",
                "--",
                "*.py",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=run_dir,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            diff = stdout.decode() if stdout else ""
        return diff, []
    except Exception as exc:
        return "", [f"Failed to get diff: {exc}"]


async def score_diff(
    task: Task,
    run_dir: Path,
    config: ReviewRubricConfig,
    *,
    diff: str | None = None,
) -> RubricResult:
    """Score a diff on all rubric dimensions and return the full result.

    Args:
        task: The completed task being reviewed.
        run_dir: Repository root.
        config: Rubric configuration.
        diff: Optional pre-computed diff. If None, computed from HEAD~1.

    Returns:
        RubricResult with per-dimension scores and composite score.
    """
    from bernstein.core.llm import call_llm

    errors: list[str] = []

    if diff is None:
        diff, diff_errors = await _fetch_diff(run_dir)
        errors.extend(diff_errors)

    if not diff.strip():
        return RubricResult(
            composite=10.0,
            dimensions=[],
            summary="No Python changes detected.",
            passed=True,
            blocked=False,
            needs_rework=False,
            detail="No Python changes to review — rubric skipped.",
        )

    diff_truncated = diff[: config.max_diff_chars]
    title = getattr(task, "title", task.id)
    description = getattr(task, "description", "") or ""

    prompt = _PROMPT_TEMPLATE.format(
        title=title,
        description=description[:2000],
        diff=diff_truncated,
    )

    # 2. Call LLM
    try:
        raw = await call_llm(
            prompt,
            model=config.model,
            provider=config.provider,
            max_tokens=config.max_tokens,
            temperature=0.1,
        )
    except Exception as exc:
        errors.append(f"LLM call failed: {exc}")
        return RubricResult(
            composite=0.0,
            dimensions=[],
            summary="",
            passed=False,
            blocked=config.block_below_threshold,
            needs_rework=True,
            detail=f"Review rubric gate failed (LLM error): {exc}",
            errors=errors,
        )

    # 3. Parse response
    scores, feedbacks, summary = _parse_rubric_response(raw)
    if not scores:
        errors.append("Failed to parse rubric scores from LLM response.")

    composite = _compute_composite(scores) if scores else 0.0
    dimensions = [
        DimensionScore(name=dim, score=scores.get(dim, 0), feedback=feedbacks.get(dim, ""))
        for dim in _DIMENSION_WEIGHTS
    ]

    passed = composite >= config.composite_threshold
    blocked = config.block_below_threshold and not passed
    needs_rework = composite < config.rework_threshold

    if passed:
        detail = f"Review rubric PASSED — composite {composite:.1f}/10. {summary[:120]}"
    else:
        low_dims = [d for d in dimensions if d.score < 5]
        low_str = ", ".join(f"{d.name}={d.score}" for d in low_dims)
        detail = (
            f"Review rubric FAILED — composite {composite:.1f}/10 "
            f"(threshold {config.composite_threshold}). "
            f"Low scores: {low_str}. {summary[:100]}"
        )

    return RubricResult(
        composite=composite,
        dimensions=dimensions,
        summary=summary,
        passed=passed,
        blocked=blocked,
        needs_rework=needs_rework,
        detail=detail,
        raw_response=raw,
        errors=errors,
    )


def run_review_rubric_sync(
    task: Task,
    run_dir: Path,
    config: ReviewRubricConfig,
    *,
    diff: str | None = None,
) -> RubricResult:
    """Synchronous wrapper for score_diff (for use in gate_runner)."""
    import asyncio

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, score_diff(task, run_dir, config, diff=diff))
                return future.result(timeout=120)
        else:
            return loop.run_until_complete(score_diff(task, run_dir, config, diff=diff))
    except Exception as exc:
        logger.exception("Review rubric gate crashed: %s", exc)
        return RubricResult(
            composite=0.0,
            dimensions=[],
            summary="",
            passed=False,
            blocked=config.block_below_threshold,
            needs_rework=True,
            detail=f"Review rubric gate error: {exc}",
            errors=[str(exc)],
        )


class RubricHistoryWriter:
    """Append rubric results to a JSONL history file for trend analysis."""

    HISTORY_FILE = Path(".sdd/metrics/review_rubric.jsonl")

    def __init__(self, workdir: Path) -> None:
        self._path = workdir / self.HISTORY_FILE

    def record(self, task_id: str, result: RubricResult) -> None:
        """Append a rubric result record to the history file."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        event: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "task_id": task_id,
            **result.as_dict(),
        }
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, sort_keys=True) + "\n")
