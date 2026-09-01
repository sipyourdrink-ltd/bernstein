"""Governance coverage route (#5067).

``GET /governance/coverage?run_id=<id>`` -- what one run's recorded evidence
can account for: the fraction of its actions tied to a principal, and the
fraction covered by a recorded ``allow`` verdict.

The route owns no arithmetic. It parses the query string, hands the values to
:func:`bernstein.core.security.governance_coverage.governance_coverage_json`,
and returns those bytes verbatim -- the same rule ``/artifacts/health``
follows, and for the same reason: an operator must be able to pin a number
from the screen and recompute it offline from ``.sdd`` alone, which is only
structurally true while there is one implementation and one serialiser.

The status is always 200 for a well-formed request. Low coverage, an empty
run, and a tampered chain are all successfully computed answers about the
evidence, not failed requests; a run that recorded nothing reports absent
ratios rather than 404, because "this run proves nothing" is the answer the
operator came for.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from bernstein.core.lineage.spine import SpineRunIdError
from bernstein.core.security.governance_coverage import governance_coverage_json

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

router = APIRouter(tags=["governance"])

_JSON_MEDIA_TYPE = "application/json"


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
