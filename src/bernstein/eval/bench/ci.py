"""
bernstein-bench: CI surface — SARIF generation, scorecard delta, and check runs.

Enables automated CI evaluation on pull requests:
1. SARIF 2.1.0 output: one finding per failed test case mapping to compliance control IDs.
2. Scorecard comparison: pass rate delta against the signed baseline bundle.
3. Check run emission: post native GitHub check run with scorecard table.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from bernstein.eval.bench.verifier import BenchVerifier, VerificationStatus

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.eval.bench.bundle import SubmissionBundle
    from bernstein.eval.bench.runner import ReplayAdapter
    from bernstein.eval.bench.suite import BenchSuite

SARIF_VERSION: Final = "2.1.0"
SARIF_SCHEMA: Final = "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/main/Schemata/sarif-schema-2.1.0.json"


# ===========================================================================
# SARIF Generation & Validation
# ===========================================================================


def generate_bench_sarif(
    suite: BenchSuite,
    bundle: SubmissionBundle,
    sarif_path: Path | None = None,
) -> dict[str, Any]:
    """Generate a SARIF 2.1.0 document from a benchmark submission bundle.

    For every failed task, generates a result with:
    - ruleId: Control ID from the suite (or task category/fallback)
    - level: "error"
    - message: Expected vs observed verdict and score
    - locations: Task definition/fixture path
    """
    task_map = {t.id: t for t in suite.tasks}
    suite_controls = getattr(suite, "controls", []) or []

    rules: dict[str, dict[str, Any]] = {}
    results: list[dict[str, Any]] = []

    for task_result in bundle.task_results:
        if task_result.passed:
            continue

        task_id = task_result.task_id
        task = task_map.get(task_id)
        task_category = task.category if task else "general"

        # Determine rule ID: pick matching or first suite control, or category-derived control
        if suite_controls:
            # Check if any control matches category
            matched_ctrl = next(
                (c for c in suite_controls if task_category.lower() in c.lower()),
                suite_controls[0],
            )
            rule_id = matched_ctrl
        else:
            rule_id = f"CTRL-{task_category.upper()}" if task_category else "CTRL-BENCH-TASK"

        if rule_id not in rules:
            rules[rule_id] = {
                "id": rule_id,
                "shortDescription": {"text": f"Compliance Control {rule_id}"},
                "fullDescription": {"text": f"Verification failure against control {rule_id}"},
                "defaultConfiguration": {"level": "error"},
            }

        # Build expected vs observed message
        err_msg = ""
        if task_result.harness_output and isinstance(task_result.harness_output, dict):
            err_msg = task_result.harness_output.get("reason", "") or task_result.harness_output.get("message", "")
        if not err_msg:
            err_msg = (
                f"Task '{task_id}' failed: expected passed=True, observed passed=False (score={task_result.score:.2f})"
            )

        fixture_uri = f"tests/fixtures/eval/{task_id}.json"

        results.append(
            {
                "ruleId": rule_id,
                "level": "error",
                "message": {"text": err_msg},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {
                                "uri": fixture_uri,
                                "uriBaseId": "%SRCROOT%",
                            },
                            "region": {
                                "startLine": 1,
                                "startColumn": 1,
                            },
                        },
                    },
                ],
            }
        )

    sarif_log: dict[str, Any] = {
        "$schema": SARIF_SCHEMA,
        "version": SARIF_VERSION,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "bernstein-bench",
                        "informationUri": "https://github.com/sipyourdrink-ltd/bernstein",
                        "rules": list(rules.values()),
                    },
                },
                "results": results,
            },
        ],
    }

    if sarif_path is not None:
        sarif_path.parent.mkdir(parents=True, exist_ok=True)
        sarif_path.write_text(json.dumps(sarif_log, indent=2), encoding="utf-8")

    return sarif_log


def validate_sarif_log(sarif_data: dict[str, Any]) -> bool:
    """Validate that a dictionary is structurally compliant with SARIF 2.1.0."""
    if not isinstance(sarif_data, dict):
        return False
    if sarif_data.get("version") != SARIF_VERSION:
        return False
    if "$schema" not in sarif_data or "sarif" not in str(sarif_data["$schema"]).lower():
        return False

    runs = sarif_data.get("runs")
    if not isinstance(runs, list) or len(runs) == 0:
        return False

    run = runs[0]
    tool = run.get("tool", {})
    driver = tool.get("driver", {})
    if not driver.get("name"):
        return False

    results = run.get("results")
    if not isinstance(results, list):
        return False

    for res in results:
        if "ruleId" not in res:
            return False
        if "message" not in res or not res["message"].get("text"):
            return False
        locations = res.get("locations", [])
        if not isinstance(locations, list):
            return False

    return True


# ===========================================================================
# Bench Scorecard & Baseline Delta
# ===========================================================================


@dataclass
class BenchScorecard:
    """Scorecard comparison between the current run and a baseline bundle."""

    suite_version: str
    current_pass_rate: float
    current_score: float
    current_bundle_hash: str
    baseline_pass_rate: float | None
    baseline_score: float | None
    baseline_bundle_hash: str | None
    baseline_verified: bool
    delta: float | None
    conclusion: str  # "success", "failure", "neutral"
    summary: str

    @property
    def delta_formatted(self) -> str:
        if self.delta is None or not self.baseline_verified:
            return "N/A (unverifiable baseline)"
        return f"{self.delta * 100:+.1f}%"

    @classmethod
    def compute(
        cls,
        current_bundle: SubmissionBundle,
        baseline_bundle: SubmissionBundle | None,
        suite: BenchSuite,
        adapter: ReplayAdapter,
        threshold: float = 0.0,
    ) -> BenchScorecard:
        """Compute scorecard delta against baseline bundle.

        Invariants:
        1. An unverifiable or unsigned baseline yields a neutral conclusion with a note, never a green.
        2. Regression above threshold produces conclusion='failure'.
        3. No regression against a verified baseline produces conclusion='success'.
        """
        curr_pr = current_bundle.pass_rate
        curr_score = current_bundle.overall_score
        curr_hash = current_bundle.bundle_hash()

        # Check baseline validity
        if baseline_bundle is None:
            return cls(
                suite_version=suite.version,
                current_pass_rate=curr_pr,
                current_score=curr_score,
                current_bundle_hash=curr_hash,
                baseline_pass_rate=None,
                baseline_score=None,
                baseline_bundle_hash=None,
                baseline_verified=False,
                delta=None,
                conclusion="neutral",
                summary=(
                    "Baseline bundle is missing. An unverifiable baseline "
                    "yields a neutral conclusion with a note, never a green."
                ),
            )

        # Baseline must be signed
        if not baseline_bundle.signature:
            return cls(
                suite_version=suite.version,
                current_pass_rate=curr_pr,
                current_score=curr_score,
                current_bundle_hash=curr_hash,
                baseline_pass_rate=None,
                baseline_score=None,
                baseline_bundle_hash=baseline_bundle.bundle_hash(),
                baseline_verified=False,
                delta=None,
                conclusion="neutral",
                summary=(
                    "Baseline bundle is unsigned. An unverifiable baseline "
                    "yields a neutral conclusion with a note, never a green."
                ),
            )

        # Verify baseline offline
        verifier = BenchVerifier(suite=suite, adapter=adapter)
        verification = verifier.verify(baseline_bundle)

        if verification.status != VerificationStatus.MATCH:
            return cls(
                suite_version=suite.version,
                current_pass_rate=curr_pr,
                current_score=curr_score,
                current_bundle_hash=curr_hash,
                baseline_pass_rate=None,
                baseline_score=None,
                baseline_bundle_hash=baseline_bundle.bundle_hash(),
                baseline_verified=False,
                delta=None,
                conclusion="neutral",
                summary=(
                    f"Baseline bundle verification failed ({verification.status.value}). "
                    "An unverifiable baseline yields a neutral conclusion with a note, never a green."
                ),
            )

        # Baseline is verified
        base_pr = baseline_bundle.pass_rate
        base_score = baseline_bundle.overall_score
        base_hash = baseline_bundle.bundle_hash()
        delta = curr_pr - base_pr

        # Check regression against threshold
        if delta < -threshold:
            conclusion = "failure"
            summary = (
                f"Pass rate regressed by {abs(delta) * 100:.1f}% "
                f"(current: {curr_pr * 100:.1f}%, baseline: {base_pr * 100:.1f}%, threshold: {threshold * 100:.1f}%)."
            )
        else:
            conclusion = "success"
            summary = (
                f"Pass rate {curr_pr * 100:.1f}% meets verified baseline {base_pr * 100:.1f}% "
                f"(delta: {delta * 100:+.1f}%)."
            )

        return cls(
            suite_version=suite.version,
            current_pass_rate=curr_pr,
            current_score=curr_score,
            current_bundle_hash=curr_hash,
            baseline_pass_rate=base_pr,
            baseline_score=base_score,
            baseline_bundle_hash=base_hash,
            baseline_verified=True,
            delta=delta,
            conclusion=conclusion,
            summary=summary,
        )

    def to_markdown(self) -> str:
        """Render markdown scorecard table for CI check runs and pull request comments."""
        base_pr_str = f"{self.baseline_pass_rate * 100:.1f}%" if self.baseline_pass_rate is not None else "N/A"
        short_hash = self.current_bundle_hash[:16]

        status_badge = {
            "success": "PASS",
            "failure": "FAIL",
            "neutral": "NEUTRAL",
        }.get(self.conclusion, "UNKNOWN")

        row = (
            f"| {self.suite_version} "
            f"| {self.current_pass_rate * 100:.1f}% "
            f"| {base_pr_str} "
            f"| {self.delta_formatted} "
            f"| `{short_hash}…` "
            f"| {status_badge} |"
        )

        lines = [
            "### Bernstein Bench Scorecard",
            "",
            "| Suite | Pass Rate | Baseline Pass Rate | Delta | Bundle Hash | Status |",
            "|---|---:|---:|---:|---|---|",
            row,
            "",
            f"> **Summary**: {self.summary}",
        ]
        return "\n".join(lines)
