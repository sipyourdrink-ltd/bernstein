"""
NIST OSCAL (Open Security Controls Assessment Language) assessment-results export.

Transforms Bernstein benchmark evaluation results and compliance control
mappings into standard NIST OSCAL v1.1.0 Assessment Results JSON format.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

    from bernstein.compliance.controls import ControlRegistry
    from bernstein.eval.bench.bundle import SubmissionBundle

OSCAL_VERSION: str = "1.1.0"
_OSCAL_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")

#: Namespace stamped on the ``props`` this export adds to a finding, per the
#: OSCAL convention that non-core properties name their origin.
BERNSTEIN_PROP_NS = "https://bernstein.dev/ns/oscal"

#: ``--standard`` value -> key under ``Control.references`` that names the
#: clause of that standard a control maps to. The standard is what the
#: document is anchored against, so every finding carries its clause.
_STANDARD_REFERENCE_KEY: dict[str, str] = {
    "ai-act": "eu_ai_act",
    "owasp-asi": "owasp_asi",
    "owasp-skills": "owasp_skills",
    "iso-42001": "iso_42001",
}

#: Mean task score at or above which a measured control is reported
#: ``satisfied``. This is the export's own policy -- the bench harness has no
#: pass threshold of its own (a bundle carries per-task ``passed`` flags and
#: a derived mean score, nothing that says what "good enough" is). The value
#: used is recorded in every finding so a reader can see what the verdict
#: was measured against.
SATISFIED_SCORE_THRESHOLD: float = 0.99


def _deterministic_uuid(name: str) -> str:
    """Generate a deterministic UUIDv5 string from a seed name."""
    return str(uuid.uuid5(_OSCAL_NAMESPACE, name))


def _collected_at(bundle: SubmissionBundle) -> str:
    """The bundle's own ``submitted_at`` as an OSCAL timestamp.

    Deterministic for a given bundle, and true -- unlike an epoch sentinel,
    which would say the observation was collected in 1970.
    """
    return datetime.fromtimestamp(float(bundle.submitted_at), tz=UTC).isoformat()


def build_oscal_assessment_results(
    standard: str,
    bundles: Sequence[SubmissionBundle],
    registry: ControlRegistry | None = None,
    *,
    satisfied_threshold: float = SATISFIED_SCORE_THRESHOLD,
) -> dict[str, Any]:
    """Build a deterministic NIST OSCAL Assessment Results document.

    ``standard`` is one of :data:`bernstein.compliance.evidence_pack.SUPPORTED_STANDARDS`;
    each finding carries the clause of that standard its control maps to
    (``props`` ``clause``), or ``unmapped`` when the registry records none.

    Controls are mapped to bundles through the suite each bundle names, via
    the same resolver the evidence pack uses
    (:func:`bernstein.compliance.evidence_pack.resolve_suite_controls`).
    When several bundles measure a control the one with the latest
    ``submitted_at`` is reported. Bundles from a suite that is not a
    built-in cannot be mapped from here; they are named in the result's
    ``remarks`` rather than silently reported as no coverage.
    """
    reference_key = _STANDARD_REFERENCE_KEY.get(standard)
    if reference_key is None:
        raise ValueError(f"unsupported standard {standard!r}; expected one of {sorted(_STANDARD_REFERENCE_KEY)}")
    if registry is None:
        from bernstein.compliance.controls import get_default_registry

        registry = get_default_registry()
    if not 0.0 <= satisfied_threshold <= 1.0:
        raise ValueError(f"satisfied_threshold must be within [0, 1], got {satisfied_threshold!r}")

    from bernstein.compliance.evidence_pack import resolve_suite_controls

    controls_by_suite, unresolvable = resolve_suite_controls(bundles)

    controls = registry.list_controls()
    control_bundles: dict[str, list[SubmissionBundle]] = {c.control_id: [] for c in controls}

    for b in bundles:
        for cid in controls_by_suite.get(b.suite_version, ()):
            if cid in control_bundles:
                control_bundles[cid].append(b)

    observations: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []

    for c in controls:
        clause = c.references.get(reference_key, "unmapped")
        clause_prop = {"name": "clause", "ns": BERNSTEIN_PROP_NS, "value": clause}
        matched_bundles = control_bundles.get(c.control_id, [])
        if matched_bundles:
            b = max(matched_bundles, key=lambda m: float(m.submitted_at))
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
                    "collected": _collected_at(b),
                    "relevant-evidence": [
                        {
                            "href": f"bench-bundles/{b_hash}.json",
                            "description": (
                                f"Submission bundle for suite {b.suite_version} "
                                "(hash-consistent; signature not verified)"
                            ),
                        }
                    ],
                }
            )

            state = "satisfied" if b.overall_score >= satisfied_threshold else "not-satisfied"
            findings.append(
                {
                    "uuid": finding_uuid,
                    "title": f"Assessment finding for {c.control_id}: {c.title}",
                    "description": (
                        f"{c.description} Verdict: mean task score {b.overall_score:.4f} "
                        f"against satisfied threshold {satisfied_threshold:.2f}."
                    ),
                    "props": [clause_prop],
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
                    "props": [clause_prop],
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
    if unresolvable:
        doc["assessment-results"]["results"][0]["remarks"] = (
            "Bundles from suites that are not built in could not be mapped to "
            "controls and are not reflected in these findings: " + ", ".join(unresolvable) + "."
        )
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
    if not isinstance(metadata, dict) or metadata.get("oscal-version") != OSCAL_VERSION:
        return False
    results = ar["results"]
    if not isinstance(results, list) or len(results) == 0:
        return False
    res = results[0]
    return isinstance(res, dict) and "findings" in res and "uuid" in res
