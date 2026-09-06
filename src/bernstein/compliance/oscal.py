"""
NIST OSCAL 1.0.0 Assessment-Results exporter for compliance evidence packs (Issue #5456).

Generates deterministic OSCAL assessment-results JSON linking audit evidence
and benchmark bundles to regulatory controls.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

OSCAL_VERSION: Final[str] = "1.0.0"
_OSCAL_NAMESPACE: Final[uuid.UUID] = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # UUID namespace DNS


def _make_deterministic_uuid(name: str) -> str:
    """Derive a deterministic RFC 4122 UUID v5 from a stable string key."""
    return str(uuid.uuid5(_OSCAL_NAMESPACE, name))


def generate_oscal_assessment_results(
    standard: str,
    regulation: str,
    bundle_id: str,
    controls: list[dict[str, Any]],
    bundles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Generate a deterministic NIST OSCAL 1.0.0 assessment-results document.

    Args:
        standard: Short standard slug (e.g. "ai-act", "owasp-asi").
        regulation: Human-readable regulatory description.
        bundle_id: Unique deterministic bundle ID.
        controls: List of control dictionaries with status and reasons.
        bundles: Optional list of benchmark bundle metadata.

    Returns:
        Deterministic OSCAL assessment-results JSON dict.
    """
    ar_uuid = _make_deterministic_uuid(f"oscal:assessment-results:{bundle_id}")
    result_uuid = _make_deterministic_uuid(f"oscal:result:{bundle_id}:{standard}")

    control_selections = [
        {
            "include-controls": [
                {"control-id": c["control_id"]} for c in sorted(controls, key=lambda x: str(x.get("control_id", "")))
            ]
        }
    ]

    findings: list[dict[str, Any]] = []
    for c in sorted(controls, key=lambda x: str(x.get("control_id", ""))):
        cid = c["control_id"]
        status_str = c.get("status", "todo")
        reason = c.get("reason", "") or c.get("requirement", "")

        # State determination
        if status_str == "measured":
            measured_val = c.get("measured_values") or {}
            pass_rate = measured_val.get("pass_rate", 1.0)
            state = "satisfied" if pass_rate >= 1.0 else "not-satisfied"
        elif status_str in ("not-applicable", "organisational"):
            state = "not-applicable"
        else:
            state = "not-tested"

        finding_uuid = _make_deterministic_uuid(f"oscal:finding:{bundle_id}:{cid}")
        finding: dict[str, Any] = {
            "uuid": finding_uuid,
            "title": f"Finding for Control {cid}",
            "description": reason,
            "target": {
                "type": "control-id",
                "target-id": cid,
                "status": {
                    "state": state,
                },
            },
        }
        if c.get("measured_values"):
            finding["remarks"] = (
                f"Pass rate: {c['measured_values'].get('pass_rate', 0.0) * 100:.1f}%, "
                f"Score: {c['measured_values'].get('overall_score', 0.0):.2f}, "
                f"Bundle: {c['measured_values'].get('bundle_hash', '')[:16]}..."
            )
        findings.append(finding)

    doc: dict[str, Any] = {
        "assessment-results": {
            "uuid": ar_uuid,
            "metadata": {
                "title": f"Bernstein Compliance Assessment Results — {regulation or standard}",
                "last-modified": "1970-01-01T00:00:00Z",
                "version": "1.0.0",
                "oscal-version": OSCAL_VERSION,
            },
            "import-ap": {
                "href": f"#ap-{bundle_id}",
            },
            "results": [
                {
                    "uuid": result_uuid,
                    "title": f"Compliance Assessment for {standard}",
                    "description": f"Deterministic automated assessment results mapped to {standard} controls.",
                    "start": "1970-01-01T00:00:00Z",
                    "reviewed-controls": {
                        "control-selections": control_selections,
                    },
                    "findings": findings,
                }
            ],
        }
    }
    return doc


def validate_oscal_assessment_results(doc: dict[str, Any]) -> bool:
    """Validate that `doc` conforms to the NIST OSCAL assessment-results schema structure."""
    if not isinstance(doc, dict):
        return False
    if "assessment-results" not in doc:
        return False
    ar = doc["assessment-results"]
    if not isinstance(ar, dict):
        return False

    if "uuid" not in ar or "metadata" not in ar or "results" not in ar:
        return False

    metadata = ar["metadata"]
    if metadata.get("oscal-version") != OSCAL_VERSION:
        return False

    results = ar.get("results", [])
    if not isinstance(results, list) or len(results) == 0:
        return False

    res0 = results[0]
    if "reviewed-controls" not in res0 or "findings" not in res0:
        return False

    findings = res0.get("findings", [])
    if not isinstance(findings, list):
        return False

    for f in findings:
        if "uuid" not in f or "target" not in f:
            return False
        target = f["target"]
        if "target-id" not in target or "status" not in target:
            return False
        if "state" not in target["status"]:
            return False
        if target["status"]["state"] not in ("satisfied", "not-satisfied", "not-applicable", "not-tested"):
            return False

    return True
