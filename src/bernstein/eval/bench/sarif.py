"""
bernstein-bench: SARIF 2.1.0 report generation.

Translates benchmark bundle failures into standard SARIF v2.1.0 diagnostics
compatible with GitHub code scanning and CI dashboards.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bernstein.eval.bench.bundle import SubmissionBundle
    from bernstein.eval.bench.suite import BenchSuite

SARIF_SCHEMA_URI = "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json"


def bundle_to_sarif(bundle: SubmissionBundle, suite: BenchSuite | None = None) -> dict[str, Any]:
    """Generate a SARIF 2.1.0 dictionary from a SubmissionBundle."""
    task_map = {t.id: t for t in suite.tasks} if suite else {}
    results: list[dict[str, Any]] = []

    for tr in bundle.task_results:
        if tr.passed:
            continue
        task = task_map.get(tr.task_id)
        rule_id = tr.task_id
        category = getattr(task, "category", "bench") if task else "bench"
        fixture_uri = f"tests/bench/{tr.task_id}.json"

        err_msg = ""
        if tr.harness_output:
            err_msg = str(
                tr.harness_output.get("error")
                or tr.harness_output.get("refusal")
                or tr.harness_output.get("note")
                or tr.harness_output
            )
        if not err_msg:
            err_msg = f"Task {tr.task_id} failed with score {tr.score:.2f}."

        results.append(
            {
                "ruleId": rule_id,
                "level": "error",
                "message": {
                    "text": f"Benchmark task {tr.task_id} [{category}] failed: {err_msg}",
                },
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {
                                "uri": fixture_uri,
                            },
                            "region": {
                                "startLine": 1,
                                "startColumn": 1,
                            },
                        }
                    }
                ],
            }
        )

    return {
        "version": "2.1.0",
        "$schema": SARIF_SCHEMA_URI,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "bernstein-bench",
                        "informationUri": "https://github.com/sipyourdrink-ltd/bernstein",
                        "semanticVersion": bundle.suite_version or "1.0.0",
                        "rules": [
                            {
                                "id": r["ruleId"],
                                "shortDescription": {
                                    "text": f"Benchmark rule for task {r['ruleId']}",
                                },
                            }
                            for r in results
                        ],
                    }
                },
                "results": results,
            }
        ],
    }
