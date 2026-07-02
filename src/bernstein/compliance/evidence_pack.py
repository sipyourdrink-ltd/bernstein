"""One-command compliance evidence pack export (issue #1316).

Walks the existing tamper-evident artefacts on disk and produces a
reviewer-friendly zip bundle mapped to the controls of a chosen
regulatory standard.

Sources read:

* ``.sdd/audit/*.jsonl`` - HMAC-chained audit log (RFC 2104 chain).
* ``.sdd/lineage/log.jsonl`` - per-artefact transparency log (Sigstore-style).
* ``.sdd/metrics/cost_history.jsonl`` - daily cost ledger snapshots.
* ``.sdd/policy/`` (optional) - recorded operator policy decisions.
* ``.sdd/attestations/`` (optional) - operator-supplied signed assertions.

This module is intentionally read-only: it does not mutate or rotate
the audit chain. The output zip is byte-deterministic for a given input
so an auditor can re-derive the SHA-256 of the bundle and compare.

The mapping from regulatory ``control_id`` to a record selector is
declarative and lives inside this module (see ``_STANDARD_MAPS``). At
MVP only the EU AI Act mapping is fleshed out. DORA and FINOS AIGF
control maps are tracked under issue #1316 and are not selectable
until the underlying clause mappings are reviewed by subject-matter
experts; attempting to build a pack for an unsupported standard
raises ``ValueError``.

Usage:

    from bernstein.compliance.evidence_pack import build_evidence_pack

    result = build_evidence_pack(
        sdd_dir=Path(".sdd"),
        standard="ai-act",
        since="2026-01-01T00:00:00+00:00",
        task="all",
        output_path=Path("/tmp/evidence.zip"),
    )

Out of scope for the MVP (tracked in #1316):

* PDF/Markdown narrative report generation.
* DORA Articles 8-15 evidence templates (selector not yet defined).
* FINOS AIGF control catalogue mapping (selector not yet defined).
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

#: Schema version emitted into manifest.json. Bump on any breaking
#: change to layout, file names, or hash inputs.
SCHEMA_VERSION: str = "1.0.0"

#: Supported standards. ``ai-act`` maps the EU AI Act Article 12/13/15
#: clauses; ``owasp-asi`` and ``owasp-skills`` map the OWASP Top 10 for
#: Agentic Applications (ASI01-ASI10) and the Agentic Skills Top 10
#: (AST01-AST10) onto the same audit-chain artefacts. DORA and FINOS
#: AIGF are tracked under #1316 and are not selectable until their
#: clause maps are reviewed by subject-matter experts; emitting
#: TODO-only bundles would mislead operators.
Standard = Literal["ai-act", "owasp-asi", "owasp-skills"]

SUPPORTED_STANDARDS: tuple[str, ...] = ("ai-act", "owasp-asi", "owasp-skills")

#: Fixed mtime for every entry in the produced zip - required for
#: byte-deterministic output. Zip cannot store dates before 1980.
_FIXED_ZIP_DT: tuple[int, int, int, int, int, int] = (1980, 1, 1, 0, 0, 0)

# ---------------------------------------------------------------------------
# Standard -> control map
# ---------------------------------------------------------------------------
#
# Each entry maps a regulatory ``control_id`` to:
#   * ``requirement`` - short paraphrase of the underlying clause.
#   * ``artefact``    - bundle file (relative to zip root) that satisfies it.
#   * ``selector``    - informational: which event attribute carries the
#                       primary evidence (``event_type``, ``resource_type``,
#                       etc). Free-form string; not enforced at MVP.
#   * ``status``      - ``"mapped"`` or ``"todo"``.
#
# The ``ai-act`` block intentionally mirrors the structure used by the
# Article 12 bundle (``article12_bundle.py``) so an auditor switching
# between the two outputs sees consistent clause IDs.

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

# The OWASP ASI / AST maps live in dedicated modules (one control class per
# module) and are registered here so ``build_evidence_pack`` reads them the
# same way it reads ``ai-act``. Registration is a plain assignment - the
# modules only depend on stdlib, so importing them at module load is cheap
# and side-effect free.
from bernstein.compliance import owasp_asi as _owasp_asi  # noqa: E402
from bernstein.compliance import owasp_skills as _owasp_skills  # noqa: E402

_STANDARD_MAPS[_owasp_asi.STANDARD_ID] = _owasp_asi.control_map()
_STANDARD_MAPS[_owasp_skills.STANDARD_ID] = _owasp_skills.control_map()


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvidencePack:
    """Result of an evidence-pack export.

    Attributes:
        standard: Regulatory standard the pack is mapped against.
        bundle_id: Stable hash over ``(standard, since, task)``.
        since: Inclusive ISO-8601 lower bound (or ``""`` if not filtered).
        task: Task filter applied (``"all"`` or a specific task id).
        event_count: Number of audit events captured.
        lineage_count: Number of lineage entries captured.
        cost_count: Number of cost snapshots captured.
        controls_mapped: How many controls have ``status == "mapped"``.
        controls_partial: How many controls have ``status == "partial"``.
        controls_todo: How many controls remain TODO for the standard.
        archive_path: On-disk path to the written zip (``None`` for dry-run).
        sha256: SHA-256 of the produced zip bytes.
    """

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
            "sha256": self.sha256,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
    """Return True iff ``entry`` belongs to ``task``.

    Task association is checked against a few well-known fields written
    by the orchestrator audit producers: ``resource_id`` (when
    ``resource_type == "task"``), an explicit ``task_id``/``task``
    detail, or a top-level ``task_id`` field. ``task == "all"`` matches
    every entry.
    """
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


def _read_audit_events(
    audit_dir: Path,
    *,
    since: str,
    task: str,
) -> list[dict[str, Any]]:
    """Read HMAC-chained audit events, filtered by ``since`` and ``task``."""
    if not audit_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(audit_dir.glob("*.jsonl")):
        # Skip archived corrupt files (live in _archived/ subtree).
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


def _read_lineage_entries(
    lineage_log: Path,
    *,
    since: str,
    task: str,
) -> list[dict[str, Any]]:
    """Read lineage log entries; filter by ``since`` / ``task`` heuristically.

    The lineage log is the source of truth for content hashes and the
    detached signature on every artefact write. Lineage entries do not
    always carry a task id, so the ``task`` filter is best-effort here
    and matches against ``meta.task_id`` when present (else falls back
    to including the entry when ``task == "all"``).
    """
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


def _read_cost_snapshots(
    metrics_dir: Path,
    *,
    since: str,
    task: str,
) -> list[dict[str, Any]]:
    """Read cost-ledger snapshots from ``.sdd/metrics/cost_history.jsonl``.

    Snapshots are filtered by ``since`` (against the snapshot
    ``date``/``timestamp``) and by ``task`` when the snapshot carries
    a ``task_id`` field.
    """
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


def _serialise_jsonl(entries: list[dict[str, Any]]) -> bytes:
    """Serialise a list of dicts as canonical JSONL (sort_keys, ``\\n``)."""
    buf = io.BytesIO()
    for entry in entries:
        buf.write(json.dumps(entry, sort_keys=True).encode("utf-8"))
        buf.write(b"\n")
    return buf.getvalue()


def _build_data_catalog(events: list[dict[str, Any]]) -> bytes:
    """Aggregate per-resource activity counts from the audit slice."""
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
    """Return ``{relative_name: bytes}`` for every regular file in ``directory``.

    Used to capture operator-supplied ``policy/`` and ``attestations/``
    folders verbatim. Output is sorted by name for determinism. Symlinks
    pointing outside ``directory`` are skipped to avoid escaping the
    project root.
    """
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
    """Operator-facing README explaining the bundle layout for ``standard``."""
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
        "- `controls.json`        - control_id -> artefact mapping for this standard.",
        "- `audit-chain/`         - HMAC-chained audit events + per-resource catalog.",
        "- `lineage/`             - Sigstore-style transparency log entries.",
        "- `costs/`               - cost ledger snapshots over the export window.",
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
    """Compute the deterministic bundle id."""
    seed = f"{standard}|{since}|{task}".encode()
    return hashlib.sha256(seed).hexdigest()[:32]


def _zip_artefacts(artefacts: dict[str, bytes]) -> bytes:
    """Pack ``{name: bytes}`` into a deterministic zip.

    Determinism rules:

    * Files written in sorted order.
    * Fixed mtime (1980-01-01).
    * Mode 0644.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(artefacts):
            info = zipfile.ZipInfo(filename=name, date_time=_FIXED_ZIP_DT)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, artefacts[name])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_standard_map(standard: str) -> dict[str, Any]:
    """Return the control-mapping block for ``standard``.

    Raises:
        ValueError: If ``standard`` is not one of ``SUPPORTED_STANDARDS``.
    """
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
    """Assemble a compliance evidence pack.

    Args:
        sdd_dir: The project's ``.sdd`` runtime directory.
        standard: One of ``SUPPORTED_STANDARDS``.
        since: ISO-8601 lower bound; ``""`` disables time filtering.
        task: Task id to scope to, or ``"all"``.
        output_path: Destination zip file. When ``None`` and
            ``write=True``, defaults to ``<sdd_dir>/evidence/<bundle_id>.zip``.
        write: When False, build in-memory only (used by ``--dry-run``).

    Returns:
        An :class:`EvidencePack` summarising the produced bundle.

    Raises:
        ValueError: For unknown standards or malformed ``since``.
    """
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

    events_bytes = _serialise_jsonl(events)
    data_catalog_bytes = _build_data_catalog(events)
    lineage_bytes = _serialise_jsonl(lineage_entries)
    costs_bytes = _serialise_jsonl(cost_entries)

    mapping = _STANDARD_MAPS[standard]
    controls_payload = {
        "schema_version": SCHEMA_VERSION,
        "standard": standard,
        "regulation": mapping.get("regulation", ""),
        "controls": mapping["controls"],
        "deferred": mapping.get("deferred", []),
    }
    controls_bytes = _canonical_json(controls_payload)

    policy_files = _read_text_directory(policy_dir)
    attestation_files = _read_text_directory(attestations_dir)

    # Assemble the artefact dict - keys are zip paths.
    artefacts: dict[str, bytes] = {
        "audit-chain/events.jsonl": events_bytes,
        "audit-chain/data_catalog.json": data_catalog_bytes,
        "lineage/log.jsonl": lineage_bytes,
        "costs/cost_history.jsonl": costs_bytes,
        "controls.json": controls_bytes,
        "README.md": _readme_for(standard, mapping),
    }
    for rel, payload in policy_files.items():
        artefacts[f"policy/{rel}"] = payload
    for rel, payload in attestation_files.items():
        artefacts[f"attestations/{rel}"] = payload

    # Touch-stones: empty directories still need a placeholder so the
    # bundle layout described in the README is always present. Zip
    # cannot hold true empty directories portably, so we emit a tiny
    # marker file when no operator-supplied content exists.
    if not policy_files:
        artefacts["policy/.empty"] = b""
    if not attestation_files:
        artefacts["attestations/.empty"] = b""

    artefact_hashes = {name: hashlib.sha256(payload).hexdigest() for name, payload in artefacts.items()}

    controls = mapping["controls"]
    controls_mapped = sum(1 for c in controls if c.get("status") == "mapped")
    controls_partial = sum(1 for c in controls if c.get("status") == "partial")
    controls_todo = sum(1 for c in controls if c.get("status") == "todo")

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
        "generated_at_utc": "1970-01-01T00:00:00+00:00",  # deterministic, see note below
        "artefacts": dict(sorted(artefact_hashes.items())),
    }
    # The bundle is byte-deterministic: ``generated_at_utc`` is a fixed
    # sentinel rather than wall-clock ``now`` so two runs of the same
    # input produce the same SHA-256. Operators who need a real "issued
    # at" timestamp should sign the zip externally with their CI's
    # provenance attestation (e.g. ``gh attestation``).
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
        archive_path=archive_path,
        sha256=archive_sha256,
    )


__all__ = [
    "SCHEMA_VERSION",
    "SUPPORTED_STANDARDS",
    "EvidencePack",
    "Standard",
    "build_evidence_pack",
    "get_standard_map",
]
