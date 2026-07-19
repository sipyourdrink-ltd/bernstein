"""Malformed-id handling on the per-goal SLA read routes (#2549).

``SLAStore.get`` refuses a contract id that does not have the derived-id
shape, with a typed error, before the id can address a file. These routes
take that id straight from the request path, so the refusal has to surface
as the route's ordinary "not found" answer rather than a 500.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import create_app

#: Ids that fail the derived-id shape check, so each one exercises the guard.
#: A bare ``..`` is deliberately absent: the client normalises ``/sla/..`` to
#: ``/`` before dispatch, so it never reaches the handler and would assert
#: nothing here. Dot segments are covered at the store, in test_sla_store.py.
MALFORMED_IDS = [
    "not-a-sla-id",
    "sla_ZZZZZZZZZZZZ",
    "sla_deadbeef",
    "%2e%2e",
    "sla_deadbeefdead%0Ainjected",
]


@pytest.fixture()
def app(tmp_path: Path) -> FastAPI:
    created = create_app(jsonl_path=tmp_path / "tasks.jsonl")
    created.state.sdd_dir = tmp_path / ".sdd"
    return created


@pytest.fixture()
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestMalformedContractIdReadsAsNotFound:
    @pytest.mark.anyio()
    @pytest.mark.parametrize("contract_id", MALFORMED_IDS)
    async def test_show_contract_returns_404(self, client: AsyncClient, contract_id: str) -> None:
        resp = await client.get(f"/sla/{contract_id}")
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"] == "contract not found"

    @pytest.mark.anyio()
    @pytest.mark.parametrize("contract_id", MALFORMED_IDS)
    async def test_contract_report_returns_404(self, client: AsyncClient, contract_id: str) -> None:
        resp = await client.get(f"/sla/{contract_id}/report")
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"] == "contract not found"

    @pytest.mark.anyio()
    async def test_a_well_formed_but_absent_id_also_reads_as_not_found(self, client: AsyncClient) -> None:
        """The guard must not be the only thing producing 404, or a later
        change could satisfy these tests while breaking the normal path."""
        resp = await client.get("/sla/sla_aaaaaaaaaaaa")
        assert resp.status_code == 404
        assert resp.json()["error"] == "contract not found"
