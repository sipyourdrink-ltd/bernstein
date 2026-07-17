"""Mission timeline routes: an outcome-level view projected from the ledger (#2510).

Task-level surfaces (tasks, fleet, approvals, costs, audit) are covered, but a
multi-day mission has no outcome-level view. These endpoints put the mission
projection -- phase lanes, gate receipts, per-phase envelope burn, and the daily
progress digest -- on one web surface while keeping the mission a pure
projection: the server folds the mission's work-ledger chain on every request
and the client renders whatever the fold returns. There is no mission-side state
anywhere, exactly like the review-board routes
(:mod:`bernstein.core.routes.review_board`).

* ``GET /missions`` -- mission ids with a ledger, newest first.
* ``GET /missions/{mission_id}`` -- the mission projection receipt: canonical
  status plus ``mission_status_hash``, ``ledger_head``, ``ledger_verified`` and
  ``evidence_verified`` so two operators can confirm they are looking at the
  same state. When chain verification fails the payload carries
  ``ledger_verified=false`` and ``overall=unverified`` so the screen switches to
  an explicit unverified banner rather than best-effort rendering.
* ``GET /missions/{mission_id}/digest?fire_time=<epoch>`` -- the canonical daily
  progress digest for a fire instant, its ``digest_hash`` / ``receipt_id``, and
  the verbatim chat message the digest projects to. Read-only: it recomputes the
  digest from the ledger and never writes to the chain.
* ``GET /missions/{mission_id}/evidence/{task_id}`` -- the sealed evidence bundle
  a phase gate verified, for the provenance link behind a timeline element.

Bearer auth: the task server's auth middleware covers every read route here
(none is registered as a public path). Every route is read-only; a mission
mutation lives only in the work ledger, never here.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Final

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from bernstein.core.evidence.bundle import read_evidence_bundle
from bernstein.core.orchestration.mission_digest import build_mission_digest, render_digest_message
from bernstein.core.orchestration.missions import (
    LedgerReader,
    list_missions,
    mission_ledger_dir,
    project_mission_from_ledger,
)

logger = logging.getLogger(__name__)

router = APIRouter()

#: Mission ids are directory names under the ledger root; reject anything that
#: could carry a path separator, a NUL, or HTML-active characters before the
#: value touches the filesystem or the page (mirrors the ledger's id alphabet).
_MISSION_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")

#: Task ids referenced by a phase gate; slug-shaped, defense in depth.
_TASK_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")


def _resolve_workdir(request: Request) -> Path:
    """Locate the project root from app state (mirrors the review-board routes)."""
    workdir = getattr(request.app.state, "workdir", None)
    if isinstance(workdir, Path):
        return workdir
    sdd_dir = getattr(request.app.state, "sdd_dir", None)
    if isinstance(sdd_dir, Path) and sdd_dir.name == ".sdd":
        return sdd_dir.parent
    return Path.cwd()


def _validate_mission_id(mission_id: str) -> str:
    if not _MISSION_ID_RE.fullmatch(mission_id):
        raise HTTPException(status_code=400, detail="invalid mission id")
    return mission_id


def _validate_task_id(task_id: str) -> str:
    if not _TASK_ID_RE.fullmatch(task_id):
        raise HTTPException(status_code=400, detail="invalid task id")
    return task_id


def _require_mission(sdd_dir: Path, mission_id: str) -> None:
    if not LedgerReader(mission_ledger_dir(sdd_dir, mission_id)).exists():
        raise HTTPException(status_code=404, detail=f"no mission ledger for: {mission_id}")


@router.get("/missions")
def missions_list(request: Request) -> JSONResponse:
    """List mission ids that have a ledger to project, newest first."""
    sdd_dir = _resolve_workdir(request) / ".sdd"
    return JSONResponse({"missions": list_missions(sdd_dir)})


@router.get("/missions/{mission_id}")
def mission_projection(mission_id: str, request: Request) -> JSONResponse:
    """Serve the mission projection receipt for ``mission_id``.

    The response is a deterministic function of the mission's ledger file: the
    same ledger bytes serve the same ``status`` and ``mission_status_hash`` from
    any server, so two operators cross-check byte for byte.
    ``ledger_verified=false`` (with ``overall=unverified``) marks a chain that no
    longer recomputes -- the timeline still renders, but the screen must show the
    unverified banner instead of trusting the state.
    """
    mission_id = _validate_mission_id(mission_id)
    workdir = _resolve_workdir(request)
    sdd_dir = workdir / ".sdd"
    _require_mission(sdd_dir, mission_id)
    projection = project_mission_from_ledger(sdd_dir=sdd_dir, workdir=workdir, mission_id=mission_id)
    return JSONResponse(projection.to_dict())


@router.get("/missions/{mission_id}/digest")
def mission_digest(
    mission_id: str,
    request: Request,
    fire_time: int = Query(..., description="Integer Unix epoch of the canonical fire instant."),
) -> JSONResponse:
    """Serve the canonical daily progress digest for a fire instant.

    Read-only: the digest is recomputed from the ledger as a pure fold, so the
    endpoint never writes to the chain. The payload carries the ``digest_hash``,
    the ``receipt_id`` (the per-fire delivery idempotency key), and the verbatim
    ``message`` the digest projects to -- the exact bytes a chat delivery posts,
    so a caller can cross-check a posted message against this projection.
    """
    mission_id = _validate_mission_id(mission_id)
    if not isinstance(fire_time, int) or fire_time < 0:
        raise HTTPException(status_code=400, detail="fire_time must be a non-negative integer epoch")
    workdir = _resolve_workdir(request)
    sdd_dir = workdir / ".sdd"
    _require_mission(sdd_dir, mission_id)
    projection = project_mission_from_ledger(sdd_dir=sdd_dir, workdir=workdir, mission_id=mission_id)
    digest = build_mission_digest(projection, fire_time=fire_time)
    payload = digest.to_dict()
    payload["digest_hash"] = digest.digest_hash()
    payload["receipt_id"] = digest.receipt_id()
    payload["message"] = render_digest_message(digest)
    return JSONResponse(payload)


@router.get("/missions/{mission_id}/evidence/{task_id}")
def mission_evidence(mission_id: str, task_id: str, request: Request) -> JSONResponse:
    """Serve the sealed evidence bundle behind a timeline element's provenance link.

    ``bundle_hash`` is recomputed from the canonical binding bytes on every read,
    so the drawer always shows the bundle's current identity -- and a bundle that
    no longer matches the hash a phase receipt bound projects that phase as
    unverified in the mission projection above.
    """
    mission_id = _validate_mission_id(mission_id)
    task_id = _validate_task_id(task_id)
    workdir = _resolve_workdir(request)
    _require_mission(workdir / ".sdd", mission_id)
    bundle = read_evidence_bundle(workdir, task_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail=f"no evidence bundle for task: {task_id}")
    payload = bundle.to_dict()
    payload["bundle_hash"] = bundle.bundle_hash()
    return JSONResponse(payload)
