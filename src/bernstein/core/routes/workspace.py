"""Workspace API routes."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from bernstein.core.seed import SeedError, parse_seed

if TYPE_CHECKING:
    from bernstein.core.workspace import Workspace

router = APIRouter(tags=["workspace"])


class WorkspaceRepoResponse(BaseModel):
    """Workspace repository status entry."""

    name: str
    path: str
    branch: str
    clean: bool
    ahead: int
    behind: int


class WorkspaceResponse(BaseModel):
    """Workspace repository status payload."""

    repos: list[WorkspaceRepoResponse]


class MergeOrderResponse(BaseModel):
    """Topological repository merge order."""

    repos: list[str]


def _load_workspace_from_request(request: Request) -> Workspace | None:
    workdir = Path(getattr(request.app.state, "workdir", Path.cwd()))
    seed_path = workdir / "bernstein.yaml"
    if not seed_path.exists():
        return None
    try:
        seed = parse_seed(seed_path)
    except SeedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return seed.workspace


@router.get("/workspace", response_model=WorkspaceResponse, responses={400: {"description": "Invalid seed file"}})
def workspace_status(request: Request) -> WorkspaceResponse:
    """Return repository status for the configured workspace."""
    workspace = _load_workspace_from_request(request)
    if workspace is None:
        return WorkspaceResponse(repos=[])

    statuses = workspace.status()
    repos = [
        WorkspaceRepoResponse(
            name=repo.name,
            path=str(workspace.resolve_repo(repo.name)),
            branch=(status.branch if (status := statuses.get(repo.name)) is not None else "unknown"),
            clean=status.clean if status is not None else False,
            ahead=status.ahead if status is not None else 0,
            behind=status.behind if status is not None else 0,
        )
        for repo in workspace.repos
    ]
    return WorkspaceResponse(repos=repos)


@router.post(
    "/workspace/merge-order",
    response_model=MergeOrderResponse,
    responses={400: {"description": "Invalid seed file"}, 404: {"description": "No workspace configured"}},
)
def workspace_merge_order(request: Request) -> MergeOrderResponse:
    """Return the repo merge order derived from current cross-repo task dependencies."""
    workspace = _load_workspace_from_request(request)
    if workspace is None:
        raise HTTPException(status_code=404, detail="No workspace configured")
    store = request.app.state.store
    repos = workspace.merge_order(store.list_tasks())
    return MergeOrderResponse(repos=repos)
