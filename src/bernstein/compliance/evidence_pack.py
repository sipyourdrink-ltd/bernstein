"""One-command compliance evidence pack export (issue #1316, #5456).

Walks the existing tamper-evident artefacts on disk and produces a
reviewer-friendly zip bundle mapped to the controls of a chosen
regulatory standard.

Sources read:

* ``.sdd/audit/*.jsonl`` - HMAC-chained audit log (RFC 2104 chain).
* ``.sdd/lineage/log.jsonl`` - per-artefact transparency log (Sigstore-style).
* ``.sdd/metrics/cost_history.jsonl`` - daily cost ledger snapshots.
* ``.sdd/bench/bundles/*.json`` - signed benchmark evaluation bundles.
* ``.sdd/policy/`` (optional) - recorded operator policy decisions.
* ``.sdd/attestations/`` (optional) - operator-supplied signed assertions.

This module is intentionally read-only: it does not mutate or rotate
the audit chain. The output zip is byte-deterministic for a given input
so an auditor can re-derive the SHA-256 of the bundle and compare.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import zipfile
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA_VERSION: str = "1.0.0"

Standard = Literal["ai-act", "owasp-asi", "owasp-skills", "iso-42001"]
SUPPORTED_STANDARDS: tuple[str, ...] = ("ai-act", "owasp-asi", "owasp-skills", "iso-42001")

_FIXED_ZIP_DT: tuple[int, int, int, int, int, int] = (1980, 1, 1, 0, 0, 0)

_STANDARD_MAPS: dict[str, dict[str, Any]] = {
    "ai-act": {
        "regulation": "EU AI Act, Regulation (EU) 2024/1689",
        "controls": [
            {
                "control_id": "art-12(1)",
                "requirement": "Automatic recording of events over the lifetime of the system.",
                "artefact": "audit-chain/events.jsonl",
                "selector": "event_type",
                "status": "mapped",
            },
            {
                "control_id": "art-12(2)(a)",
                "requirement": "Identification of situations presenting a risk per Article 79(1).",
                "artefact": "audit-chain/events.jsonl",
                "selector": "event_type,outcome",
                "status": "mapped",
            },
            {
                "control_id": "art-12(2)(b)",
                "requirement": "Facilitation of post-market monitoring (Article 72).",
                "artefact": "audit-chain/data_catalog.json",
                "selector": "resource_type,resource_id",
                "status": "mapped",
            },
            {
                "control_id": "art-12(2)(c)",
                "requirement": "Monitoring of operation under Article 26(5).",
                "artefact": "audit-chain/events.jsonl",
                "selector": "actor,event_type",
                "status": "mapped",
            },
            {
                "control_id": "art-12(3)",
                "requirement": ("Logs kept at least 6 months; 10 years for high-risk systems under Article 19(1)."),
                "artefact": "manifest.json (retention block)",
                "selector": "n/a",
                "status": "mapped",
            },
            {
                "control_id": "art-15(1)",
                "requirement": "Accuracy, robustness and cybersecurity - evidence via lineage chain.",
                "artefact": "lineage/log.jsonl",
                "selector": "content_hash,parent_hashes",
                "status": "mapped",
            },
            {
                "control_id": "art-13",
                "requirement": "Transparency to deployers - cost + model attribution per task.",
                "artefact": "costs/cost_history.jsonl",
                "selector": "model,task_id,usd",
                "status": "mapped",
            },
        ],
        "deferred": [
            "Article 43 conformity assessment paperwork (out of MVP scope)",
            "Annex IV technical documentation (handled by compliance/eu_ai_act.py)",
        ],
    },
}

from bernstein.compliance import iso42001 as _iso42001  # noqa: E402
from bernstein.compliance import owasp_asi as _owasp_asi  # noqa: E402
from bernstein.compliance import owasp_skills as _owasp_skills  # noqa: E402

_STANDARD_MAPS[_owasp_asi.STANDARD_ID] = _owasp_asi.control_map()
_STANDARD_MAPS[_owasp_skills.STANDARD_ID] = _owasp_skills.control_map()
_STANDARD_MAPS[_iso42001.STANDARD_ID] = _iso42001.control_map()


@dataclass(frozen=True, slots=True)
class EvidencePack:
    """Result of an evidence-pack export."""

    standard: str
    bundle_id: str
    since: str
    task: str
    event_count: int
    lineage_count: int
    cost_count: int
    controls_mapped: int
    controls_partial: int
    controls_todo: int
    controls_organisational: int
    archive_path: Path | None
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of this result."""
        return {
            "schema_version": SCHEMA_VERSION,
            "standard": self.standard,
            "bundle_id": self.bundle_id,
            "since": self.since,
            "task": self.task,
            "event_count": self.event_count,
            "lineage_count": self.lineage_count,
            "cost_count": self.cost_count,
            "controls_mapped": self.controls_mapped,
            "controls_partial": self.controls_partial,
            "controls_todo": self.controls_todo,
            "controls_organisational": self.controls_organisational,
            "sha256": self.sha256,
        }


def _canonical_json(payload: Any) -> bytes:
    """Serialise ``payload`` as deterministic JSON (sort_keys, indent=2)."""
    return (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _parse_iso(value: str) -> datetime | None:
    """Parse an ISO-8601 string permissively; return ``None`` on failure."""
    if not value:
        return None
    cleaned = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None


def _matches_task(entry: dict[str, Any], task: str) -> bool:
    if task == "all":
        return True
    rtype = str(entry.get("resource_type", ""))
    rid = str(entry.get("resource_id", ""))
    if rtype == "task" and rid == task:
        return True
    if str(entry.get("task_id", "")) == task:
        return True
    details = entry.get("details") or {}
    if isinstance(details, dict):
        if str(details.get("task_id", "")) == task:
            return True
        if str(details.get("task", "")) == task:
            return True
    return False


def _read_audit_events(audit_dir: Path, *, since: str, task: str) -> list[dict[str, Any]]:
    if not audit_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(audit_dir.glob("*.jsonl")):
        if "_archived" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            ts = str(entry.get("timestamp", ""))
            if since and ts and ts < since:
                continue
            if not _matches_task(entry, task):
                continue
            out.append(entry)
    out.sort(key=lambda e: (str(e.get("timestamp", "")), str(e.get("hmac", ""))))
    return out


def _read_lineage_entries(lineage_log: Path, *, since: str, task: str) -> list[dict[str, Any]]:
    if not lineage_log.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        text = lineage_log.read_text(encoding="utf-8")
    except OSError:
        return []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        ts = str(entry.get("timestamp", ""))
        if since and ts and ts < since:
            continue
        if task != "all":
            meta = entry.get("meta") or {}
            meta_task = ""
            if isinstance(meta, dict):
                meta_task = str(meta.get("task_id", ""))
            if meta_task != task:
                continue
        out.append(entry)
    out.sort(key=lambda e: (str(e.get("timestamp", "")), str(e.get("entry_hash", ""))))
    return out


def _read_cost_snapshots(metrics_dir: Path, *, since: str, task: str) -> list[dict[str, Any]]:
    candidate = metrics_dir / "cost_history.jsonl"
    if not candidate.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        text = candidate.read_text(encoding="utf-8")
    except OSError:
        return []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        when = str(entry.get("date") or entry.get("timestamp") or "")
        if since and when and when < since:
            continue
        if task != "all" and str(entry.get("task_id", "")) != task:
            continue
        out.append(entry)
    out.sort(key=lambda e: str(e.get("date") or e.get("timestamp") or ""))
    return out


def _read_bench_bundles(sdd_dir: Path) -> list[Any]:
    from bernstein.eval.bench.bundle import SubmissionBundle

    bundles: list[Any] = []
    seen: set[str] = set()
    for candidate_dir in [sdd_dir / "bench" / "bundles", sdd_dir / "bundles"]:
        if not candidate_dir.is_dir():
            continue
        for path in sorted(candidate_dir.glob("*.json")):
            try:
                b = SubmissionBundle.load(path)
                b_hash = b.bundle_hash()
                if b_hash not in seen:
                    seen.add(b_hash)
                    bundles.append(b)
            except Exception:
                continue
    return bundles


def _compute_bench_assessment(bundles: list[Any]) -> dict[str, Any]:
    from bernstein.compliance.controls import get_default_registry

    registry = get_default_registry()
    controls = registry.list_controls()
    assessment: dict[str, Any] = {}

    for c in controls:
        matched = []
        for b in bundles:
            suite_ctls = getattr(b, "controls", [])
            if not suite_ctls and b.suite_version == "golden-v1":
                suite_ctls = ["CTL-ROB-01", "CTL-EVAL-01", "CTL-EVAL-02", "CTL-QUAL-02"]
            if c.control_id in suite_ctls:
                matched.append(b)

        if matched:
            latest = matched[-1]
            b_hash = latest.bundle_hash()
            assessment[c.control_id] = {
                "status": "measured",
                "suite_version": latest.suite_version,
                "bundle_hash": b_hash,
                "score": latest.overall_score,
                "tasks_count": len(latest.task_results),
                "passed_count": sum(1 for r in latest.task_results if r.passed),
                "reason": f"measured by suite {latest.suite_version} ({b_hash[:12]})",
            }
        else:
            assessment[c.control_id] = {
                "status": "declared_not_measured",
                "suite_version": None,
                "bundle_hash": None,
                "score": None,
                "tasks_count": 0,
                "passed_count": 0,
                "reason": (
                    f"Control {c.control_id} is registered in catalogue "
                    "but no matching evaluation bundle was found in the pack."
                ),
            }
    return assessment


def _serialise_jsonl(entries: list[dict[str, Any]]) -> bytes:
    buf = io.BytesIO()
    for entry in entries:
        buf.write(json.dumps(entry, sort_keys=True).encode("utf-8"))
        buf.write(b"\n")
    return buf.getvalue()


def _build_data_catalog(events: list[dict[str, Any]]) -> bytes:
    catalog: dict[str, dict[str, int]] = {}
    for ev in events:
        rtype = str(ev.get("resource_type", "")) or "unknown"
        rid = str(ev.get("resource_id", "")) or "unknown"
        bucket = catalog.setdefault(rtype, {})
        bucket[rid] = bucket.get(rid, 0) + 1
    payload = {
        "schema_version": SCHEMA_VERSION,
        "resources": {rtype: dict(sorted(items.items())) for rtype, items in sorted(catalog.items())},
        "total_events": len(events),
    }
    return _canonical_json(payload)


def _read_text_directory(directory: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    if not directory.is_dir():
        return out
    base = directory.resolve()
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        try:
            resolved = path.resolve()
            resolved.relative_to(base)
        except (OSError, ValueError):
            continue
        try:
            out[str(path.relative_to(directory))] = path.read_bytes()
        except OSError:
            continue
    return out


def _readme_for(standard: str, mapping: dict[str, Any]) -> bytes:
    lines = [
        "# Bernstein compliance evidence pack",
        "",
        f"Standard: {standard}",
        f"Regulation: {mapping.get('regulation', 'n/a')}",
        f"Schema: {SCHEMA_VERSION}",
        "",
        "## Layout",
        "",
        "- `manifest.json`        - bundle metadata + SHA-256 of every artefact.",
        "- `controls.json`        - control_id -> artefact mapping & benchmark assessment.",
        "- `audit-chain/`         - HMAC-chained audit events + per-resource catalog.",
        "- `lineage/`             - Sigstore-style transparency log entries.",
        "- `costs/`               - cost ledger snapshots over the export window.",
        "- `bench-bundles/`       - signed evaluation benchmark bundles.",
        "- `policy/`              - operator policy snapshot (optional).",
        "- `attestations/`        - operator-supplied attestations (optional).",
        "",
        "## Verification",
        "",
        "Each artefact in `manifest.json` carries a SHA-256 digest. To check ",
        "the bundle was not modified after export, re-hash each file and ",
        "compare against the manifest entry. To verify the audit chain ",
        "itself, run `bernstein audit verify-hmac` against the original ",
        "`.sdd/audit/` directory (the HMAC key never travels in the pack).",
        "",
        "## Out of scope",
        "",
        "This pack is evidence, not a report. Operators are expected to ",
        "produce the human-readable narrative (e.g. an EU AI Act Annex IV ",
        "section, a DORA Article 28 register, or a FINOS AIGF cross-walk) ",
        "separately, citing the artefacts in this bundle.",
        "",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _bundle_id(standard: str, since: str, task: str) -> str:
    seed = f"{standard}|{since}|{task}".encode()
    return hashlib.sha256(seed).hexdigest()[:32]


def _zip_artefacts(artefacts: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(artefacts):
            info = zipfile.ZipInfo(filename=name, date_time=_FIXED_ZIP_DT)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, artefacts[name])
    return buf.getvalue()


def get_standard_map(standard: str) -> dict[str, Any]:
    if standard not in _STANDARD_MAPS:
        raise ValueError(
            f"unknown standard {standard!r}; supported: {', '.join(SUPPORTED_STANDARDS)}",
        )
    return _STANDARD_MAPS[standard]


def build_evidence_pack(
    sdd_dir: Path,
    *,
    standard: str,
    since: str = "",
    task: str = "all",
    output_path: Path | None = None,
    write: bool = True,
) -> EvidencePack:
    if standard not in _STANDARD_MAPS:
        raise ValueError(
            f"unknown standard {standard!r}; supported: {', '.join(SUPPORTED_STANDARDS)}",
        )
    if since and _parse_iso(since) is None:
        raise ValueError(f"--since must be ISO-8601, got {since!r}")

    audit_dir = sdd_dir / "audit"
    lineage_log = sdd_dir / "lineage" / "log.jsonl"
    metrics_dir = sdd_dir / "metrics"
    policy_dir = sdd_dir / "policy"
    attestations_dir = sdd_dir / "attestations"

    events = _read_audit_events(audit_dir, since=since, task=task)
    lineage_entries = _read_lineage_entries(lineage_log, since=since, task=task)
    cost_entries = _read_cost_snapshots(metrics_dir, since=since, task=task)
    bundles = _read_bench_bundles(sdd_dir)

    events_bytes = _serialise_jsonl(events)
    data_catalog_bytes = _build_data_catalog(events)
    lineage_bytes = _serialise_jsonl(lineage_entries)
    costs_bytes = _serialise_jsonl(cost_entries)

    mapping = _STANDARD_MAPS[standard]
    bench_assessment = _compute_bench_assessment(bundles)
    controls_payload = {
        "schema_version": SCHEMA_VERSION,
        "standard": standard,
        "regulation": mapping.get("regulation", ""),
        "controls": mapping["controls"],
        "deferred": mapping.get("deferred", []),
        "bench_assessment": bench_assessment,
    }
    controls_bytes = _canonical_json(controls_payload)

    policy_files = _read_text_directory(policy_dir)
    attestation_files = _read_text_directory(attestations_dir)

    artefacts: dict[str, bytes] = {
        "audit-chain/events.jsonl": events_bytes,
        "audit-chain/data_catalog.json": data_catalog_bytes,
        "lineage/log.jsonl": lineage_bytes,
        "costs/cost_history.jsonl": costs_bytes,
        "controls.json": controls_bytes,
        "README.md": _readme_for(standard, mapping),
    }

    # Embed benchmark bundles
    for b in bundles:
        bundle_bytes = _canonical_json(b.to_dict())
        b_hash = b.bundle_hash()
        artefacts[f"bench-bundles/{b_hash}.json"] = bundle_bytes

    for rel, payload in policy_files.items():
        artefacts[f"policy/{rel}"] = payload
    for rel, payload in attestation_files.items():
        artefacts[f"attestations/{rel}"] = payload

    if not policy_files:
        artefacts["policy/.empty"] = b""
    if not attestation_files:
        artefacts["attestations/.empty"] = b""

    artefact_hashes = {name: hashlib.sha256(payload).hexdigest() for name, payload in artefacts.items()}

    controls = mapping["controls"]
    controls_mapped = sum(1 for c in controls if c.get("status") == "mapped")
    controls_partial = sum(1 for c in controls if c.get("status") == "partial")
    controls_todo = sum(1 for c in controls if c.get("status") == "todo")
    controls_organisational = sum(1 for c in controls if c.get("status") == "organisational")

    bundle_id = _bundle_id(standard, since, task)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "bundle_id": bundle_id,
        "standard": standard,
        "regulation": mapping.get("regulation", ""),
        "since": since,
        "task": task,
        "event_count": len(events),
        "lineage_count": len(lineage_entries),
        "cost_count": len(cost_entries),
        "controls_mapped": controls_mapped,
        "controls_partial": controls_partial,
        "controls_todo": controls_todo,
        "controls_organisational": controls_organisational,
        "generated_at_utc": "1970-01-01T00:00:00+00:00",
        "artefacts": dict(sorted(artefact_hashes.items())),
    }
    artefacts["manifest.json"] = _canonical_json(manifest)

    archive_bytes = _zip_artefacts(artefacts)
    archive_sha256 = hashlib.sha256(archive_bytes).hexdigest()

    archive_path: Path | None = None
    if write:
        if output_path is None:
            output_dir = sdd_dir / "evidence"
            output_dir.mkdir(parents=True, exist_ok=True)
            archive_path = output_dir / f"evidence_{standard}_{bundle_id}.zip"
        else:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            archive_path = output_path
        archive_path.write_bytes(archive_bytes)

    return EvidencePack(
        standard=standard,
        bundle_id=bundle_id,
        since=since,
        task=task,
        event_count=len(events),
        lineage_count=len(lineage_entries),
        cost_count=len(cost_entries),
        controls_mapped=controls_mapped,
        controls_partial=controls_partial,
        controls_todo=controls_todo,
        controls_organisational=controls_organisational,
        archive_path=archive_path,
        sha256=archive_sha256,
    )


def verify_evidence_pack(pack_path: Path) -> bool:
    """Verify integrity of an evidence pack and its embedded benchmark bundles."""
    if not pack_path.is_file():
        return False
    try:
        with zipfile.ZipFile(pack_path, "r") as zf:
            names = zf.namelist()
            if "manifest.json" not in names:
                return False
            manifest_bytes = zf.read("manifest.json")
            manifest = json.loads(manifest_bytes.decode("utf-8"))
            artefact_hashes = manifest.get("artefacts", {})

            for name, expected_hash in artefact_hashes.items():
                if name not in names:
                    return False
                member_bytes = zf.read(name)
                actual_hash = hashlib.sha256(member_bytes).hexdigest()
                if actual_hash != expected_hash:
                    return False

            # Verify all benchmark bundles embedded in the pack
            from bernstein.eval.bench.bundle import SubmissionBundle, TaskResult

            for name in names:
                if name.startswith("bench-bundles/") and name.endswith(".json"):
                    raw = json.loads(zf.read(name).decode("utf-8"))
                    task_results = []
                    for tr in raw.get("task_results", raw.get("results", [])):
                        receipt = tr.get("receipt", {})
                        canonical = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
                        recomputed_hash = hashlib.sha256(canonical).hexdigest()
                        if tr.get("receipt_hash") != recomputed_hash:
                            return False
                        task_results.append(
                            TaskResult(
                                task_id=tr["task_id"],
                                task_hash=tr["task_hash"],
                                receipt=tr["receipt"],
                                passed=tr["passed"],
                                score=tr["score"],
                                harness_output=tr.get("harness_output", {}),
                                stored_receipt_hash=tr.get("receipt_hash", ""),
                            )
                        )

                    bundle = SubmissionBundle(
                        suite_version=raw["suite_version"],
                        suite_hash=raw["suite_hash"],
                        task_results=task_results,
                        scheduler_config=raw.get("scheduler_config", {}),
                        submitted_at=raw.get("submitted_at", 0.0),
                        signature=raw.get("signature", ""),
                        signer_fingerprint=raw.get("signer_fingerprint", ""),
                    )
                    if bundle.bundle_hash() != raw.get("bundle_hash"):
                        return False
            return True
    except Exception:
        return False


__all__ = [
    "SCHEMA_VERSION",
    "SUPPORTED_STANDARDS",
    "EvidencePack",
    "Standard",
    "build_evidence_pack",
    "get_standard_map",
    "verify_evidence_pack",
]
