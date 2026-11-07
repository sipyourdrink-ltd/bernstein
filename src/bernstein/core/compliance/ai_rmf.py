"""NIST AI RMF evidence pack (one artefact for GOVERN, MAP, MEASURE, MANAGE).

Projects the agreed subcategory table in ``docs/compliance/nist-ai-rmf-mapping.md``
onto the lineage chain for a window. Covered rows carry the chain entries
whose artefact path matches that row's mechanism. A window with nothing that
matches produces a sealed pack and does not claim those rows were evidenced.

The Generative AI Profile (NIST.AI.600-1) is not a second catalogue: each row's
``genai_profile_ref`` is the same Core subcategory id, which is how that
profile publishes its suggested actions.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from bernstein.core.compliance.pack import (
    _canonical_json,
    _collect_sig_card_members,
    _filter_entries,
    _read_entries,
    _regulator_verify_instructions,
    _seal_pack,
)
from bernstein.core.lineage.entry import canonicalise, entry_hash

if TYPE_CHECKING:
    from datetime import date
    from pathlib import Path

    from bernstein.core.lineage.entry import LineageEntry

PACK_KIND_AI_RMF = "ai-rmf"

_SUBCATEGORY_RE = re.compile(r"^\| `(GOVERN|MAP|MEASURE|MANAGE)-\d+\.\d+` \|")
_VERDICT_RE = re.compile(r"\b(Covered|Partial|Not-covered)\b")

#: Path tokens that tie a Covered subcategory to chain entries. A row marked
#: Covered in the mapping and missing from this map cannot be evidenced; the
#: pack test fails closed on that drift.
#:
#: Rows share tokens on purpose (``denial``, ``incident``, ``quarantine``,
#: ``audit-chain``, ``sbom``, ``dlp``, ``data-residency``, ``plan-approval``):
#: each row is matched independently, so one quarantine entry evidences
#: GOVERN-4.3, MANAGE-2.3 and MANAGE-2.4 at once.
#:
#: This table is agreement, not verification. None of the modules the mapping
#: names writes to the lineage log, and no in-tree lineage producer emits an
#: entry these tokens match today.
#: ``test_covered_row_evidences_from_real_producer_events`` records that per
#: row and turns red when a producer starts emitting one.
COVERED_EVIDENCE_SELECTORS: dict[str, tuple[str, ...]] = {
    "GOVERN-3.2": ("approval", "plan-approval", "oversight-gate"),
    "GOVERN-4.3": ("incident", "denial", "quarantine"),
    "GOVERN-6.1": ("sbom", "sigstore", "attest", "certify"),
    "MAP-2.1": ("model-routing", "task-graph"),
    "MAP-3.5": ("dual-approval", "auto-approve", "plan-approval"),
    "MAP-4.2": ("capability-matrix", "data-residency", "dlp", "input-refusal"),
    "MEASURE-2.4": ("cost-ledger", "denial", "audit-chain"),
    "MEASURE-2.7": ("socket-guard", "state-encryption", "asi-detector", "sbom"),
    "MEASURE-2.8": ("audit-dsse", "article12", "audit-chain"),
    "MEASURE-2.10": ("dlp", "pii-output", "data-residency"),
    "MEASURE-3.1": ("denial", "incident", "audit-chain"),
    "MANAGE-1.3": ("incident-response",),
    "MANAGE-2.3": ("incident-response", "quarantine", "denial"),
    "MANAGE-2.4": ("quarantine", "role-adapter-deny"),
    "MANAGE-3.1": ("endpoint-cert", "sigstore", "data-residency"),
    "MANAGE-4.3": ("incident-response", "denial", "audit-chain"),
}


def parse_mapping_rows(text: str) -> list[dict[str, str]]:
    """Return one dict per subcategory row, in document order."""
    rows: list[dict[str, str]] = []
    for line in text.splitlines():
        if not _SUBCATEGORY_RE.match(line):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 5:
            raise ValueError(f"malformed NIST AI RMF mapping row: {line!r}")
        subcategory, outcome, mechanism, modules, verdict_cell = cells[:5]
        sub_id = subcategory.strip().strip("`")
        verdict_match = _VERDICT_RE.search(verdict_cell)
        if verdict_match is None:
            raise ValueError(f"{sub_id}: verdict cell {verdict_cell!r} is not a known verdict")
        rows.append(
            {
                "id": sub_id,
                "function": sub_id.split("-", 1)[0],
                "outcome": outcome,
                "mechanism": mechanism,
                "modules": modules,
                "verdict": verdict_match.group(1),
            }
        )
    if not rows:
        raise ValueError("NIST AI RMF mapping contained no subcategory rows")
    return rows


def _matches(entry: LineageEntry, tokens: tuple[str, ...]) -> bool:
    """Return whether any token is a substring of the entry's path or kind.

    Matching is literal and case-insensitive substring, with no path-segment
    boundary: ``approval`` matches ``pre-approval-bypass``, and a source edit
    whose path merely contains a token (``src/app/approval.py``) counts the
    same as a control record. The verifier cannot tell the two apart, so a
    selector token is a claim about every path that contains it.
    """
    haystack = f"{entry.artefact_path} {entry.artefact_kind}".lower()
    return any(token in haystack for token in tokens)


def _window_claim(entry_count: int, any_evidenced: bool) -> str:
    if entry_count == 0:
        return "empty"
    if not any_evidenced:
        return "unmatched"
    return "evidenced"


def _readme(*, org: str, since: date, until: date, claim: str, entry_count: int) -> str:
    return (
        f"# NIST AI RMF evidence pack - {org}\n\n"
        f"**Period:** {since.isoformat()} -> {until.isoformat()}\n"
        f"**Lineage entries in period:** {entry_count}\n"
        f"**Window claim:** {claim}\n\n"
        "One artefact for the four AI RMF functions (GOVERN, MAP, MEASURE,\n"
        "MANAGE). Each Covered subcategory is judged on its own and carries\n"
        "the chain entries that match its mechanism; the window claim is\n"
        "`evidenced` when at least one row carries entries, so other Covered\n"
        "rows in the same pack can still show `evidenced: false`. `empty` and\n"
        "`unmatched` windows are valid packs and do not claim those rows were\n"
        "evidenced.\n\n"
        "Partial rows carry `evidenced: false` and no chain entries: the\n"
        "mapping says the control exists but does not reach the whole row.\n"
        "Not-covered rows carry none either.\n\n"
        "`mapping_sha256` in `subcategory-evidence.json` is the SHA-256 of the\n"
        "crosswalk file that projected this pack.\n\n"
        "Generative AI Profile (NIST.AI.600-1) suggested actions are read\n"
        "against `genai_profile_ref` on the same Core subcategory row. This\n"
        "pack is not a second control catalogue and not a certification.\n\n"
        "## Contents\n\n"
        "- `subcategory-evidence.json` - one row per subcategory, with chain\n"
        "  entry hashes for Covered rows the window actually evidenced.\n"
        "- `lineage-log.jsonl` - the signed entries in the window (canonical bytes).\n"
        "- `signatures/`, `agent-cards/` - per-entry Ed25519 JWS + cards.\n"
        "- `pack-manifest.json(.sig)` - signed SLSA-style provenance.\n"
    )


def build_ai_rmf_pack(
    *,
    since: date,
    until: date,
    org: str,
    lineage_dir: Path,
    agent_cards_dir: Path,
    mapping_path: Path,
    output_path: Path,
    operator_key_path: Path,
) -> Path:
    """Assemble one sealed NIST AI RMF pack for ``[since, until]``."""
    build_started_at = datetime.now(UTC).isoformat(timespec="seconds")
    mapping_bytes = mapping_path.read_bytes()
    mapping_rows = parse_mapping_rows(mapping_bytes.decode("utf-8"))

    all_entries = _read_entries(lineage_dir / "log.jsonl")
    filtered = _filter_entries(all_entries, since, until)
    ordered = sorted(filtered, key=lambda entry: (entry.ts_ns, entry_hash(entry)))
    hashed = [(entry, entry_hash(entry)) for entry in ordered]

    evidence_rows: list[dict[str, Any]] = []
    any_evidenced = False
    for row in mapping_rows:
        tokens = COVERED_EVIDENCE_SELECTORS.get(row["id"], ())
        matched: list[str] = []
        if row["verdict"] == "Covered" and tokens:
            matched = [digest for entry, digest in hashed if _matches(entry, tokens)]
        evidenced = bool(matched)
        any_evidenced = any_evidenced or evidenced
        evidence_rows.append(
            {
                "id": row["id"],
                "function": row["function"],
                "verdict": row["verdict"],
                "genai_profile_ref": row["id"],
                "chain_entry_hashes": matched,
                "evidenced": evidenced,
            }
        )

    claim = _window_claim(len(ordered), any_evidenced)
    evidence = {
        "kind": PACK_KIND_AI_RMF,
        "org": org,
        "period": {"since": since.isoformat(), "until": until.isoformat()},
        "entry_count": len(ordered),
        "window_claim": claim,
        "mapping_sha256": hashlib.sha256(mapping_bytes).hexdigest(),
        "rows": evidence_rows,
    }
    evidence_bytes = _canonical_json(evidence)

    log_lines = [canonicalise(entry) for entry in ordered]
    log_bytes = b"\n".join(log_lines) + (b"\n" if log_lines else b"")
    sig_payload, card_payload = _collect_sig_card_members(
        ordered,
        lineage_dir / "signatures",
        agent_cards_dir,
    )

    members: dict[str, bytes] = (
        {
            "README.md": _readme(
                org=org,
                since=since,
                until=until,
                claim=claim,
                entry_count=len(ordered),
            ).encode("utf-8"),
            "verify-instructions.md": _regulator_verify_instructions(PACK_KIND_AI_RMF).encode("utf-8"),
            "subcategory-evidence.json": evidence_bytes,
            "lineage-log.jsonl": log_bytes,
        }
        | sig_payload
        | card_payload
    )

    build_finished_at = datetime.now(UTC).isoformat(timespec="seconds")
    return _seal_pack(
        kind=PACK_KIND_AI_RMF,
        members=members,
        manifest_core={
            "org": org,
            "period": {"since": since.isoformat(), "until": until.isoformat()},
            "build_started_at": build_started_at,
            "build_finished_at": build_finished_at,
            "entry_count": len(ordered),
            "window_claim": claim,
        },
        operator_key_path=operator_key_path,
        output_path=output_path,
        member_order=[
            "README.md",
            "verify-instructions.md",
            "subcategory-evidence.json",
            "lineage-log.jsonl",
        ],
    )
