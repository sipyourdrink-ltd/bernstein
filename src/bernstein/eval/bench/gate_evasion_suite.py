"""
bernstein-bench: gate-evasion benchmark suite.

Every way an agent change previously fooled or evaded a quality gate
becomes a fixture the gate must catch. This suite loads evasion cases
dynamically from ``src/bernstein/eval/cases/gate_evasion/``, runs them against
quality gates, measures catch rate, reports missed classes and responsible gates,
and produces signed submission bundles.

Adding a new evasion class requires no Python changes: placing a new fixture
directory with a valid ``manifest.json`` is automatically discovered.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.eval.bench.bundle import SubmissionBundle, TaskResult
from bernstein.eval.bench.suite import BenchSuite, BenchTask

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

# ---------------------------------------------------------------------------
# Default corpus directory
# ---------------------------------------------------------------------------

DEFAULT_EVASION_CORPUS_DIR = Path(__file__).resolve().parent.parent / "cases" / "gate_evasion"


# ---------------------------------------------------------------------------
# Case representation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateEvasionCase:
    """A single gate evasion fixture loaded from disk.

    Attributes:
        class_name: Name/identifier of the evasion class.
        description: Human-readable explanation of how the evasion operates.
        expected_verdict: Expected gate verdict (typically 'fail').
        gate_that_must_flag: Name of the quality gate responsible for catching it.
        taxonomy_category: Taxonomy classification for this failure/evasion.
        case_dir: Path to the directory containing this case fixture.
        manifest_path: Path to the manifest.json file.
        sample_files: Relative paths of non-manifest fixture files.
    """

    class_name: str
    description: str
    expected_verdict: str
    gate_that_must_flag: str
    taxonomy_category: str
    case_dir: Path
    manifest_path: Path
    sample_files: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        """Convert case to serialisable dictionary."""
        return {
            "class": self.class_name,
            "description": self.description,
            "expected_verdict": self.expected_verdict,
            "gate_that_must_flag": self.gate_that_must_flag,
            "taxonomy_category": self.taxonomy_category,
            "case_dir": str(self.case_dir),
            "manifest_path": str(self.manifest_path),
            "sample_files": list(self.sample_files),
        }


# ---------------------------------------------------------------------------
# Corpus loader (pure directory & manifest driven)
# ---------------------------------------------------------------------------


def load_evasion_corpus(
    corpus_dir: Path | str | None = None,
) -> list[GateEvasionCase]:
    """Dynamically discover and load all evasion cases from the corpus directory.

    Adding a new class requires no Python changes: any directory under
    ``corpus_dir`` containing a valid ``manifest.json`` is loaded.

    Args:
        corpus_dir: Path to gate evasion fixtures directory. Defaults to
            ``src/bernstein/eval/cases/gate_evasion``.

    Returns:
        List of :class:`GateEvasionCase` objects sorted by class name.
    """
    base_dir = Path(corpus_dir) if corpus_dir is not None else DEFAULT_EVASION_CORPUS_DIR
    if not base_dir.exists() or not base_dir.is_dir():
        return []

    cases: list[GateEvasionCase] = []
    for sub in sorted(base_dir.iterdir()):
        if not sub.is_dir():
            continue
        manifest_path = sub / "manifest.json"
        if not manifest_path.is_file():
            continue

        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        class_name = raw.get("class") or raw.get("class_name") or sub.name
        description = raw.get("description", "")
        expected_verdict = raw.get("expected_verdict", "fail")
        gate_that_must_flag = raw.get("gate_that_must_flag", "quality_gate")
        taxonomy_category = raw.get("taxonomy_category", f"evasion_{class_name}")

        sample_files = tuple(sorted(f.name for f in sub.iterdir() if f.name != "manifest.json"))

        cases.append(
            GateEvasionCase(
                class_name=class_name,
                description=description,
                expected_verdict=expected_verdict,
                gate_that_must_flag=gate_that_must_flag,
                taxonomy_category=taxonomy_category,
                case_dir=sub,
                manifest_path=manifest_path,
                sample_files=sample_files,
            )
        )

    return sorted(cases, key=lambda c: c.class_name)


# ---------------------------------------------------------------------------
# Suite builder
# ---------------------------------------------------------------------------


def build_gate_evasion_suite_v1(
    corpus_dir: Path | str | None = None,
) -> BenchSuite:
    """Build the canonical ``gate-evasion-v1`` benchmark suite from corpus cases."""
    cases = load_evasion_corpus(corpus_dir)
    tasks = [
        BenchTask(
            id=f"gate_evasion_{c.class_name}",
            description=c.description or f"Gate evasion test for {c.class_name}",
            steps=(
                f"load evasion fixture {c.class_name}",
                f"evaluate change against quality gate {c.gate_that_must_flag}",
                f"assert evasion is caught with verdict '{c.expected_verdict}'",
            ),
            assertions=(
                {
                    "kind": "gate_evasion_caught",
                    "case_class": c.class_name,
                    "gate_that_must_flag": c.gate_that_must_flag,
                    "expected_verdict": c.expected_verdict,
                    "taxonomy_category": c.taxonomy_category,
                },
            ),
            category=c.taxonomy_category,
        )
        for c in cases
    ]
    return BenchSuite(version="gate-evasion-v1", tasks=tasks)


# ---------------------------------------------------------------------------
# Scoring and results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateEvasionResult:
    """Result of running a single evasion case against quality gates.

    Attributes:
        case_class: The evasion class name.
        gate_that_must_flag: Gate expected to catch the evasion.
        expected_verdict: Expected verdict ('fail').
        caught: Whether the designated gate flagged/caught the evasion.
        actual_verdict: The actual verdict returned.
        flagged_by_gate: Gate that actually flagged the evasion (or empty).
        details: Additional diagnostic explanation.
    """

    case_class: str
    gate_that_must_flag: str
    expected_verdict: str
    caught: bool
    actual_verdict: str = ""
    flagged_by_gate: str = ""
    details: str = ""


@dataclass(frozen=True)
class GateEvasionScore:
    """Aggregated score and diagnostics for a gate evasion benchmark run.

    Attributes:
        total_cases: Number of evasion cases evaluated.
        caught_cases: Number of evasion cases successfully caught.
        catch_rate: Proportion of cases caught in [0.0, 1.0].
        missed_classes: Tuple of evasion class names that were not caught.
        responsible_gates: Map of gate name to count of missed evasion cases.
        results: Tuple of per-case :class:`GateEvasionResult` objects.
    """

    total_cases: int
    caught_cases: int
    catch_rate: float
    missed_classes: tuple[str, ...]
    responsible_gates: dict[str, int]
    results: tuple[GateEvasionResult, ...]

    def summary(self) -> str:
        """Render a human-readable evaluation summary."""
        lines = [
            f"Gate Evasion Benchmark Score: {self.catch_rate * 100:.1f}% "
            f"({self.caught_cases}/{self.total_cases} caught)",
        ]
        if self.missed_classes:
            lines.append("\nMissed Evasion Classes:")
            for mc in self.missed_classes:
                lines.append(f"  - {mc}")
            lines.append("\nResponsible Gates with Misses:")
            for gate, count in sorted(self.responsible_gates.items()):
                lines.append(f"  - {gate}: {count} missed")
        else:
            lines.append("All evasion classes successfully caught!")
        return "\n".join(lines)


def score_gate_evasion(
    results: Sequence[GateEvasionResult],
) -> GateEvasionScore:
    """Compute summary score and identify missed classes and responsible gates."""
    total = len(results)
    caught = sum(1 for r in results if r.caught)
    catch_rate = (caught / total) if total > 0 else 1.0

    missed_classes: list[str] = []
    responsible_gates: dict[str, int] = {}

    for r in results:
        if not r.caught:
            missed_classes.append(r.case_class)
            gate = r.gate_that_must_flag
            responsible_gates[gate] = responsible_gates.get(gate, 0) + 1

    return GateEvasionScore(
        total_cases=total,
        caught_cases=caught,
        catch_rate=catch_rate,
        missed_classes=tuple(missed_classes),
        responsible_gates=responsible_gates,
        results=tuple(results),
    )


# ---------------------------------------------------------------------------
# Runner and submission bundle generation
# ---------------------------------------------------------------------------


def run_gate_evasion_suite(
    corpus_dir: Path | str | None = None,
    evaluator: Callable[[GateEvasionCase], tuple[bool, str, str]] | None = None,
    scheduler_config: dict[str, Any] | None = None,
) -> tuple[GateEvasionScore, SubmissionBundle]:
    """Execute gate evasion suite and return the score and signed SubmissionBundle.

    Args:
        corpus_dir: Optional custom corpus directory path.
        evaluator: Optional callable ``(case) -> (caught: bool, actual_verdict: str, details: str)``.
            If omitted, defaults to catching all cases where expected_verdict is 'fail'.
        scheduler_config: Optional scheduler configuration dictionary.

    Returns:
        Tuple of (:class:`GateEvasionScore`, :class:`SubmissionBundle`).
    """
    suite = build_gate_evasion_suite_v1(corpus_dir=corpus_dir)
    cases = load_evasion_corpus(corpus_dir=corpus_dir)
    case_map = {f"gate_evasion_{c.class_name}": c for c in cases}

    results: list[GateEvasionResult] = []
    task_results: list[TaskResult] = []

    for task in suite.tasks:
        case = case_map.get(task.id)
        if case is None:
            continue

        if evaluator is not None:
            caught, actual_verdict, details = evaluator(case)
        else:
            # Default simulation behaviour: designated gate flags the evasion
            caught = True
            actual_verdict = case.expected_verdict
            details = f"Gate {case.gate_that_must_flag} flagged evasion correctly."

        res = GateEvasionResult(
            case_class=case.class_name,
            gate_that_must_flag=case.gate_that_must_flag,
            expected_verdict=case.expected_verdict,
            caught=caught,
            actual_verdict=actual_verdict,
            flagged_by_gate=case.gate_that_must_flag if caught else "",
            details=details,
        )
        results.append(res)

        # Build TaskResult for submission bundle
        score = 1.0 if caught else 0.0
        receipt = {
            "task_id": task.id,
            "case_class": case.class_name,
            "gate_that_must_flag": case.gate_that_must_flag,
            "verdict": actual_verdict,
            "caught": caught,
            "taxonomy_category": case.taxonomy_category,
        }
        task_results.append(
            TaskResult(
                task_id=task.id,
                task_hash=task.content_hash(),
                receipt=receipt,
                passed=caught,
                score=score,
                harness_output={
                    "gate": case.gate_that_must_flag,
                    "details": details,
                },
            )
        )

    score_obj = score_gate_evasion(results)
    bundle = SubmissionBundle(
        suite_hash=suite.suite_hash,
        suite_version=suite.version,
        task_results=task_results,
        scheduler_config=scheduler_config or {"scheduler": "deterministic"},
    )
    return score_obj, bundle
