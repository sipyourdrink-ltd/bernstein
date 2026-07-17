"""Per-goal SLA contract REST routes (#2549).

Exposes the operator-declared SLA contracts, their deterministic error-budget
reports, and the signed violation receipts alongside the fleet SLO surface in
:mod:`bernstein.core.routes.slo`. The receipt-verify endpoint re-runs the same
offline verification the ``bernstein sla verify`` CLI runs, so a dashboard can
prove a breach receipt without trusting orchestrator state.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter()


def _sdd_dir(request: Request) -> Path:
    """Resolve the ``.sdd`` directory from app state, falling back to cwd."""
    state_dir = getattr(request.app.state, "sdd_dir", None)
    if state_dir is not None:
        return Path(state_dir)
    return Path(".sdd")


@router.get("/sla")
def list_contracts(request: Request) -> JSONResponse:
    """Return every registered SLA contract."""
    from bernstein.core.planning.sla_store import SLAStore

    contracts = SLAStore(_sdd_dir(request)).list()
    return JSONResponse({"contracts": [c.to_dict() for c in contracts]})


@router.get("/sla/receipts")
def list_receipts(request: Request) -> JSONResponse:
    """Return the operator projection of every persisted violation receipt."""
    from bernstein.core.orchestration.sla_receipt import load_receipts, project_receipt

    receipts = load_receipts(_sdd_dir(request))
    return JSONResponse({"receipts": [project_receipt(r) for r in receipts]})


@router.get("/sla/receipts/{receipt_id}/verify")
def verify_receipt_endpoint(request: Request, receipt_id: str) -> JSONResponse:
    """Verify a persisted violation receipt offline and return the verdict."""
    from bernstein.core.orchestration.sla_receipt import read_receipt, verify_receipt

    receipt = read_receipt(_sdd_dir(request), receipt_id)
    if receipt is None:
        return JSONResponse({"error": "receipt not found", "receipt_id": receipt_id}, status_code=404)
    result = verify_receipt(receipt)
    return JSONResponse({"receipt_id": receipt_id, "ok": result.ok, "errors": list(result.errors)})


@router.get("/sla/{contract_id}")
def show_contract(request: Request, contract_id: str) -> JSONResponse:
    """Return one SLA contract's full record."""
    from bernstein.core.planning.sla_store import SLAStore

    contract = SLAStore(_sdd_dir(request)).get(contract_id)
    if contract is None:
        return JSONResponse({"error": "contract not found", "contract_id": contract_id}, status_code=404)
    return JSONResponse(contract.to_dict())


@router.get("/sla/{contract_id}/report")
def contract_report(request: Request, contract_id: str) -> JSONResponse:
    """Return the deterministic error-budget report for a contract."""
    from bernstein.core.orchestration.sla_monitor import build_report
    from bernstein.core.planning.sla_store import SLAStore

    sdd = _sdd_dir(request)
    contract = SLAStore(sdd).get(contract_id)
    if contract is None:
        return JSONResponse({"error": "contract not found", "contract_id": contract_id}, status_code=404)
    return JSONResponse(build_report(sdd, contract))
