"""Worker-join auth coherence across the two cluster auth layers.

Regression coverage for the cluster worker-join credential story:

* A cluster-enabled server exposes ``/cluster/*`` behind two independent
  auth layers - the outer :class:`SSOAuthMiddleware` and the inner
  :class:`ClusterAuthenticator`. A single worker credential must satisfy
  both, otherwise no worker can register (issue #2805).
* The worker must fail fast on an auth rejection instead of retrying a
  doomed request every 5 s forever (issues #2805 / #2802).

The tests intentionally exercise the real ``create_app`` wiring so the two
layers are verified together, not in isolation.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bernstein.core.cluster_auth import (
    SCOPE_NODE_HEARTBEAT,
    SCOPE_NODE_REGISTER,
    ClusterAuthConfig,
    ClusterAuthenticator,
    ClusterAuthError,
)
from bernstein.core.models import ClusterConfig
from fastapi.testclient import TestClient

from bernstein.core.server import create_app

_SECRET = "cluster-shared-secret-value"  # NOSONAR - test fixture, not a real credential

_NODE_PAYLOAD = {
    "name": "worker-1",
    "url": "",
    "capacity": {
        "max_agents": 4,
        "available_slots": 4,
        "active_agents": 0,
        "gpu_available": False,
        "supported_models": ["sonnet", "opus", "haiku"],
    },
    "labels": {},
    "cell_ids": [],
}


# ---------------------------------------------------------------------------
# Inner layer: ClusterAuthenticator accepts the raw shared secret
# ---------------------------------------------------------------------------


class TestSharedSecretAcceptance:
    """The inner authenticator must accept the raw cluster secret."""

    def test_raw_secret_is_accepted(self) -> None:
        auth = ClusterAuthenticator(ClusterAuthConfig(secret=_SECRET))
        payload = auth.verify_request(f"Bearer {_SECRET}", SCOPE_NODE_REGISTER)
        assert SCOPE_NODE_REGISTER in payload.scopes
        assert SCOPE_NODE_HEARTBEAT in payload.scopes

    def test_raw_secret_covers_heartbeat_scope(self) -> None:
        auth = ClusterAuthenticator(ClusterAuthConfig(secret=_SECRET))
        payload = auth.verify_request(f"Bearer {_SECRET}", SCOPE_NODE_HEARTBEAT)
        assert SCOPE_NODE_HEARTBEAT in payload.scopes

    def test_wrong_raw_secret_is_rejected(self) -> None:
        auth = ClusterAuthenticator(ClusterAuthConfig(secret=_SECRET))
        with pytest.raises(ClusterAuthError):
            auth.verify_request("Bearer not-the-secret", SCOPE_NODE_REGISTER)

    def test_extra_shared_secret_is_accepted(self) -> None:
        # The operator's legacy bearer token, distinct from the cluster
        # secret, is accepted so a split config still has one worker token.
        auth = ClusterAuthenticator(ClusterAuthConfig(secret=_SECRET, shared_secrets=("operator-legacy-token",)))
        payload = auth.verify_request("Bearer operator-legacy-token", SCOPE_NODE_REGISTER)
        assert SCOPE_NODE_REGISTER in payload.scopes

    def test_issued_jwt_still_accepted(self) -> None:
        # The pre-existing JWT issuance path must keep working.
        auth = ClusterAuthenticator(ClusterAuthConfig(secret=_SECRET))
        token = auth.issue_node_token("node-1")
        payload = auth.verify_request(f"Bearer {token}", SCOPE_NODE_REGISTER)
        assert payload.user_id == "node-1"


# ---------------------------------------------------------------------------
# End-to-end: both auth layers accept one worker credential
# ---------------------------------------------------------------------------


@pytest.mark.auth_enabled
class TestWorkerJoinEndToEnd:
    """A single worker credential must pass outer middleware + cluster route."""

    def _client(self, tmp_path: Path, *, cluster_secret: str, api_token: str) -> TestClient:
        app = create_app(
            jsonl_path=tmp_path / "tasks.jsonl",
            auth_token=api_token,
            cluster_config=ClusterConfig(enabled=True, auth_token=cluster_secret),
        )
        return TestClient(app)

    def test_single_secret_join_succeeds(self, tmp_path: Path) -> None:
        """Common config: cluster secret == API token. Worker uses that token."""
        client = self._client(tmp_path, cluster_secret=_SECRET, api_token=_SECRET)
        headers = {"Authorization": f"Bearer {_SECRET}"}

        resp = client.post("/cluster/nodes", json=_NODE_PAYLOAD, headers=headers)
        assert resp.status_code == 201, resp.text
        node_id = resp.json()["id"]

        hb = client.post(
            f"/cluster/nodes/{node_id}/heartbeat",
            json={"capacity": None},
            headers=headers,
        )
        assert hb.status_code == 200, hb.text

        status = client.get("/cluster/status", headers=headers)
        assert status.status_code == 200, status.text
        assert status.json()["total_nodes"] >= 1

    def test_join_without_token_is_rejected(self, tmp_path: Path) -> None:
        client = self._client(tmp_path, cluster_secret=_SECRET, api_token=_SECRET)
        resp = client.post("/cluster/nodes", json=_NODE_PAYLOAD)
        assert resp.status_code == 401

    def test_join_with_wrong_token_is_rejected(self, tmp_path: Path) -> None:
        client = self._client(tmp_path, cluster_secret=_SECRET, api_token=_SECRET)
        resp = client.post(
            "/cluster/nodes",
            json=_NODE_PAYLOAD,
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401

    def test_split_config_operator_token_joins(self, tmp_path: Path) -> None:
        """Split config: distinct cluster secret + API token.

        The operator's API token is a universal worker credential - it passes
        the outer middleware (legacy) and the cluster route layer (extra
        shared secret).
        """
        client = self._client(tmp_path, cluster_secret="distinct-cluster-secret", api_token=_SECRET)
        headers = {"Authorization": f"Bearer {_SECRET}"}
        resp = client.post("/cluster/nodes", json=_NODE_PAYLOAD, headers=headers)
        assert resp.status_code == 201, resp.text

    def test_split_config_cluster_secret_joins_and_pulls(self, tmp_path: Path) -> None:
        """Split config: the CLUSTER SECRET is also a universal worker token.

        It clears the outer middleware and the inner cluster route, and it
        authenticates task pull (a non-cluster path) - which proves the two
        layers no longer accept disjoint credentials (#2805).
        """
        cluster_secret = "cluster-only-secret"  # NOSONAR - test fixture
        client = self._client(tmp_path, cluster_secret=cluster_secret, api_token=_SECRET)
        headers = {"Authorization": f"Bearer {cluster_secret}"}

        resp = client.post("/cluster/nodes", json=_NODE_PAYLOAD, headers=headers)
        assert resp.status_code == 201, resp.text

        # Task pull is a non-cluster route; the same credential must authenticate
        # it. No open tasks -> 404, but crucially NOT 401/403.
        pull = client.get("/tasks/next/backend", headers=headers)
        assert pull.status_code not in (401, 403), pull.text

    def test_cluster_secret_barred_from_operator_endpoint(self, tmp_path: Path) -> None:
        """The cluster secret must not reach operator-only endpoints."""
        cluster_secret = "cluster-only-secret"  # NOSONAR - test fixture
        client = self._client(tmp_path, cluster_secret=cluster_secret, api_token=_SECRET)
        resp = client.post(
            "/shutdown",
            json={},
            headers={"Authorization": f"Bearer {cluster_secret}"},
        )
        assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# Worker fail-fast on auth rejection
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status_code: int, *, text: str = "", payload: dict | None = None) -> None:
        self.status_code = status_code
        self.text = text
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    def __init__(self, resp: _FakeResp) -> None:
        self._resp = resp
        self.calls = 0

    def post(self, *_a: object, **_k: object) -> _FakeResp:
        self.calls += 1
        return self._resp


class TestWorkerFailFast:
    """Worker must not loop forever on a 401/403."""

    def _loop(self):  # type: ignore[no-untyped-def]
        from bernstein.cli.commands.worker_cmd import WorkerLoop

        # Explicit adapter avoids slow, non-hermetic agent auto-detection.
        loop = WorkerLoop(server_url="http://central:8052", auth_token="whatever", adapter="claude")
        loop._running = True
        return loop

    def test_register_retry_stops_on_401(self) -> None:
        loop = self._loop()
        client = _FakeClient(_FakeResp(401, text='{"detail":"Invalid or expired token"}'))
        node_id = loop._register_with_retry(client)  # type: ignore[arg-type]
        assert node_id is None
        assert loop._running is False
        assert client.calls == 1  # fail-fast: no silent retry loop

    def test_register_retry_stops_on_403(self) -> None:
        loop = self._loop()
        client = _FakeClient(_FakeResp(403, text='{"detail":"forbidden"}'))
        node_id = loop._register_with_retry(client)  # type: ignore[arg-type]
        assert node_id is None
        assert loop._running is False
        assert client.calls == 1

    def test_register_succeeds_on_201(self) -> None:
        loop = self._loop()
        client = _FakeClient(_FakeResp(201, payload={"id": "node-xyz"}))
        node_id = loop._register_with_retry(client)  # type: ignore[arg-type]
        assert node_id == "node-xyz"
        assert loop._running is True
