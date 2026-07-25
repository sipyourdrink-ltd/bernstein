"""Artifact routes: the health verdict and attribution log, by URI (#2559).

* ``GET /artifacts`` - every artifact key the local spines carry.
* ``GET /artifacts/health`` - the rolled-up verdict for one key.
* ``GET /artifacts/log`` - who produced the current tip of one key.

The health route deliberately owns **no** projection logic. It parses the query
string, hands the parsed values to
:func:`bernstein.core.lineage.artifact_health.artifact_health_json`, and returns
those bytes verbatim. ``bernstein artifact health --json`` calls the same
function with the same arguments, so the two surfaces do not merely agree --
they cannot differ, because there is only one implementation and only one
serialiser. An operator can pin a verdict from the dashboard and recompute it
offline from ``.sdd`` alone.

``at`` is an explicit query parameter for the same reason the CLI has ``--at``:
a verdict that read the wall clock could never be reproducible. Omitting it
means "now", which is fine for a dashboard poll and useless for a comparison,
so any comparison pins it.

The key travels as a query parameter, not a path segment: an artifact URI
carries ``:`` and ``/``, and threading that through a path would invite exactly
the double-decoding ambiguity the canonical-key rule exists to prevent.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from bernstein.core.lineage.artifact_health import (
    artifact_attempts,
    artifact_log,
    artifact_log_json,
    list_artifact_keys,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

router = APIRouter()

#: Media type for the pinned verdict bytes. Returned as a raw ``Response`` so
#: FastAPI's own JSON encoder cannot re-order keys and break byte-identity with
#: the CLI.
_JSON_MEDIA_TYPE = "application/json"

_MAX_LOG_LIMIT = 500


def _workdir(request: Request) -> Path:
    """Return the project root the server was started against."""
    workdir: Path = request.app.state.workdir
    return workdir


def _hmac_key() -> bytes:
    from bernstein.core.security.audit import load_or_create_audit_key

    return load_or_create_audit_key()


def _int_param(request: Request, name: str) -> int | None:
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{name} must be an integer") from None


def _required_uri(request: Request) -> str:
    uri = request.query_params.get("uri", "")
    if not uri:
        raise HTTPException(status_code=400, detail="uri query parameter is required")
    return uri


@router.get("/artifacts")
def list_artifacts(request: Request) -> JSONResponse:
    """Return every artifact key the local lineage spines carry."""
    counts = list_artifact_keys(_workdir(request))
    return JSONResponse({"artifacts": [{"uri": uri, "productions": n} for uri, n in sorted(counts.items())]})


@router.get("/artifacts/health")
def artifact_health(request: Request) -> Response:
    """Return the canonical health verdict for ``?uri=``.

    Query parameters:

    * ``uri`` (required) - the artifact key.
    * ``at`` - evaluation instant; defaults to the wall clock. Pin it to
      reproduce a verdict byte-for-byte against the CLI.
    * ``cadence_seconds`` - declared refresh cadence; omitted means the cadence
      leg reports ``not_applicable``.

    The body is the exact string the CLI prints for the same state and instant,
    byte for byte. The status is always 200: the verdict is the payload, and a
    red artifact is a successfully computed answer, not a failed request.
    """
    from bernstein.core.lineage.artifact_health import artifact_health_json

    uri = _required_uri(request)
    at = _int_param(request, "at")
    cadence = _int_param(request, "cadence_seconds")

    payload = artifact_health_json(
        _workdir(request),
        uri,
        hmac_key=_hmac_key(),
        at=at if at is not None else int(time.time()),
        cadence_seconds=cadence,
    )
    return Response(content=payload, media_type=_JSON_MEDIA_TYPE)


@router.get("/artifacts/log")
def artifact_log_route(request: Request) -> Response:
    """Return productions of ``?uri=``, newest first (the attribution log).

    Recorded attempts -- tasks that declared this artifact and did not deliver it
    -- travel in the same document under ``attempts`` (issue #2559), so a
    consumer cannot see the productions without also seeing what tried and
    failed. Byte-identical to what the CLI prints for the same state.
    """
    uri = _required_uri(request)
    limit = _int_param(request, "limit") or 0
    limit = min(max(limit, 0), _MAX_LOG_LIMIT)
    workdir = _workdir(request)
    key = _hmac_key()
    records = artifact_log(workdir, uri, hmac_key=key, limit=limit)
    attempts = artifact_attempts(workdir, uri, hmac_key=key, limit=limit)
    return Response(
        content=artifact_log_json(records, uri=uri, attempts=attempts),
        media_type=_JSON_MEDIA_TYPE,
    )
