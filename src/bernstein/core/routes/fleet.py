"""Fleet aggregator stub for the operator UI.

The Bernstein fleet dashboard (``bernstein fleet --web``) is a separate
FastAPI application served on its own port - see
:mod:`bernstein.core.fleet.web`. The operator UI ("/ui") talks to the
*single-project* task server, which historically had no fleet endpoints.

This router adds the minimal fleet surface the SPA needs in order to ship
the Fleet-mode toggle without depending on the side-car app being
running. It is intentionally a stub:

    * When the host orchestrator process is running a fleet aggregator
      (``request.app.state.fleet_aggregator``), the stub delegates to it
      and returns real ``ProjectSnapshot`` rows.
    * Otherwise it returns an empty list with ``stub=True`` and a hint
      pointing operators at ``bernstein fleet --web`` so the frontend
      can wire itself end-to-end without a populated fleet.

The endpoints intentionally mirror :mod:`bernstein.core.fleet.web` shape
so a future merge can drop the stub for the real aggregator without any
client-side change.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Query, Request

if TYPE_CHECKING:
    from bernstein.core.fleet.aggregator import FleetAggregator

router = APIRouter()


def _aggregator(request: Request) -> FleetAggregator | None:
    """Return the live fleet aggregator if the host process has one attached.

    The single-project task server owns one only when it was started with
    ``BERNSTEIN_FLEET_CONFIG`` set (see ``server_app._start_fleet_aggregator``);
    embedding deployments are free to attach one via
    ``app.state.fleet_aggregator``.
    """
    return getattr(request.app.state, "fleet_aggregator", None)  # type: ignore[no-any-return]


@router.get("/fleet/projects")
def fleet_projects(request: Request) -> dict[str, Any]:
    """Return aggregated per-project snapshots for the fleet overview.

    Response shape mirrors :func:`bernstein.core.fleet.web.api_projects`:

    .. code-block:: json

        {
          "projects": [ProjectSnapshot, ...],
          "errors": [],
          "stub": true|false,
          "hint": "Run `bernstein fleet --web` for the real aggregator."
        }

    ``stub: true`` means the operator UI is talking to a single-project
    server that has no fleet aggregator wired in; the ``projects`` list
    is empty in that case so the SPA can render the empty-state.
    """
    aggregator = _aggregator(request)
    if aggregator is None:
        return {
            "projects": [],
            "errors": [],
            "stub": True,
            "hint": (
                "No fleet aggregator is attached to this task server. "
                "Start it with BERNSTEIN_FLEET_CONFIG pointing at a "
                "projects.toml, or run `bernstein fleet --web` for the "
                "standalone multi-project supervisor view."
            ),
            "ts": time.time(),
        }
    return {
        "projects": [s.to_dict() for s in aggregator.snapshots()],
        "errors": [],
        "stub": False,
        "ts": time.time(),
    }


@router.get("/fleet/search")
def fleet_search(
    request: Request,
    q: Annotated[str, Query(description="Cross-project search query")] = "",
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> dict[str, Any]:
    """Cross-project search stub for the topbar search bar.

    Accepts a free-text query plus the ``agent:/status:/across:`` operator
    syntax used by the frontend search component; the stub does not yet
    execute the search and instead returns the parsed filters so the SPA
    can demonstrate the round-trip while the backend implementation is
    being built.

    Returns:
        ``{"query": str, "filters": {...}, "matches": [], "stub": bool}``.
    """
    filters: dict[str, str] = {}
    free_text_parts: list[str] = []
    for token in q.split():
        if ":" in token:
            key, _, value = token.partition(":")
            key = key.strip().lower()
            value = value.strip()
            if key and value:
                filters[key] = value
                continue
        free_text_parts.append(token)
    free_text = " ".join(free_text_parts).strip()
    aggregator = _aggregator(request)
    return {
        "query": q,
        "free_text": free_text,
        "filters": filters,
        "matches": [],
        "limit": limit,
        "stub": aggregator is None,
        "ts": time.time(),
    }
