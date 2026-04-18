"""Knowledge-graph API routes."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from bernstein.core.knowledge.knowledge_graph import query_impact

router = APIRouter(tags=["graph"])


class ImpactResponse(BaseModel):
    """Response body for ``GET /graph/impact``."""

    file_query: str
    matched_files: list[str]
    impacted_files: list[str]
    built_at: str


@router.get("/graph/impact")
def graph_impact(
    request: Request,
    file: Annotated[str, Query(..., min_length=1)],
) -> ImpactResponse:
    """Return downstream files impacted by changing the given file."""
    workdir = getattr(request.app.state, "workdir", Path.cwd())
    result = query_impact(Path(workdir), file)
    return ImpactResponse(
        file_query=result.file_query,
        matched_files=result.matched_files,
        impacted_files=result.impacted_files,
        built_at=result.built_at,
    )
