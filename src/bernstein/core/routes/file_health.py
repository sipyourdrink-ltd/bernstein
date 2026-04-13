"""Per-file code health score API routes.

GET /quality/file-health        — list all tracked files, sorted worst-first
GET /quality/file-health/flagged — files flagged for human review
GET /quality/file-health/{path}  — history for a single file
"""

from __future__ import annotations

import contextlib
import json
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from bernstein.core.file_health import FileHealthTracker

if TYPE_CHECKING:
    from pathlib import Path

router = APIRouter()


def _get_tracker(request: Request) -> FileHealthTracker:
    sdd_dir: Path = request.app.state.sdd_dir
    return FileHealthTracker(sdd_dir=sdd_dir)


@router.get("/quality/file-health")
def list_file_health(request: Request) -> JSONResponse:
    """Return per-file code health scores, worst files first.

    Query parameters:
    - ``limit``: max results (default 50, max 500).
    - ``min_score``: only return files at or below this score.
    - ``grade``: filter by grade (A/B/C/D/F).

    Returns a JSON object with ``files`` list and summary statistics.
    """
    params = dict(request.query_params)
    try:
        limit = min(int(params.get("limit", 50)), 500)
    except (ValueError, TypeError):
        limit = 50

    min_score_raw = params.get("min_score")
    min_score: int | None = None
    if min_score_raw is not None:
        with contextlib.suppress(ValueError, TypeError):
            min_score = int(min_score_raw)

    grade_filter = params.get("grade", "").upper() or None

    tracker = _get_tracker(request)
    scores = tracker.get_all()

    # Apply filters
    if min_score is not None:
        scores = [s for s in scores if s.total <= min_score]
    if grade_filter:
        scores = [s for s in scores if s.grade == grade_filter]

    scores = scores[:limit]

    # Summary stats
    total_files = len(scores)
    flagged_count = sum(1 for s in scores if s.flagged)
    avg_score = round(sum(s.total for s in scores) / total_files, 1) if total_files else 0.0

    return JSONResponse(
        {
            "files": [s.to_dict() for s in scores],
            "summary": {
                "total_tracked": total_files,
                "flagged_for_review": flagged_count,
                "avg_score": avg_score,
            },
            "generated_at": time.time(),
        }
    )


@router.get("/quality/file-health/flagged")
def list_flagged_files(request: Request) -> JSONResponse:
    """Return files currently flagged for human review due to health degradation.

    A file is flagged when:
    - A task dropped its health score by ≥10 points, OR
    - Its total health score is below 60 (grade D or F).

    Returns ``files`` list with detailed health scores and degradation context.
    """
    tracker = _get_tracker(request)
    flagged = tracker.get_flagged()

    return JSONResponse(
        {
            "files": [s.to_dict() for s in flagged],
            "count": len(flagged),
            "generated_at": time.time(),
        }
    )


@router.get("/quality/file-health/{file_path:path}", responses={404: {"description": "File not tracked yet"}})
def get_file_health(file_path: str, request: Request) -> JSONResponse:
    """Return the current health score for a single file.

    Args:
        file_path: File path relative to repository root (URL-encoded).

    Returns 404 if the file has never been tracked.
    """
    tracker = _get_tracker(request)
    score = tracker.get(file_path)
    if score is None:
        raise HTTPException(status_code=404, detail=f"File '{file_path}' not tracked yet")

    # Also return the recent touch history for this file
    touches = _read_touch_history(request, file_path)

    return JSONResponse(
        {
            "score": score.to_dict(),
            "touch_history": touches,
            "generated_at": time.time(),
        }
    )


def _read_touch_history(request: Request, path: str, limit: int = 20) -> list[dict[str, Any]]:
    """Read recent touch events for a specific file path.

    Args:
        request: FastAPI request (for sdd_dir access).
        path: File path to filter by.
        limit: Max number of records to return.

    Returns:
        List of touch event dicts, most recent first.
    """
    sdd_dir: Path = request.app.state.sdd_dir
    touch_path = sdd_dir / "metrics" / "file_health_touches.jsonl"
    if not touch_path.exists():
        return []

    records: list[dict[str, Any]] = []
    try:
        for line in touch_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record: dict[str, Any] = json.loads(line)
                if str(record.get("path", "")) == path:
                    records.append(record)
            except json.JSONDecodeError:
                continue
    except OSError:
        return []

    # Return most recent first
    return list(reversed(records[-limit:]))
