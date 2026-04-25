"""Schema-parse tests for the review pipeline DSL."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from bernstein.core.quality.review_pipeline import (
    AggregatorConfig,
    ReviewPipeline,
    ReviewPipelineError,
    load_pipeline,
    parse_pipeline_yaml,
)

_GOOD = textwrap.dedent(
    """
    version: 1
    pass_threshold: 0.66
    name: smoke
    stages:
      - name: cheap
        parallelism: 2
        aggregator:
          strategy: majority
        agents:
          - role: lint
            model: google/gemini-flash-1.5
            adapter: gemini
            prompt_template: lint.md
            effort: low
          - role: tests
            model: google/gemini-flash-1.5
            adapter: gemini
            prompt_template: tests.md
            effort: low
      - name: senior
        parallelism: 1
        aggregator:
          strategy: any
        agents:
          - role: senior
            model: anthropic/claude-opus-4-5-20250514
            adapter: claude
            prompt_template: senior.md
            effort: high
    """
).strip()


def test_parse_good_pipeline() -> None:
    pipeline = parse_pipeline_yaml(_GOOD, source="<test>")
    assert isinstance(pipeline, ReviewPipeline)
    assert pipeline.name == "smoke"
    assert pipeline.pass_threshold == pytest.approx(0.66)
    assert len(pipeline.stages) == 2
    cheap = pipeline.stages[0]
    assert cheap.parallelism == 2
    assert cheap.aggregator.strategy == "majority"
    assert [a.role for a in cheap.agents] == ["lint", "tests"]


def test_unknown_keys_rejected() -> None:
    bad = _GOOD + "\nbogus_key: 1\n"
    with pytest.raises(ReviewPipelineError) as excinfo:
        parse_pipeline_yaml(bad, source="<bad>")
    assert "bogus_key" in str(excinfo.value)


def test_duplicate_stage_names_rejected() -> None:
    dup = textwrap.dedent(
        """
        stages:
          - name: a
            agents:
              - role: r
          - name: a
            agents:
              - role: r
        """
    ).strip()
    with pytest.raises(ReviewPipelineError) as excinfo:
        parse_pipeline_yaml(dup, source="<dup>")
    assert "duplicate stage name" in str(excinfo.value)


def test_invalid_aggregator_strategy_pinpoints_line() -> None:
    bad = textwrap.dedent(
        """
        stages:
          - name: a
            aggregator:
              strategy: nonsense
            agents:
              - role: r
        """
    ).strip()
    with pytest.raises(ReviewPipelineError) as excinfo:
        parse_pipeline_yaml(bad, source="<bad>")
    err = str(excinfo.value)
    # Must mention the offending line (the strategy line is line 4 in the
    # stripped block).
    assert ":4:" in err or ":5:" in err
    assert "strategy" in err


def test_invalid_yaml_carries_line() -> None:
    bad = "stages:\n  - name: 'unterminated"
    with pytest.raises(ReviewPipelineError) as excinfo:
        parse_pipeline_yaml(bad, source="<broken>")
    assert "invalid YAML" in str(excinfo.value)


def test_load_pipeline_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ReviewPipelineError) as excinfo:
        load_pipeline(tmp_path / "missing.yaml")
    assert "file not found" in str(excinfo.value)


def test_aggregator_weights_in_range() -> None:
    cfg = AggregatorConfig(strategy="weighted", weights={"lint": 0.5, "tests": 1.0})
    assert cfg.weights["lint"] == pytest.approx(0.5)


def test_aggregator_weights_out_of_range_rejected() -> None:
    with pytest.raises(ValueError, match=r"weight 'bad'=2.0"):
        AggregatorConfig(strategy="weighted", weights={"bad": 2.0})


def test_loads_default_3_phase_template() -> None:
    """The shipped default-3-phase pipeline must be valid."""
    repo_root = Path(__file__).resolve().parents[4]
    template = repo_root / "templates" / "review" / "default-3-phase.yaml"
    pipeline = load_pipeline(template)
    assert pipeline.name == "default-3-phase"
    assert len(pipeline.stages) == 3
    assert pipeline.stages[0].parallelism == 5
    assert len(pipeline.stages[0].agents) == 5
