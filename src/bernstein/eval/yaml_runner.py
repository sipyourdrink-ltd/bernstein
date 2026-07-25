"""YAML eval harness - operator-runnable spec format with judge and golden diff.

The YAML runner is a thin wrapper around the existing eval primitives in
``bernstein.eval`` (``harness``, ``judge``, ``taxonomy``, ``calibration``).
Its job is integration:

  * Parse and validate an ``EvalSpec`` YAML file with a Pydantic model.
  * Load a JSONL dataset of prompts plus expected-output assertions.
  * Fan each prompt out across the configured list of adapter ids.
  * Score every (prompt, adapter) pair with:
        - a golden-dataset comparison (``expected_output_contains`` /
          ``expected_output_regex``) producing a deterministic pass / fail,
        - an optional LLM-as-judge verdict using a configurable rubric.
  * Aggregate per-metric stats and apply ``thresholds`` from the spec.
  * Emit JSON plus a markdown report and a lineage tag for the run.

The runner is intentionally pure-Python with file I/O at the edges so it
is unit-testable without a network. Adapter execution uses an injectable
``PromptExecutor`` callable - the default mock executor is sufficient for
tests and offline smoke runs; real CLI adapters plug in via the public
``YAMLRunner.run`` API.

The schema is deliberately small and stable - the value is in the integration
with the existing taxonomy and calibration log, not the schema surface.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema - Pydantic models
# ---------------------------------------------------------------------------


class JudgeSpec(BaseModel):
    """Configuration block for the LLM-as-judge.

    Attributes:
        model: LLM model id (passed to the judge backend).
        provider: LLM provider id (passed to the judge backend).
        rubric: Free-form rubric text injected into the judge prompt.
        weight: Multiplier applied to the judge score when aggregating.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = "anthropic/claude-sonnet-4"
    provider: str = "openrouter_free"
    rubric: str = ""
    weight: float = Field(default=1.0, ge=0.0, le=1.0)


class ThresholdSpec(BaseModel):
    """Per-metric thresholds applied to the aggregated run.

    Each threshold is a minimum fraction in ``[0.0, 1.0]``.

    Attributes:
        golden_pass_rate_min: Minimum golden-dataset pass rate.
        judge_score_min: Minimum mean judge score (0.0 - 1.0).
        overall_score_min: Minimum composite score.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    golden_pass_rate_min: float = Field(default=0.0, ge=0.0, le=1.0)
    judge_score_min: float = Field(default=0.0, ge=0.0, le=1.0)
    overall_score_min: float = Field(default=0.0, ge=0.0, le=1.0)


class PromptSpec(BaseModel):
    """Single prompt-level instruction in an eval spec.

    Attributes:
        id: Stable identifier for the prompt within the spec.
        text: Prompt body sent to the adapter.
        expected_output_contains: List of substrings that must appear in
            the output. All must match for a golden pass.
        expected_output_regex: Optional regex that must match the output.
        tags: Free-form labels surfaced in the report.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    text: str
    expected_output_contains: list[str] = Field(default_factory=list)
    expected_output_regex: str | None = None
    tags: list[str] = Field(default_factory=list)

    @field_validator("expected_output_regex")
    @classmethod
    def _validate_regex(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            re.compile(value)
        except re.error as exc:
            msg = f"invalid expected_output_regex: {exc}"
            raise ValueError(msg) from exc
        return value


class EvalSpec(BaseModel):
    """Top-level YAML eval spec.

    Attributes:
        name: Human-readable eval suite name.
        dataset: Optional path to a JSONL file with additional prompts.
        prompts: Inline prompt list (merged with ``dataset`` contents).
        adapters: Adapter ids to fan out across.
        judge: Judge configuration (optional - omit to skip judge scoring).
        thresholds: Per-metric pass thresholds.
        lineage_tag: Free-form tag persisted in the lineage entry.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    dataset: str | None = None
    prompts: list[PromptSpec] = Field(default_factory=list)
    adapters: list[str] = Field(min_length=1)
    judge: JudgeSpec | None = None
    thresholds: ThresholdSpec = Field(default_factory=ThresholdSpec)
    lineage_tag: str = "eval"

    @model_validator(mode="after")
    def _check_has_prompts(self) -> EvalSpec:
        if not self.prompts and not self.dataset:
            msg = "EvalSpec must declare at least one prompt or a dataset path"
            raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# Runtime types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PromptOutcome:
    """Single (prompt, adapter) scored outcome.

    Attributes:
        prompt_id: Identifier from :class:`PromptSpec`.
        adapter: Adapter id this outcome belongs to.
        output: Raw adapter output (string).
        golden_passed: Whether all golden-dataset assertions matched.
        golden_reason: Short reason string for failures (empty on pass).
        judge_score: Optional normalised judge score in ``[0.0, 1.0]``.
        duration_s: Wall-clock seconds for the adapter invocation.
    """

    prompt_id: str
    adapter: str
    output: str
    golden_passed: bool
    golden_reason: str = ""
    judge_score: float | None = None
    duration_s: float = 0.0


@dataclass(frozen=True)
class AdapterAggregate:
    """Aggregated metrics for one adapter across all prompts.

    Attributes:
        adapter: Adapter id.
        prompt_count: Number of prompts evaluated.
        golden_passed: Count of golden passes.
        golden_pass_rate: ``golden_passed / prompt_count``.
        judge_mean: Arithmetic mean of judge scores (0.0 if none recorded).
        overall_score: Weighted composite of golden and judge.
    """

    adapter: str
    prompt_count: int
    golden_passed: int
    golden_pass_rate: float
    judge_mean: float
    overall_score: float


@dataclass(frozen=True)
class RunReport:
    """Final report emitted by the runner.

    Attributes:
        spec_name: ``EvalSpec.name``.
        lineage_tag: ``EvalSpec.lineage_tag``.
        started_at: ISO-8601 UTC timestamp string.
        duration_s: Total wall-clock seconds.
        outcomes: Flat list of per-(prompt, adapter) outcomes.
        per_adapter: One :class:`AdapterAggregate` per adapter id.
        thresholds_passed: Whether every configured threshold was met.
        threshold_failures: Per-threshold pass / fail messages.
    """

    spec_name: str
    lineage_tag: str
    started_at: str
    duration_s: float
    outcomes: tuple[PromptOutcome, ...]
    per_adapter: tuple[AdapterAggregate, ...]
    thresholds_passed: bool
    threshold_failures: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a deterministic dict for JSON serialisation."""
        return {
            "spec_name": self.spec_name,
            "lineage_tag": self.lineage_tag,
            "started_at": self.started_at,
            "duration_s": round(self.duration_s, 4),
            "thresholds_passed": self.thresholds_passed,
            "threshold_failures": list(self.threshold_failures),
            "outcomes": [
                {
                    "prompt_id": o.prompt_id,
                    "adapter": o.adapter,
                    "output": o.output,
                    "golden_passed": o.golden_passed,
                    "golden_reason": o.golden_reason,
                    "judge_score": o.judge_score,
                    "duration_s": round(o.duration_s, 4),
                }
                for o in self.outcomes
            ],
            "per_adapter": [
                {
                    "adapter": a.adapter,
                    "prompt_count": a.prompt_count,
                    "golden_passed": a.golden_passed,
                    "golden_pass_rate": round(a.golden_pass_rate, 4),
                    "judge_mean": round(a.judge_mean, 4),
                    "overall_score": round(a.overall_score, 4),
                }
                for a in self.per_adapter
            ],
        }

    def to_json(self, *, indent: int = 2) -> str:
        """Render the report as a deterministic JSON string."""
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    def to_markdown(self) -> str:
        """Render a human-readable markdown report."""
        lines: list[str] = [
            f"# Eval report: {self.spec_name}",
            "",
            f"- Lineage tag: `{self.lineage_tag}`",
            f"- Started: `{self.started_at}`",
            f"- Duration: `{self.duration_s:.2f}s`",
            f"- Thresholds passed: **{'yes' if self.thresholds_passed else 'no'}**",
            "",
            "## Per-adapter summary",
            "",
            "| Adapter | Prompts | Golden pass | Golden % | Judge mean | Overall |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
        for a in self.per_adapter:
            lines.append(
                f"| {a.adapter} | {a.prompt_count} | {a.golden_passed} | "
                f"{a.golden_pass_rate * 100:.1f}% | {a.judge_mean:.3f} | "
                f"{a.overall_score:.3f} |"
            )

        if self.threshold_failures:
            lines.extend(["", "## Threshold failures", ""])
            for failure in self.threshold_failures:
                lines.append(f"- {failure}")

        lines.extend(
            [
                "",
                "## Per-prompt outcomes",
                "",
                "| Prompt | Adapter | Golden | Judge | Reason |",
                "| --- | --- | :---: | ---: | --- |",
            ]
        )
        for o in self.outcomes:
            judge_str = "-" if o.judge_score is None else f"{o.judge_score:.3f}"
            golden_icon = "pass" if o.golden_passed else "fail"
            lines.append(f"| {o.prompt_id} | {o.adapter} | {golden_icon} | {judge_str} | {o.golden_reason} |")
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Executor / judge protocols
# ---------------------------------------------------------------------------


PromptExecutor = Callable[[str, PromptSpec], str]
"""Synchronous executor: ``(adapter_id, prompt) -> raw_output_string``.

Real callers plug in a function that drives the adapter registry; tests
use :func:`mock_executor` for offline runs.
"""


JudgeFn = Callable[[JudgeSpec, PromptSpec, str], float]
"""Synchronous judge: ``(judge_spec, prompt, output) -> judge_score``.

Score must be in ``[0.0, 1.0]``. The default implementation uses
``bernstein.eval.judge`` when invoked from real CLI runs; tests inject a
deterministic stub.
"""


def mock_executor(adapter: str, prompt: PromptSpec) -> str:
    """Deterministic executor used as the default for tests / dry-runs.

    Echoes the prompt text alongside the adapter id so golden assertions
    can be exercised without a network. Real executors override this.
    """
    return f"[{adapter}] {prompt.text}"


# ---------------------------------------------------------------------------
# Spec loading
# ---------------------------------------------------------------------------


def load_spec(path: Path) -> EvalSpec:
    """Load and validate an :class:`EvalSpec` from a YAML file.

    Args:
        path: Path to the YAML spec file.

    Returns:
        Validated :class:`EvalSpec`.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
        ValueError: If the YAML body fails schema validation.
    """
    if not path.exists():
        msg = f"eval spec not found: {path}"
        raise FileNotFoundError(msg)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        msg = f"eval spec must be a mapping at the top level: {path}"
        raise ValueError(msg)
    return EvalSpec.model_validate(raw)


def load_dataset_jsonl(path: Path) -> list[PromptSpec]:
    """Load a JSONL dataset of prompt entries.

    Each line must be a JSON object that validates against
    :class:`PromptSpec`. Blank lines are skipped.

    Args:
        path: Path to the JSONL dataset.

    Returns:
        Ordered list of :class:`PromptSpec`.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
        ValueError: On JSON decode or schema validation failure.
    """
    if not path.exists():
        msg = f"dataset not found: {path}"
        raise FileNotFoundError(msg)
    prompts: list[PromptSpec] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError as exc:
            msg = f"{path}:{lineno}: invalid JSON: {exc}"
            raise ValueError(msg) from exc
        prompts.append(PromptSpec.model_validate(obj))
    return prompts


def merge_prompts(spec: EvalSpec, *, base_dir: Path) -> list[PromptSpec]:
    """Combine inline ``spec.prompts`` with dataset-loaded prompts.

    Dataset prompts are appended after the inline ones. Prompt ids must
    be unique across both sources.

    Args:
        spec: The validated :class:`EvalSpec`.
        base_dir: Directory the dataset path is resolved against.

    Returns:
        Ordered list of :class:`PromptSpec`.

    Raises:
        ValueError: If duplicate prompt ids are detected.
    """
    prompts: list[PromptSpec] = spec.prompts.copy()
    if spec.dataset:
        dataset_path = Path(spec.dataset)
        if not dataset_path.is_absolute():
            dataset_path = (base_dir / dataset_path).resolve()
        prompts.extend(load_dataset_jsonl(dataset_path))

    seen: set[str] = set()
    duplicates: list[str] = []
    for p in prompts:
        if p.id in seen:
            duplicates.append(p.id)
        seen.add(p.id)
    if duplicates:
        msg = f"duplicate prompt ids: {sorted(set(duplicates))}"
        raise ValueError(msg)
    return prompts


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------


def evaluate_golden(prompt: PromptSpec, output: str) -> tuple[bool, str]:
    """Apply ``expected_output_contains`` and ``expected_output_regex``.

    Args:
        prompt: The :class:`PromptSpec` carrying the assertions.
        output: Raw adapter output.

    Returns:
        ``(passed, reason)`` where ``reason`` is empty on pass.
    """
    for needle in prompt.expected_output_contains:
        if needle not in output:
            return False, f"missing substring {needle!r}"
    if prompt.expected_output_regex is not None and not re.search(prompt.expected_output_regex, output):
        return False, f"regex {prompt.expected_output_regex!r} did not match"
    return True, ""


def aggregate_adapter(
    adapter: str,
    outcomes: list[PromptOutcome],
    judge_weight: float,
) -> AdapterAggregate:
    """Reduce a per-adapter list of outcomes to a single aggregate.

    The composite ``overall_score`` is a weighted sum of the golden pass
    rate and the judge mean. When no judge runs were recorded the judge
    component contributes zero and the golden pass rate dominates.
    """
    n = len(outcomes)
    if n == 0:
        return AdapterAggregate(
            adapter=adapter,
            prompt_count=0,
            golden_passed=0,
            golden_pass_rate=0.0,
            judge_mean=0.0,
            overall_score=0.0,
        )
    passed = sum(1 for o in outcomes if o.golden_passed)
    judge_scores = [o.judge_score for o in outcomes if o.judge_score is not None]
    judge_mean = sum(judge_scores) / len(judge_scores) if judge_scores else 0.0

    golden_rate = passed / n
    if judge_scores:
        clamped_weight = max(0.0, min(judge_weight, 1.0))
        overall = (1.0 - clamped_weight) * golden_rate + clamped_weight * judge_mean
    else:
        overall = golden_rate

    return AdapterAggregate(
        adapter=adapter,
        prompt_count=n,
        golden_passed=passed,
        golden_pass_rate=golden_rate,
        judge_mean=judge_mean,
        overall_score=overall,
    )


def check_thresholds(
    spec: EvalSpec,
    per_adapter: list[AdapterAggregate],
) -> tuple[bool, list[str]]:
    """Apply :class:`ThresholdSpec` to per-adapter aggregates.

    A threshold fails for an adapter when any configured minimum is not
    met. The run is considered passing iff every threshold passes for
    every adapter.
    """
    failures: list[str] = []
    t = spec.thresholds
    for a in per_adapter:
        if t.golden_pass_rate_min > 0 and a.golden_pass_rate < t.golden_pass_rate_min:
            failures.append(
                f"{a.adapter}: golden_pass_rate {a.golden_pass_rate:.3f} < min {t.golden_pass_rate_min:.3f}"
            )
        if t.judge_score_min > 0 and a.judge_mean < t.judge_score_min:
            failures.append(f"{a.adapter}: judge_mean {a.judge_mean:.3f} < min {t.judge_score_min:.3f}")
        if t.overall_score_min > 0 and a.overall_score < t.overall_score_min:
            failures.append(f"{a.adapter}: overall_score {a.overall_score:.3f} < min {t.overall_score_min:.3f}")
    return (len(failures) == 0, failures)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


@dataclass
class YAMLRunner:
    """Execute an :class:`EvalSpec` and emit a :class:`RunReport`.

    Args:
        executor: Adapter invocation callable. Defaults to
            :func:`mock_executor`, which is sufficient for unit tests.
        judge_fn: Optional judge callable. When ``None`` and the spec has
            a ``judge`` block, the judge is skipped silently - real
            callers should inject a judge backed by :mod:`bernstein.eval.judge`.
    """

    executor: PromptExecutor = field(default=mock_executor)
    judge_fn: JudgeFn | None = None

    def run(self, spec: EvalSpec, *, base_dir: Path | None = None) -> RunReport:
        """Execute ``spec`` and return a :class:`RunReport`.

        Args:
            spec: The validated eval spec.
            base_dir: Directory the dataset path is resolved against.
                Defaults to the current working directory.

        Returns:
            A populated :class:`RunReport`.
        """
        started = datetime.now(UTC)
        t0 = time.monotonic()
        prompts = merge_prompts(spec, base_dir=base_dir or Path.cwd())

        outcomes: list[PromptOutcome] = []
        for adapter in spec.adapters:
            for prompt in prompts:
                outcomes.append(self._run_one(adapter, prompt, spec.judge))

        per_adapter: list[AdapterAggregate] = []
        judge_weight = spec.judge.weight if spec.judge else 0.0
        for adapter in spec.adapters:
            adapter_outcomes = [o for o in outcomes if o.adapter == adapter]
            per_adapter.append(aggregate_adapter(adapter, adapter_outcomes, judge_weight))

        thresholds_passed, threshold_failures = check_thresholds(spec, per_adapter)
        duration = time.monotonic() - t0

        return RunReport(
            spec_name=spec.name,
            lineage_tag=spec.lineage_tag,
            started_at=started.isoformat().replace("+00:00", "Z"),
            duration_s=duration,
            outcomes=tuple(outcomes),
            per_adapter=tuple(per_adapter),
            thresholds_passed=thresholds_passed,
            threshold_failures=tuple(threshold_failures),
        )

    def _run_one(
        self,
        adapter: str,
        prompt: PromptSpec,
        judge_spec: JudgeSpec | None,
    ) -> PromptOutcome:
        t0 = time.monotonic()
        output = self.executor(adapter, prompt)
        passed, reason = evaluate_golden(prompt, output)
        judge_score: float | None = None
        if judge_spec is not None and self.judge_fn is not None:
            judge_score = float(self.judge_fn(judge_spec, prompt, output))
            judge_score = max(0.0, min(judge_score, 1.0))
        return PromptOutcome(
            prompt_id=prompt.id,
            adapter=adapter,
            output=output,
            golden_passed=passed,
            golden_reason=reason,
            judge_score=judge_score,
            duration_s=time.monotonic() - t0,
        )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


_RUN_FILE_PREFIX = "yaml_run_"


def runs_dir(state_dir: Path) -> Path:
    """Return the canonical directory for YAML-runner outputs."""
    return state_dir / "eval" / "yaml_runs"


def save_report(
    report: RunReport,
    *,
    state_dir: Path,
    write_markdown: bool = True,
) -> tuple[Path, Path | None]:
    """Persist a :class:`RunReport` as JSON (and optional markdown).

    Args:
        report: The completed run report.
        state_dir: ``.sdd`` directory root.
        write_markdown: When ``True`` also emit a sibling ``.md`` report.

    Returns:
        ``(json_path, md_path_or_None)``.
    """
    out_dir = runs_dir(state_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    safe_name = re.sub(r"[^a-zA-Z0-9_-]+", "_", report.spec_name).strip("_") or "eval"
    stem = f"{_RUN_FILE_PREFIX}{ts}_{safe_name}"
    json_path = out_dir / f"{stem}.json"
    json_path.write_text(report.to_json() + "\n", encoding="utf-8")
    md_path: Path | None = None
    if write_markdown:
        md_path = out_dir / f"{stem}.md"
        md_path.write_text(report.to_markdown(), encoding="utf-8")
    logger.info("YAML eval run saved to %s", json_path)
    return json_path, md_path


def list_runs(state_dir: Path) -> list[Path]:
    """Return all persisted JSON run files, newest first."""
    out_dir = runs_dir(state_dir)
    if not out_dir.is_dir():
        return []
    return sorted(out_dir.glob(f"{_RUN_FILE_PREFIX}*.json"), reverse=True)


def load_report(path: Path) -> dict[str, Any]:
    """Load a previously persisted run report from JSON.

    Returns the raw dict so downstream consumers (e.g. diff) can compare
    runs without re-validating against the dataclass shape.
    """
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class DiffEntry:
    """One per-adapter row in a :class:`DiffReport`."""

    adapter: str
    overall_a: float
    overall_b: float
    overall_delta: float
    golden_rate_a: float
    golden_rate_b: float
    golden_rate_delta: float


@dataclass(frozen=True)
class DiffReport:
    """Comparison of two :class:`RunReport` JSON payloads.

    Attributes:
        run_a: Path to run A (older / baseline).
        run_b: Path to run B (newer / candidate).
        entries: Per-adapter diff rows (deterministic order).
        winner: ``"a"``, ``"b"``, or ``"tie"`` based on overall mean.
    """

    run_a: str
    run_b: str
    entries: tuple[DiffEntry, ...]
    winner: Literal["a", "b", "tie"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_a": self.run_a,
            "run_b": self.run_b,
            "winner": self.winner,
            "entries": [
                {
                    "adapter": e.adapter,
                    "overall_a": round(e.overall_a, 4),
                    "overall_b": round(e.overall_b, 4),
                    "overall_delta": round(e.overall_delta, 4),
                    "golden_rate_a": round(e.golden_rate_a, 4),
                    "golden_rate_b": round(e.golden_rate_b, 4),
                    "golden_rate_delta": round(e.golden_rate_delta, 4),
                }
                for e in self.entries
            ],
        }


def diff_runs(path_a: Path, path_b: Path, *, tolerance: float = 0.01) -> DiffReport:
    """Compare two persisted YAML eval runs.

    Adapters that appear in only one run contribute a zero-baseline entry
    so the diff stays exhaustive. The winner is the run with the higher
    mean overall score across all adapters, with ``tie`` declared inside
    the ``tolerance`` band.
    """
    data_a = load_report(path_a)
    data_b = load_report(path_b)

    def _by_adapter(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {row["adapter"]: row for row in data.get("per_adapter", [])}

    a_map = _by_adapter(data_a)
    b_map = _by_adapter(data_b)
    adapters = sorted(set(a_map) | set(b_map))

    entries: list[DiffEntry] = []
    sum_a = 0.0
    sum_b = 0.0
    for adapter in adapters:
        row_a = a_map.get(adapter, {})
        row_b = b_map.get(adapter, {})
        overall_a = float(row_a.get("overall_score", 0.0))
        overall_b = float(row_b.get("overall_score", 0.0))
        golden_a = float(row_a.get("golden_pass_rate", 0.0))
        golden_b = float(row_b.get("golden_pass_rate", 0.0))
        sum_a += overall_a
        sum_b += overall_b
        entries.append(
            DiffEntry(
                adapter=adapter,
                overall_a=overall_a,
                overall_b=overall_b,
                overall_delta=overall_b - overall_a,
                golden_rate_a=golden_a,
                golden_rate_b=golden_b,
                golden_rate_delta=golden_b - golden_a,
            )
        )

    n = max(1, len(adapters))
    mean_a = sum_a / n
    mean_b = sum_b / n
    winner: Literal["a", "b", "tie"]
    if abs(mean_a - mean_b) <= tolerance:
        winner = "tie"
    elif mean_b > mean_a:
        winner = "b"
    else:
        winner = "a"

    return DiffReport(
        run_a=str(path_a),
        run_b=str(path_b),
        entries=tuple(entries),
        winner=winner,
    )


# ---------------------------------------------------------------------------
# Lineage integration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LineageStub:
    """Lineage entry stub written for runs that lack a recorder.

    The full lineage recorder requires HMAC + Ed25519 material that is
    not present in offline test runs. This stub captures the minimum
    operator-visible fields so a CI gate can still tag the run.
    """

    artefact_path: str
    content_hash: str
    lineage_tag: str
    ts_ns: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "artefact_path": self.artefact_path,
            "content_hash": self.content_hash,
            "lineage_tag": self.lineage_tag,
            "ts_ns": self.ts_ns,
        }


def lineage_stub_for(path: Path, *, lineage_tag: str) -> LineageStub:
    """Build a :class:`LineageStub` from a persisted run JSON path.

    The content hash is sha256 over the file bytes so it is reproducible
    by any auditor with the same file.
    """
    import hashlib

    content = path.read_bytes()
    return LineageStub(
        artefact_path=str(path),
        content_hash="sha256:" + hashlib.sha256(content).hexdigest(),
        lineage_tag=lineage_tag,
        ts_ns=time.time_ns(),
    )


__all__ = [
    "AdapterAggregate",
    "DiffEntry",
    "DiffReport",
    "EvalSpec",
    "JudgeFn",
    "JudgeSpec",
    "LineageStub",
    "PromptExecutor",
    "PromptOutcome",
    "PromptSpec",
    "RunReport",
    "ThresholdSpec",
    "YAMLRunner",
    "aggregate_adapter",
    "check_thresholds",
    "diff_runs",
    "evaluate_golden",
    "lineage_stub_for",
    "list_runs",
    "load_dataset_jsonl",
    "load_report",
    "load_spec",
    "merge_prompts",
    "mock_executor",
    "runs_dir",
    "save_report",
]
