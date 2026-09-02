"""Produce a portable authority envelope from a run's governance records.

Issue #5055. ``schemas/authority-envelope-v1.json`` defines the artefact and
``verify_cli/bernstein_verify_envelope`` checks one without a Bernstein
install; until now nothing wrote one. This module is the run-scoped producer:
it reads the :class:`~bernstein.core.security.governance.GovernanceDecision`
records persisted under ``lineage_root/<run_id>/`` and renders the subset that
concerns one principal as a signed envelope.

What the envelope asserts, and what it recomputes
-------------------------------------------------
Every hash the producer writes is one the verifier re-derives from material the
envelope itself carries, in the idiom
:func:`~bernstein.core.security.governance._access_inputs_hash` already
establishes: the principal identifier is bound to its key, the grant link's
hash covers its own scope and expiry, and each decision's ``inputs_hash``
covers the decision's policy inputs together with the grant it cites. A
signature says who asserted the envelope; the recomputation says the assertion
follows from its own inputs.

The authority the run actually resolved is the signed
:class:`~bernstein.core.security.governance.RoleBindings`: the acting
principal's IDP groups resolve to a role, and that role's permission set is the
scope of the single grant link the envelope carries. Multi-link delegation
chains are admitted by the schema and rejected here only because no delegation
hop is written yet.

What it refuses to carry
------------------------
The producer emits nothing an auditor would have to take on faith:

* A decision record claiming ``allow`` for an action outside the resolved
  role's permission set does not follow from the recorded authority, so the
  build raises rather than signing it.
* A decision timestamped after the grant expires is likewise refused.
* Records about other subjects, and budget records -- which draw authority from
  the spend policy rather than from the role grant -- are not carried, and
  ``coverage.statement`` names how many of each were left behind.
* A carried decision whose record has no lineage anchor has no evidence, so it
  is listed in ``coverage.uncovered`` with the reason.

Determinism: every field is a pure function of the records on disk and the
caller's arguments. Two builds over the same run produce byte-identical
envelopes, so an envelope can be re-derived and diffed rather than trusted.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from bernstein.core.security.agent_card_signer import (
    canonicalize_jcs,
    ed25519_public_jwk,
    sign_detached_jws_over_canonical,
)
from bernstein.core.security.governance import (
    RoleBindings,
    _access_inputs_hash,
    read_decisions,
    resolve_role,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from bernstein.core.security.governance import GovernanceDecision

#: Wire-format constants. These MUST match ``schemas/authority-envelope-v1.json``
#: and ``verify_cli/bernstein_verify_envelope/verify.py``.
SCHEMA_VERSION = "1.0.0"
ENVELOPE_TYPE = "https://bernstein.run/attestations/authority-envelope/v1"
JWS_TYP = "application/vnd.bernstein.authority-envelope+jws"

#: Policy identifier stamped on every access decision the envelope carries. The
#: policy *version* is the bindings' content hash, so the exact policy bytes
#: that produced the verdict are named.
ACCESS_POLICY_ID = "bernstein.core.security.governance/decide_access"

#: The evidence entry name for a decision record's lineage-spine anchor.
LINEAGE_ANCHOR = "lineage-anchor"

#: The action string ``governance.check_budget_decision`` records. Budget rows
#: are excluded from a role-grant envelope; see the module docstring.
BUDGET_ACTION = "budget"

_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_JWK_MEMBERS = ("kty", "crv", "x")


class AuthorityEnvelopeError(ValueError):
    """Raised when a run's records cannot be rendered as a valid envelope."""


def _sha256_jcs(value: Any) -> str:
    """Content hash over the RFC 8785 canonical bytes of *value*."""
    return hashlib.sha256(canonicalize_jcs(value)).hexdigest()


def _okp_jwk(public_key_pem: bytes, *, kid: str) -> dict[str, str]:
    """Return the bare RFC 8037 OKP JWK the envelope schema admits.

    ``ed25519_public_jwk`` also carries ``alg``/``use``/``kid``; the envelope's
    JWK definition forbids extra members, so only the three key-material ones
    are kept.
    """
    try:
        jwk = ed25519_public_jwk(public_key_pem, kid=kid)
    except (ValueError, TypeError) as exc:
        raise AuthorityEnvelopeError(f"unusable Ed25519 public key: {exc}") from exc
    return {member: jwk[member] for member in _JWK_MEMBERS}


def _public_pem_of(private_key_pem: bytes) -> bytes:
    """Return the SPKI PEM of *private_key_pem*'s public half."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    try:
        private_key = serialization.load_pem_private_key(private_key_pem, password=None)
    except (ValueError, TypeError) as exc:
        raise AuthorityEnvelopeError(f"signing key is not a readable PEM private key: {exc}") from exc
    if not isinstance(private_key, Ed25519PrivateKey):
        raise AuthorityEnvelopeError("the authority envelope is signed with EdDSA; supply an Ed25519 key")
    return private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _require_timestamp(value: str, *, field: str) -> datetime:
    """Parse an RFC 3339 UTC timestamp in the envelope's one accepted shape."""
    if not _TIMESTAMP_PATTERN.match(value):
        raise AuthorityEnvelopeError(f"{field} must be RFC 3339 UTC as YYYY-MM-DDThh:mm:ssZ, got {value!r}")
    return datetime.strptime(value, _TIMESTAMP_FORMAT).replace(tzinfo=UTC)


def _record_timestamp(decision: GovernanceDecision) -> str:
    """Render a decision record's integer timestamp in the envelope's format."""
    try:
        return datetime.fromtimestamp(decision.timestamp, tz=UTC).strftime(_TIMESTAMP_FORMAT)
    except (OSError, OverflowError, ValueError) as exc:
        raise AuthorityEnvelopeError(
            f"decision record for {decision.subject!r} has an unrenderable timestamp "
            f"{decision.timestamp!r}; the envelope records seconds since the epoch"
        ) from exc


def _anchor_digest(decision: GovernanceDecision) -> str:
    """Return the bare sha256 hex of a record's spine anchor, or the empty string."""
    anchor = decision.journal_entry_hash.removeprefix("sha256:")
    return anchor if _SHA256_HEX.match(anchor) else ""


def _grant_link(
    *,
    grant_id: str,
    issuer: str,
    subject: str,
    scope: Sequence[str],
    not_after: str,
) -> dict[str, Any]:
    """Build the root grant link with its chained hash filled in."""
    ordered = sorted(set(scope))
    grant_hash = _sha256_jcs(
        {
            "v": SCHEMA_VERSION,
            "grant_id": grant_id,
            "issuer": issuer,
            "subject": subject,
            "scope": ordered,
            "not_after": not_after,
            "parent_hash": "",
        }
    )
    return {
        "grant_id": grant_id,
        "parent": None,
        "issuer": issuer,
        "subject": subject,
        "scope": ordered,
        "not_after": not_after,
        "grant_hash": grant_hash,
    }


def _decision_entry(
    *,
    index: int,
    decision: GovernanceDecision,
    grant: dict[str, Any],
    role: str,
    bindings_hash: str,
    resource: str,
    run_id: str,
) -> dict[str, Any]:
    """Render one governance record as an envelope decision."""
    inputs: dict[str, Any] = {
        "kind": "access",
        "role": role,
        "idp_role_source": "role_bindings",
        "bindings_hash": bindings_hash,
        "governance_inputs_hash": decision.inputs_hash,
        "recorded_verdict": decision.verdict,
        "run_id": run_id,
    }
    if decision.context:
        inputs["context"] = dict(sorted(decision.context.items()))
    policy = {"id": ACCESS_POLICY_ID, "version": bindings_hash}
    fragment = decision.inputs_hash.removeprefix("sha256:")[:16]
    entry: dict[str, Any] = {
        "decision_id": f"{index:06d}-{fragment}",
        "grant": grant["grant_id"],
        "subject": decision.subject,
        "action": decision.action,
        "resource": resource,
        "verdict": decision.verdict,
        "policy": policy,
        "inputs": inputs,
        "timestamp": _record_timestamp(decision),
    }
    entry["inputs_hash"] = _sha256_jcs(
        {
            "v": SCHEMA_VERSION,
            "subject": entry["subject"],
            "action": entry["action"],
            "resource": entry["resource"],
            "policy": policy,
            "inputs": inputs,
            "grant_hash": grant["grant_hash"],
        }
    )
    return entry


def _check_follows_from_the_grant(entry: dict[str, Any], grant: dict[str, Any], grant_expiry: datetime) -> None:
    """Refuse a decision the recorded authority does not support."""
    if entry["verdict"] not in ("allow", "deny"):
        raise AuthorityEnvelopeError(
            f"decision record for {entry['action']!r} has verdict {entry['verdict']!r}; "
            "the envelope carries allow/deny access decisions only"
        )
    if entry["verdict"] == "allow" and entry["action"] not in grant["scope"]:
        raise AuthorityEnvelopeError(
            f"decision record allows {entry['action']!r}, which is outside the scope of grant "
            f"{grant['grant_id']!r} ({grant['scope']}); signing it would assert authority "
            "the bindings never granted"
        )
    if _require_timestamp(entry["timestamp"], field="decision timestamp") > grant_expiry:
        raise AuthorityEnvelopeError(
            f"decision record taken at {entry['timestamp']} is after grant "
            f"{grant['grant_id']!r} expires at {grant['not_after']}"
        )


def _coverage(
    *,
    decisions: Sequence[dict[str, Any]],
    evidence: Sequence[dict[str, Any]],
    principal_id: str,
    run_id: str,
    other_subject_records: int,
    budget_records: int,
) -> dict[str, Any]:
    """Compute the coverage section from the records, never from an assertion."""
    with_evidence = {entry["decision"] for entry in evidence}
    covered = sorted(with_evidence)
    uncovered = [
        {
            "decision_id": decision["decision_id"],
            "action": decision["action"],
            "reason": (
                "the decision record carries no lineage-spine anchor, so nothing ties this decision to an artefact"
            ),
        }
        for decision in decisions
        if decision["decision_id"] not in with_evidence
    ]
    statement = (
        f"This envelope carries {len(decisions)} access decision(s) taken by {principal_id} "
        f"in run {run_id}, of which {len(covered)} carry evidence. "
        f"{other_subject_records} decision record(s) in the run concern other subjects and are "
        f"not carried. {budget_records} budget record(s) draw authority from the spend policy "
        "rather than from the role grant and are not carried. The envelope proves neither that "
        "the grant was unrevoked at the time of use nor that the run itself was replayed."
    )
    return {"covered": covered, "uncovered": uncovered, "statement": statement}


def build_run_authority_envelope(
    *,
    lineage_root: Path,
    run_id: str,
    principal_id: str,
    principal_public_key_pem: bytes,
    idp_groups: Sequence[str],
    bindings: RoleBindings,
    grant_id: str,
    grant_issuer: str,
    grant_not_after: str,
    signing_key_pem: bytes,
    signing_kid: str,
) -> dict[str, Any]:
    """Render one principal's access decisions in *run_id* as a signed envelope.

    Args:
        lineage_root: Spine root (``.sdd/lineage``) holding the run's records.
        run_id: The run whose persisted decision records are read.
        principal_id: The acting identity the envelope is about. Only records
            whose subject is this principal are carried.
        principal_public_key_pem: SPKI PEM of the principal's Ed25519 public
            key, bound to ``principal_id`` by ``principal.id_binding``.
        idp_groups: The principal's IDP group memberships, resolved against
            *bindings* to the role whose permissions become the grant scope.
        bindings: The signed role bindings the run's decisions projected over.
        grant_id: Identifier for the grant link the envelope records.
        grant_issuer: The identity that issued the role grant.
        grant_not_after: RFC 3339 UTC expiry of the grant, ``YYYY-MM-DDThh:mm:ssZ``.
        signing_key_pem: PKCS#8 PEM of the Ed25519 key that signs the envelope.
            The signer need not be the principal.
        signing_kid: Key identifier stamped into the JWS protected header.

    Returns:
        The complete envelope as a JSON-serialisable dict.

    Raises:
        AuthorityEnvelopeError: When the run holds no decision for the
            principal, when a record does not follow from the recorded
            authority, or when an argument is not in the envelope's format.
    """
    if not principal_id:
        raise AuthorityEnvelopeError("principal_id is required; an envelope names who acted")
    grant_expiry = _require_timestamp(grant_not_after, field="grant_not_after")

    role = resolve_role(tuple(idp_groups), bindings)
    if not role:
        raise AuthorityEnvelopeError(
            f"IDP groups {sorted(idp_groups)} resolve to no role under the presented bindings, "
            "so there is no authority for the envelope to record"
        )
    scope = sorted(set(bindings.role_permissions.get(role, ())))
    grant = _grant_link(
        grant_id=grant_id,
        issuer=grant_issuer,
        subject=principal_id,
        scope=scope,
        not_after=grant_not_after,
    )

    records = read_decisions(lineage_root, run_id)
    mine = [r for r in records if r.subject == principal_id and r.action != BUDGET_ACTION]
    budget_records = sum(1 for r in records if r.action == BUDGET_ACTION)
    other_subject_records = sum(1 for r in records if r.subject != principal_id and r.action != BUDGET_ACTION)
    if not mine:
        raise AuthorityEnvelopeError(
            f"run {run_id!r} holds no access decision for {principal_id!r}; an envelope with no "
            "decision would state nothing"
        )

    resource = f"urn:bernstein:run:{run_id}"
    bindings_hash = bindings.bindings_hash()
    decisions: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    for index, record in enumerate(mine):
        if _access_inputs_hash(role=role, action=record.action, bindings=bindings) != record.inputs_hash:
            raise AuthorityEnvelopeError(
                f"record for {record.subject!r}/{record.action!r} pins inputs_hash computed from a role "
                f"{record.inputs_hash!r}; the caller-supplied IDP groups resolve to {role!r}, which does not "
                "match the role the record was authored under. The envelope cannot attest authority that the "
                "record does not pin"
            )
        entry = _decision_entry(
            index=index,
            decision=record,
            grant=grant,
            role=role,
            bindings_hash=bindings_hash,
            resource=resource,
            run_id=run_id,
        )
        _check_follows_from_the_grant(entry, grant, grant_expiry)
        decisions.append(entry)
        digest = _anchor_digest(record)
        if digest:
            evidence.append(
                {
                    "decision": entry["decision_id"],
                    "name": LINEAGE_ANCHOR,
                    "digest": {"sha256": digest},
                }
            )

    principal_jwk = _okp_jwk(principal_public_key_pem, kid=principal_id)
    principal = {
        "id": principal_id,
        "key": principal_jwk,
        "id_binding": _sha256_jcs({"v": SCHEMA_VERSION, "id": principal_id, "key": principal_jwk}),
    }
    coverage = _coverage(
        decisions=decisions,
        evidence=evidence,
        principal_id=principal_id,
        run_id=run_id,
        other_subject_records=other_subject_records,
        budget_records=budget_records,
    )

    body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "envelope_type": ENVELOPE_TYPE,
        "principal": principal,
        "grants": [grant],
        "decisions": decisions,
        "evidence": evidence,
        "coverage": coverage,
    }
    body["section_digests"] = {
        name: _sha256_jcs(body[name]) for name in ("principal", "grants", "decisions", "evidence", "coverage")
    }

    jws = sign_detached_jws_over_canonical(
        canonicalize_jcs(body),
        signing_key_pem,
        typ=JWS_TYP,
        kid=signing_kid,
    )
    return body | {
        "signature": {
            "alg": "EdDSA",
            "kid": signing_kid,
            "public_key_jwk": _okp_jwk(_public_pem_of(signing_key_pem), kid=signing_kid),
            "jws": jws,
        }
    }
