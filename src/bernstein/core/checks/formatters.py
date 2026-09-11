"""Output formatters and exit code calculator for govern audit (#5076).

Emits JSON (one object per finding with every contract field) and SARIF 2.1.0
(with ruleId = finding.id and kind property carrying the three-state verdict).
Computes CI exit codes according to the check contract truth table.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from bernstein.core.checks.contract import Finding, Verdict

if TYPE_CHECKING:
    from collections.abc import Sequence

SARIF_VERSION = "2.1.0"
SARIF_SCHEMA = "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/main/Schemata/sarif-schema-2.1.0.json"


def verdict_to_kind(finding: Finding) -> str:
    """Map a finding to its three-state verdict kind (pass, fail, not_measurable) or declared."""
    if finding.verdict == Verdict.NOT_MEASURABLE:
        return "not_measurable"
    if finding.verdict == Verdict.DECLARED:
        return "declared"
    if finding.verdict == Verdict.FAIL or finding.passed is False:
        return "fail"
    if finding.verdict == Verdict.PASS or finding.passed is True:
        return "pass"
    return str(finding.verdict.value)


def findings_to_json(findings: Sequence[Finding], *, indent: int = 2) -> str:
    """Serialize findings to JSON containing one object per finding with every contract field."""
    return json.dumps([f.to_dict() for f in findings], indent=indent, ensure_ascii=False)


def findings_to_sarif(findings: Sequence[Finding]) -> dict[str, Any]:
    """Emit a SARIF 2.1.0 log dictionary with ruleId = finding.id and kind carrying the verdict."""
    rules: list[dict[str, Any]] = []
    seen_rule_ids: set[str] = set()
    results: list[dict[str, Any]] = []

    for f in findings:
        rule_id = f.id
        if rule_id not in seen_rule_ids:
            seen_rule_ids.add(rule_id)
            desc = f.summary or f.message or rule_id
            rules.append(
                {
                    "id": rule_id,
                    "shortDescription": {"text": desc},
                    "fullDescription": {"text": f.message or desc},
                }
            )

        kind = verdict_to_kind(f)
        if kind == "fail":
            level = "error"
        elif kind == "pass":
            level = "none"
        else:
            level = "warning"

        result: dict[str, Any] = {
            "ruleId": rule_id,
            "kind": kind,
            "level": level,
            "message": {"text": f.message or f.summary or rule_id},
            "properties": {
                "kind": kind,
                "verdict": f.verdict.value if isinstance(f.verdict, Verdict) else str(f.verdict),
                "area": f.area,
                "passed": f.passed,
                "remediation": f.remediation,
                "what_would_make_it_measurable": f.what_would_make_it_measurable,
            },
        }
        results.append(result)

    return {
        "$schema": SARIF_SCHEMA,
        "version": SARIF_VERSION,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "bernstein-govern-audit",
                        "informationUri": "https://bernstein.run",
                        "rules": rules,
                    }
                },
                "results": results,
            }
        ],
    }


def compute_audit_exit_code(
    findings: Sequence[Finding],
    *,
    strict: bool = False,
    required: frozenset[str] | set[str] | None = None,
) -> int:
    """Compute the CI exit code from audit findings.

    Rules:
    - 0: every required check is measured and passed (or no failures/strict not_measurable).
    - 1: any required check is measured and failed.
    - 2: any required check is not_measurable and strict is set.
    - Declared findings never change the exit code.
    """
    gated_findings = [
        f
        for f in findings
        if f.verdict != Verdict.DECLARED and (required is None or f.id in required or f.check_id in required)
    ]

    has_failure = any(f.verdict == Verdict.FAIL or f.passed is False for f in gated_findings)
    if has_failure:
        return 1

    if strict:
        has_not_measurable = any(f.verdict == Verdict.NOT_MEASURABLE for f in gated_findings)
        if has_not_measurable:
            return 2

    return 0
