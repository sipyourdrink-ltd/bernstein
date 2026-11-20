"""Governance evidence routes (#5067, #5070).

``GET /governance/coverage?run_id=<id>`` -- what one run's recorded evidence
can account for: the fraction of its actions tied to a principal, and the
fraction covered by a recorded ``allow`` verdict.

``POST /governance/verify-receipt`` -- what one dropped receipt proves, with
the tier the pass reached attached to it.

``GET /governance/decisions?run_id=<id>`` and
``GET /governance/decisions/<decision_id>?run_id=<id>`` -- recorded decision
fields and explicitly sourced budget projections. These reads do not create
keys, records or directories and do not verify journal anchors. Budget input
consistency delegates to the existing verifier, with no route-owned arithmetic.

The coverage route parses the query string, hands the values to
:func:`bernstein.core.security.governance_coverage.governance_coverage_json`,
and returns those bytes verbatim -- the same rule ``/artifacts/health``
follows, and for the same reason: an operator must be able to pin a number
from the screen and recompute it offline from ``.sdd`` alone, which is only
structurally true while there is one implementation and one serialiser.

The coverage status is always 200 for a well-formed request. Low coverage, an empty
run, and a tampered chain are all successfully computed answers about the
evidence, not failed requests; a run that recorded nothing reports absent
ratios rather than 404, because "this run proves nothing" is the answer the
operator came for.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import stat
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from bernstein.core.lineage.spine import SpineRunIdError, _validate_run_id
from bernstein.core.persistence.anchored_read import ANCHORED_OPEN_SUPPORTED, open_anchored
from bernstein.core.security import governance as recorded_governance
from bernstein.core.security.governance_coverage import governance_coverage_json
from bernstein.core.security.governance_receipt_verdict import receipt_verdict_json
from bernstein.core.security.path_containment import contained_subpath

logger = logging.getLogger(__name__)

router = APIRouter(tags=["governance"])

_JSON_MEDIA_TYPE = "application/json"

_NO_POLICY_VERSION = "this decision names no policy version"
_BUDGET_SCOPE = "Budget input consistency only; journal anchor and signer are not verified by this read."


class BudgetValue(BaseModel):
    """A recorded or projected USD value with an explicit evidence state."""

    value: float | None
    state: Literal["verified", "unavailable", "mismatch"]
    provenance: str
    reason: str


class BudgetEvidence(BaseModel):
    cap_usd: BudgetValue
    prior_spend_usd: BudgetValue
    verification_scope: str = _BUDGET_SCOPE


class RecordedDecision(BaseModel):
    decision_id: str
    run_id: str
    subject: str
    action: str
    verdict: str
    inputs_hash: str
    timestamp: int
    context: dict[str, Any]
    journal_entry_hash: str
    policy_version: str
    budget: BudgetEvidence | None


class RecordedDecisions(BaseModel):
    run_id: str
    decisions: list[RecordedDecision]


def _finite_usd(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    try:
        result = float(value)
    except (OverflowError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _check_descriptor_view(path: Path, fd: int) -> None:
    """Require a readable alias for this exact open file, not a missing path."""
    with path.open("rb", buffering=0) as bridge:
        if not os.path.samestat(os.fstat(fd), os.fstat(bridge.fileno())):
            raise OSError("descriptor view names a different file")


def _checked_evidence_path(root: Path, relative: str) -> Path:
    """Resolve below a canonical root and reject redirection even within it."""
    checked = contained_subpath(root, relative, label="decision evidence")
    if checked != root / relative:
        raise ValueError("decision evidence path contains a symlink")
    return checked


def _budget_evidence(record: recorded_governance.GovernanceDecision, workdir: Path) -> BudgetEvidence:
    cap = _finite_usd(record.context.get("cap_usd"))
    next_cost = _finite_usd(record.context.get("next_cost_usd"))
    dimension = record.context.get("dimension", "agent")
    state: Literal["verified", "unavailable", "mismatch"] = "unavailable"
    reason = "Budget policy context is missing or unsupported."
    prior: float | None = None
    if cap is not None and next_cost is not None and dimension in ("agent", "task", "role", "feature_label"):
        try:
            path = _checked_evidence_path(workdir, ".sdd/cost/ledger.jsonl")
            fd = open_anchored(workdir, *path.relative_to(workdir).parts, flags=os.O_RDONLY, require_regular_file=True)
            # A missing ledger is not evidence of zero spend. Observe its bytes
            # around both existing projections so concurrent appends cannot be
            # reported as a verified decision-time value. Both projections open
            # the same descriptor through /dev/fd, never a re-resolved pathname.
            with os.fdopen(fd, "rb", buffering=0) as stream:
                ledger = Path("/dev/fd") / str(stream.fileno())
                _check_descriptor_view(ledger, stream.fileno())
                before = stream.read()
                stream.seek(0)
                problem = recorded_governance._verify_budget_row(record, ledger)
                stream.seek(0)
                candidate = recorded_governance.seat_spend(ledger, record.subject, dimension=dimension)
                stream.seek(0)
                after = stream.read()
                _check_descriptor_view(ledger, stream.fileno())
            if before != after:
                reason = "Ledger changed while projecting budget inputs."
            elif not math.isfinite(candidate):
                reason = "Ledger projection is not a finite USD value."
            elif problem:
                state = "mismatch"
                reason = problem
            else:
                state = "verified"
                reason = "Existing budget verifier matched the recorded inputs hash and verdict."
                prior = candidate
        except (OSError, ValueError, TypeError, OverflowError, AttributeError, RecursionError):
            reason = "Ledger evidence is unavailable."

    return BudgetEvidence(
        cap_usd=BudgetValue(
            value=cap,
            state=state,
            provenance="recorded context.cap_usd (USD)",
            reason=reason,
        ),
        prior_spend_usd=BudgetValue(
            value=prior,
            state=state,
            provenance=(
                ".sdd/cost/ledger.jsonl via seat_spend, checked by the existing budget verifier "
                "against recorded inputs_hash; no historical ledger snapshot is stored"
            ),
            reason=reason,
        ),
    )


def _read_recorded_decisions(workdir: Path, run_id: str) -> list[recorded_governance.GovernanceDecision]:
    """Parse the same directory snapshot whose files are opened without links."""
    if not ANCHORED_OPEN_SUPPORTED:
        raise HTTPException(status_code=409, detail="anchored decision reads are unavailable on this platform")
    try:
        directory = _checked_evidence_path(workdir, f".sdd/lineage/{run_id}/governance_decisions")
        directory_fd = open_anchored(
            workdir,
            *directory.relative_to(workdir).parts,
            flags=os.O_RDONLY | os.O_DIRECTORY,
        )
    except FileNotFoundError:
        return []
    try:
        records = []
        for name in sorted(os.listdir(directory_fd)):
            if not name.endswith(".json"):
                continue
            record_path = _checked_evidence_path(directory, name)
            fd = os.open(record_path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise OSError("decision is not a regular file")
                try:
                    row = json.loads(stream.read())
                    anchor = row.get("journal_entry_hash")
                    if anchor is None:
                        row["journal_entry_hash"] = ""
                    elif not isinstance(anchor, str):
                        raise HTTPException(status_code=409, detail="decision has an unreadable journal anchor")
                    records.append(recorded_governance.GovernanceDecision.from_dict(row))
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    # Preserve the existing reader's malformed-record behavior.
                    logger.warning("governance: malformed decision record")
        return records
    finally:
        os.close(directory_fd)


def _recorded_decisions(request: Request, run_id: str) -> RecordedDecisions:
    try:
        _validate_run_id(run_id)
    except SpineRunIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", run_id):
        raise HTTPException(
            status_code=400,
            detail="run_id must be 1-200 ASCII letters, digits, underscores, dots, colons or hyphens",
        )
    try:
        workdir = _workdir(request).resolve()
        records = _read_recorded_decisions(workdir, run_id)
        decisions = []
        for record in records:
            if record.run_id != run_id:
                raise HTTPException(status_code=409, detail="decision names a different run")
            # The ID names these exact record bytes, including an absent anchor.
            encoded = json.dumps(record.to_dict(), sort_keys=True, allow_nan=False).encode()
            version = record.context.get("policy_version")
            decisions.append(
                RecordedDecision(
                    decision_id=hashlib.sha256(encoded).hexdigest(),
                    run_id=record.run_id,
                    subject=record.subject,
                    action=record.action,
                    verdict=record.verdict,
                    inputs_hash=record.inputs_hash,
                    timestamp=record.timestamp,
                    context=record.context,
                    journal_entry_hash=record.journal_entry_hash,
                    policy_version=version if isinstance(version, str) and version.strip() else _NO_POLICY_VERSION,
                    budget=_budget_evidence(record, workdir) if record.action == "budget" else None,
                )
            )
    except (OSError, ValueError, TypeError, AttributeError, OverflowError, RecursionError) as exc:
        logger.warning("Cannot read recorded governance decisions: %s", type(exc).__name__)
        raise HTTPException(status_code=409, detail="recorded decisions are unreadable") from None
    return RecordedDecisions(run_id=run_id, decisions=decisions)


def _decision_response(model: RecordedDecisions | RecordedDecision) -> Response:
    """Finish response serialization inside the controlled evidence boundary."""
    try:
        payload = model.model_dump_json()
    except (ValueError, RecursionError):
        raise HTTPException(status_code=409, detail="recorded decisions are unreadable") from None
    return Response(content=payload, media_type=_JSON_MEDIA_TYPE)


@router.get("/governance/decisions", response_model=RecordedDecisions)
def list_recorded_decisions(request: Request, run_id: str = "") -> Response:
    """Read persisted decisions without creating directories, keys or records."""
    return _decision_response(_recorded_decisions(request, run_id))


@router.get("/governance/decisions/{decision_id}", response_model=RecordedDecision)
def get_recorded_decision(decision_id: str, request: Request, run_id: str = "") -> Response:
    """Read one exact recorded projection; this does not verify its anchor."""
    if not re.fullmatch(r"[0-9a-f]{64}", decision_id):
        raise HTTPException(status_code=404, detail="recorded decision not found")
    for decision in _recorded_decisions(request, run_id).decisions:
        if decision.decision_id == decision_id:
            return _decision_response(decision)
    raise HTTPException(status_code=404, detail="recorded decision not found")


def _workdir(request: Request) -> Path:
    """Return the project root the server was started against."""
    workdir: Path = request.app.state.workdir
    return workdir


def _hmac_key() -> bytes:
    from bernstein.core.security.audit import load_or_create_audit_key

    return load_or_create_audit_key()


@router.get("/governance/coverage")
def governance_coverage(request: Request) -> Response:
    """Return the canonical coverage document for ``?run_id=``."""
    run_id = request.query_params.get("run_id", "")
    if not run_id:
        raise HTTPException(status_code=400, detail="run_id query parameter is required")
    try:
        payload = governance_coverage_json(_workdir(request), run_id, hmac_key=_hmac_key())
    except SpineRunIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return Response(content=payload, media_type=_JSON_MEDIA_TYPE)


@router.post("/governance/verify-receipt", name="verify_receipt_endpoint")
async def verify_receipt_endpoint(request: Request) -> Response:
    """Verify the posted receipt file and return the verdict document.

    The request body is the receipt verbatim -- whatever the operator dropped,
    of whatever content type. Nothing under ``.sdd`` and no key material is
    read: the verifier works from the file's own bytes, so this endpoint
    answers about the upload and not about the installation serving it.

    The status is always 200 for a request that carried a body. A tampered
    receipt, a file that is not a receipt, and an empty drop are all verdicts
    about the evidence rather than failed requests, and each is the answer an
    operator dropped the file to get.

    No key can be pinned through this endpoint, so the pass it reports is
    always the integrity-only tier and always carries the caveat saying so.
    A key arriving in the same request as the receipt is not an out-of-band
    anchor -- it is the same channel, and treating it as one would report
    provenance on the strength of the upload rather than of any evidence.
    """
    payload = receipt_verdict_json(await request.body())
    return Response(content=payload, media_type=_JSON_MEDIA_TYPE)
