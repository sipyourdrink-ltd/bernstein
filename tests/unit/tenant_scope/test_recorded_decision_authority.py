"""The installation's recorded decisions carry no tenant ownership."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from bernstein.core.auth import AuthRole

from bernstein.core.routes import governance as routes
from tests.unit.tenant_scope.conftest import TENANT_A, TENANT_B, _sso_credential

if TYPE_CHECKING:
    from pathlib import Path

    from httpx import AsyncClient

    from tests.unit.tenant_scope.conftest import Fixture

pytestmark = [pytest.mark.anyio, pytest.mark.auth_enabled]


async def test_project_records_require_operator_authority(
    fx: Fixture, client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx.app.state.workdir = tmp_path
    directory = tmp_path / ".sdd" / "lineage" / "shared-run" / "governance_decisions"
    directory.mkdir(parents=True)
    record = {
        "run_id": "shared-run",
        "subject": "private-seat",
        "action": "access",
        "verdict": "deny",
        "inputs_hash": "recorded",
        "timestamp": 1000,
    }
    (directory / "record.json").write_text(json.dumps(record))
    admin = _sso_credential(fx.app, "installation-admin", AuthRole.ADMIN, TENANT_A)
    viewer_b = _sso_credential(fx.app, "viewer-b", AuthRole.VIEWER, TENANT_B)
    denied = [fx.credentials[name] for name in ("sso_viewer", "sso_operator", "agent_unrestricted", "cluster_secret")]
    denied.append(viewer_b)
    query = {"run_id": "shared-run"}
    for prefix in ("", "/api/v1"):
        listing = prefix + "/governance/decisions"
        response = await client.get(listing, params=query, headers=admin.headers)
        assert response.status_code == 200
        (row,) = response.json()["decisions"]
        assert row["subject"] == "private-seat"
        for url in (listing, f"{listing}/{row['decision_id']}"):
            assert (
                await client.get(url, params=query, headers=fx.credentials["legacy_bearer"].headers)
            ).status_code == 200
            with monkeypatch.context() as denied_storage:
                denied_storage.setattr(
                    routes, "_read_recorded_decisions", lambda *_: pytest.fail("unauthorized storage read")
                )
                for credential in denied:
                    response = await client.get(url, params=query, headers=credential.headers)
                    assert response.status_code == 403, credential.name
                    assert "private-seat" not in response.text
