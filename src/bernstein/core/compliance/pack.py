"""One-command EU AI Act Article 12 evidence pack.

See ``docs/decisions/009-lineage-v1.md`` §8 for the design rationale.

Public surface:

* :func:`build_pack` - assemble a ZIP bundle for the
  ``(since, until, org)`` triple, signed by the operator key.

The pack's manifest follows the SLSA v1.1 provenance shape (a flat dict
with ``builder``, ``build_started_at``, ``build_finished_at``,
``input_hashes``, ``output_hash``) so external auditor tooling can
re-verify the bundle without depending on Bernstein internals.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import UTC, datetime
from importlib import metadata
from typing import TYPE_CHECKING, Any

from bernstein.core.compliance.article12 import (
    _MIN_RETENTION_DAYS,
    ARTICLE12_PARAGRAPH_MAP,
    render_csv,
    render_pdf,
)
from bernstein.core.compliance.regulator_renderers import (
    render_incident_csv,
    render_incident_pdf,
    render_oversight_csv,
    render_oversight_pdf,
    render_retention_csv,
    render_retention_pdf,
)
from bernstein.core.lineage.entry import LineageEntry, canonicalise, entry_hash
from bernstein.core.lineage.identity import sign_detached

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import date
    from pathlib import Path

__all__ = [
    "PACK_FORMAT_VERSION",
    "PACK_KIND_ARTICLE12",
    "PACK_KIND_INCIDENT",
    "PACK_KIND_OVERSIGHT",
    "PACK_KIND_RETENTION",
    "build_incident_pack",
    "build_oversight_pack",
    "build_pack",
    "build_retention_pack",
]


_OPERATOR_KID = "operator-pack-signer"

#: Pack-kind discriminator recorded in ``pack-manifest.json:kind``. The
#: Article 12 bundle predates the field, so an absent ``kind`` is treated as
#: Article 12 by the offline verifier (mirroring the ``pack_format_version``
#: legacy default). The three regulator-mapped kinds below each project a
#: different obligation out of the same chain.
PACK_KIND_ARTICLE12 = "article-12"
PACK_KIND_RETENTION = "retention"
PACK_KIND_INCIDENT = "incident"
PACK_KIND_OVERSIGHT = "oversight"

_PACK_SCHEMA = "https://bernstein.run/compliance/pack-manifest/v1"

#: Compliance-pack format version recorded in ``pack-manifest.json``.
#:
#: v1 (pre-fix) wrote ``lineage-log.jsonl`` with ``json.dumps(..., sort_keys=
#: True)`` default separators (spaced ``", "`` / ``": "``), so the on-disk
#: bytes did not equal the JCS-canonical signed form. The offline auditor
#: therefore re-canonicalised the parsed entry to verify, which accepted any
#: value-preserving byte rewrite (issue #1871).
#:
#: v2 writes each entry as its exact JCS-canonical bytes (``canonicalise``)
#: terminated by a single ``\n``, so the offline auditor binds verification to
#: the on-disk bytes (``canonicalise(entry) == raw_line``) and rejects a
#: value-preserving tamper. ``bernstein_verify.verify.verify_pack`` dispatches
#: on this recorded version, so pre-fix v1 packs still verify under their
#: original rule.
PACK_FORMAT_VERSION = 2


def _date_to_ns_inclusive(d: date, *, end_of_day: bool = False) -> int:
    """Convert a calendar date to ns-since-epoch.

    If ``end_of_day`` is True, returns 23:59:59.999999999 UTC of that day
    so the window is inclusive on both sides.
    """
    if end_of_day:
        dt = datetime(d.year, d.month, d.day, 23, 59, 59, 999_999, tzinfo=UTC)
    else:
        dt = datetime(d.year, d.month, d.day, tzinfo=UTC)
    base_ns = int(dt.timestamp() * 1_000_000_000)
    if end_of_day:
        base_ns += 999  # bump to make the boundary unambiguously inclusive
    return base_ns


def _read_entries(log_path: Path) -> list[LineageEntry]:
    if not log_path.exists():
        return []
    entries: list[LineageEntry] = []
    for raw in log_path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        record = json.loads(raw)
        entries.append(LineageEntry(**record))
    return entries


def _filter_entries(entries: list[LineageEntry], since: date, until: date) -> list[LineageEntry]:
    lo = _date_to_ns_inclusive(since, end_of_day=False)
    hi = _date_to_ns_inclusive(until, end_of_day=True)
    return [e for e in entries if lo <= e.ts_ns <= hi]


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _readme_text(*, org: str, since: date, until: date, entry_count: int) -> str:
    return (
        f"# Compliance pack - {org}\n\n"
        f"**Period:** {since.isoformat()} → {until.isoformat()}\n"
        f"**Entries in period:** {entry_count}\n\n"
        "This bundle implements the record-keeping obligations of Article 12 of\n"
        "Regulation (EU) 2024/1689 (EU AI Act).\n\n"
        "## Contents\n\n"
        "- `article12-evidence.pdf` - human-readable summary keyed to Article 12 paragraphs.\n"
        "- `article12-evidence.csv` - one row per artefact write event.\n"
        "- `lineage-log.jsonl` - raw lineage log filtered to the period.\n"
        "- `signatures/` - per-entry detached Ed25519 JWS (RFC 7515, RFC 8785 JCS).\n"
        "- `agent-cards/` - A2A v1.0 Agent Cards used to verify the signatures.\n"
        "- `verify-instructions.md` - how to re-verify this bundle independently.\n"
        "- `pack-manifest.json` - SLSA-style provenance for this pack.\n"
        "- `pack-manifest.json.sig` - operator-issued Ed25519 JWS over the manifest.\n"
    )


def _verify_instructions() -> str:
    return (
        "# Verifying this compliance pack\n\n"
        "## Quick path\n\n"
        "```\n"
        "pip install bernstein-verify\n"
        "bernstein-verify pack ./acme-compliance-2026-q2.zip\n"
        "```\n\n"
        "Exit 0 + a one-line PASS summary indicates: every entry in\n"
        "`lineage-log.jsonl` is stored in its exact RFC 8785 canonical bytes,\n"
        "its detached JWS in `signatures/` verifies under the Agent Card in\n"
        "`agent-cards/`, and `pack-manifest.json.sig` verifies against the\n"
        "operator public key.\n\n"
        "This pack is format v2 (`pack-manifest.json:pack_format_version`):\n"
        "verification is bound to the on-disk log bytes. Each line must equal\n"
        "its canonical form byte-for-byte (including a single trailing `\\n`),\n"
        "so a value-preserving rewrite - reordered JSON keys, inserted\n"
        "whitespace, a flipped or stripped line terminator - is rejected even\n"
        "though it parses to the same field values.\n\n"
        "## Manual path (no Bernstein install)\n\n"
        "1. Unzip the bundle.\n"
        "2. Read `lineage-log.jsonl` as bytes and split strictly on `\\n`\n"
        "   (not `splitlines()` - that treats `\\r` and other characters as\n"
        "   record boundaries). For every line, RFC 8785 canonicalise the\n"
        "   parsed JSON and assert the result equals the original line bytes;\n"
        "   reject the pack on any mismatch. Then sha256 the canonical bytes\n"
        "   -> `entry_hash`.\n"
        "3. Open `signatures/<hex(entry_hash)>.jws`; verify the detached\n"
        "   Ed25519 JWS (RFC 7515 + RFC 7797 `b64=false`) against the public\n"
        "   key in the matching `agent-cards/<agent_id>.json`.\n"
        "4. Verify `pack-manifest.json.sig` against the operator public key\n"
        "   you received out of band.\n"
    )


def _load_operator_signer(key_path: Path) -> str:
    """Return the operator private key PEM contents.

    The compliance pack manifest is short and per-pack, so we re-use the
    lineage Ed25519 JWS primitives directly rather than the heavier
    KMS adapter surface. Customers running with KMS-backed keys can
    point ``operator_key_path`` at a file the adapter writes ephemerally
    (see ``bernstein.core.security.lineage_kms``).
    """
    return key_path.read_text(encoding="utf-8")


def _builder_label() -> str:
    try:
        version = metadata.version("bernstein")
    except metadata.PackageNotFoundError:  # pragma: no cover - dev shim
        version = "0+unknown"
    return f"bernstein/{version} compliance.pack"


def build_pack(
    *,
    since: date,
    until: date,
    org: str,
    lineage_dir: Path,
    agent_cards_dir: Path,
    output_path: Path,
    operator_key_path: Path,
) -> Path:
    """Assemble the Article 12 evidence ZIP.

    Args:
        since: Window start (inclusive, UTC calendar day).
        until: Window end (inclusive, UTC calendar day).
        org: Customer-visible organisation name; surfaces in the PDF/README.
        lineage_dir: Path to ``.sdd/lineage/`` (must contain ``log.jsonl``;
            ``signatures/`` is optional but typical).
        agent_cards_dir: Path to ``.sdd/agents/`` (Agent Card JSON files).
        output_path: Where to write the resulting ``.zip``.
        operator_key_path: PEM PKCS#8 Ed25519 private key used to sign the
            manifest. The matching public key must be handed to the
            auditor out of band.

    Returns:
        ``output_path``.
    """
    build_started_at = datetime.now(UTC).isoformat(timespec="seconds")

    log_path = lineage_dir / "log.jsonl"
    signatures_src = lineage_dir / "signatures"

    all_entries = _read_entries(log_path)
    filtered = _filter_entries(all_entries, since, until)

    # 1. lineage-log.jsonl (filtered)
    #
    # Emit each entry as its exact JCS-canonical bytes (the same form that was
    # signed) terminated by a single ``\n``, so the offline auditor can bind
    # verification to these on-disk bytes (``canonicalise(entry) == raw_line``)
    # rather than re-canonicalising the parsed entry. Re-using ``canonicalise``
    # keeps the writer and the signature over one byte-form; a value-preserving
    # rewrite (reordered keys, spaced separators, a flipped or stripped
    # terminator) then no longer matches and is rejected at verify time (#1871).
    log_lines = [canonicalise(e) for e in filtered]
    log_bytes = b"\n".join(log_lines) + (b"\n" if log_lines else b"")

    # 2. article12-evidence.csv
    csv_bytes = render_csv(filtered).encode("utf-8")

    # 3. article12-evidence.pdf
    pdf_bytes = render_pdf(
        filtered,
        org=org,
        period=(since.isoformat(), until.isoformat()),
    )

    # 4. README.md
    readme_bytes = _readme_text(
        org=org,
        since=since,
        until=until,
        entry_count=len(filtered),
    ).encode("utf-8")

    # 5. verify-instructions.md
    verify_bytes = _verify_instructions().encode("utf-8")

    # 6. signatures/ -- only entries we kept.
    in_window_hashes = {entry_hash(e).split(":", 1)[1] for e in filtered}
    sig_payload: dict[str, bytes] = {}
    if signatures_src.exists():
        for sig_file in signatures_src.iterdir():
            if not sig_file.is_file() or not sig_file.name.endswith(".jws"):
                continue
            stem = sig_file.stem
            if stem in in_window_hashes:
                sig_payload[f"signatures/{sig_file.name}"] = sig_file.read_bytes()

    # 7. agent-cards/ -- only cards referenced by filtered entries.
    used_agent_ids = {e.agent_id for e in filtered}
    card_payload: dict[str, bytes] = {}
    if agent_cards_dir.exists():
        for card_file in agent_cards_dir.iterdir():
            if not card_file.is_file() or not card_file.name.endswith(".json"):
                continue
            try:
                card_data = json.loads(card_file.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if card_data.get("agent_id") in used_agent_ids or not used_agent_ids:
                card_payload[f"agent-cards/{card_file.name}"] = card_file.read_bytes()

    # 8. pack-manifest.json -- SLSA-style.
    input_hashes: dict[str, str] = {
        "lineage-log.jsonl": _sha256(log_bytes),
        "article12-evidence.csv": _sha256(csv_bytes),
        "article12-evidence.pdf": _sha256(pdf_bytes),
        "README.md": _sha256(readme_bytes),
        "verify-instructions.md": _sha256(verify_bytes),
    }
    for name, content in sorted(sig_payload.items()):
        input_hashes[name] = _sha256(content)
    for name, content in sorted(card_payload.items()):
        input_hashes[name] = _sha256(content)

    # Roll up the per-paragraph facts so the manifest is itself a
    # self-contained evidence statement (auditors can read it without
    # parsing the PDF).
    period_strs = (since.isoformat(), until.isoformat())
    article12_facts: list[dict[str, Any]] = [fn(filtered, period_strs) for fn in ARTICLE12_PARAGRAPH_MAP.values()]

    build_finished_at = datetime.now(UTC).isoformat(timespec="seconds")

    manifest: dict[str, Any] = {
        "schema": "https://bernstein.run/compliance/pack-manifest/v1",
        "pack_format_version": PACK_FORMAT_VERSION,
        "builder": _builder_label(),
        "org": org,
        "period": {"since": since.isoformat(), "until": until.isoformat()},
        "build_started_at": build_started_at,
        "build_finished_at": build_finished_at,
        "input_hashes": input_hashes,
        "entry_count": len(filtered),
        "article12_facts": article12_facts,
        "operator_kid": _OPERATOR_KID,
    }
    # Compute output_hash over the canonical manifest body itself so
    # the manifest is self-anchoring: a verifier can derive output_hash
    # from the bytes they're holding.
    manifest_bytes_no_output = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    manifest["output_hash"] = _sha256(manifest_bytes_no_output)
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")

    # 9. pack-manifest.json.sig - operator-signed.
    operator_pem = _load_operator_signer(operator_key_path)
    sig = sign_detached(manifest_bytes, operator_pem, kid=_OPERATOR_KID)

    # 10. Assemble ZIP. Deterministic ordering for reproducible packs.
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.md", readme_bytes)
        zf.writestr("article12-evidence.pdf", pdf_bytes)
        zf.writestr("article12-evidence.csv", csv_bytes)
        zf.writestr("lineage-log.jsonl", log_bytes)
        zf.writestr("verify-instructions.md", verify_bytes)
        for name in sorted(sig_payload):
            zf.writestr(name, sig_payload[name])
        for name in sorted(card_payload):
            zf.writestr(name, card_payload[name])
        zf.writestr("pack-manifest.json", manifest_bytes)
        zf.writestr("pack-manifest.json.sig", sig)

    return output_path


# ===========================================================================
# Regulator-mapped pack family (kind = retention / incident / oversight)
#
# Each builder below is a deterministic projection of the chain sealed with
# the same operator-key signing, signed provenance manifest, and canonical-
# bytes rule as the Article 12 pack. Members carry no wall-clock state, so
# two builds over the same window yield byte-identical member hashes; build
# timestamps live only in the manifest. Strip the chain (the signed lineage
# log, the prev_hmac-chained audit slice, the receipt bindings) and each pack
# degrades to a report generator with a log - the substrate recomputation is
# what the offline verifier binds to.
# ===========================================================================


def _canonical_json(payload: Any) -> bytes:
    """RFC 8785-style canonical JSON bytes (matches the offline verifier)."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _safe_member_name(value: str) -> str:
    """Sanitise a caller-supplied id into a flat, path-safe member stem."""
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in value) or "unnamed"


def _seal_pack(
    *,
    kind: str,
    members: dict[str, bytes],
    manifest_core: dict[str, Any],
    operator_key_path: Path,
    output_path: Path,
    member_order: list[str] | None = None,
) -> Path:
    """Seal ``members`` into a signed pack ZIP with a ``kind``-tagged manifest.

    ``input_hashes`` binds every member by sha256 (so a one-byte tamper in any
    member, including a rendered PDF, is caught offline by hash mismatch).
    ``output_hash`` self-anchors the manifest body, and ``pack-manifest.json``
    is Ed25519-signed by the operator key. Build timestamps sit in
    ``manifest_core`` and are excluded from ``input_hashes``.
    """
    input_hashes = {name: _sha256(content) for name, content in sorted(members.items())}
    manifest: dict[str, Any] = {
        "schema": _PACK_SCHEMA,
        "pack_format_version": PACK_FORMAT_VERSION,
        "kind": kind,
        "builder": _builder_label(),
        **manifest_core,
        "input_hashes": input_hashes,
        "operator_kid": _OPERATOR_KID,
    }
    manifest_bytes_no_output = _canonical_json(manifest)
    manifest["output_hash"] = _sha256(manifest_bytes_no_output)
    manifest_bytes = _canonical_json(manifest)

    operator_pem = _load_operator_signer(operator_key_path)
    sig = sign_detached(manifest_bytes, operator_pem, kid=_OPERATOR_KID)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    order = member_order or []
    written: set[str] = set()
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in order:
            if name in members and name not in written:
                zf.writestr(name, members[name])
                written.add(name)
        for name in sorted(members):
            if name not in written:
                zf.writestr(name, members[name])
                written.add(name)
        zf.writestr("pack-manifest.json", manifest_bytes)
        zf.writestr("pack-manifest.json.sig", sig)
    return output_path


def _collect_sig_card_members(
    filtered: list[LineageEntry],
    signatures_src: Path,
    agent_cards_dir: Path,
) -> tuple[dict[str, bytes], dict[str, bytes]]:
    """Gather the per-entry JWS sidecars and Agent Cards for ``filtered``.

    Ships exactly the signatures and cards needed to re-verify every entry
    offline, and nothing else. Each JWS is re-keyed to
    ``signatures/<entry_hash>.jws`` (the full ``sha256:`` form the offline
    verifier looks up), so the pack is verifiable regardless of whether the
    source ``.sdd/lineage/signatures`` layout named files by hex stem or by
    the prefixed entry hash.
    """
    hex_to_full = {entry_hash(e).split(":", 1)[1]: entry_hash(e) for e in filtered}
    sig_payload: dict[str, bytes] = {}
    if signatures_src.exists():
        for sig_file in signatures_src.iterdir():
            if not sig_file.is_file() or not sig_file.name.endswith(".jws"):
                continue
            stem = sig_file.stem
            full_hash: str | None = None
            if stem in hex_to_full:
                full_hash = hex_to_full[stem]
            elif stem.startswith("sha256:") and stem.split(":", 1)[1] in hex_to_full:
                full_hash = stem
            if full_hash is not None:
                sig_payload[f"signatures/{full_hash}.jws"] = sig_file.read_bytes()

    used_agent_ids = {e.agent_id for e in filtered}
    card_payload: dict[str, bytes] = {}
    if agent_cards_dir.exists():
        for card_file in agent_cards_dir.iterdir():
            if not card_file.is_file() or not card_file.name.endswith(".json"):
                continue
            try:
                card_data = json.loads(card_file.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if card_data.get("agent_id") in used_agent_ids or not used_agent_ids:
                card_payload[f"agent-cards/{card_file.name}"] = card_file.read_bytes()
    return sig_payload, card_payload


def _regulator_verify_instructions(kind: str) -> str:
    return (
        f"# Verifying this {kind} compliance pack\n\n"
        "## Quick path (no Bernstein install)\n\n"
        "```\n"
        "pip install bernstein-verify\n"
        "python -m bernstein_verify pack ./pack.zip\n"
        "```\n\n"
        "Exit 0 + a one-line PASS summary indicates every member's sha256\n"
        "matches `pack-manifest.json:input_hashes`, the manifest self-anchors\n"
        "(`output_hash`), and the chained substrate this pack projects re-\n"
        "verifies: for a retention pack the embedded signed lineage log and\n"
        "its boundary head hashes; for an incident pack the prev_hmac linkage\n"
        "of `audit-slice.jsonl` plus the declared evidence-gap list; for an\n"
        "oversight pack the recomputed displayed-versus-executed binding of\n"
        "every approval receipt.\n\n"
        "Flipping a single byte in any member (including a rendered PDF) is\n"
        "rejected by hash mismatch, naming the member. The pack is a\n"
        "projection of the chain, not a new log: verification recomputes the\n"
        "facts from the substrate rather than trusting the rendered surface.\n"
    )


def _retention_readme(*, org: str, since: date, until: date, entry_count: int) -> str:
    return (
        f"# Retention evidence pack - {org}\n\n"
        f"**Period:** {since.isoformat()} -> {until.isoformat()}\n"
        f"**Entries in period:** {entry_count}\n\n"
        "Chain-continuity evidence for the record-keeping obligation of\n"
        "Article 12(3) of Regulation (EU) 2024/1689 (EU AI Act): the logs\n"
        "existed over the window and were not truncated or rewritten.\n\n"
        "## Contents\n\n"
        "- `retention-evidence.json` - boundary head hashes, entry count,\n"
        "  detected coverage gaps, and the retention parameters in force.\n"
        "- `retention-evidence.pdf` / `.csv` - human- and machine-readable views.\n"
        "- `lineage-log.jsonl` - the signed entries in the window (canonical bytes).\n"
        "- `signatures/`, `agent-cards/` - per-entry Ed25519 JWS + cards.\n"
        "- `pack-manifest.json(.sig)` - signed SLSA-style provenance.\n"
    )


def _incident_readme(*, org: str, run_id: str, gap_count: int) -> str:
    return (
        f"# Serious-incident report pack - {org}\n\n"
        f"**Run:** {run_id}\n"
        f"**Evidence gaps:** {gap_count}\n\n"
        "A serious-incident report in the shape Article 73 of Regulation (EU)\n"
        "2024/1689 expects: the incident timeline joined with the referenced\n"
        "audit slice, evidence bundles, and approval/delegation receipts.\n\n"
        "## Contents\n\n"
        "- `incident-timeline.json` - the correlated timeline.\n"
        "- `audit-slice.jsonl` - the prev_hmac-chained audit events (canonical).\n"
        "- `evidence-bundles/`, `receipts/` - referenced artefacts present in the store.\n"
        "- `gaps.json` - explicit entries for referenced artefacts missing from the store.\n"
        "- `incident-report.pdf` / `.csv` - human- and machine-readable views.\n"
        "- `pack-manifest.json(.sig)` - signed SLSA-style provenance.\n"
    )


def _oversight_readme(*, org: str, since: date, until: date, receipt_count: int) -> str:
    return (
        f"# Human-oversight evidence pack - {org}\n\n"
        f"**Period:** {since.isoformat()} -> {until.isoformat()}\n"
        f"**Approval receipts:** {receipt_count}\n\n"
        "Human-oversight evidence in the shape Article 14 of Regulation (EU)\n"
        "2024/1689 expects: who approved what, and that the displayed action\n"
        "equalled the executed action, decision by decision.\n\n"
        "## Contents\n\n"
        "- `oversight-evidence.json` - one row per receipt with the attested\n"
        "  displayed-versus-executed binding, principal, and decision outcome.\n"
        "- `receipts/` - the raw approval receipts (canonical bytes).\n"
        "- `oversight-report.pdf` / `.csv` - human- and machine-readable views.\n"
        "- `pack-manifest.json(.sig)` - signed SLSA-style provenance.\n"
    )


def _detect_coverage_gaps(
    all_entries: list[LineageEntry],
    filtered: list[LineageEntry],
) -> list[dict[str, str]]:
    """Report in-window entries whose parent is absent from the whole log.

    A missing parent is the signature of truncation or rewrite: the child was
    kept but the record it descends from is gone. Boundary crossings are not
    gaps here - only parents absent from the *entire* log are flagged.
    """
    present = {entry_hash(e) for e in all_entries}
    gaps: list[dict[str, str]] = []
    for e in sorted(filtered, key=lambda x: (x.ts_ns, entry_hash(x))):
        for parent in e.parent_hashes:
            if parent not in present:
                gaps.append({"entry_hash": entry_hash(e), "missing_parent": parent})
    return gaps


def build_retention_pack(
    *,
    since: date,
    until: date,
    org: str,
    lineage_dir: Path,
    agent_cards_dir: Path,
    output_path: Path,
    operator_key_path: Path,
) -> Path:
    """Assemble a chain-continuity (retention) evidence pack.

    Projects the signed lineage log onto the Article 12(3) retention
    obligation: boundary head hashes at the window edges, entry count,
    detected coverage gaps, and the retention parameters in force. The
    embedded log ships its per-entry signatures so an auditor recomputes the
    boundary head hashes from the actual signed entries offline.
    """
    build_started_at = datetime.now(UTC).isoformat(timespec="seconds")

    log_path = lineage_dir / "log.jsonl"
    signatures_src = lineage_dir / "signatures"
    all_entries = _read_entries(log_path)
    filtered = _filter_entries(all_entries, since, until)

    log_lines = [canonicalise(e) for e in filtered]
    log_bytes = b"\n".join(log_lines) + (b"\n" if log_lines else b"")

    ordered = sorted(filtered, key=lambda e: (e.ts_ns, entry_hash(e)))
    if ordered:
        boundary: dict[str, Any] = {
            "first_entry_hash": entry_hash(ordered[0]),
            "last_entry_hash": entry_hash(ordered[-1]),
            "first_ts_ns": ordered[0].ts_ns,
            "last_ts_ns": ordered[-1].ts_ns,
        }
    else:
        boundary = {"first_entry_hash": None, "last_entry_hash": None, "first_ts_ns": None, "last_ts_ns": None}

    coverage_gaps = _detect_coverage_gaps(all_entries, filtered)
    span_days = (until - since).days
    retention_params = {
        "period_days": span_days,
        "minimum_required_days": _MIN_RETENTION_DAYS,
        "meets_minimum": span_days >= _MIN_RETENTION_DAYS,
    }

    evidence = {
        "kind": PACK_KIND_RETENTION,
        "org": org,
        "period": {"since": since.isoformat(), "until": until.isoformat()},
        "entry_count": len(filtered),
        "boundary": boundary,
        "coverage_gaps": coverage_gaps,
        "retention_params": retention_params,
    }
    evidence_bytes = _canonical_json(evidence)

    sig_payload, card_payload = _collect_sig_card_members(filtered, signatures_src, agent_cards_dir)
    members: dict[str, bytes] = (
        {
            "README.md": _retention_readme(org=org, since=since, until=until, entry_count=len(filtered)).encode(
                "utf-8"
            ),
            "verify-instructions.md": _regulator_verify_instructions(PACK_KIND_RETENTION).encode("utf-8"),
            "retention-evidence.json": evidence_bytes,
            "retention-evidence.csv": render_retention_csv(evidence).encode("utf-8"),
            "retention-evidence.pdf": render_retention_pdf(
                org=org, period=(since.isoformat(), until.isoformat()), evidence=evidence
            ),
            "lineage-log.jsonl": log_bytes,
        }
        | sig_payload
        | card_payload
    )

    build_finished_at = datetime.now(UTC).isoformat(timespec="seconds")
    manifest_core = {
        "org": org,
        "period": {"since": since.isoformat(), "until": until.isoformat()},
        "build_started_at": build_started_at,
        "build_finished_at": build_finished_at,
        "entry_count": len(filtered),
        "boundary": boundary,
        "coverage_gaps": coverage_gaps,
        "retention_params": retention_params,
    }
    return _seal_pack(
        kind=PACK_KIND_RETENTION,
        members=members,
        manifest_core=manifest_core,
        operator_key_path=operator_key_path,
        output_path=output_path,
        member_order=[
            "README.md",
            "verify-instructions.md",
            "retention-evidence.pdf",
            "retention-evidence.csv",
            "retention-evidence.json",
            "lineage-log.jsonl",
        ],
    )


def build_oversight_pack(
    *,
    since: date,
    until: date,
    org: str,
    approvals: Sequence[Mapping[str, Any]],
    output_path: Path,
    operator_key_path: Path,
) -> Path:
    """Assemble a human-oversight (Article 14) evidence pack from receipts.

    Each approval in the window becomes a canonical receipt carrying the
    attested displayed-versus-executed binding: ``displayed_hash`` and
    ``executed_hash`` are content-addressed over the two payloads, so an
    auditor recomputes the binding offline and cannot be told "displayed
    equalled executed" without the bytes to prove it.
    """
    build_started_at = datetime.now(UTC).isoformat(timespec="seconds")

    lo = _date_to_ns_inclusive(since, end_of_day=False)
    hi = _date_to_ns_inclusive(until, end_of_day=True)
    in_window = [a for a in approvals if lo <= int(a["ts_ns"]) <= hi]
    in_window.sort(key=lambda a: (int(a["ts_ns"]), str(a["receipt_id"])))

    receipt_members: dict[str, bytes] = {}
    evidence_rows: list[dict[str, Any]] = []
    decision_counts: dict[str, int] = {}
    for a in in_window:
        displayed = a["displayed"]
        executed = a["executed"]
        displayed_hash = _sha256(_canonical_json(displayed))
        executed_hash = _sha256(_canonical_json(executed))
        binding_ok = displayed_hash == executed_hash
        receipt = {
            "v": 1,
            "receipt_id": str(a["receipt_id"]),
            "principal": str(a["principal"]),
            "decision": str(a["decision"]),
            "ts_ns": int(a["ts_ns"]),
            "displayed": displayed,
            "executed": executed,
            "displayed_hash": displayed_hash,
            "executed_hash": executed_hash,
        }
        receipt_members[f"receipts/{_safe_member_name(receipt['receipt_id'])}.json"] = _canonical_json(receipt)
        evidence_rows.append(
            {
                "receipt_id": receipt["receipt_id"],
                "principal": receipt["principal"],
                "decision": receipt["decision"],
                "ts_ns": receipt["ts_ns"],
                "displayed_hash": displayed_hash,
                "executed_hash": executed_hash,
                "binding_ok": binding_ok,
            }
        )
        decision_counts[receipt["decision"]] = decision_counts.get(receipt["decision"], 0) + 1

    evidence = {
        "kind": PACK_KIND_OVERSIGHT,
        "org": org,
        "period": {"since": since.isoformat(), "until": until.isoformat()},
        "receipt_count": len(evidence_rows),
        "decision_counts": decision_counts,
        "receipts": evidence_rows,
    }
    members = {
        "README.md": _oversight_readme(org=org, since=since, until=until, receipt_count=len(evidence_rows)).encode(
            "utf-8"
        ),
        "verify-instructions.md": _regulator_verify_instructions(PACK_KIND_OVERSIGHT).encode("utf-8"),
        "oversight-evidence.json": _canonical_json(evidence),
        "oversight-report.csv": render_oversight_csv(evidence).encode("utf-8"),
        "oversight-report.pdf": render_oversight_pdf(
            org=org, period=(since.isoformat(), until.isoformat()), evidence=evidence
        ),
    } | receipt_members

    build_finished_at = datetime.now(UTC).isoformat(timespec="seconds")
    manifest_core = {
        "org": org,
        "period": {"since": since.isoformat(), "until": until.isoformat()},
        "build_started_at": build_started_at,
        "build_finished_at": build_finished_at,
        "receipt_count": len(evidence_rows),
        "decision_counts": decision_counts,
    }
    return _seal_pack(
        kind=PACK_KIND_OVERSIGHT,
        members=members,
        manifest_core=manifest_core,
        operator_key_path=operator_key_path,
        output_path=output_path,
        member_order=[
            "README.md",
            "verify-instructions.md",
            "oversight-report.pdf",
            "oversight-report.csv",
            "oversight-evidence.json",
        ],
    )


def build_incident_pack(
    *,
    run_id: str,
    org: str,
    timeline: Mapping[str, Any],
    audit_events: Sequence[Mapping[str, Any]],
    evidence_bundles: Mapping[str, bytes],
    receipts: Mapping[str, bytes],
    gaps: Sequence[Mapping[str, Any]],
    output_path: Path,
    operator_key_path: Path,
) -> Path:
    """Assemble a serious-incident (Article 73) report pack.

    Joins the incident timeline with the prev_hmac-chained audit slice, the
    referenced evidence bundles, and the approval/delegation receipts for the
    affected window. A referenced bundle or receipt missing from the store is
    recorded as an explicit ``gaps.json`` entry (and counted in the manifest)
    so the pack never fabricates completeness - the offline verifier surfaces
    the gap list rather than passing silently.
    """
    build_started_at = datetime.now(UTC).isoformat(timespec="seconds")

    timeline_bytes = _canonical_json(timeline)
    slice_bytes = b"".join(_canonical_json(ev) + b"\n" for ev in audit_events)
    gap_list = [dict(g) for g in gaps]
    gaps_doc = {"kind": PACK_KIND_INCIDENT, "run_id": run_id, "gaps": gap_list}

    members: dict[str, bytes] = {
        "README.md": _incident_readme(org=org, run_id=run_id, gap_count=len(gap_list)).encode("utf-8"),
        "verify-instructions.md": _regulator_verify_instructions(PACK_KIND_INCIDENT).encode("utf-8"),
        "incident-timeline.json": timeline_bytes,
        "audit-slice.jsonl": slice_bytes,
        "gaps.json": _canonical_json(gaps_doc),
        "incident-report.csv": render_incident_csv(dict(timeline)).encode("utf-8"),
        "incident-report.pdf": render_incident_pdf(org=org, timeline=dict(timeline), gaps=gap_list),
    }
    for name, data in evidence_bundles.items():
        members[f"evidence-bundles/{_safe_member_name(name)}"] = data
    for name, data in receipts.items():
        members[f"receipts/{_safe_member_name(name)}"] = data

    build_finished_at = datetime.now(UTC).isoformat(timespec="seconds")
    manifest_core = {
        "org": org,
        "run_id": run_id,
        "build_started_at": build_started_at,
        "build_finished_at": build_finished_at,
        "event_count": len(list(timeline.get("events", []))),
        "audit_event_count": len(audit_events),
        "gap_count": len(gap_list),
        "gaps": gap_list,
    }
    return _seal_pack(
        kind=PACK_KIND_INCIDENT,
        members=members,
        manifest_core=manifest_core,
        operator_key_path=operator_key_path,
        output_path=output_path,
        member_order=[
            "README.md",
            "verify-instructions.md",
            "incident-report.pdf",
            "incident-report.csv",
            "incident-timeline.json",
            "audit-slice.jsonl",
            "gaps.json",
        ],
    )
