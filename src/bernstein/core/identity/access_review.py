"""Per-principal access review derived from the identity chains (issue #4974).

An access review asks who granted an agent the power it had, when, and on whose
authority. Every fact that answers it is already recorded: the per-hop
delegation receipts (:mod:`bernstein.core.identity.delegation`) hold the
authorization trail, and the grant chain (:mod:`bernstein.core.identity.grants`)
holds the capability ceilings. This module projects both into one windowed,
per-principal document instead of leaving an operator to assemble it by hand.

The document is a projection, never a store
-------------------------------------------
:func:`build_review` reads only verified chains. A run whose grant or delegation
chain fails HMAC verification contributes no rows; it is named in
``unverified`` instead, so a review over a broken chain says so rather than
quietly listing fewer facts. Nothing here recomputes an RBAC or budget verdict -
that is ``bernstein governance verify`` - and nothing here approves anything.

Two anchors over one body, as the grant records carry
-----------------------------------------------------
The review body is signed twice over, in the same shape
:mod:`bernstein.core.identity.grants` already uses:

* a **digest** (``sha256:<hex>``) over the RFC 8785 canonical bytes of the body,
  which is the content address a sign-off attaches to; and
* an **Ed25519 signature** by the install manager identity over the same body,
  so a third party can check authorship offline from the embedded public key.

Neither reads a wall clock, so regenerating a review over the same window and
the same chain state produces byte-identical output.

The sign-off is a chain event
-----------------------------
:func:`record_signoff` appends one HMAC-chained record to
``<root>/access_review/signoffs.jsonl``, keyed by the install audit key exactly
as the delegation and grant chains are. The record names the reviewer, the
decision, and the digest of the reviewed body. :func:`verify_signoff` recomputes
the digest from the supplied bytes and looks for a record carrying it, so a
sign-off cannot be carried over to a document whose bytes differ: editing a row
changes the digest and the sign-off no longer covers anything.

What a passing verification does not establish: that the reviewed grants were
appropriate policy; that the chains handed to the reviewer were complete; that
the reviewer read what they signed. It establishes only that these exact bytes
were projected from those chains and that a named reviewer signed off on them.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac as _hmac
import json
import os
import sys
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

from bernstein.core.identity import delegation as _delegation
from bernstein.core.identity import grants as _grants
from bernstein.core.security.agent_card_signer import canonicalize_jcs

if TYPE_CHECKING:
    from collections.abc import Generator

if sys.platform == "win32":
    fcntl = None  # type: ignore[assignment]
else:
    import fcntl  # type: ignore[no-redef]

__all__ = [
    "DEFAULT_ROOT",
    "GENESIS_HMAC",
    "ROW_CEILING_CHANGE",
    "ROW_DELEGATION",
    "ROW_GRANT_ISSUED",
    "ROW_GRANT_REVOKED",
    "SCHEMA",
    "SIGNOFF_KIND",
    "AccessReview",
    "AccessReviewError",
    "ReviewVerification",
    "SignoffChainResult",
    "SignoffRecord",
    "SignoffVerdict",
    "build_default_review",
    "build_review",
    "parse_timestamp",
    "record_default_signoff",
    "record_signoff",
    "signoff_path",
    "verify_default_signoff",
    "verify_signed_review",
    "verify_signoff",
    "verify_signoff_chain",
]

#: Schema identifier carried by every review body.
SCHEMA: Final[str] = "bernstein.access-review/v1"

#: Genesis linkage value for the first sign-off record (the audit-chain
#: convention shared with :mod:`delegation` and :mod:`grants`).
GENESIS_HMAC: Final[str] = "0" * 64

#: Default root - the audit tree that already holds ``delegation/`` and
#: ``grants/``, so ``access_review/`` sits beside them.
DEFAULT_ROOT: Final[Path] = Path(".sdd/audit")

_SUBDIR: Final[str] = "access_review"
_SIGNOFF_FILE: Final[str] = "signoffs.jsonl"

#: Row kinds. Grant and delegation rows are direct readings of a chain record;
#: a ceiling-change row is derived by comparing a task's successive grants.
ROW_GRANT_ISSUED: Final[str] = "grant_issued"
ROW_GRANT_REVOKED: Final[str] = "grant_revoked"
ROW_DELEGATION: Final[str] = "delegation"
ROW_CEILING_CHANGE: Final[str] = "capability_ceiling_change"

#: The only record kind the sign-off chain carries.
SIGNOFF_KIND: Final[str] = "access_review_signoff"

#: Decisions a reviewer may record. A review records a human decision; it never
#: makes one, so there is no "auto" member here.
DECISIONS: Final[tuple[str, ...]] = ("approved", "rejected")

_SIGNOFF_LOCK: Final[threading.Lock] = threading.Lock()


class AccessReviewError(Exception):
    """Raised on malformed review input (bad window bound, bad envelope)."""


# ---------------------------------------------------------------------------
# Window bounds
# ---------------------------------------------------------------------------


def parse_timestamp(text: str) -> int:
    """Return the epoch second for an ISO date/datetime or a bare epoch string.

    A naive value is read as UTC, so ``2026-01-01`` and
    ``2026-01-01T00:00:00Z`` name the same instant. Raises
    :class:`AccessReviewError` on anything else rather than silently defaulting,
    because a mis-parsed bound silently changes which facts the review covers.
    """
    raw = text.strip()
    if not raw:
        raise AccessReviewError("empty timestamp")
    if raw.isdigit():
        return int(raw)
    normalised = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(normalised)
    except ValueError as exc:
        raise AccessReviewError(f"not an ISO date/datetime or epoch second: {text!r} ({exc})") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def _in_window(created: int, since: int | None, until: int | None) -> bool:
    """Return True when ``created`` falls in ``[since, until)``."""
    if since is not None and created < since:
        return False
    return not (until is not None and created >= until)


# ---------------------------------------------------------------------------
# The review document
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccessReview:
    """A signed, chain-derived access review over one window."""

    document: dict[str, Any]
    digest: str
    issuer: str
    issuer_pubkey: str
    signature: str

    @property
    def rows(self) -> list[dict[str, Any]]:
        """The windowed rows, in the document's canonical order."""
        return list(self.document["rows"])

    @classmethod
    def from_envelope(cls, envelope_bytes: bytes) -> AccessReview:
        """Rebuild a review from envelope bytes, refusing one that does not verify.

        Raises:
            AccessReviewError: when the digest or the signature does not check
                out, so nothing downstream can attach to bytes already broken.
        """
        verification = verify_signed_review(envelope_bytes)
        if not verification.ok or verification.document is None:
            raise AccessReviewError("; ".join(verification.errors) or "envelope did not verify")
        envelope = cast("dict[str, Any]", json.loads(envelope_bytes.decode("utf-8")))
        return cls(
            document=verification.document,
            digest=verification.digest,
            issuer=str(envelope.get("issuer", "")),
            issuer_pubkey=str(envelope.get("issuer_pubkey", "")),
            signature=str(envelope.get("signature", "")),
        )

    def envelope(self) -> dict[str, Any]:
        """Return the signed envelope: the body plus both of its anchors."""
        return {
            "digest": self.digest,
            "issuer": self.issuer,
            "issuer_pubkey": self.issuer_pubkey,
            "review": self.document,
            "signature": self.signature,
        }

    def envelope_bytes(self) -> bytes:
        """Return the on-disk bytes of the envelope.

        Pretty-printed for a human reviewer; the digest and the signature are
        both taken over the *canonical* encoding of ``review``, so re-indenting
        the file cannot change what a sign-off covers.
        """
        return json.dumps(self.envelope(), sort_keys=True, indent=2, ensure_ascii=False).encode() + b"\n"


def _row(
    *,
    principal: str,
    event: str,
    authorized_by: str,
    chain: str,
    run_id: str,
    sequence: int,
    chain_event: str,
    created: int,
    detail: dict[str, Any],
) -> dict[str, Any]:
    return {
        "authorized_by": authorized_by,
        "chain": chain,
        "chain_event": chain_event,
        "created": created,
        "detail": detail,
        "event": event,
        "principal": principal,
        "run_id": run_id,
        "sequence": sequence,
    }


def _sort_key(row: dict[str, Any]) -> tuple[int, str, str, int, str]:
    return (row["created"], row["chain"], row["run_id"], row["sequence"], row["event"])


def _chain_runs(root: Path, subdir: str) -> list[str]:
    """Return the run ids backing ``<root>/<subdir>/*.jsonl``, sorted."""
    directory = Path(root) / subdir
    if not directory.is_dir():
        return []
    return sorted(path.stem for path in directory.glob("*.jsonl"))


def _grant_rows(
    root: Path,
    key: bytes,
    *,
    since: int | None,
    until: int | None,
    sources: list[dict[str, Any]],
    unverified: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project the grant chains into windowed rows, skipping broken chains."""
    rows: list[dict[str, Any]] = []
    for run_id in _chain_runs(root, "grants"):
        result = _grants.verify_grant_chain(root=root, run_id=run_id, key=key)
        if not result.valid:
            unverified.append(
                {"chain": "grants", "error": "; ".join(result.errors) or "chain did not verify", "run_id": run_id}
            )
            continue
        sources.append(
            {
                "chain": "grants",
                "head_event": result.records[-1].hmac,
                "records": len(result.records),
                "run_id": run_id,
            }
        )
        # A revoke record may carry an empty task_id; the issuing record for the
        # same grant_id is the authority on which principal it belonged to.
        task_of_grant = {r.grant_id: r.task_id for r in result.records if r.kind == _grants.GRANT_ISSUED}
        ceiling_of_task: dict[str, list[str]] = {}
        for record in result.records:
            principal = f"task:{record.task_id or task_of_grant.get(record.grant_id, '')}"
            inside = _in_window(record.created, since, until)
            if record.kind == _grants.GRANT_ISSUED:
                ceiling = list(record.capability_ceiling)
                previous = ceiling_of_task.get(record.task_id)
                ceiling_of_task[record.task_id] = ceiling
                if inside:
                    rows.append(
                        _row(
                            principal=principal,
                            event=ROW_GRANT_ISSUED,
                            authorized_by=record.issuer,
                            chain="grants",
                            run_id=run_id,
                            sequence=record.record_index,
                            chain_event=record.hmac,
                            created=record.created,
                            detail={
                                "audience": record.audience,
                                "capability_ceiling": ceiling,
                                "expiry": record.expiry,
                                "grant_id": record.grant_id,
                            },
                        )
                    )
                    if previous is not None and previous != ceiling:
                        rows.append(
                            _row(
                                principal=principal,
                                event=ROW_CEILING_CHANGE,
                                authorized_by=record.issuer,
                                chain="grants",
                                run_id=run_id,
                                sequence=record.record_index,
                                chain_event=record.hmac,
                                created=record.created,
                                detail={"from": previous, "grant_id": record.grant_id, "to": ceiling},
                            )
                        )
            elif record.kind == _grants.GRANT_REVOKED and inside:
                rows.append(
                    _row(
                        principal=principal,
                        event=ROW_GRANT_REVOKED,
                        authorized_by=record.issuer,
                        chain="grants",
                        run_id=run_id,
                        sequence=record.record_index,
                        chain_event=record.hmac,
                        created=record.created,
                        detail={"grant_id": record.grant_id, "reason": record.reason},
                    )
                )
    return rows


def _delegation_rows(
    root: Path,
    key: bytes,
    *,
    since: int | None,
    until: int | None,
    sources: list[dict[str, Any]],
    unverified: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project the delegation chains into windowed rows, skipping broken chains."""
    rows: list[dict[str, Any]] = []
    for run_id in _chain_runs(root, "delegation"):
        result = _delegation.verify_run_chain(root=root, run_id=run_id, key=key)
        if not result.chain_ok:
            unverified.append(
                {
                    "chain": "delegation",
                    "error": "; ".join(result.errors) or "chain did not verify",
                    "run_id": run_id,
                }
            )
            continue
        sources.append(
            {
                "chain": "delegation",
                "head_event": result.receipts[-1].hmac,
                "records": len(result.receipts),
                "run_id": run_id,
            }
        )
        for receipt in result.receipts:
            if not _in_window(receipt.created, since, until):
                continue
            rows.append(
                _row(
                    principal=receipt.audience,
                    event=ROW_DELEGATION,
                    authorized_by=receipt.issuer,
                    chain="delegation",
                    run_id=run_id,
                    sequence=receipt.hop_index,
                    chain_event=receipt.hmac,
                    created=receipt.created,
                    detail={
                        "act": receipt.act,
                        "scope_ref": receipt.scope_ref or "",
                        "subject": receipt.subject,
                    },
                )
            )
    return rows


def digest_body(document: dict[str, Any]) -> str:
    """Return the content address of a review body (``sha256:<hex>``)."""
    return "sha256:" + hashlib.sha256(canonicalize_jcs(document)).hexdigest()


def build_review(
    *,
    root: Path,
    key: bytes,
    since: int | None = None,
    until: int | None = None,
    signer: _grants.GrantSigner | None = None,
) -> AccessReview:
    """Derive the signed access review for ``[since, until)`` from the chains.

    Args:
        root: Audit root holding ``delegation/`` and ``grants/``.
        key: The install audit HMAC key the chains were written with.
        since: Inclusive lower bound (epoch seconds); ``None`` leaves it open.
        until: Exclusive upper bound (epoch seconds); ``None`` leaves it open.
            Left open by default on purpose - reading a wall clock here would
            make two projections of the same chain differ.
        signer: Ed25519 signer; defaults to the install manager identity.

    Returns:
        An :class:`AccessReview` whose bytes depend only on the chain state and
        the window, never on when it was built.
    """
    active_signer = signer if signer is not None else _install_signer()
    sources: list[dict[str, Any]] = []
    unverified: list[dict[str, Any]] = []
    rows = _grant_rows(root, key, since=since, until=until, sources=sources, unverified=unverified)
    rows += _delegation_rows(root, key, since=since, until=until, sources=sources, unverified=unverified)
    rows.sort(key=_sort_key)

    document = {
        "period": {"since": since, "until": until},
        "principals": sorted({row["principal"] for row in rows}),
        "row_count": len(rows),
        "rows": rows,
        "schema": SCHEMA,
        "sources": sorted(sources, key=lambda s: (s["chain"], s["run_id"])),
        "unverified": sorted(unverified, key=lambda u: (u["chain"], u["run_id"])),
    }
    return AccessReview(
        document=document,
        digest=digest_body(document),
        issuer=active_signer.issuer,
        issuer_pubkey=active_signer.public_key_pem,
        signature=active_signer.sign(document),
    )


@dataclass(frozen=True)
class ReviewVerification:
    """Outcome of checking a signed review envelope offline."""

    ok: bool
    digest: str = ""
    document: dict[str, Any] | None = None
    errors: tuple[str, ...] = ()


def verify_signed_review(envelope_bytes: bytes) -> ReviewVerification:
    """Recompute a review envelope's digest and check its Ed25519 signature.

    Both anchors are recomputed from the parsed body, so a re-indented file
    still verifies while a changed fact does not.
    """
    parsed: object
    try:
        parsed = json.loads(envelope_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return ReviewVerification(ok=False, errors=(f"envelope is not JSON: {exc}",))
    if not isinstance(parsed, dict):
        return ReviewVerification(ok=False, errors=("envelope carries no review body",))
    envelope = cast("dict[str, Any]", parsed)
    body = envelope.get("review")
    if not isinstance(body, dict):
        return ReviewVerification(ok=False, errors=("envelope carries no review body",))

    document = cast("dict[str, Any]", body)
    errors: list[str] = []
    recomputed = digest_body(document)
    if recomputed != str(envelope.get("digest", "")):
        errors.append("digest does not match the review body (document bytes differ)")
    if not _grants.verify_grant_signature(
        str(envelope.get("issuer_pubkey", "")), document, str(envelope.get("signature", ""))
    ):
        errors.append("Ed25519 signature invalid (review not authored by the embedded key)")
    return ReviewVerification(
        ok=not errors,
        digest=recomputed,
        document=document,
        errors=tuple(errors),
    )


# ---------------------------------------------------------------------------
# Sign-off chain
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignoffRecord:
    """One HMAC-chained reviewer sign-off over a review digest."""

    record_index: int
    kind: str
    reviewer: str
    decision: str
    review_digest: str
    period_since: int | None
    period_until: int | None
    row_count: int
    note: str
    created: int
    prev_hmac: str = GENESIS_HMAC
    hmac: str = ""

    def chain_body(self) -> dict[str, Any]:
        """Return the signed body (every field except ``hmac``)."""
        return {
            "created": self.created,
            "decision": self.decision,
            "kind": self.kind,
            "note": self.note,
            "period_since": self.period_since,
            "period_until": self.period_until,
            "prev_hmac": self.prev_hmac,
            "record_index": self.record_index,
            "review_digest": self.review_digest,
            "reviewer": self.reviewer,
            "row_count": self.row_count,
        }

    @classmethod
    def from_entry(cls, obj: dict[str, Any]) -> SignoffRecord:
        """Rebuild a record from its on-disk JSONL entry."""
        return cls(
            record_index=int(obj["record_index"]),
            kind=str(obj["kind"]),
            reviewer=str(obj["reviewer"]),
            decision=str(obj["decision"]),
            review_digest=str(obj["review_digest"]),
            period_since=None if obj.get("period_since") is None else int(obj["period_since"]),
            period_until=None if obj.get("period_until") is None else int(obj["period_until"]),
            row_count=int(obj.get("row_count", 0)),
            note=str(obj.get("note", "")),
            created=int(obj["created"]),
            prev_hmac=str(obj.get("prev_hmac", GENESIS_HMAC)),
            hmac=str(obj.get("hmac", "")),
        )


def signoff_path(root: Path) -> Path:
    """Return the JSONL file backing the sign-off chain under ``root``."""
    return Path(root) / _SUBDIR / _SIGNOFF_FILE


def _compute_hmac(key: bytes, prev_hmac: str, body: dict[str, Any]) -> str:
    """HMAC-SHA256 over ``prev_hmac`` concatenated with the canonical body.

    Identical construction to :func:`bernstein.core.identity.grants._compute_hmac`,
    so the sign-off chain shares tamper-evidence semantics with the chains it
    reviews.
    """
    payload = prev_hmac + json.dumps(body, sort_keys=True)
    return _hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


@contextlib.contextmanager
def _append_lock(path: Path) -> Generator[None]:
    """Serialise the tail-read-through-append across threads and processes.

    Without both locks two writers recover the same ``prev_hmac`` and fork the
    chain, which is the failure the delegation ledger already guards against.
    """
    with _SIGNOFF_LOCK:
        if fcntl is None:  # pragma: no cover - Windows path
            yield
            return
        lock_path = path.with_suffix(path.suffix + ".lock")
        fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _tail(path: Path) -> tuple[str, int]:
    """Return ``(prev_hmac, next_record_index)`` for the sign-off chain."""
    if not path.is_file():
        return GENESIS_HMAC, 0
    prev = GENESIS_HMAC
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        prev = json.loads(stripped).get("hmac", prev)
        count += 1
    return prev, count


def record_signoff(
    *,
    root: Path,
    key: bytes,
    reviewer: str,
    review: AccessReview,
    decision: str = "approved",
    note: str = "",
    created: int,
) -> SignoffRecord:
    """Append one sign-off chain event over ``review``'s digest.

    Args:
        root: Audit root; the record lands under ``<root>/access_review/``.
        key: The install audit HMAC key that anchors the chain.
        reviewer: The human who reviewed. Recorded verbatim.
        review: The review whose digest the sign-off attaches to.
        decision: One of :data:`DECISIONS`.
        note: Free-text context the reviewer supplies.
        created: Epoch second of the sign-off. Required rather than defaulted,
            so the caller decides what instant the record claims.

    Returns:
        The freshly appended :class:`SignoffRecord`.
    """
    if decision not in DECISIONS:
        raise AccessReviewError(f"decision must be one of {DECISIONS}, got {decision!r}")
    if not reviewer.strip():
        raise AccessReviewError("a sign-off must name its reviewer")

    path = signoff_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _append_lock(path):
        prev_hmac, index = _tail(path)
        record = SignoffRecord(
            record_index=index,
            kind=SIGNOFF_KIND,
            reviewer=reviewer,
            decision=decision,
            review_digest=review.digest,
            period_since=review.document["period"]["since"],
            period_until=review.document["period"]["until"],
            row_count=int(review.document["row_count"]),
            note=note,
            created=created,
            prev_hmac=prev_hmac,
        )
        body = record.chain_body()
        entry = dict(body)
        entry["hmac"] = _compute_hmac(key, prev_hmac, body)
        with path.open("a", encoding="utf-8", newline="") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
    return SignoffRecord.from_entry(entry)


@dataclass
class SignoffChainResult:
    """Outcome of reconstructing the sign-off chain offline."""

    valid: bool
    records: list[SignoffRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def verify_signoff_chain(*, root: Path, key: bytes) -> SignoffChainResult:
    """Walk the sign-off chain from genesis, recomputing every HMAC.

    A mutated, deleted, or reordered record surfaces as an error naming the
    offending index; verification stops at the first break, so records past it
    are not reported as verified.
    """
    path = signoff_path(root)
    if not path.is_file():
        return SignoffChainResult(valid=False, errors=["no sign-off records"])

    records: list[SignoffRecord] = []
    errors: list[str] = []
    prev_hmac = GENESIS_HMAC
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines()):
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError as exc:
            errors.append(f"record {lineno}: malformed JSON: {exc}")
            break
        index = obj.get("record_index", lineno)
        body = {k: v for k, v in obj.items() if k != "hmac"}
        if body.get("prev_hmac") != prev_hmac:
            errors.append(f"record {index}: broken linkage (prev_hmac does not match preceding record)")
            break
        expected = _compute_hmac(key, prev_hmac, body)
        if not _hmac.compare_digest(expected, str(obj.get("hmac", ""))):
            errors.append(f"record {index}: HMAC mismatch (sign-off tampered or wrong key)")
            break
        try:
            records.append(SignoffRecord.from_entry(obj))
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"record {index}: unreadable sign-off record: {exc}")
            break
        prev_hmac = str(obj["hmac"])

    return SignoffChainResult(valid=not errors and len(records) > 0, records=records, errors=errors)


@dataclass(frozen=True)
class SignoffVerdict:
    """Whether some chain-anchored sign-off covers exactly these bytes."""

    ok: bool
    record: SignoffRecord | None = None
    digest: str = ""
    errors: tuple[str, ...] = ()


def verify_signoff(
    *,
    root: Path,
    key: bytes,
    envelope_bytes: bytes,
    reviewer: str | None = None,
) -> SignoffVerdict:
    """Return the sign-off that covers ``envelope_bytes``, if any verifies.

    The digest is recomputed from the supplied bytes rather than read from the
    envelope, so a document edited after its sign-off matches no record. The
    latest matching sign-off wins when a review was signed off more than once.
    """
    verification = verify_signed_review(envelope_bytes)
    if not verification.ok:
        return SignoffVerdict(ok=False, digest=verification.digest, errors=verification.errors)

    chain = verify_signoff_chain(root=root, key=key)
    if not chain.valid:
        return SignoffVerdict(ok=False, digest=verification.digest, errors=tuple(chain.errors))

    matches = [r for r in chain.records if r.review_digest == verification.digest]
    if reviewer is not None:
        matches = [r for r in matches if r.reviewer == reviewer]
    if not matches:
        return SignoffVerdict(
            ok=False,
            digest=verification.digest,
            errors=("no sign-off records this document's digest",),
        )
    return SignoffVerdict(ok=True, record=matches[-1], digest=verification.digest)


# ---------------------------------------------------------------------------
# Install-anchored defaults
# ---------------------------------------------------------------------------


def _audit_key() -> bytes:
    """Return the install-scoped audit HMAC key (the sign-off chain anchor)."""
    from bernstein.core.security.audit import load_or_create_audit_key

    return load_or_create_audit_key()


def _install_signer() -> _grants.GrantSigner:
    """Return the install manager identity that signs review documents."""
    return _grants.install_grant_signer()


def build_default_review(
    *,
    root: Path | None = None,
    since: int | None = None,
    until: int | None = None,
) -> AccessReview:
    """Derive a review using the install audit key and manager identity."""
    return build_review(root=root or DEFAULT_ROOT, key=_audit_key(), since=since, until=until)


def record_default_signoff(
    *,
    review: AccessReview,
    reviewer: str,
    decision: str = "approved",
    note: str = "",
    root: Path | None = None,
    created: int | None = None,
) -> SignoffRecord:
    """Append a sign-off using the install audit key.

    ``created`` defaults to now: a sign-off is a human act at a real instant,
    unlike the projection it attaches to, which reads no clock at all.
    """
    import time

    return record_signoff(
        root=root or DEFAULT_ROOT,
        key=_audit_key(),
        reviewer=reviewer,
        review=review,
        decision=decision,
        note=note,
        created=int(time.time()) if created is None else created,
    )


def verify_default_signoff(
    *,
    envelope_bytes: bytes,
    root: Path | None = None,
    reviewer: str | None = None,
) -> SignoffVerdict:
    """Verify a sign-off using the install audit key."""
    return verify_signoff(
        root=root or DEFAULT_ROOT,
        key=_audit_key(),
        envelope_bytes=envelope_bytes,
        reviewer=reviewer,
    )
