"""
bernstein-bench: gate-evasion benchmark suite.

Every way an agent change previously fooled or evaded a quality gate
becomes a fixture the gate must catch. This suite loads evasion cases
from ``src/bernstein/eval/cases/gate_evasion/``, materialises each one into
a scratch working directory, runs the gate its manifest names through
:class:`bernstein.core.quality.gate_runner.GateRunner`, and records what
that gate actually returned. Catch rate is the share of cases whose gate
produced a *finding*; a missed case names the gate that should have flagged
it, and a case whose manifest names a gate that does not exist is missed
with that as the reason.

A finding is not the same thing as a nonzero exit. pytest exits nonzero when
a test module fails to import and ruff exits nonzero when its own invocation
is wrong, and in neither case did the gate form an opinion about the change.
Counting those as catches inflates the catch rate with the suite's own
breakage, so a gate is only credited when its output carries the signature of
a finding -- pytest's JUnit failure count, ruff's ``Found N errors.`` line
(#5448 review).

There is no simulated verdict anywhere in this module. The first cut of
this suite defaulted to "caught" when no evaluator was supplied, which
made a 100% catch rate a tautology (#5448 review, F1).

Adding a new evasion class requires no Python changes: placing a new fixture
directory with a valid ``manifest.json`` is automatically discovered.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.eval.bench.bundle import SubmissionBundle, TaskResult
from bernstein.eval.bench.suite import BenchSuite, BenchTask

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

#: Registry controls this suite measures: gate-evasion resistance, and the
#: benchmark reproducibility every suite carries.
GATE_EVASION_SUITE_CONTROLS = ("CTL-SEC-04", "CTL-EVAL-01")


def gate_runner_gates() -> frozenset[str]:
    """Gate names GateRunner can dispatch, taken from the canonical registry.

    A manifest naming anything else is reported as "no such gate", never
    silently counted as caught -- so this set decides whether a case is a
    real measurement or an unmeasurable one, and a hand-written copy of it
    drifts. It did: ``incident_evals`` was in ``VALID_GATE_NAMES`` and not in
    the mirror, so a case naming it would have been charged to the corpus
    rather than to the copy that had fallen behind.

    This is the set a configuration may *name*, which is not quite the set
    GateRunner can *dispatch*: ``incident_evals`` passes config validation
    and then raises ``Unsupported gate name`` when it runs. The difference is
    handled where it shows up -- ``evaluate_with_gate_runner`` catches that
    refusal and reports ``no_gate`` -- rather than by keeping a second list
    here that would drift the same way the first one did.

    Imported inside the function for the same reason every other
    ``core.quality`` import in this module is: ``bench_cli`` imports this
    module to list suite names and must not drag the gate runner in with it.
    """
    from bernstein.core.quality.gate_pipeline import VALID_GATE_NAMES

    return VALID_GATE_NAMES


#: How each command gate says "I found something", as distinct from "I could
#: not run". A nonzero exit is not evidence of a finding -- pytest exits
#: nonzero when a module fails to import, ruff when its own invocation is
#: wrong -- and the two were indistinguishable while ``caught`` was
#: ``status == "fail"``. The ``tests`` gate is handled separately, through
#: pytest's JUnit report, which counts failures and collection errors apart.
_FINDING_SIGNATURE: dict[str, re.Pattern[str]] = {
    # ruff closes every run that has diagnostics with "Found 3 errors." and
    # a ruff that could not start prints no such line.
    "lint": re.compile(r"^Found \d+ errors?\.$", re.MULTILINE),
    # vulture has no summary line; each finding is "path:line: unused ...".
    "dead_code": re.compile(r"^\S+:\d+: unused ", re.MULTILINE),
}

#: Attributes of pytest's ``<testsuite>`` element. Read with a regex rather
#: than an XML parser: the document is one this module asked pytest to write
#: seconds earlier into a private temporary directory, and pulling a parser
#: in for three integers would be the larger surface.
_JUNIT_ATTR_RE = {key: re.compile(rf'<testsuite\b[^>]*\b{key}="(\d+)"') for key in ("tests", "failures", "errors")}

_PATCH_PATH_RE = re.compile(r"^(?:---|\+\+\+) [ab]/(\S+)", re.MULTILINE)

#: The Python module each command gate shells out to. Checked before the gate
#: runs: a gate whose tool is not installed cannot have caught anything, and
#: the dead-code gate reports a missing vulture as ``fail`` -- which would
#: count as a catch. Here it is ``command_not_found``, a miss with a reason.
_GATE_TOOL_MODULE = {"lint": "ruff", "dead_code": "vulture", "tests": "pytest", "type_check": "pyright"}

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
    if not base_dir.exists():
        if corpus_dir is not None:
            raise FileNotFoundError(f"Corpus directory not found: {base_dir}")
        return []
    if not base_dir.is_dir():
        if corpus_dir is not None:
            raise NotADirectoryError(f"Corpus path is not a directory: {base_dir}")
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
# Running one case through the real gate
# ---------------------------------------------------------------------------


def materialise_case(case: GateEvasionCase, into: Path) -> list[str]:
    """Lay the fixture out as a working tree and return its changed files.

    Every file in the fixture directory except ``manifest.json`` is copied
    as-is, with one rule: a fixture named ``*.py.txt`` is laid out as
    ``*.py``. The corpus lives under the shipped package, and a source file
    that exists to not parse must not be parsed by the repository's own
    scanners on the way in -- it becomes Python only inside the scratch
    tree the gate runs on. A ``diff.patch`` is not applied -- the fixture
    directory *is* the post-change state -- but the paths it names are the
    change set, which is how a deletion reaches a gate: the file is absent
    from the tree and present in ``changed_files``.
    """
    changed: list[str] = []
    for name in case.sample_files:
        src = case.case_dir / name
        if name == "diff.patch":
            changed.extend(dict.fromkeys(_PATCH_PATH_RE.findall(src.read_text(encoding="utf-8"))))
            continue
        laid_out = name.removesuffix(".txt") if name.endswith(".py.txt") else name
        dst = into / laid_out
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        changed.append(laid_out)
    return list(dict.fromkeys(changed))


def _hermetic_config() -> Any:
    """A QualityGatesConfig whose tools run through this interpreter.

    The gates' defaults reach for ``uv run``; inside a benchmark the tool that
    is measured must not depend on a launcher being on PATH.
    """
    from bernstein.core.quality.quality_gates import QualityGatesConfig

    py = sys.executable
    return QualityGatesConfig(
        lint_command=f"{py} -m ruff check .",
        dead_code_command=f"{py} -m vulture",
        test_command=f"{py} -m pytest -x -q -p no:cacheprovider",
        cache_enabled=False,
        timeout_s=120,
    )


def _scrub(detail: str, scratch: Path) -> str:
    """Make gate output reproducible: no scratch path, no timings."""
    text = detail.replace(str(scratch), "<case>").replace(scratch.as_posix(), "<case>")
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    text = re.sub(r"\b\d+(?:\.\d+)?s\b", "<t>", text)
    # pytest prints mock reprs with a per-process id and objects with an
    # address; neither is part of the verdict.
    text = re.sub(r"id='\d+'", "id='<n>'", text)
    text = re.sub(r"0x[0-9A-Fa-f]{6,}", "0x<addr>", text)
    return text[:2000]


def _junit_counts(report: Path) -> tuple[int, int, int] | None:
    """``(tests, failures, errors)`` from pytest's JUnit report, or ``None``.

    ``None`` means pytest never wrote the report, which is itself a tool
    error: the run did not reach the point of summarising anything.
    """
    try:
        text = report.read_text(encoding="utf-8")
    except OSError:
        return None
    counts = []
    for key in ("tests", "failures", "errors"):
        match = _JUNIT_ATTR_RE[key].search(text)
        if match is None:
            return None
        counts.append(int(match.group(1)))
    return counts[0], counts[1], counts[2]


def _classify_tests_gate(report: Path) -> tuple[bool, str, str, str]:
    """``(caught, verdict, basis, note)`` for the tests gate, from the JUnit report.

    pytest exits nonzero for a failing assertion and for a test module that
    will not import, and ``GateRunner`` maps both to ``fail``. Only the first
    is the gate catching the evasion; the second is the fixture failing to
    run, and crediting it counted the suite's own breakage as a result. The
    JUnit report separates the two by construction: ``failures`` is
    assertions that ran and lost, ``errors`` is collection and setup.
    """
    counts = _junit_counts(report)
    if counts is None:
        return (False, "tool_error", "junit_missing", "pytest wrote no JUnit report; nothing ran to completion")
    tests, failures, errors = counts
    if failures > 0:
        return (True, "fail", "junit_failures", f"{failures} test(s) failed, {errors} error(s), {tests} collected")
    if errors > 0:
        return (
            False,
            "tool_error",
            "junit_errors",
            f"{errors} collection/setup error(s) and no test failure; no assertion reached the change",
        )
    if tests == 0:
        return (False, "tool_error", "junit_no_tests", "pytest collected no tests")
    return (False, "pass", "junit_failures", f"{tests} test(s) ran, none failed")


def evaluate_with_gate_runner(case: GateEvasionCase) -> GateEvasionResult:
    """Run *case* through the gate its manifest names and report what it said.

    ``caught`` is true only when the gate's own output identifies a finding:
    pytest's JUnit failure count for the ``tests`` gate, and the signature in
    :data:`_FINDING_SIGNATURE` for the other command gates. A ``fail`` with
    no such signature is reported as ``tool_error`` -- a miss with a reason,
    because the gate never got far enough to have an opinion about the
    change. ``pass`` is a miss too (the evasion worked), and so are
    ``skipped``, ``command_not_found``, ``no_gate`` and ``inconclusive``,
    each recorded verbatim in ``actual_verdict``.
    """
    from bernstein.core.models import Task
    from bernstein.core.quality.gate_pipeline import GatePipelineStep
    from bernstein.core.quality.gate_runner import GateRunner

    gate = case.gate_that_must_flag
    if gate not in gate_runner_gates():
        return GateEvasionResult(
            case_class=case.class_name,
            gate_that_must_flag=gate,
            expected_verdict=case.expected_verdict,
            caught=False,
            actual_verdict="no_gate",
            details=f"GateRunner has no gate named {gate!r}; nothing can flag this class today.",
            verdict_basis="no_gate",
        )

    tool = _GATE_TOOL_MODULE.get(gate)
    if tool is not None and importlib.util.find_spec(tool) is None:
        return GateEvasionResult(
            case_class=case.class_name,
            gate_that_must_flag=gate,
            expected_verdict=case.expected_verdict,
            caught=False,
            actual_verdict="command_not_found",
            details=f"the {gate} gate runs {tool}, which is not installed in this environment",
            verdict_basis="tool_missing",
        )

    with tempfile.TemporaryDirectory(prefix="gate-evasion-") as tmp:
        # The scratch tree is a subdirectory so the JUnit report can sit
        # beside it: it is this suite's instrument, not part of the change
        # the gate is looking at.
        scratch = Path(tmp) / "case"
        scratch.mkdir()
        junit = Path(tmp) / "junit.xml"
        changed = materialise_case(case, scratch)
        runner = GateRunner(_hermetic_config(), scratch)
        override = None
        if gate == "tests":
            # The impacted-test analyser needs a repository; the fixture is
            # not one. Run the fixture's own tests, which is what the gate
            # would have selected.
            tests = [f for f in changed if Path(f).name.startswith("test_") and f.endswith(".py")]
            if not tests:
                return GateEvasionResult(
                    case_class=case.class_name,
                    gate_that_must_flag=gate,
                    expected_verdict=case.expected_verdict,
                    caught=False,
                    actual_verdict="skipped",
                    details="the change carries no test file for the tests gate to run",
                    verdict_basis="no_tests_in_change",
                )
            override = f"{sys.executable} -m pytest {' '.join(tests)} -x -q -p no:cacheprovider --junitxml={junit}"
        step = GatePipelineStep(name=gate, required=True, command_override=override)
        task = Task(
            id=f"gate-evasion-{case.class_name}", title=case.class_name, description=case.description, role="qa"
        )
        try:
            result = asyncio.run(runner.run_gate(step, task, scratch, changed))
        except Exception as exc:
            # `VALID_GATE_NAMES` is the set a config may name, which is not
            # the same as the set GateRunner can dispatch: `incident_evals`
            # passes config validation and then raises "Unsupported gate
            # name" here. Deriving the suite's set from the registry and
            # catching the refusal is how both stay true without a mirror.
            unsupported = "Unsupported gate name" in str(exc)
            return GateEvasionResult(
                case_class=case.class_name,
                gate_that_must_flag=gate,
                expected_verdict=case.expected_verdict,
                caught=False,
                actual_verdict="no_gate" if unsupported else "runner_error",
                details=f"{type(exc).__name__}: {exc}",
                verdict_basis="no_gate" if unsupported else "runner_error",
            )
        details = _scrub(result.details or "", scratch)

        if gate == "tests":
            caught, verdict, basis, note = _classify_tests_gate(junit)
            if result.status not in ("fail", "pass"):
                # The gate did not even get to run pytest.
                caught, verdict, basis, note = False, result.status, "gate_verdict", ""
        elif result.status != "fail":
            caught, verdict, basis, note = False, result.status, "gate_verdict", ""
        elif (signature := _FINDING_SIGNATURE.get(gate)) is not None:
            if signature.search(details):
                caught, verdict, basis, note = True, "fail", "finding_signature", ""
            else:
                caught, verdict, basis = False, "tool_error", "no_finding_signature"
                note = f"the {gate} gate exited nonzero without emitting a finding"
        else:
            # No signature declared for this gate, so the catch rests on the
            # gate's own word. Named rather than hidden.
            caught, verdict, basis, note = True, "fail", "unclassified_fail", ""

        return GateEvasionResult(
            case_class=case.class_name,
            gate_that_must_flag=gate,
            expected_verdict=case.expected_verdict,
            caught=caught,
            actual_verdict=verdict,
            flagged_by_gate=gate if caught else "",
            details=f"{note}\n\n{details}".strip() if note else details,
            verdict_basis=basis,
        )


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
        caught: Whether the designated gate produced a finding against the evasion.
        actual_verdict: The actual verdict returned.
        flagged_by_gate: Gate that actually flagged the evasion (or empty).
        details: Additional diagnostic explanation.
        verdict_basis: What ``caught`` was decided from. ``junit_failures``
            and ``finding_signature`` are positive identifications of a
            finding; ``unclassified_fail`` means the gate returned ``fail``
            and this suite has no signature for it, so the catch is taken on
            the gate's word. A gap that is named is one a reader can price.
    """

    case_class: str
    gate_that_must_flag: str
    expected_verdict: str
    caught: bool
    actual_verdict: str = ""
    flagged_by_gate: str = ""
    details: str = ""
    verdict_basis: str = ""


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
        if self.total_cases == 0:
            lines.append("No cases evaluated.")
        elif self.missed_classes:
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
    catch_rate = (caught / total) if total > 0 else 0.0

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
    evaluator: Callable[[GateEvasionCase], GateEvasionResult] | None = None,
    scheduler_config: dict[str, Any] | None = None,
) -> tuple[GateEvasionScore, SubmissionBundle]:
    """Run every case through its gate and return the score and an unsigned bundle.

    Args:
        corpus_dir: Optional custom corpus directory path.
        evaluator: ``(case) -> GateEvasionResult``. Defaults to
            :func:`evaluate_with_gate_runner`, which runs the real gate. Tests
            of the scorer may pass their own; nothing else should.
        scheduler_config: Optional scheduler configuration dictionary.

    Returns:
        Tuple of (:class:`GateEvasionScore`, :class:`SubmissionBundle`). The
        bundle is unsigned; ``bench run`` signs it.
    """
    suite = build_gate_evasion_suite_v1(corpus_dir=corpus_dir)
    cases = load_evasion_corpus(corpus_dir=corpus_dir)
    case_map = {f"gate_evasion_{c.class_name}": c for c in cases}
    evaluate = evaluator or evaluate_with_gate_runner

    results: list[GateEvasionResult] = []
    task_results: list[TaskResult] = []

    for task in suite.tasks:
        case = case_map.get(task.id)
        if case is None:
            continue

        res = evaluate(case)
        caught, actual_verdict, details = res.caught, res.actual_verdict, res.details
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


# ---------------------------------------------------------------------------
# bench adapter: what `bench run gate-evasion-v1` / `bench verify` go through
# ---------------------------------------------------------------------------


class GateEvasionReplayAdapter:
    """Runs each task's fixture through its gate; replays the verdict from the receipt.

    ``run_task`` executes the gate and records its status and scrubbed
    output. ``score_task`` -- the path ``bench verify`` replays through --
    re-derives the verdict from the receipt alone, so a bundle verifies
    without re-running the gates, and a receipt whose status is anything
    but ``fail`` scores zero.
    """

    def __init__(self, corpus_dir: Path | str | None = None) -> None:
        self._cases = {f"gate_evasion_{c.class_name}": c for c in load_evasion_corpus(corpus_dir)}

    def run_task(self, task: BenchTask, scheduler_config: dict[str, Any]) -> dict[str, Any]:
        case = self._cases.get(task.id)
        if case is None:
            raise ValueError(f"task {task.id!r} names no case in the gate-evasion corpus")
        res = evaluate_with_gate_runner(case)
        return {
            "journal_head": "",
            "spine_head": "",
            "run_id": f"gate-evasion-{task.content_hash()[:12]}",
            "case_class": res.case_class,
            "gate_that_must_flag": res.gate_that_must_flag,
            "status": res.actual_verdict,
            "caught": res.caught,
            "details": res.details,
        }

    def score_task(self, task: BenchTask, receipt: dict[str, Any]) -> tuple[bool, float, dict[str, Any]]:
        status = receipt.get("status")
        caught = status == "fail"
        return (
            caught,
            1.0 if caught else 0.0,
            {
                "gate": receipt.get("gate_that_must_flag", ""),
                "status": status,
                "caught": caught,
                "details": receipt.get("details", ""),
            },
        )
