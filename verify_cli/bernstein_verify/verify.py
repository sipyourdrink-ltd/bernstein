"""Standalone re-implementation of Bernstein lineage v1 verification.

This module is the heart of `bernstein-verify`. It MUST NOT import
anything from `bernstein.*`. Three primitives are re-implemented here:

  * `jcs_canonicalise` - RFC 8785 JSON Canonicalisation Scheme, byte-for-byte
    identical to `bernstein.core.lineage.entry.canonicalise` on the flat
    dict shapes used by lineage v1. Cross-tested under tests/test_verify.py.
  * `verify_jws_detached` - RFC 7515 detached JWS with EdDSA / Ed25519
    (RFC 8037) and the unencoded-payload extension (RFC 7797, `b64=false`).
    Matches `bernstein.core.lineage.identity.verify_detached` exactly.
  * `walk_chain` - parent-hash DAG walk; surfaces orphans + duplicates.

`verify_pack` wires the three primitives against a compliance-pack ZIP.

Air-gap guarantee: no network calls. No imports of httpx/requests/urllib*.
Only stdlib + `cryptography`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# Names of the files we expect inside a compliance pack.
_LOG_NAME = "lineage-log.jsonl"
_SIG_DIR = "signatures/"
_CARD_DIR = "agent-cards/"
_MANIFEST_NAME = "pack-manifest.json"

#: Compliance-pack format version that binds verification to the on-disk log
#: bytes. v1 (pre-fix) packs wrote ``lineage-log.jsonl`` non-canonically
#: (``json.dumps(..., sort_keys=True)`` default separators), so this verifier
#: had to re-canonicalise the parsed entry to check a signature - which
#: accepted any value-preserving byte rewrite (reordered keys, spaced
#: separators, a flipped or stripped line terminator) (issue #1871). v2 packs
#: write each entry as its exact JCS-canonical bytes terminated by ``\n``, so a
#: v2 pack is verified by requiring the on-disk line to equal
#: ``jcs_canonicalise(entry)`` byte-for-byte.
_PACK_FORMAT_BYTE_BINDING = 2

#: Format assumed when ``pack-manifest.json`` predates the
#: ``pack_format_version`` field (or is absent). Mirrors the Merkle seal's
#: legacy-default scheme dispatch (issue #1866): an unmarked pack is pre-fix,
#: so it is verified under the original re-canonicalise rule.
_PACK_FORMAT_LEGACY = 1

# Regulator-mapped pack kinds (issue #2517). An absent ``kind`` is the
# Article 12 bundle (verified by the legacy path below); these three kinds are
# projections of the chain that also carry a member-integrity + substrate-
# recomputation contract enforced by ``_verify_regulator_pack``.
_KIND_ARTICLE12 = "article-12"
_KIND_RETENTION = "retention"
_KIND_INCIDENT = "incident"
_KIND_OVERSIGHT = "oversight"
_REGULATOR_KINDS = frozenset({_KIND_RETENTION, _KIND_INCIDENT, _KIND_OVERSIGHT})


# ---------- RFC 8785 JCS ----------


def jcs_canonicalise(d: dict[str, Any]) -> bytes:
    """RFC 8785 JSON Canonicalisation Scheme (the subset used by lineage v1).

    LineageEntry is a flat dataclass of (str, int, list[str]); none of the
    full-blown ES6-number / nested-object corner cases of RFC 8785 apply.
    The subset reduces to: sort_keys=True, minimal separators, UTF-8 bytes.

    Cross-tested for byte-equality with bernstein's `canonicalise` in
    tests/test_verify.py. If bernstein ever extends the schema, this MUST
    be updated and the byte-equality test will fail loudly.
    """
    return json.dumps(
        d,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


# ---------- RFC 7515 detached JWS ----------


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def verify_jws_detached(
    payload: bytes,
    jws: str,
    public_key_pem: str,
    *,
    expected_kid: str | None = None,
) -> bool:
    """Verify a detached Ed25519 JWS against a PEM-encoded public key.

    Matches `bernstein.core.lineage.identity.verify_detached`. Returns
    False on ANY malformed input, mismatched kid, wrong key, invalid
    signature, or non-EdDSA algorithm. Never raises on bad input - the
    auditor invokes this on attacker-controlled bytes.

    `expected_kid` is enforced when supplied. Pass `None` to skip the
    kid check (rare; usually you have a card to bind against).
    """
    try:
        protected_b64, empty, sig_b64 = jws.split(".", maxsplit=2)
    except ValueError:
        return False
    if empty != "":
        return False
    if "." in sig_b64:
        return False  # 4+ segments

    try:
        header = json.loads(_b64url_decode(protected_b64))
    except (ValueError, json.JSONDecodeError):
        return False
    if not isinstance(header, dict):
        return False
    if header.get("alg") != "EdDSA":
        return False
    if expected_kid is not None and header.get("kid") != expected_kid:
        return False

    try:
        pub = serialization.load_pem_public_key(public_key_pem.encode("ascii"))
    except (ValueError, TypeError, UnicodeEncodeError):
        return False
    if not isinstance(pub, Ed25519PublicKey):
        return False

    signing_input = protected_b64.encode("ascii") + b"." + payload
    try:
        sig_bytes = _b64url_decode(sig_b64)
    except (ValueError, base64.binascii.Error):
        return False

    try:
        pub.verify(sig_bytes, signing_input)
    except InvalidSignature:
        return False
    return True


# ---------- chain walking ----------


def _entry_hash(entry: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(jcs_canonicalise(entry)).hexdigest()


def walk_chain(entries: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    """Validate the parent-hash DAG.

    Reports:
      * duplicate entries (same entry_hash appears >1 time)
      * orphan parents (entry references a parent_hash not present in the log)

    Order-independent: parents may appear after children in `entries`.
    Returns (ok, errors). `errors` is a list of human-readable diagnostics.

    NOTE: This does NOT verify signatures - that's `verify_jws_detached`'s
    job. `verify_pack` composes both. Splitting them keeps each unit
    testable in isolation and lets the caller decide whether to skip
    signature checks (e.g. fast fork-detection on CI).
    """
    errors: list[str] = []
    by_hash: dict[str, dict[str, Any]] = {}

    for idx, e in enumerate(entries):
        if not isinstance(e, dict):
            errors.append(f"entry #{idx}: not a JSON object")
            continue
        h = _entry_hash(e)
        if h in by_hash:
            errors.append(f"duplicate entry {h}")
            continue
        by_hash[h] = e

    for h, e in by_hash.items():
        parents = e.get("parent_hashes", [])
        if not isinstance(parents, list):
            errors.append(f"entry {h}: parent_hashes is not a list")
            continue
        for p in parents:
            if not isinstance(p, str):
                errors.append(f"entry {h}: parent hash not a string")
                continue
            if p not in by_hash:
                errors.append(f"entry {h}: orphan parent (unknown parent {p})")

    return (not errors, errors)


# ---------- pack verification ----------


@dataclass
class VerifyResult:
    """Outcome of a verify_pack call.

    Surfaces in CLI JSON output (stderr). `ok` is the boolean exit signal.
    `errors` is human-readable; `stats` is structured for machine consumers.
    """

    ok: bool
    errors: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


def _read_text_member(zf: zipfile.ZipFile, name: str) -> str | None:
    try:
        with zf.open(name) as f:
            return f.read().decode("utf-8")
    except (KeyError, UnicodeDecodeError):
        return None


def _read_bytes_member(zf: zipfile.ZipFile, name: str) -> bytes | None:
    try:
        with zf.open(name) as f:
            return f.read()
    except KeyError:
        return None


def _pack_format_version(zf: zipfile.ZipFile) -> int:
    """Return the pack's ``pack_format_version``, defaulting to legacy.

    The version lives in ``pack-manifest.json``. An absent manifest, an
    absent/unparseable field, or a stray boolean maps to the legacy format
    (re-canonicalise rule), mirroring the Merkle seal's ``_seal_scheme``
    legacy-default (issue #1866). The version field rides inside the
    operator-signed manifest body, so a tamperer who downgrades it to defeat
    the v2 byte-binding rule invalidates ``pack-manifest.json.sig`` - the
    signature an operator verifies on the with-key path.
    """
    raw = _read_text_member(zf, _MANIFEST_NAME)
    if raw is None:
        return _PACK_FORMAT_LEGACY
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError:
        return _PACK_FORMAT_LEGACY
    if not isinstance(manifest, dict):
        return _PACK_FORMAT_LEGACY
    value = manifest.get("pack_format_version", _PACK_FORMAT_LEGACY)
    if isinstance(value, bool):
        # ``bool`` is an ``int`` subclass; a stray boolean is not a version.
        return _PACK_FORMAT_LEGACY
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return _PACK_FORMAT_LEGACY
    return _PACK_FORMAT_LEGACY


def _read_manifest(zf: zipfile.ZipFile) -> dict[str, Any] | None:
    """Return the parsed ``pack-manifest.json`` dict, or None if absent/bad."""
    raw = _read_text_member(zf, _MANIFEST_NAME)
    if raw is None:
        return None
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return manifest if isinstance(manifest, dict) else None


def _pack_kind(manifest: dict[str, Any] | None) -> str:
    """Return the pack ``kind``; an absent/invalid field is the Article 12 bundle."""
    if manifest is None:
        return _KIND_ARTICLE12
    value = manifest.get("kind", _KIND_ARTICLE12)
    return value if isinstance(value, str) else _KIND_ARTICLE12


def _split_jsonl_bytes(raw_bytes: bytes) -> list[bytes]:
    """Strictly split the log's raw bytes on ``b"\\n"`` only.

    Text-mode iteration / ``str.splitlines()`` treats ``\\r``, ``\\r\\n`` and
    every universal newline (and ``\\v``, ``\\f``, ``\\x1c``-``\\x1e``,
    ``\\x85``, ``\\u2028``, ``\\u2029``) as a record boundary, so flipping a
    terminator (``0x0A`` -> ``0x0D``) reframes the file without changing any
    ``json.loads`` result for the survivors - a framing attack that hides or
    merges records while every surviving signature still verifies. Splitting
    on ``b"\\n"`` only keeps any other separator *inside* a record, where the
    byte-canonical check surfaces it as a verification failure. This mirrors
    ``bernstein.core.lineage.gate._split_jsonl_bytes`` so the offline auditor
    offers the same tamper-evidence as the in-tree lineage gate (issue #1871).
    """
    parts = raw_bytes.split(b"\n")
    # The v2 writer always ends the file with ``\n`` -> a trailing empty
    # element which we drop. A genuinely missing terminator is reported
    # separately by the caller before this is used.
    if parts and parts[-1] == b"":
        parts.pop()
    return parts


def _parse_log_v1(log_raw: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Legacy (v1) parse: text-mode ``splitlines`` + parse each line.

    Verification re-canonicalises the parsed entry, so pre-fix packs (written
    with non-canonical bytes) still verify under their original rule. Retained
    only for backward compatibility (issue #1871).
    """
    entries: list[dict[str, Any]] = []
    errors: list[str] = []
    for lineno, line in enumerate(log_raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError as exc:
            errors.append(f"{_LOG_NAME}:{lineno}: invalid JSON ({exc.msg})")
    return entries, errors


def _parse_log_v2(log_bytes: bytes) -> tuple[list[dict[str, Any]], list[str]]:
    """Byte-binding (v2) parse: split on ``b"\\n"``, require canonical bytes.

    Every record must equal its JCS-canonical form byte-for-byte
    (``jcs_canonicalise(entry) == raw_line``). A value-preserving rewrite -
    reordered keys, inserted whitespace, a flipped terminator - parses to
    identical fields but differs on disk, so it is rejected here rather than
    silently re-canonicalised and accepted. A missing trailing newline is
    surfaced as tamper-evidence (a truncated or terminator-stripped final
    record). Mirrors the in-tree lineage gate's ``_parse_log`` (issue #1848).
    """
    entries: list[dict[str, Any]] = []
    errors: list[str] = []
    if log_bytes and not log_bytes.endswith(b"\n"):
        errors.append(f"{_LOG_NAME}: missing trailing newline")
    for lineno, raw_line in enumerate(_split_jsonl_bytes(log_bytes), start=1):
        if raw_line == b"":
            continue
        try:
            obj = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            errors.append(f"{_LOG_NAME}:{lineno}: invalid JSON ({exc.msg})")
            continue
        if not isinstance(obj, dict):
            errors.append(f"{_LOG_NAME}:{lineno}: not a JSON object")
            continue
        if jcs_canonicalise(obj) != raw_line:
            errors.append(f"{_LOG_NAME}:{lineno}: non-canonical line bytes")
            continue
        entries.append(obj)
    return entries, errors


def verify_pack(zip_path: Path | str) -> VerifyResult:
    """Verify a compliance-pack ZIP end-to-end.

    Expected layout (per ADR-009 §8.2):

        lineage-log.jsonl
        signatures/<entry_hash>.jws       (one file per entry)
        agent-cards/<agent_id>.json       (one file per agent seen)

    Steps:
      1. Open the zip (defensive: never extractall - read members in memory).
      2. Read ``pack-manifest.json:pack_format_version`` and dispatch the log
         parse: v2 binds verification to the exact on-disk bytes (split on
         ``b"\\n"``, every record must equal its JCS-canonical form
         byte-for-byte, a missing trailing newline is tamper-evidence); v1 (or
         an unmarked legacy pack) keeps the original re-canonicalise rule so
         pre-fix packs still verify (issues #1871, #1848, #1866).
      3. Walk the parent-hash chain (orphans, dupes).
      4. For every entry: compute entry_hash, find sidecar JWS, find Agent
         Card by agent_id, verify Ed25519 JWS using card's public key + kid.

    Returns a VerifyResult with ok=False on the first ZIP-level failure
    (missing log, unreadable archive) so the CLI can short-circuit.
    All per-entry failures are collected into `errors`.
    """
    path = Path(zip_path)
    if not path.exists():
        return VerifyResult(ok=False, errors=[f"pack not found: {path}"])

    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        return VerifyResult(ok=False, errors=[f"not a valid zip archive: {path}"])

    with zf:
        manifest = _read_manifest(zf)
        pack_format = _pack_format_version(zf)
        kind = _pack_kind(manifest)

        if kind in _REGULATOR_KINDS:
            return _verify_regulator_pack(zf, manifest, kind=kind, pack_format=pack_format)

        entries, parse_errors = _parse_lineage_log(zf, pack_format)
        if entries is None:
            return VerifyResult(ok=False, errors=parse_errors)

        result_errors: list[str] = list(parse_errors)
        chain_ok, chain_errors = walk_chain(entries)
        result_errors.extend(chain_errors)

        cards, card_errors = _load_agent_cards(zf)
        result_errors.extend(card_errors)

        sig_failures, sig_errors = _verify_entry_signatures(zf, entries, cards)
        result_errors.extend(sig_errors)

        stats = {
            "entries": len(entries),
            "agents": len(cards),
            "chain_ok": chain_ok,
            "signature_failures": sig_failures,
            "pack_format_version": pack_format,
        }
        ok = not parse_errors and chain_ok and sig_failures == 0
        return VerifyResult(ok=ok, errors=result_errors, stats=stats)


# ---------- shared lineage-log verification helpers ----------


def _parse_lineage_log(
    zf: zipfile.ZipFile, pack_format: int
) -> tuple[list[dict[str, Any]] | None, list[str]]:
    """Parse ``lineage-log.jsonl`` under the format-appropriate rule.

    Returns ``(None, [error])`` when the log member is absent so the caller
    can short-circuit; otherwise ``(entries, parse_errors)``.
    """
    if pack_format >= _PACK_FORMAT_BYTE_BINDING:
        log_bytes = _read_bytes_member(zf, _LOG_NAME)
        if log_bytes is None:
            return None, [f"missing {_LOG_NAME} in pack"]
        return _parse_log_v2(log_bytes)
    log_raw = _read_text_member(zf, _LOG_NAME)
    if log_raw is None:
        return None, [f"missing {_LOG_NAME} in pack"]
    return _parse_log_v1(log_raw)


def _load_agent_cards(zf: zipfile.ZipFile) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Pre-load Agent Cards keyed by agent_id, skipping zip-slip paths."""
    cards: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for info in zf.infolist():
        if ".." in Path(info.filename).parts:
            continue
        if not info.filename.startswith(_CARD_DIR) or info.filename.endswith("/"):
            continue
        card_raw = _read_text_member(zf, info.filename)
        if card_raw is None:
            continue
        try:
            card = json.loads(card_raw)
        except json.JSONDecodeError:
            errors.append(f"{info.filename}: invalid JSON")
            continue
        aid = card.get("agent_id")
        if isinstance(aid, str):
            cards[aid] = card
    return cards, errors


def _verify_entry_signatures(
    zf: zipfile.ZipFile,
    entries: list[dict[str, Any]],
    cards: dict[str, dict[str, Any]],
) -> tuple[int, list[str]]:
    """Verify the detached JWS of every entry against its Agent Card."""
    errors: list[str] = []
    sig_failures = 0
    for e in entries:
        entry_hash = _entry_hash(e)
        agent_id = e.get("agent_id", "")
        expected_kid = e.get("agent_card_kid", "")
        card = cards.get(agent_id)
        if card is None:
            errors.append(f"entry {entry_hash}: no Agent Card for {agent_id}")
            sig_failures += 1
            continue
        if card.get("kid") != expected_kid:
            errors.append(
                f"entry {entry_hash}: kid mismatch (card={card.get('kid')!r}, "
                f"entry={expected_kid!r})"
            )
            sig_failures += 1
            continue
        sig_member = f"{_SIG_DIR}{entry_hash}.jws"
        jws = _read_text_member(zf, sig_member)
        if jws is None:
            errors.append(f"entry {entry_hash}: missing signature {sig_member}")
            sig_failures += 1
            continue
        payload = jcs_canonicalise(e)
        pub_pem = card.get("public_key_pem", "")
        if not isinstance(pub_pem, str) or not verify_jws_detached(
            payload, jws, pub_pem, expected_kid=expected_kid
        ):
            errors.append(f"entry {entry_hash}: signature verification failed")
            sig_failures += 1
    return sig_failures, errors


# ===========================================================================
# Regulator-mapped pack verification (kind = retention / incident / oversight)
#
# Each kind is a projection of the chain, not a new log. Verification recomputes
# the facts from the substrate the pack carries: the signed lineage log (with its
# boundary head hashes) for retention, the prev_hmac-chained audit slice plus the
# declared evidence-gap list for incident, and the content-addressed displayed-
# versus-executed binding of every approval receipt for oversight. On top of that
# every member is bound by sha256 to the signed manifest, so a one-byte tamper in
# any member (including a rendered PDF) is rejected by hash mismatch naming the
# member. Strip the substrate and there is nothing left to recompute.
# ===========================================================================


def _sha256_hex(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _canonical_any(obj: Any) -> bytes:
    """Canonical JSON bytes for any JSON value (matches the pack builder)."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def _verify_manifest_anchor(manifest: dict[str, Any]) -> list[str]:
    """Confirm ``output_hash`` self-anchors the canonical manifest body."""
    output_hash = manifest.get("output_hash")
    if not isinstance(output_hash, str):
        return [f"{_MANIFEST_NAME}: missing output_hash"]
    body = {k: v for k, v in manifest.items() if k != "output_hash"}
    recomputed = _sha256_hex(_canonical_any(body))
    if recomputed != output_hash:
        return [
            f"{_MANIFEST_NAME}: output_hash mismatch (expected {output_hash}, got {recomputed})"
        ]
    return []


def _verify_member_integrity(zf: zipfile.ZipFile, manifest: dict[str, Any]) -> list[str]:
    """Recompute the sha256 of every member listed in ``input_hashes``.

    Names the offending member on any mismatch or absence so an auditor can
    point at the exact file - including a rendered PDF - that was altered.
    """
    input_hashes = manifest.get("input_hashes")
    if not isinstance(input_hashes, dict):
        return [f"{_MANIFEST_NAME}: input_hashes missing or malformed"]
    present = set(zf.namelist())
    errors: list[str] = []
    for name, expected in sorted(input_hashes.items()):
        if name not in present:
            errors.append(f"{name}: member listed in input_hashes is missing from the pack")
            continue
        data = _read_bytes_member(zf, name)
        if data is None:
            errors.append(f"{name}: member unreadable")
            continue
        actual = _sha256_hex(data)
        if actual != expected:
            errors.append(f"{name}: content hash mismatch (expected {expected}, got {actual})")
    return errors


def _parse_canonical_jsonl(raw_bytes: bytes, name: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Split ``name`` on ``b"\\n"`` and require every record be byte-canonical."""
    events: list[dict[str, Any]] = []
    errors: list[str] = []
    if raw_bytes and not raw_bytes.endswith(b"\n"):
        errors.append(f"{name}: missing trailing newline")
    for lineno, raw_line in enumerate(_split_jsonl_bytes(raw_bytes), start=1):
        if raw_line == b"":
            continue
        try:
            obj = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            errors.append(f"{name}:{lineno}: invalid JSON ({exc.msg})")
            continue
        if not isinstance(obj, dict):
            errors.append(f"{name}:{lineno}: not a JSON object")
            continue
        if _canonical_any(obj) != raw_line:
            errors.append(f"{name}:{lineno}: non-canonical line bytes")
            continue
        events.append(obj)
    return events, errors


def _verify_prev_hmac_chain(events: list[dict[str, Any]], name: str) -> list[str]:
    """Confirm the slice is contiguous: each ``prev_hmac`` chains to its predecessor."""
    errors: list[str] = []
    prev_hmac: str | None = None
    for idx, event in enumerate(events):
        cur_prev = str(event.get("prev_hmac", ""))
        cur_hmac = str(event.get("hmac", ""))
        if prev_hmac is not None and cur_prev != prev_hmac:
            errors.append(f"{name}: prev_hmac chain break at index {idx}")
        prev_hmac = cur_hmac
    return errors


def _verify_retention(
    zf: zipfile.ZipFile,
    manifest: dict[str, Any],
    pack_format: int,
    errors: list[str],
    stats: dict[str, Any],
) -> None:
    """Re-verify the embedded signed log and recompute the boundary head hashes."""
    entries, parse_errors = _parse_lineage_log(zf, pack_format)
    errors.extend(parse_errors)
    if entries is None:
        return
    chain_ok, chain_errors = walk_chain(entries)
    errors.extend(chain_errors)
    cards, card_errors = _load_agent_cards(zf)
    errors.extend(card_errors)
    sig_failures, sig_errors = _verify_entry_signatures(zf, entries, cards)
    errors.extend(sig_errors)
    stats.update(entries=len(entries), chain_ok=chain_ok, signature_failures=sig_failures)

    evidence_raw = _read_bytes_member(zf, "retention-evidence.json")
    if evidence_raw is None:
        errors.append("retention-evidence.json: missing")
        return
    try:
        evidence = json.loads(evidence_raw)
    except json.JSONDecodeError:
        errors.append("retention-evidence.json: invalid JSON")
        return

    ordered = sorted(entries, key=lambda e: (e.get("ts_ns", 0), _entry_hash(e)))
    first = _entry_hash(ordered[0]) if ordered else None
    last = _entry_hash(ordered[-1]) if ordered else None
    boundary = evidence.get("boundary", {}) if isinstance(evidence, dict) else {}
    if boundary.get("first_entry_hash") != first:
        errors.append("retention-evidence.json: first_entry_hash does not match the embedded log")
    if boundary.get("last_entry_hash") != last:
        errors.append("retention-evidence.json: last_entry_hash does not match the embedded log")
    if evidence.get("entry_count") != len(entries):
        errors.append("retention-evidence.json: entry_count does not match the embedded log")

    m_boundary = manifest.get("boundary", {})
    if isinstance(m_boundary, dict) and (
        m_boundary.get("first_entry_hash") != first or m_boundary.get("last_entry_hash") != last
    ):
        errors.append(f"{_MANIFEST_NAME}: boundary head hashes do not match the embedded log")
    stats["coverage_gaps"] = len(
        evidence.get("coverage_gaps", []) if isinstance(evidence, dict) else []
    )


def _verify_oversight(
    zf: zipfile.ZipFile, manifest: dict[str, Any], errors: list[str], stats: dict[str, Any]
) -> None:
    """Recompute the displayed-versus-executed binding of every approval receipt."""
    evidence_raw = _read_bytes_member(zf, "oversight-evidence.json")
    ev_by_id: dict[str, dict[str, Any]] = {}
    if evidence_raw is None:
        errors.append("oversight-evidence.json: missing")
    else:
        try:
            evidence = json.loads(evidence_raw)
            if isinstance(evidence, dict):
                for row in evidence.get("receipts", []):
                    if isinstance(row, dict):
                        ev_by_id[str(row.get("receipt_id"))] = row
        except json.JSONDecodeError:
            errors.append("oversight-evidence.json: invalid JSON")

    receipt_count = 0
    binding_failures = 0
    for info in zf.infolist():
        name = info.filename
        if ".." in Path(name).parts or not name.startswith("receipts/") or name.endswith("/"):
            continue
        raw = _read_bytes_member(zf, name)
        if raw is None:
            continue
        try:
            receipt = json.loads(raw)
        except json.JSONDecodeError:
            errors.append(f"{name}: invalid JSON")
            binding_failures += 1
            continue
        receipt_count += 1
        displayed_hash = _sha256_hex(_canonical_any(receipt.get("displayed")))
        executed_hash = _sha256_hex(_canonical_any(receipt.get("executed")))
        if displayed_hash != receipt.get("displayed_hash"):
            errors.append(f"{name}: displayed_hash does not match the displayed payload")
            binding_failures += 1
        if executed_hash != receipt.get("executed_hash"):
            errors.append(f"{name}: executed_hash does not match the executed payload")
            binding_failures += 1
        row = ev_by_id.get(str(receipt.get("receipt_id")))
        if row is None:
            errors.append(f"{name}: receipt is not present in oversight-evidence.json")
            binding_failures += 1
        else:
            expected_binding = receipt.get("displayed_hash") == receipt.get("executed_hash")
            if (
                row.get("displayed_hash") != receipt.get("displayed_hash")
                or row.get("executed_hash") != receipt.get("executed_hash")
                or row.get("binding_ok") != expected_binding
            ):
                errors.append(f"{name}: oversight-evidence row is inconsistent with the receipt")
                binding_failures += 1
    stats.update(receipts=receipt_count, binding_failures=binding_failures)


def _verify_incident(
    zf: zipfile.ZipFile, manifest: dict[str, Any], errors: list[str], stats: dict[str, Any]
) -> None:
    """Re-walk the audit slice and surface the declared evidence-gap list."""
    slice_bytes = _read_bytes_member(zf, "audit-slice.jsonl")
    if slice_bytes is None:
        errors.append("audit-slice.jsonl: missing")
    else:
        events, slice_errors = _parse_canonical_jsonl(slice_bytes, "audit-slice.jsonl")
        errors.extend(slice_errors)
        errors.extend(_verify_prev_hmac_chain(events, "audit-slice.jsonl"))
        stats["audit_events"] = len(events)

    gaps_list: list[Any] = []
    gaps_raw = _read_bytes_member(zf, "gaps.json")
    if gaps_raw is None:
        errors.append("gaps.json: missing")
    else:
        try:
            gaps_doc = json.loads(gaps_raw)
            if isinstance(gaps_doc, dict):
                gaps_list = list(gaps_doc.get("gaps", []))
        except json.JSONDecodeError:
            errors.append("gaps.json: invalid JSON")

    stats["gaps"] = gaps_list
    stats["gap_count"] = len(gaps_list)
    m_gap_count = manifest.get("gap_count")
    if (
        isinstance(m_gap_count, int)
        and not isinstance(m_gap_count, bool)
        and m_gap_count != len(gaps_list)
    ):
        errors.append(
            f"gaps.json: gap_count {len(gaps_list)} disagrees with manifest gap_count {m_gap_count}"
        )


def _verify_regulator_pack(
    zf: zipfile.ZipFile,
    manifest: dict[str, Any] | None,
    *,
    kind: str,
    pack_format: int,
) -> VerifyResult:
    """Verify a retention / incident / oversight pack as a chain projection."""
    stats: dict[str, Any] = {"kind": kind, "pack_format_version": pack_format}
    if manifest is None:
        return VerifyResult(
            ok=False, errors=[f"{_MANIFEST_NAME}: missing or unparseable"], stats=stats
        )

    errors: list[str] = []
    errors.extend(_verify_manifest_anchor(manifest))
    errors.extend(_verify_member_integrity(zf, manifest))

    if kind == _KIND_RETENTION:
        _verify_retention(zf, manifest, pack_format, errors, stats)
    elif kind == _KIND_OVERSIGHT:
        _verify_oversight(zf, manifest, errors, stats)
    elif kind == _KIND_INCIDENT:
        _verify_incident(zf, manifest, errors, stats)

    return VerifyResult(ok=not errors, errors=errors, stats=stats)
