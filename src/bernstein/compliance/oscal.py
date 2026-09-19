"""OSCAL assessment-results export for Bernstein compliance evidence packs.

Generates NIST OSCAL v1.1.0 assessment-results JSON documents from the
tamper-evident audit chain and lineage log. Output is validated against
the vendored OSCAL schema shipped as package data.
"""

from __future__ import annotations

import importlib.resources
import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import jsonschema

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

# OSCAL schema version this module targets
OSCAL_VERSION = "1.1.0"


def _load_schema() -> dict[str, Any]:
    """Load the vendored OSCAL assessment-results JSON schema."""
    try:
        # Try loading from package data (installed wheel)
        data = importlib.resources.read_binary(
            "bernstein.compliance.oscal_schema",
            "assessment-results-schema.json",
        )
        return json.loads(data.decode("utf-8"))
    except (FileNotFoundError, ImportError, ModuleNotFoundError):
        # Fallback to source tree for development
        schema_path = (
            Path(__file__).parent / "oscal_schema" / "assessment-results-schema.json"
        )
        if schema_path.is_file():
            return json.loads(schema_path.read_text(encoding="utf-8"))
        raise FileNotFoundError(
            "OSCAL assessment-results schema not found in package data or source tree"
        )


# Cached schema for validation
_SCHEMA_CACHE: dict[str, Any] | None = None


def _get_schema() -> dict[str, Any]:
    """Return cached schema or load and cache it."""
    global _SCHEMA_CACHE
    if _SCHEMA_CACHE is None:
        _SCHEMA_CACHE = _load_schema()
    return _SCHEMA_CACHE


def _now_iso() -> str:
    """Current UTC time in ISO-8601 with seconds precision."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    """Serialise as RFC 8785 (JCS) canonical JSON bytes."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def validate_oscal_assessment_results(
    document: dict[str, Any],
    *,
    strict: bool = True,
) -> tuple[bool, list[str]]:
    """Validate an OSCAL assessment-results document against the vendored schema.

    Args:
        document: The OSCAL assessment-results document to validate.
        strict: If True, raise on validation failure; if False, return errors.

    Returns:
        Tuple of (is_valid, error_messages).
    """
    schema = _get_schema()
    validator = jsonschema.Draft7Validator(schema)
    errors = list(validator.iter_errors(document))

    if not errors:
        return True, []

    messages = [f"{e.json_path}: {e.message}" for e in errors]
    if strict:
        raise jsonschema.ValidationError(
            f"OSCAL assessment-results validation failed: {'; '.join(messages)}"
        )
    return False, messages


def build_oscal_assessment_results(
    sdd_dir: Path,
    *,
    title: str = "Bernstein Compliance Assessment Results",
    version: str = "1.0.0",
    since: str = "",
    task: str = "all",
    include_lineage: bool = True,
    include_costs: bool = True,
) -> dict[str, Any]:
    """Build an OSCAL assessment-results document from the .sdd runtime directory.

    This reads the audit chain, lineage log, and cost ledger, then constructs
    an OSCAL assessment-results document mapping each control to findings
    derived from the tamper-evident evidence.

    Args:
        sdd_dir: Path to the project's .sdd runtime directory.
        title: Document title.
        version: Document version string.
        since: ISO-8601 lower bound for evidence; empty string disables filtering.
        task: Task filter ("all" or specific task id).
        include_lineage: Include lineage observations.
        include_costs: Include cost observations.

    Returns:
        OSCAL assessment-results document as a dict.
    """
    from bernstein.compliance.evidence_pack import (
        _read_audit_events,
        _read_lineage_entries,
        _read_cost_snapshots,
        _matches_task,
    )

    audit_dir = sdd_dir / "audit"
    lineage_log = sdd_dir / "lineage" / "log.jsonl"
    metrics_dir = sdd_dir / "metrics"

    events = _read_audit_events(audit_dir, since=since, task=task)
    lineage_entries = _read_lineage_entries(lineage_log, since=since, task=task)
    cost_entries = _read_cost_snapshots(metrics_dir, since=since, task=task)

    doc_uuid = str(uuid.uuid4())
    now = _now_iso()

    # Build parties list first to avoid circular reference
    parties = [
        {
            "uuid": str(uuid.uuid4()),
            "type": "organization",
            "name": "Bernstein",
        }
    ]

    # Build metadata
    metadata = {
        "title": title,
        "published": now,
        "last-modified": now,
        "version": version,
        "oscal-version": OSCAL_VERSION,
        "roles": [
            {"id": "assessor", "title": "Automated Bernstein Assessor"},
            {"id": "tool", "title": "Bernstein Governance Layer"},
        ],
        "parties": parties,
        "responsible-parties": [
            {
                "role-id": "assessor",
                "party-uuids": [p["uuid"] for p in parties],
            }
        ],
        "remarks": (
            f"Generated from Bernstein .sdd runtime directory at {sdd_dir}. "
            f"Filter: since={since or 'none'}, task={task}."
        ),
    }

    # Build import-ap reference to the evidence pack (if available)
    import_ap = {"href": "evidence-pack.zip", "remarks": "Bernstein compliance evidence pack"}

    # Build results - one result per control domain
    results = []

    # Aggregate events by control domain (using resource_type as proxy)
    control_domains: dict[str, list[dict[str, Any]]] = {}
    for ev in events:
        rtype = str(ev.get("resource_type", "unknown"))
        control_domains.setdefault(rtype, []).append(ev)

    for domain, domain_events in sorted(control_domains.items()):
        result_uuid = str(uuid.uuid4())

        # Observations from audit events
        observations = []
        for idx, ev in enumerate(domain_events):
            obs_uuid = str(uuid.uuid4())
            ts = str(ev.get("timestamp", now))
            observations.append(
                {
                    "uuid": obs_uuid,
                    "title": f"Audit event: {ev.get('event_type', 'unknown')}",
                    "description": json.dumps(ev, sort_keys=True),
                    "methods": ["EXAMINE", "TEST"],
                    "collected": ts,
                    "relevant-evidence": [
                        {
                            "description": f"Audit event {idx} in domain {domain}",
                            "href": f"audit-chain/events.jsonl#line-{idx}",
                        }
                    ],
                    "subject-uuids": [result_uuid],
                    "props": [
                        {"name": "event_type", "ns": "https://bernstein.run/oscal", "value": str(ev.get("event_type", ""))},
                        {"name": "outcome", "ns": "https://bernstein.run/oscal", "value": str(ev.get("outcome", ""))},
                        {"name": "actor", "ns": "https://bernstein.run/oscal", "value": str(ev.get("actor", ""))},
                        {"name": "resource_id", "ns": "https://bernstein.run/oscal", "value": str(ev.get("resource_id", ""))},
                    ],
                    "remarks": f"HMAC: {ev.get('hmac', 'n/a')[:16]}...",
                }
            )

        # Add lineage observations if requested
        if include_lineage:
            for le in lineage_entries:
                if not _matches_task(le, task):
                    continue
                obs_uuid = str(uuid.uuid4())
                ts = str(le.get("timestamp", now))
                observations.append(
                    {
                        "uuid": obs_uuid,
                        "title": "Lineage entry: artefact write",
                        "description": json.dumps(le, sort_keys=True),
                        "methods": ["EXAMINE", "TEST"],
                        "collected": ts,
                        "relevant-evidence": [
                            {
                                "description": f"Lineage entry {le.get('entry_hash', 'n/a')[:16]}...",
                                "href": f"lineage/log.jsonl#{le.get('entry_hash', '')}",
                            }
                        ],
                        "subject-uuids": [result_uuid],
                        "props": [
                            {"name": "content_hash", "ns": "https://bernstein.run/oscal", "value": str(le.get("content_hash", ""))},
                            {"name": "parent_hashes", "ns": "https://bernstein.run/oscal", "value": ",".join(str(h) for h in le.get("parent_hashes", []))},
                        ],
                        "remarks": "Sigstore-style transparency log entry",
                    }
                )

        # Add cost observations if requested
        if include_costs:
            for ce in cost_entries:
                obs_uuid = str(uuid.uuid4())
                ts = str(ce.get("date") or ce.get("timestamp") or now)
                observations.append(
                    {
                        "uuid": obs_uuid,
                        "title": "Cost ledger snapshot",
                        "description": json.dumps(ce, sort_keys=True),
                        "methods": ["EXAMINE"],
                        "collected": ts,
                        "relevant-evidence": [
                            {
                                "description": f"Cost snapshot for {ce.get('model', 'unknown')}",
                                "href": f"costs/cost_history.jsonl#{ce.get('task_id', '')}",
                            }
                        ],
                        "subject-uuids": [result_uuid],
                        "props": [
                            {"name": "model", "ns": "https://bernstein.run/oscal", "value": str(ce.get("model", ""))},
                            {"name": "usd", "ns": "https://bernstein.run/oscal", "value": str(ce.get("usd", 0))},
                            {"name": "task_id", "ns": "https://bernstein.run/oscal", "value": str(ce.get("task_id", ""))},
                        ],
                        "remarks": f"Tokens: {ce.get('tokens', 'n/a')}",
                    }
                )

        # Build findings - each domain maps to one finding
        findings = [
            {
                "uuid": str(uuid.uuid4()),
                "title": f"Control domain: {domain}",
                "description": f"Evidence from {len(domain_events)} audit events, {len(lineage_entries)} lineage entries, {len(cost_entries)} cost snapshots",
                "target": {
                    "type": "control",
                    "target-id": domain,
                    "status": {
                        "state": "satisfied" if domain_events else "not-satisfied",
                        "reason": "Audit events present" if domain_events else "No audit events found for this domain",
                    },
                },
                "related-observations": [
                    {"observation-uuid": o["uuid"], "title": o["title"]} for o in observations
                ],
                "props": [
                    {"name": "event_count", "ns": "https://bernstein.run/oscal", "value": str(len(domain_events))},
                    {"name": "lineage_count", "ns": "https://bernstein.run/oscal", "value": str(len(lineage_entries))},
                    {"name": "cost_count", "ns": "https://bernstein.run/oscal", "value": str(len(cost_entries))},
                ],
                "remarks": "Automatically derived from Bernstein tamper-evident audit chain",
            }
        ]

        results.append(
            {
                "uuid": result_uuid,
                "title": f"Assessment result: {domain}",
                "description": f"Evidence assessment for control domain {domain}",
                "start": min(
                    [str(e.get("timestamp", now)) for e in domain_events] + [now]
                ),
                "end": now,
                "observations": observations,
                "findings": findings,
                "related-results": [],
                "remarks": "",
            }
        )

    # Build back-matter with resources referenced
    resources = [
        {
            "uuid": str(uuid.uuid4()),
            "title": "Bernstein audit chain",
            "description": "HMAC-chained audit events",
            "href": "audit-chain/events.jsonl",
            "media-type": "application/jsonl",
        },
        {
            "uuid": str(uuid.uuid4()),
            "title": "Bernstein lineage log",
            "description": "Sigstore-style transparency log",
            "href": "lineage/log.jsonl",
            "media-type": "application/jsonl",
        },
        {
            "uuid": str(uuid.uuid4()),
            "title": "Bernstein cost ledger",
            "description": "Per-task cost snapshots",
            "href": "costs/cost_history.jsonl",
            "media-type": "application/jsonl",
        },
    ]

    document = {
        "assessment-results": {
            "uuid": doc_uuid,
            "metadata": metadata,
            "import-ap": import_ap,
            "results": results,
            "back-matter": {"resources": resources},
            "remarks": "",
        }
    }

    # Validate against schema
    validate_oscal_assessment_results(document, strict=True)

    return document


def export_oscal_assessment_results(
    sdd_dir: Path,
    output_path: Path,
    *,
    title: str = "Bernstein Compliance Assessment Results",
    version: str = "1.0.0",
    since: str = "",
    task: str = "all",
    include_lineage: bool = True,
    include_costs: bool = True,
) -> Path:
    """Build and write an OSCAL assessment-results JSON file.

    Args:
        sdd_dir: Path to the project's .sdd runtime directory.
        output_path: Destination file path.
        title: Document title.
        version: Document version string.
        since: ISO-8601 lower bound for evidence.
        task: Task filter ("all" or specific task id).
        include_lineage: Include lineage observations.
        include_costs: Include cost observations.

    Returns:
        The output_path that was written.
    """
    document = build_oscal_assessment_results(
        sdd_dir,
        title=title,
        version=version,
        since=since,
        task=task,
        include_lineage=include_lineage,
        include_costs=include_costs,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(document, indent=2, sort_keys=True), encoding="utf-8"
    )

    logger.info("OSCAL assessment-results written to %s", output_path)
    return output_path


def get_oscal_schema_path() -> Path:
    """Return the path to the vendored OSCAL assessment-results schema.

    This is useful for external tooling that needs the schema file directly.
    """
    try:
        # Try package data first (installed wheel)
        with importlib.resources.path(
            "bernstein.compliance.oscal_schema", "assessment-results-schema.json"
        ) as p:
            return p
    except (FileNotFoundError, ImportError, ModuleNotFoundError):
        # Fallback to source tree
        return Path(__file__).parent / "oscal_schema" / "assessment-results-schema.json"