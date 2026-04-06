"""WEB-009: Grafana dashboard YAML/JSON generator endpoint.

GET /grafana/dashboard — returns dashboard JSON from current metric definitions.
"""

from __future__ import annotations

import json

from fastapi import APIRouter
from fastapi.responses import Response

from bernstein.core.grafana_dashboard import generate_grafana_dashboard

router = APIRouter()


@router.get("/grafana/dashboard")
def grafana_dashboard_endpoint(datasource: str = "Prometheus") -> Response:
    """Generate and return the Grafana dashboard JSON.

    Query params:
        datasource: Prometheus datasource name (default ``Prometheus``).
    """
    dashboard = generate_grafana_dashboard(datasource)
    return Response(
        content=json.dumps(dashboard, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=bernstein-dashboard.json"},
    )
