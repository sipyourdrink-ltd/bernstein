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
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from bernstein.core.cost.spend_ledger import LedgerEntry
from bernstein.core.lineage.spine import SpineRunIdError, _validate_run_id
from bernstein.core.persistence.anchored_read import ANCHORED_OPEN_SUPPORTED, open_anchored
from bernstein.core.routes._operator import require_operator
from bernstein.core.security import governance as recorded_governance
from bernstein.core.security.governance_coverage import governance_coverage_json
from bernstein.core.security.governance_receipt_verdict import receipt_verdict_json
from bernstein.core.security.path_containment import contained_subpath

if TYPE_CHECKING:
    from pathlib import Path

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


def _checked_evidence_path(root: Path, relative: str) -> Path:
    """Resolve below a canonical root and reject redirection even within it."""
    checked = contained_subpath(root, relative, label="decision evidence")
    if checked != root / relative:
        raise ValueError("decision evidence path contains a symlink")
    return checked


def _budget_key(record: recorded_governance.GovernanceDecision) -> tuple[str, str] | None:
    dimension = record.context.get("dimension", "agent")
    if dimension not in ("agent", "task", "role", "feature_label"):
        return None
    if _finite_usd(record.context.get("cap_usd")) is None or _finite_usd(record.context.get("next_cost_usd")) is None:
        return None
    return dimension, record.subject


def _budget_evidence(
    record: recorded_governance.GovernanceDecision, projected: float | None, reason: str
) -> BudgetEvidence:
    state: Literal["verified", "unavailable", "mismatch"] = "unavailable"
    prior = None
    if projected is not None and not math.isfinite(projected):
        reason = "Ledger projection is not a finite USD value."
    elif projected is not None:
        prior, problem = recorded_governance.verify_budget_projection(record, projected)
        state = "mismatch" if problem else "verified"
        reason = problem or "The projected inputs hash and verdict match the record."
    return BudgetEvidence(
        cap_usd=BudgetValue(
            value=_finite_usd(record.context.get("cap_usd")),
            state=state,
            provenance="recorded context.cap_usd (USD)",
            reason=reason,
        ),
        prior_spend_usd=BudgetValue(
            value=prior,
            state=state,
            reason=reason,
            provenance=(
                ".sdd/cost/ledger.jsonl projected for the recorded subject and dimension. "
                "The inputs hash binds USD to six decimal places. No historical ledger snapshot is stored."
            ),
        ),
    )


def _ledger_entries(snapshot: bytes) -> list[LedgerEntry]:
    """Reject incomplete evidence before a partial ledger can substantiate a spend."""
    entries = []
    for line in snapshot.splitlines():
        if line.strip():
            row = json.loads(line)
            if not isinstance(row, dict) or _finite_usd(row.get("cost_usd")) is None:
                raise ValueError("unreadable spend entry")
            entries.append(LedgerEntry.from_dict(row))
    return entries


def _project_budgets(workdir: Path, records: list[recorded_governance.GovernanceDecision]) -> list[BudgetEvidence]:
    keys = [_budget_key(record) for record in records]
    subjects = {key for key in keys if key is not None}
    reason = "Budget policy context is missing or unsupported."
    if subjects:
        try:
            path = _checked_evidence_path(workdir, ".sdd/cost/ledger.jsonl")
            fd = open_anchored(workdir, *path.relative_to(workdir).parts, flags=os.O_RDONLY, require_regular_file=True)
            with os.fdopen(fd, "rb", buffering=0) as stream:
                snapshot = stream.read()
                totals = recorded_governance.project_seat_spend(_ledger_entries(snapshot), subjects)
                evidence = [
                    _budget_evidence(record, totals[key] if key is not None else None, reason)
                    for record, key in zip(records, keys, strict=True)
                ]
                stream.seek(0)
                if stream.read() == snapshot:
                    return evidence
                reason = "Ledger changed while projecting budget inputs."
        except (OSError, ValueError, TypeError, OverflowError, AttributeError, RecursionError):
            reason = "Ledger evidence is unavailable."
    return [_budget_evidence(record, None, reason) for record in records]


def _read_decision(row: Any) -> recorded_governance.GovernanceDecision:
    """Check the persisted types before the legacy deserializer normalizes them."""
    if not isinstance(row, dict) or not all(
        isinstance(row.get(key), str) for key in ("run_id", "subject", "action", "verdict", "inputs_hash")
    ):
        raise ValueError("unreadable decision fields")
    context = row.get("context", {})
    if type(row.get("timestamp")) is not int or not isinstance(context, dict):
        raise ValueError("unreadable decision inputs")
    version = context.get("policy_version")
    if version is not None and not isinstance(version, str):
        raise ValueError("unreadable policy version")
    anchor = row.get("journal_entry_hash")
    if anchor is None:
        row["journal_entry_hash"] = ""
    elif not isinstance(anchor, str):
        raise ValueError("unreadable journal anchor")
    return recorded_governance.GovernanceDecision.from_dict(row)


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
                except json.JSONDecodeError:
                    # Historically, an unfinished JSON document is skipped.
                    logger.warning("governance: malformed decision record")
                    continue
                records.append(_read_decision(row))
        return records
    finally:
        os.close(directory_fd)


def _recorded_decisions(request: Request, decision_id: str | None = None) -> RecordedDecisions:
    run_id = request.query_params.get("run_id", "")
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
        selected = []
        for record in records:
            if record.run_id != run_id:
                raise HTTPException(status_code=409, detail="decision names a different run")
            # The ID names these exact record bytes, including an absent anchor.
            encoded = json.dumps(record.to_dict(), sort_keys=True, allow_nan=False).encode()
            digest = hashlib.sha256(encoded).hexdigest()
            if decision_id is None or (digest == decision_id and not selected):
                selected.append((digest, record))
        budgets = iter(_project_budgets(workdir, [record for _, record in selected if record.action == "budget"]))
        decisions = []
        for digest, record in selected:
            version = record.context.get("policy_version")
            decisions.append(
                RecordedDecision(
                    decision_id=digest,
                    run_id=record.run_id,
                    subject=record.subject,
                    action=record.action,
                    verdict=record.verdict,
                    inputs_hash=record.inputs_hash,
                    timestamp=record.timestamp,
                    context=record.context,
                    journal_entry_hash=record.journal_entry_hash,
                    policy_version=version if isinstance(version, str) and version.strip() else _NO_POLICY_VERSION,
                    budget=next(budgets) if record.action == "budget" else None,
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


_RUN_QUERY = {
    "parameters": [
        {
            "name": "run_id",
            "in": "query",
            "required": True,
            "description": "Run whose recorded decisions to read.",
            "schema": {"type": "string"},
        }
    ]
}
_READ_ERRORS = {
    400: {"description": "Missing or invalid run ID"},
    401: {"description": "Authentication required"},
    403: {"description": "Installation operator authority required"},
    409: {"description": "Unreadable evidence or descriptor-relative reads unavailable"},
}


@router.get(
    "/governance/decisions",
    response_model=RecordedDecisions,
    dependencies=[Depends(require_operator)],
    responses=_READ_ERRORS,
    openapi_extra=_RUN_QUERY,
)
def list_recorded_decisions(request: Request) -> Response:
    """Read persisted decisions without creating directories, keys or records."""
    return _decision_response(_recorded_decisions(request))


@router.get(
    "/governance/decisions/{decision_id}",
    response_model=RecordedDecision,
    dependencies=[Depends(require_operator)],
    responses={**_READ_ERRORS, 404: {"description": "Invalid digest or unknown decision"}},
    openapi_extra=_RUN_QUERY,
)
def get_recorded_decision(decision_id: str, request: Request) -> Response:
    """Read one exact recorded projection; this does not verify its anchor."""
    if not re.fullmatch(r"[0-9a-f]{64}", decision_id):
        raise HTTPException(status_code=404, detail="recorded decision not found")
    matches = _recorded_decisions(request, decision_id).decisions
    if not matches:
        raise HTTPException(status_code=404, detail="recorded decision not found")
    return _decision_response(matches[0])


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
