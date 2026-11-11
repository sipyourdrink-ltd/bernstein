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


def _bernstein_version() -> str | None:
    """The tool's own version: ``semanticVersion`` describes the tool, not the suite.

    ``None`` when the package is not installed. The caller omits the field
    rather than substituting a placeholder: SARIF makes ``semanticVersion``
    optional, and a report is read by operators and by whatever consumes the
    CI artefact, so "the version is unknown here" must not be spelled as the
    factual claim "the version is 0.0.0".
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("bernstein")
    except PackageNotFoundError:
        return None


def bundle_to_sarif(
    bundle: SubmissionBundle,
    suite: BenchSuite | None = None,
    *,
    suite_uri: str | None = None,
) -> dict[str, Any]:
    """Generate a SARIF 2.1.0 dictionary from a SubmissionBundle.

    One ``result`` per failed task. ``suite_uri`` is the repository-relative
    path the suite's tasks are defined in (a suite JSON file, or the module
    that builds a built-in suite); it is the only location a task really
    has, so it is the only one a result claims. Without it results carry no
    ``locations`` at all rather than a path that does not exist.
    """
    task_map = {t.id: t for t in suite.tasks} if suite else {}
    results: list[dict[str, Any]] = []

    for tr in bundle.task_results:
        if tr.passed:
            continue
        task = task_map.get(tr.task_id)
        rule_id = tr.task_id
        category = getattr(task, "category", "bench") if task else "bench"

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

        result: dict[str, Any] = {
            "ruleId": rule_id,
            "level": "error",
            "message": {
                "text": f"Benchmark task {tr.task_id} [{category}] failed: {err_msg}",
            },
            "partialFingerprints": {"bernstein/taskHash": tr.task_hash},
        }
        if suite_uri:
            result["locations"] = [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": suite_uri},
                    }
                }
            ]
        results.append(result)

    driver: dict[str, Any] = {
        "name": "bernstein-bench",
        "informationUri": "https://github.com/sipyourdrink-ltd/bernstein",
    }
    # Omitted, never defaulted. `semanticVersion` is optional in SARIF, so an
    # uninstalled package leaves the field out instead of asserting a version
    # the tool does not have.
    tool_version = _bernstein_version()
    if tool_version is not None:
        driver["semanticVersion"] = tool_version
    driver["properties"] = {
        "suiteVersion": bundle.suite_version,
        "suiteHash": bundle.suite_hash,
        "bundleHash": bundle.bundle_hash(),
    }
    driver["rules"] = [
        {
            "id": r["ruleId"],
            "shortDescription": {"text": f"Benchmark rule for task {r['ruleId']}"},
        }
        for r in results
    ]

    return {
        "version": "2.1.0",
        "$schema": SARIF_SCHEMA_URI,
        "runs": [
            {
                "tool": {"driver": driver},
                "results": results,
            }
        ],
    }
