"""
NIST OSCAL (Open Security Controls Assessment Language) assessment-results export.

Transforms Bernstein benchmark evaluation results and compliance control
mappings into standard NIST OSCAL v1.1.0 Assessment Results JSON format.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bernstein.compliance.controls import ControlRegistry
    from bernstein.eval.bench.bundle import SubmissionBundle

OSCAL_VERSION: str = "1.1.0"
_OSCAL_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def _deterministic_uuid(name: str) -> str:
    """Generate a deterministic UUIDv5 string from a seed name."""
    return str(uuid.uuid5(_OSCAL_NAMESPACE, name))


def build_oscal_assessment_results(
    standard: str,
    bundles: list[SubmissionBundle],
    registry: ControlRegistry | None = None,
) -> dict[str, Any]:
    """Build a deterministic NIST OSCAL Assessment Results document."""
    if registry is None:
        from bernstein.compliance.controls import get_default_registry

        registry = get_default_registry()

    controls = registry.list_controls()
    control_bundles: dict[str, list[SubmissionBundle]] = {c.control_id: [] for c in controls}

    for b in bundles:
        suite_controls = getattr(b, "controls", [])
        if not suite_controls and b.suite_version == "golden-v1":
            suite_controls = ["CTL-ROB-01", "CTL-EVAL-01", "CTL-EVAL-02", "CTL-QUAL-02"]
        for cid in suite_controls:
            if cid in control_bundles:
                control_bundles[cid].append(b)

    observations: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []

    for c in controls:
        matched_bundles = control_bundles.get(c.control_id, [])
        if matched_bundles:
            b = matched_bundles[-1]
            b_hash = b.bundle_hash()
            obs_uuid = _deterministic_uuid(f"observation-{c.control_id}-{b_hash}")
            finding_uuid = _deterministic_uuid(f"finding-{c.control_id}-{b_hash}")

            observations.append(
                {
                    "uuid": obs_uuid,
                    "title": f"Benchmark observation for {c.control_id}",
                    "description": (
                        f"Measured by benchmark suite {b.suite_version} "
                        f"(bundle {b_hash[:12]}). Score: {b.overall_score:.4f}."
                    ),
                    "methods": ["test", "examine"],
                    "collected": "1970-01-01T00:00:00+00:00",
                    "relevant-evidence": [
                        {
                            "href": f"bench-bundles/{b_hash}.json",
                            "description": f"Signed submission bundle for suite {b.suite_version}",
                        }
                    ],
                }
            )

            state = "satisfied" if b.overall_score >= 0.99 else "not-satisfied"
            findings.append(
                {
                    "uuid": finding_uuid,
                    "title": f"Assessment finding for {c.control_id}: {c.title}",
                    "description": c.description,
                    "target": {
                        "type": "control",
                        "target-id": c.control_id,
                        "status": {"state": state},
                    },
                    "related-observations": [{"observation-uuid": obs_uuid}],
                }
            )
        else:
            finding_uuid = _deterministic_uuid(f"finding-{c.control_id}-unmeasured")
            findings.append(
                {
                    "uuid": finding_uuid,
                    "title": f"Assessment finding for {c.control_id}: {c.title}",
                    "description": c.description,
                    "target": {
                        "type": "control",
                        "target-id": c.control_id,
                        "status": {"state": "not-satisfied"},
                    },
                    "remarks": "Control is declared in registry but not measured by any supplied bundle.",
                }
            )

    doc: dict[str, Any] = {
        "assessment-results": {
            "uuid": _deterministic_uuid(f"assessment-results-{standard}"),
            "metadata": {
                "title": f"Bernstein Automated Compliance Assessment Results ({standard})",
                "published": "1970-01-01T00:00:00+00:00",
                "last-modified": "1970-01-01T00:00:00+00:00",
                "version": "1.0.0",
                "oscal-version": OSCAL_VERSION,
            },
            "import-ap": {
                "href": "#assessment-plan-bernstein",
            },
            "results": [
                {
                    "uuid": _deterministic_uuid(f"results-{standard}"),
                    "title": f"Evaluation Benchmark & Governance Assessment ({standard})",
                    "description": (
                        "Continuous automated evaluation of compliance controls via signed benchmark bundles."
                    ),
                    "start": "1970-01-01T00:00:00+00:00",
                    "observations": observations,
                    "findings": findings,
                }
            ],
        }
    }
    return doc


def validate_oscal_assessment_results(doc: dict[str, Any]) -> bool:
    """Validate structure of OSCAL assessment results document."""
    if not isinstance(doc, dict):
        return False
    ar = doc.get("assessment-results")
    if not isinstance(ar, dict):
        return False
    if "uuid" not in ar or "metadata" not in ar or "results" not in ar:
        return False
    metadata = ar["metadata"]
    if metadata.get("oscal-version") != OSCAL_VERSION:
        return False
    results = ar["results"]
    if not isinstance(results, list) or len(results) == 0:
        return False
    res = results[0]
    return "findings" in res and "uuid" in res
