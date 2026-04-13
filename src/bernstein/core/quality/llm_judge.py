"""LLM-as-judge evaluation framework for agent output quality.

Provides a structured, multi-dimensional scoring system that uses an LLM
to evaluate agent-produced outputs against configurable quality dimensions.

Each dimension carries a weight, and the final score is the weighted average
of all dimension scores.  The framework builds prompts, parses structured
responses, and renders Markdown scorecards -- but does NOT call any LLM API.
Integration with a concrete LLM backend is handled separately.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JudgeDimension:
    """A single evaluation dimension with a weight in [0, 1].

    Attributes:
        name: Machine-readable identifier (e.g. ``"task_completion"``).
        weight: Relative importance, 0.0-1.0.  Weights across all dimensions
            in a scoring call are normalised to sum to 1.0.
        description: Human-readable explanation shown to the judge LLM.
    """

    name: str
    weight: float
    description: str


@dataclass(frozen=True)
class DimensionScore:
    """Score and reasoning for a single dimension.

    Attributes:
        dimension: The dimension that was scored.
        score: Numeric score in [0.0, 1.0].
        reasoning: One-sentence justification from the judge.
    """

    dimension: JudgeDimension
    score: float
    reasoning: str


@dataclass(frozen=True)
class JudgeResult:
    """Full evaluation result for one task output.

    Attributes:
        task_id: Identifier of the evaluated task.
        overall_score: Weighted average of dimension scores, in [0.0, 1.0].
        dimensions: Per-dimension scores and reasoning.
        model_used: LLM model identifier used for judging.
        cost_usd: Estimated cost of the judge call in USD.
    """

    task_id: str
    overall_score: float
    dimensions: tuple[DimensionScore, ...]
    model_used: str
    cost_usd: float


# ---------------------------------------------------------------------------
# Default dimensions
# ---------------------------------------------------------------------------

DEFAULT_DIMENSIONS: tuple[JudgeDimension, ...] = (
    JudgeDimension(
        name="task_completion",
        weight=0.30,
        description=(
            "Does the output fully satisfy the stated task requirements? "
            "Score 1.0 if every requirement is met, 0.0 if none are."
        ),
    ),
    JudgeDimension(
        name="code_correctness",
        weight=0.25,
        description=(
            "Is the code logically correct and free of bugs? "
            "Consider edge cases, off-by-ones, type mismatches, and runtime errors."
        ),
    ),
    JudgeDimension(
        name="edge_cases",
        weight=0.20,
        description=(
            "Are boundary conditions, error paths, and unusual inputs handled? "
            "Score higher when the code anticipates and guards against edge cases."
        ),
    ),
    JudgeDimension(
        name="maintainability",
        weight=0.15,
        description=(
            "Is the code readable, well-structured, and easy to modify? "
            "Consider naming, modularity, documentation, and complexity."
        ),
    ),
    JudgeDimension(
        name="style",
        weight=0.10,
        description=(
            "Does the code follow project conventions and language idioms? "
            "Consider formatting, naming conventions, and docstring quality."
        ),
    ),
)


def _validate_dimensions(dimensions: Sequence[JudgeDimension]) -> None:
    """Raise ``ValueError`` if dimensions are invalid."""
    if not dimensions:
        msg = "At least one dimension is required."
        raise ValueError(msg)
    names: set[str] = set()
    for dim in dimensions:
        if dim.weight < 0.0:
            msg = f"Dimension {dim.name!r} has negative weight {dim.weight}."
            raise ValueError(msg)
        if dim.name in names:
            msg = f"Duplicate dimension name: {dim.name!r}."
            raise ValueError(msg)
        names.add(dim.name)
    total_weight = sum(d.weight for d in dimensions)
    if total_weight <= 0.0:
        msg = "Total weight of dimensions must be positive."
        raise ValueError(msg)


def _normalise_weights(
    dimensions: Sequence[JudgeDimension],
) -> dict[str, float]:
    """Return dimension name -> normalised weight (summing to 1.0)."""
    total = sum(d.weight for d in dimensions)
    return {d.name: d.weight / total for d in dimensions}


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

_JUDGE_PROMPT_TEMPLATE = """\
You are an expert code reviewer acting as an impartial judge. Evaluate the \
following agent output against the task description on each dimension below.

## Task Description
{task_description}

## Agent Output
{agent_output}

## Evaluation Dimensions
{dimensions_block}

## Output Format
Respond with ONLY a JSON object with exactly this structure:
{{
  "scores": {{
{scores_schema}
  }}
}}

Each score MUST be a number between 0.0 and 1.0 (inclusive).
Each reasoning MUST be a single sentence.
Output ONLY the JSON. No markdown fences. No extra text.
"""

_DIMENSION_ENTRY = """\
- **{name}** (weight {weight:.0%}): {description}"""

_SCORE_ENTRY = '    "{name}": {{"score": <float 0.0-1.0>, "reasoning": "<one sentence>"}}'


def build_judge_prompt(
    task_description: str,
    agent_output: str,
    dimensions: Sequence[JudgeDimension] | None = None,
) -> str:
    """Build the evaluation prompt for the judge LLM.

    Args:
        task_description: What the agent was asked to do.
        agent_output: The agent's response / produced code.
        dimensions: Scoring dimensions.  Defaults to ``DEFAULT_DIMENSIONS``.

    Returns:
        A fully formatted prompt string.
    """
    dims = dimensions if dimensions is not None else DEFAULT_DIMENSIONS
    _validate_dimensions(dims)

    dimensions_block = "\n".join(
        _DIMENSION_ENTRY.format(name=d.name, weight=d.weight, description=d.description)
        for d in dims
    )
    scores_schema = ",\n".join(_SCORE_ENTRY.format(name=d.name) for d in dims)

    return _JUDGE_PROMPT_TEMPLATE.format(
        task_description=task_description,
        agent_output=agent_output,
        dimensions_block=dimensions_block,
        scores_schema=scores_schema,
    )


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def parse_judge_response(
    response_text: str,
    dimensions: Sequence[JudgeDimension] | None = None,
) -> list[DimensionScore]:
    """Parse structured scores from an LLM judge response.

    The expected format is a JSON object with a ``"scores"`` key containing
    per-dimension ``{"score": float, "reasoning": str}`` entries.

    Args:
        response_text: Raw LLM response text.
        dimensions: The dimensions used in the prompt.  Defaults to
            ``DEFAULT_DIMENSIONS``.

    Returns:
        A list of ``DimensionScore`` objects, one per dimension.

    Raises:
        ValueError: If the response cannot be parsed or is missing required
            dimension scores.
    """
    dims = dimensions if dimensions is not None else DEFAULT_DIMENSIONS

    # Strip markdown fences if present
    clean = re.sub(r"^```(?:json)?\s*\n", "", response_text.strip(), flags=re.MULTILINE)
    clean = re.sub(r"\n```\s*$", "", clean.strip(), flags=re.MULTILINE)

    try:
        data: dict[str, object] = json.loads(clean)
    except json.JSONDecodeError:
        # Try extracting JSON from inside the text
        m = re.search(r"\{.*\}", clean, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group())
            except json.JSONDecodeError as exc:
                msg = f"Failed to parse judge response as JSON: {exc}"
                raise ValueError(msg) from exc
        else:
            msg = "No JSON object found in judge response."
            raise ValueError(msg) from None

    scores_raw = data.get("scores")
    if not isinstance(scores_raw, dict):
        msg = "Judge response missing 'scores' key or it is not an object."
        raise ValueError(msg)

    scores_dict = cast("dict[str, Any]", scores_raw)

    results: list[DimensionScore] = []
    for dim in dims:
        entry: object = scores_dict.get(dim.name)
        if not isinstance(entry, dict):
            msg = f"Missing or invalid score entry for dimension {dim.name!r}."
            raise ValueError(msg)

        entry_dict = cast("dict[str, Any]", entry)
        raw_score: object = entry_dict.get("score")
        if not isinstance(raw_score, (int, float)):
            msg = f"Score for {dim.name!r} is not a number."
            raise ValueError(msg)

        score = max(0.0, min(1.0, float(raw_score)))
        reasoning = str(entry_dict.get("reasoning", ""))

        results.append(
            DimensionScore(dimension=dim, score=score, reasoning=reasoning)
        )

    return results


# ---------------------------------------------------------------------------
# Orchestration (placeholder -- no actual LLM call)
# ---------------------------------------------------------------------------


def score_output(
    task_description: str,
    agent_output: str,
    dimensions: Sequence[JudgeDimension] | None = None,
    *,
    task_id: str | None = None,
    model: str = "placeholder/judge-model",
) -> JudgeResult:
    """Orchestrate a full evaluation and return a ``JudgeResult``.

    This builds the judge prompt and returns a placeholder result with zero
    scores.  It does NOT call any LLM API -- that integration is handled
    separately when wiring this into the orchestrator.

    Args:
        task_description: What the agent was asked to do.
        agent_output: The agent's response / produced code.
        dimensions: Scoring dimensions.  Defaults to ``DEFAULT_DIMENSIONS``.
        task_id: Optional task identifier.  Generated if not provided.
        model: LLM model identifier to record in the result.

    Returns:
        A ``JudgeResult`` with placeholder scores (all 0.0).
    """
    dims = dimensions if dimensions is not None else DEFAULT_DIMENSIONS
    _validate_dimensions(dims)

    resolved_task_id = task_id if task_id is not None else str(uuid.uuid4())

    # Build prompt (validates inputs, ensures everything is wired correctly)
    _ = build_judge_prompt(task_description, agent_output, dims)

    # Placeholder dimension scores -- real scoring requires an LLM call
    dim_scores = tuple(
        DimensionScore(dimension=d, score=0.0, reasoning="Placeholder -- no LLM call made.")
        for d in dims
    )

    return JudgeResult(
        task_id=resolved_task_id,
        overall_score=0.0,
        dimensions=dim_scores,
        model_used=model,
        cost_usd=0.0,
    )


# ---------------------------------------------------------------------------
# Markdown report rendering
# ---------------------------------------------------------------------------


def _score_bar(score: float, width: int = 20) -> str:
    """Render a simple text progress bar for a score in [0, 1]."""
    filled = round(score * width)
    empty = width - filled
    return f"[{'#' * filled}{'.' * empty}]"


def _grade(score: float) -> str:
    """Map a 0-1 score to a letter grade."""
    if score >= 0.9:
        return "A"
    if score >= 0.8:
        return "B"
    if score >= 0.7:
        return "C"
    if score >= 0.6:
        return "D"
    return "F"


def render_judge_report(result: JudgeResult) -> str:
    """Render a Markdown scorecard from a ``JudgeResult``.

    Args:
        result: The judge evaluation result.

    Returns:
        A Markdown-formatted report string.
    """
    lines: list[str] = [
        f"# Judge Report -- Task `{result.task_id}`",
        "",
        f"**Overall Score:** {result.overall_score:.2f} / 1.00 "
        f"({_grade(result.overall_score)})",
        f"**Model:** `{result.model_used}`",
        f"**Cost:** ${result.cost_usd:.4f}",
        "",
        "## Dimension Scores",
        "",
        "| Dimension | Weight | Score | Grade | Bar |",
        "|-----------|--------|-------|-------|-----|",
    ]

    weights = _normalise_weights(
        tuple(ds.dimension for ds in result.dimensions)
    ) if result.dimensions else {}

    for ds in result.dimensions:
        w = weights.get(ds.dimension.name, 0.0)
        lines.append(
            f"| {ds.dimension.name} | {w:.0%} | {ds.score:.2f} | "
            f"{_grade(ds.score)} | `{_score_bar(ds.score, 10)}` |"
        )

    lines.extend(["", "## Reasoning", ""])
    for ds in result.dimensions:
        lines.append(f"- **{ds.dimension.name}:** {ds.reasoning}")

    lines.append("")
    return "\n".join(lines)
