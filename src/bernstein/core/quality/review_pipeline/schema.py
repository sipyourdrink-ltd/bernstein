"""Pydantic schema for the YAML-driven review pipeline DSL.

A ``review.yaml`` file declares an ordered list of stages.  Each stage runs
N agents in parallel; stage outputs are forwarded to the next stage's
context via the bulletin board (no new IPC).

Schema:

.. code-block:: yaml

    version: 1
    pass_threshold: 0.66
    stages:
      - name: cheap-verifiers
        parallelism: 5
        aggregator:
          strategy: majority         # any | all | majority | weighted
          weights: {gemini: 1.0}      # only for "weighted"
          pass_threshold: 0.5         # optional per-stage override
        agents:
          - role: lint
            model: google/gemini-flash-1.5
            adapter: gemini
            prompt_template: lint.md
            effort: low
      - name: senior-synthesis
        parallelism: 1
        agents:
          - role: senior_reviewer
            model: anthropic/claude-opus-4-5-20250514
            adapter: claude
            prompt_template: senior_synthesis.md
            effort: high

All schema-validation errors carry the originating YAML line so operators
can correct the file without grep'ing.  See :func:`load_pipeline`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

# Aggregator strategy literals.  Mirrors the ticket spec:
# any | all | majority | weighted.
AggregatorStrategy = Literal["any", "all", "majority", "weighted"]

# Effort levels accepted by adapters.
EffortLevel = Literal["low", "medium", "high"]

#: Default pass threshold used when neither stage nor pipeline overrides it.
#: 0.5 matches today's single-pass verifier QUORUM(1, 1) semantics for a
#: one-agent pipeline: any single approve passes.
DEFAULT_PASS_THRESHOLD: float = 0.5


class ReviewPipelineError(ValueError):
    """Raised when ``review.yaml`` is missing, malformed, or invalid.

    Carries the originating path *and* (when available) the offending YAML
    line so operators can locate the problem quickly.
    """

    def __init__(self, path: Path | str, detail: str, line: int | None = None) -> None:
        location = f"{path}:{line}" if line is not None else str(path)
        super().__init__(f"{location}: {detail}")
        self.path = Path(path) if not isinstance(path, Path) else path
        self.detail = detail
        self.line = line


class AggregatorConfig(BaseModel):
    """Aggregation rule for a stage.

    Attributes:
        strategy: How agent verdicts combine into a stage verdict.
        weights: Optional per-agent / per-model weights (0.0-1.0).  Used
            only by ``weighted``.  Keys may be agent ``role`` *or* model
            identifier; runner picks the first match.
        pass_threshold: Per-stage override of the pipeline default.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    strategy: AggregatorStrategy = "majority"
    weights: dict[str, float] = Field(default_factory=dict[str, float])
    pass_threshold: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("weights")
    @classmethod
    def _validate_weights(cls, value: dict[str, float]) -> dict[str, float]:
        for k, v in value.items():
            if v < 0.0 or v > 1.0:
                raise ValueError(f"weight {k!r}={v} must be in [0.0, 1.0]")
        return value


class AgentSpec(BaseModel):
    """Per-agent configuration inside a stage.

    Attributes:
        role: Free-form role tag (e.g. ``lint``, ``security``).  Used for
            audit + as a key into :attr:`AggregatorConfig.weights`.
        model: OpenRouter (or compatible) model identifier.  ``None`` lets
            the cost cascade router pick a model based on stage budget.
        adapter: CLI adapter to spawn (e.g. ``claude``, ``gemini``,
            ``codex``).  ``None`` falls back to whatever ``model`` implies.
        prompt_template: Template name resolved against
            ``templates/review/`` then ``templates/prompts/``.
        effort: Effort hint for the adapter / cost cascade.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    role: str = Field(min_length=1, max_length=64)
    model: str | None = None
    adapter: str | None = None
    prompt_template: str | None = None
    effort: EffortLevel = "low"


class StageSpec(BaseModel):
    """A single stage in the review pipeline.

    Attributes:
        name: Stage identifier; must be unique within a pipeline.
        parallelism: Maximum number of agents that may run concurrently.
            Capped at ``len(agents)`` by the runner.
        agents: List of agents to run for this stage.
        aggregator: How to combine agent verdicts into a stage verdict.
        description: Optional human-readable note (audit / docs only).
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str = Field(min_length=1, max_length=64)
    parallelism: int = Field(default=1, ge=1, le=32)
    agents: list[AgentSpec] = Field(min_length=1)
    aggregator: AggregatorConfig = Field(default_factory=AggregatorConfig)
    description: str | None = None


class ReviewPipeline(BaseModel):
    """Top-level review pipeline configuration.

    Attributes:
        version: Schema version (currently always ``1``).
        pass_threshold: Default fraction of stages that must pass for the
            pipeline to approve.  Stage-level overrides win when set.
        stages: Ordered, sequential stages.  Parallelism lives within a
            stage; we deliberately do not support diamond joins.
        block_on_fail: When True, a failing pipeline blocks the janitor /
            merge gate the same way the cross-model verifier does today.
        name: Optional pipeline name (audit / docs only).
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: int = 1
    pass_threshold: float = Field(default=DEFAULT_PASS_THRESHOLD, ge=0.0, le=1.0)
    stages: list[StageSpec] = Field(min_length=1)
    block_on_fail: bool = True
    name: str | None = None

    @field_validator("stages")
    @classmethod
    def _unique_stage_names(cls, value: list[StageSpec]) -> list[StageSpec]:
        seen: set[str] = set()
        for stage in value:
            if stage.name in seen:
                raise ValueError(f"duplicate stage name {stage.name!r}")
            seen.add(stage.name)
        return value


# ---------------------------------------------------------------------------
# YAML loading with line-aware errors
# ---------------------------------------------------------------------------


def _line_for_pointer(loc: tuple[Any, ...], data: object, raw: str) -> int | None:
    """Best-effort: map a Pydantic error location to a YAML line number.

    Pydantic error locations look like ``("stages", 0, "agents", 1, "role")``.
    We re-parse the YAML stream and walk node tags to grab a line number.
    """
    if not raw:
        return None
    try:
        # Use ``yaml.compose`` to build a node tree that carries marks.
        root = yaml.compose(raw)
    except yaml.YAMLError:
        return None
    if root is None:
        return None
    node: yaml.Node | None = root
    for key in loc:
        if node is None:
            return None
        if isinstance(node, yaml.MappingNode):
            target_node: yaml.Node | None = None
            for k_node, v_node in node.value:
                k_val: object | None = None
                if isinstance(k_node, yaml.ScalarNode):
                    k_val = k_node.value
                if k_val == key:
                    target_node = v_node
                    break
            node = target_node
        elif isinstance(node, yaml.SequenceNode):
            if isinstance(key, int) and 0 <= key < len(node.value):
                node = cast("yaml.Node", node.value[key])
            else:
                return None
        else:
            return None
    if node is None:
        return None
    # PyYAML marks are 0-indexed.
    return node.start_mark.line + 1 if node.start_mark else None


def parse_pipeline_yaml(text: str, *, source: Path | str = "<string>") -> ReviewPipeline:
    """Parse pipeline YAML text into a :class:`ReviewPipeline`.

    Args:
        text: YAML source.
        source: Path used in error messages.

    Returns:
        A validated :class:`ReviewPipeline`.

    Raises:
        ReviewPipelineError: On YAML or schema-validation failure.  When the
            offending location can be mapped to a YAML line, the error
            carries it.
    """
    try:
        raw_data: object = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        # PyYAML errors carry a ``problem_mark`` with line/col info.
        line: int | None = None
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            line = int(mark.line) + 1
        raise ReviewPipelineError(source, f"invalid YAML: {exc}", line) from exc

    if raw_data is None:
        raise ReviewPipelineError(source, "pipeline file is empty", 1)
    if not isinstance(raw_data, dict):
        raise ReviewPipelineError(
            source,
            f"top-level YAML must be a mapping, got {type(raw_data).__name__}",
            1,
        )

    try:
        return ReviewPipeline.model_validate(raw_data)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = first.get("loc", ())
        msg = first.get("msg", "validation error")
        line = _line_for_pointer(tuple(loc), raw_data, text)
        path_str = ".".join(str(p) for p in loc) if loc else "<root>"
        raise ReviewPipelineError(source, f"{path_str}: {msg}", line) from exc


def load_pipeline(path: Path | str) -> ReviewPipeline:
    """Load and validate a review pipeline from disk.

    Args:
        path: Filesystem path to ``review.yaml``.

    Returns:
        Validated :class:`ReviewPipeline`.

    Raises:
        ReviewPipelineError: If the file is missing, unreadable, or invalid.
    """
    p = Path(path)
    if not p.is_file():
        raise ReviewPipelineError(p, "file not found")
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReviewPipelineError(p, f"cannot read file: {exc}") from exc
    return parse_pipeline_yaml(text, source=p)
