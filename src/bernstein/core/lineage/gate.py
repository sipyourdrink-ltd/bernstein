"""Lineage CI gate (ADR-009 §6.2).

`check(log_path, agent_cards_dir)` returns a `GateResult` reporting whether
every entry in `log.jsonl` is:

  1. Parsable as JSON and satisfies the LineageEntry schema.
  2. Backed by a matching detached JWS sidecar that verifies against the
     agent's published Agent Card (Ed25519, RFC 7515 detached).
  3. (Optional) HMAC-protected with the supplied operator secret.
  4. Anchored - every `parent_hash` resolves to another entry in the log.
  5. Free of unresolved forks (each open tip is single OR is a merge entry).
  6. (Optional) Authored by a steward-allow-listed agent when the entry is
     a merge (parent_hashes length >= 2).
  7. (Optional) Coupled to a caller-supplied set of authenticated tool-call
     attestations.

The check is read-only and does not depend on the LineageStore - it can
operate on a frozen log + cards directory (e.g. an audit pack).
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bernstein.core.lineage.entry import LineageEntry, canonicalise, compute_operator_hmac, entry_hash
from bernstein.core.lineage.identity import AgentCard, jws_header_kid, verify_detached
from bernstein.core.lineage.tips import compute_tips, detect_forks

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class GateResult:
    """Outcome of `check`. `ok` is True iff `failures` is empty."""

    ok: bool
    failures: list[str] = field(default_factory=list)


def _load_cards(cards_dir: Path) -> tuple[dict[tuple[str, str], AgentCard], list[str]]:
    """Load all Agent Cards, keyed by ``(agent_id, kid)``.

    Two on-disk layouts are accepted so the gate stays verifiable across a key
    rotation (issue #1837):

      * Legacy single-card: ``<agent-id>/card.json`` - one key per agent, the
        layout production writes today.
      * Per-kid: ``<agent-id>/<kid>/card.json`` - lets an agent keep multiple
        historical keys side by side after rotating its ``kid``.

    The card's own ``agent_id`` and ``kid`` body fields are authoritative for
    the map key (not the directory names), so a misfiled card cannot
    masquerade under a different identity.

    When two distinct card payloads map to the same ``(agent_id, kid)`` the
    loader does *not* pick a winner: "last one read wins" would make the
    verification outcome depend on filesystem read order, which a kid-binding
    security gate must never do (issue #1837). The conflicting key is reported
    as an explicit failure naming the ``agent_id``/``kid``. Byte-identical
    duplicates - the normal case when the legacy and per-kid layouts carry the
    same key - are accepted without error.

    Returns:
        ``(cards, failures)`` where ``cards`` maps ``(agent_id, kid)`` to the
        loaded :class:`AgentCard` and ``failures`` lists any conflict messages.
    """
    out: dict[tuple[str, str], AgentCard] = {}
    failures: list[str] = []
    if not cards_dir.exists():
        return out, failures
    # Legacy ``<agent-id>/card.json`` then per-kid ``<agent-id>/<kid>/card.json``.
    for card_file in (*cards_dir.glob("*/card.json"), *cards_dir.glob("*/*/card.json")):
        try:
            data = json.loads(card_file.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        agent_id = data.get("agent_id")
        kid = data.get("kid")
        pub = data.get("public_key_pem")
        if not (isinstance(agent_id, str) and isinstance(kid, str) and isinstance(pub, str)):
            continue
        card = AgentCard(
            agent_id=agent_id,
            kid=kid,
            public_key_pem=pub,
            protocol_version=data.get("protocolVersion", "a2a/1.0"),
        )
        key = (agent_id, kid)
        prev = out.get(key)
        if prev is not None and prev != card:
            # Two on-disk cards claim the same identity with different key
            # material or protocol version. Refuse to choose; surface it.
            failures.append(
                f"conflicting agent cards for (agent_id={agent_id!r}, kid={kid!r}): "
                "two on-disk cards map to the same identity with different contents"
            )
            continue
        out[key] = card
    return out, failures


def _shard_path(artefact_path: str) -> tuple[str, str]:
    """Returns (shard, full_hash) for the per-artefact signatures layout."""
    digest = hashlib.sha256(artefact_path.encode()).hexdigest()
    return digest[:2], digest


def _signature_path(log_dir: Path, entry: LineageEntry, eh: str) -> Path:
    shard, full = _shard_path(entry.artefact_path)
    return log_dir / "signatures" / shard / full / (eh.replace("sha256:", "") + ".jws")


def _split_jsonl_bytes(raw_bytes: bytes) -> list[bytes]:
    """Strictly split the log's raw bytes on ``b"\\n"`` only.

    Text-mode iteration (``for line in f``) treats ``\\r``, ``\\r\\n`` and
    every universal newline as a record boundary, so flipping a record
    terminator (e.g. ``0x0A`` -> ``0x0D``) reframes the file without changing
    any ``json.loads`` result for the survivors - a framing attack that hides
    or merges records while every surviving signature still verifies. Splitting
    on ``b"\\n"`` only keeps any other separator *inside* a record, where the
    byte-canonical check downstream surfaces it as a verification failure. This
    mirrors ``bernstein.core.security.audit._split_jsonl_bytes`` so the lineage
    log offers the same tamper-evidence as the sibling HMAC audit log.
    """
    parts = raw_bytes.split(b"\n")
    # The writer always ends the file with ``\n`` -> ``split`` yields a trailing
    # empty element which we drop. A genuinely missing terminator is reported
    # separately by ``_parse_log`` before this is called.
    if parts and parts[-1] == b"":
        parts.pop()
    return parts


def _parse_log(log_path: Path) -> tuple[list[LineageEntry], list[str]]:
    """Parse the JSONL log; return (entries, parse_failures).

    Verification binds to the *exact bytes on disk*: the log is read as bytes
    and split strictly on ``b"\\n"``, and every record must equal its JCS
    canonical form byte-for-byte (``canonicalise(entry) == raw_line``). A
    non-canonical rewrite that preserves the field values - reordered keys,
    inserted whitespace, a flipped terminator - parses to identical fields but
    differs on disk, so it is rejected here rather than silently re-canonicalised
    and accepted. The on-disk ``log.jsonl`` is the provenance anchor (see
    ``store.py``), so its raw bytes are themselves checked for integrity (issue
    #1848).
    """
    entries: list[LineageEntry] = []
    failures: list[str] = []
    if not log_path.exists():
        return entries, failures
    raw_bytes = log_path.read_bytes()
    if raw_bytes and not raw_bytes.endswith(b"\n"):
        # The writer always terminates with ``\n``; absence is itself
        # tamper-evidence (a truncated final record or a terminator stripped /
        # flipped at EOF). Surface it, then continue so the per-line loop still
        # reports any in-record corruption.
        failures.append("log: missing trailing newline")
    for line_no, raw_line in enumerate(_split_jsonl_bytes(raw_bytes), start=1):
        if raw_line == b"":
            continue
        try:
            obj = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            failures.append(f"log line {line_no}: parse error: {exc}")
            continue
        try:
            entry = LineageEntry(**obj)
        except (TypeError, ValueError) as exc:
            failures.append(f"log line {line_no}: corrupt entry: {exc}")
            continue
        # Byte-canonical tamper-evidence: ``json.loads`` accepts non-canonical
        # bytes (reordered keys, extra whitespace, a stray ``\r``) that would
        # otherwise re-canonicalise to a valid signature. Require the on-disk
        # line to equal the canonical form so any such rewrite is a failure.
        if canonicalise(entry) != raw_line:
            failures.append(f"log line {line_no}: non-canonical line bytes")
            continue
        entries.append(entry)
    return entries, failures


def check(
    log_path: Path,
    agent_cards_dir: Path,
    *,
    operator_secret: bytes | None = None,
    steward_allowlist: frozenset[str] | None = None,
    verified_tool_call_ids: frozenset[str] | None = None,
) -> GateResult:
    """Run all lineage invariants against the log + cards on disk.

    Args:
        log_path: path to `.sdd/lineage/log.jsonl`.
        agent_cards_dir: directory containing `<agent-id>/card.json`.
        operator_secret: when given, verify each entry's `operator_hmac`
            against an HMAC of the entry's canonical bytes (without the
            HMAC field itself). When None, the HMAC check is skipped.
        steward_allowlist: when given, every merge entry's `agent_id` must
            be in this set or the gate fails (privilege escalation guard).
        verified_tool_call_ids: authenticated tool-call request identifiers
            supplied by the caller. When provided, every entry's signed
            ``tool_call_id`` must occur in this set. ``None`` preserves the
            legacy/audit-disabled gate; an empty set enables the coupling and
            therefore rejects every referenced tool call.

    Returns:
        GateResult with ok=True iff failures is empty.
    """
    failures: list[str] = []
    entries, parse_fails = _parse_log(log_path)
    failures.extend(parse_fails)

    if not entries:
        return GateResult(ok=not failures, failures=failures)

    cards, card_failures = _load_cards(agent_cards_dir)
    failures.extend(card_failures)
    log_dir = log_path.parent

    # Per-entry signature + HMAC + card lookups.
    known_hashes: set[str] = set()
    for entry in entries:
        eh = entry_hash(entry)
        known_hashes.add(eh)
        # Bind verification to the kid the entry *signed*: resolve the card by
        # the ``(agent_id, agent_card_kid)`` pair, not by ``agent_id`` alone.
        # Selecting the card by agent_id only let a kid-substitution slip
        # through and broke every historical entry on key rotation - see
        # issue #1837. A missing card for that exact kid is a kid-binding
        # failure, distinct from a generic bad signature.
        card = cards.get((entry.agent_id, entry.agent_card_kid))
        if card is None:
            failures.append(
                f"{entry.artefact_path}: no agent card for "
                f"(agent_id={entry.agent_id!r}, kid={entry.agent_card_kid!r}) - "
                f"kid binding cannot be established (entry {eh})"
            )
            continue
        # Signature
        sig_path = _signature_path(log_dir, entry, eh)
        if not sig_path.exists():
            failures.append(f"{entry.artefact_path}: missing signature sidecar for entry {eh}")
        else:
            try:
                jws = sig_path.read_text().strip()
            except OSError as exc:
                failures.append(f"{entry.artefact_path}: cannot read signature {sig_path}: {exc}")
                continue
            # The JWS header kid must match the kid the entry committed to in
            # its signed body. A divergence means the signature was made under
            # a different key id than the entry claims; reject it as a
            # kid-binding failure even if it would verify against some card.
            header_kid = jws_header_kid(jws)
            if header_kid != entry.agent_card_kid:
                failures.append(
                    f"{entry.artefact_path}: kid binding mismatch on entry {eh} - "
                    f"signed body kid {entry.agent_card_kid!r} != JWS header kid {header_kid!r}"
                )
                continue
            canonical = canonicalise(entry)
            if not verify_detached(canonical, jws, card):
                failures.append(f"{entry.artefact_path}: invalid signature on entry {eh}")
        # HMAC. Body covers every entry field (with ``operator_hmac`` blanked)
        # so a substitution swapping ``agent_id`` or ``artefact_path`` after
        # signing is independently caught here - see ADR-009 §5.2.
        if operator_secret is not None:
            expected = compute_operator_hmac(entry, operator_secret)
            if not _hmac.compare_digest(expected, entry.operator_hmac):
                failures.append(f"{entry.artefact_path}: HMAC mismatch on entry {eh}")
        # Steward allow-list for merge entries.
        if steward_allowlist is not None and len(entry.parent_hashes) >= 2 and entry.agent_id not in steward_allowlist:
            failures.append(
                f"{entry.artefact_path}: merge entry {eh} written by non-steward {entry.agent_id!r} (not in allowlist)"
            )
        # The lineage module deliberately does not open or interpret the audit
        # chain.  The security caller verifies that chain and projects exactly
        # the request ids supported by identity-bound tool-call evidence.  This
        # keeps payload contents from selecting their own verification mode and
        # keeps the frozen lineage gate usable in legacy/audit-disabled packs.
        if verified_tool_call_ids is not None and entry.tool_call_id not in verified_tool_call_ids:
            failures.append(
                f"{entry.artefact_path}: tool_call_id {entry.tool_call_id!r} on entry {eh} "
                "has no matching verified toolcall.attestation"
            )

    # Parent-hash chain integrity.
    for entry in entries:
        for ph in entry.parent_hashes:
            if ph not in known_hashes:
                failures.append(f"{entry.artefact_path}: dangling parent_hash {ph} on entry {entry_hash(entry)}")

    # Tip / fork analysis.
    tips = compute_tips(entries)
    for path, tipset in tips.items():
        if len(tipset["open"]) > 1:
            failures.append(f"{path}: {len(tipset['open'])} unresolved open tips: {tipset['open']}")
    for fork in detect_forks(entries):
        # A fork is "resolved" iff some entry has parent_hashes covering ALL
        # of the fork's child_hashes (subset thereof - diamond merges count).
        resolved = False
        children = set(fork.child_hashes)
        for entry in entries:
            if len(entry.parent_hashes) >= 2 and children.issubset(set(entry.parent_hashes)):
                resolved = True
                break
        if not resolved:
            failures.append(
                f"{fork.artefact_path}: unresolved fork at parent {fork.parent_hash} "
                f"with children {list(fork.child_hashes)}"
            )

    return GateResult(ok=not failures, failures=failures)


# ---------------------------------------------------------------------------
# Skill lockfile extension (issue #1796)
# ---------------------------------------------------------------------------


def check_skill_lockfile(
    lockfile_path: Path,
    known_good_manifest_shas: frozenset[str],
) -> GateResult:
    """Reject a PR whose skill lockfile references an un-anchored manifest.

    This is an additive check on top of the existing lineage-v1 gate (it
    does NOT introduce a new gate). The caller runs :func:`check` for
    the lineage log and `check_skill_lockfile` for any `skills.lock`
    file present in the PR; both must pass for CI to be green.

    Every `[[catalog]]` row in the lockfile carries a `manifest_sha256`
    that must appear in `known_good_manifest_shas`, which is derived
    from the audit chain's `skill.catalog.install` / `skill.catalog.upgrade`
    events. A row whose sha is missing indicates either a tampered
    lockfile or a manifest that was never anchored into the chain;
    either way the PR is rejected.

    Args:
        lockfile_path: Path to `skills.lock`. Missing or empty lockfiles
            return a passing result (no rows to check).
        known_good_manifest_shas: Set of manifest digests anchored in
            the audit chain.

    Returns:
        :class:`GateResult` with `ok=True` iff every row is anchored.
    """
    if not lockfile_path.is_file():
        return GateResult(ok=True, failures=[])
    try:
        # Local import keeps the lineage module free of skill-package
        # imports at top level; the dependency is one-way (catalog->lineage).
        from bernstein.core.skills.catalog.lockfile import read_state
    except ImportError:  # pragma: no cover - module always present
        return GateResult(ok=False, failures=["skill catalog lockfile module is missing"])

    state = read_state(lockfile_path)
    failures: list[str] = [
        (
            f"{row.id}: lockfile manifest_sha256 {row.manifest_sha256[:12]}... "
            "is not present in the audit chain's known-good set"
        )
        for row in state.catalog
        if row.manifest_sha256 not in known_good_manifest_shas
    ]
    return GateResult(ok=not failures, failures=failures)


__all__ = ["GateResult", "check", "check_skill_lockfile"]
