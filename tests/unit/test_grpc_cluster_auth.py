"""Credential, token, and re-registration semantics of the gRPC cluster surface.

Nothing in ``src/`` starts the gRPC server yet, so every test here drives the
servicer object directly -- the same object ``add_ClusterServiceServicer_to_server``
would receive.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import grpc
import pytest
from bernstein.core.models import ClusterConfig

from bernstein.core.grpc_gen import cluster_pb2
from bernstein.core.protocols.cluster import NodeRegistry
from bernstein.core.protocols.cluster.cluster_auth import (
    SCOPE_NODE_ADMIN,
    SCOPE_NODE_HEARTBEAT,
    SCOPE_NODE_REGISTER,
    ClusterAuthConfig,
    ClusterAuthenticator,
)
from bernstein.core.protocols.grpc.grpc_server import (
    BernsteinGrpcServer,
    ClusterServiceImpl,
    GrpcServerConfig,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

SECRET = "cluster-shared-secret"


class Aborted(Exception):
    """What a live ``grpc.aio`` context raises out of ``abort``."""

    def __init__(self, code: grpc.StatusCode, details: str) -> None:
        super().__init__(details)
        self.code = code
        self.details = details


class FakeContext:
    """Servicer context that aborts the way ``grpc.aio`` does."""

    def __init__(self, metadata: Sequence[tuple[str, str]] = ()) -> None:
        self._metadata = tuple(metadata)

    def invocation_metadata(self) -> tuple[tuple[str, str], ...]:
        return self._metadata

    async def abort(self, code: grpc.StatusCode, details: str) -> None:
        raise Aborted(code, details)


def _bearer(token: str) -> FakeContext:
    return FakeContext([("authorization", f"Bearer {token}")])


def _authenticator(require_auth: bool = True) -> ClusterAuthenticator:
    return ClusterAuthenticator(ClusterAuthConfig(secret=SECRET, require_auth=require_auth))


def _registry() -> NodeRegistry:
    return NodeRegistry(ClusterConfig())


def _register_request(name: str = "worker-a", url: str = "http://worker-a:8052", max_agents: int = 4) -> Any:
    return cluster_pb2.RegisterNodeRequest(
        name=name,
        url=url,
        capacity=cluster_pb2.NodeCapacity(
            max_agents=max_agents,
            available_slots=max_agents,
            supported_models=["sonnet"],
        ),
        labels={"role": "worker"},
    )


async def _register_one(service: ClusterServiceImpl, context: FakeContext | None = None, **kwargs: Any) -> Any:
    return await service.RegisterNode(_register_request(**kwargs), context or FakeContext())


class TestMutatingHandlersRequireCredentials:
    @pytest.mark.asyncio
    async def test_register_node_without_credential_is_unauthenticated(self) -> None:
        registry = _registry()
        service = ClusterServiceImpl(registry, _authenticator())

        with pytest.raises(Aborted) as excinfo:
            await service.RegisterNode(_register_request(), FakeContext())

        assert excinfo.value.code is grpc.StatusCode.UNAUTHENTICATED
        assert registry.list_nodes() == []

    @pytest.mark.asyncio
    async def test_register_node_with_heartbeat_only_scope_is_permission_denied(self) -> None:
        registry = _registry()
        auth = _authenticator()
        service = ClusterServiceImpl(registry, auth)
        token = auth.issue_node_token("node-1", scopes=[SCOPE_NODE_HEARTBEAT])

        with pytest.raises(Aborted) as excinfo:
            await service.RegisterNode(_register_request(), _bearer(token))

        assert excinfo.value.code is grpc.StatusCode.PERMISSION_DENIED
        assert registry.list_nodes() == []

    @pytest.mark.asyncio
    async def test_register_node_with_register_scope_is_admitted(self) -> None:
        registry = _registry()
        auth = _authenticator()
        service = ClusterServiceImpl(registry, auth)
        token = auth.issue_node_token("node-1", scopes=[SCOPE_NODE_REGISTER])

        await service.RegisterNode(_register_request(), _bearer(token))

        assert [n.name for n in registry.list_nodes()] == ["worker-a"]

    @pytest.mark.asyncio
    async def test_heartbeat_without_credential_is_unauthenticated(self) -> None:
        registry = _registry()
        auth = _authenticator()
        node = (await _register_one(ClusterServiceImpl(registry), None)).node
        service = ClusterServiceImpl(registry, auth)
        before = registry.get(node.id).last_heartbeat

        with pytest.raises(Aborted) as excinfo:
            await service.Heartbeat(cluster_pb2.HeartbeatRequest(node_id=node.id), FakeContext())

        assert excinfo.value.code is grpc.StatusCode.UNAUTHENTICATED
        assert registry.get(node.id).last_heartbeat == before

    @pytest.mark.asyncio
    async def test_heartbeat_with_register_only_scope_is_permission_denied(self) -> None:
        registry = _registry()
        auth = _authenticator()
        node = (await _register_one(ClusterServiceImpl(registry), None)).node
        service = ClusterServiceImpl(registry, auth)
        token = auth.issue_node_token(node.id, scopes=[SCOPE_NODE_REGISTER])

        with pytest.raises(Aborted) as excinfo:
            await service.Heartbeat(cluster_pb2.HeartbeatRequest(node_id=node.id), _bearer(token))

        assert excinfo.value.code is grpc.StatusCode.PERMISSION_DENIED

    @pytest.mark.asyncio
    async def test_stream_heartbeats_without_credential_is_unauthenticated(self) -> None:
        registry = _registry()
        auth = _authenticator()
        node = (await _register_one(ClusterServiceImpl(registry), None)).node
        service = ClusterServiceImpl(registry, auth)
        before = registry.get(node.id).last_heartbeat

        async def requests() -> Any:
            yield cluster_pb2.HeartbeatRequest(node_id=node.id)

        with pytest.raises(Aborted) as excinfo:
            async for _ in service.StreamHeartbeats(requests(), FakeContext()):
                pass

        assert excinfo.value.code is grpc.StatusCode.UNAUTHENTICATED
        assert registry.get(node.id).last_heartbeat == before

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("method", "request_factory"),
        [
            ("UnregisterNode", cluster_pb2.UnregisterNodeRequest),
            ("CordonNode", cluster_pb2.CordonRequest),
            ("UncordonNode", cluster_pb2.UncordonRequest),
            ("DrainNode", cluster_pb2.DrainRequest),
        ],
    )
    async def test_lifecycle_verbs_without_credential_are_unauthenticated(
        self, method: str, request_factory: Any
    ) -> None:
        registry = _registry()
        node = (await _register_one(ClusterServiceImpl(registry), None)).node
        service = ClusterServiceImpl(registry, _authenticator())

        with pytest.raises(Aborted) as excinfo:
            await getattr(service, method)(request_factory(node_id=node.id), FakeContext())

        assert excinfo.value.code is grpc.StatusCode.UNAUTHENTICATED
        assert registry.get(node.id) is not None
        assert registry.get(node.id).status.value == "online"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("method", "request_factory"),
        [
            ("UnregisterNode", cluster_pb2.UnregisterNodeRequest),
            ("CordonNode", cluster_pb2.CordonRequest),
            ("UncordonNode", cluster_pb2.UncordonRequest),
            ("DrainNode", cluster_pb2.DrainRequest),
        ],
    )
    async def test_lifecycle_verbs_reject_a_heartbeat_scoped_token(self, method: str, request_factory: Any) -> None:
        registry = _registry()
        auth = _authenticator()
        node = (await _register_one(ClusterServiceImpl(registry), None)).node
        service = ClusterServiceImpl(registry, auth)
        token = auth.issue_node_token(node.id, scopes=[SCOPE_NODE_REGISTER, SCOPE_NODE_HEARTBEAT])

        with pytest.raises(Aborted) as excinfo:
            await getattr(service, method)(request_factory(node_id=node.id), _bearer(token))

        assert excinfo.value.code is grpc.StatusCode.PERMISSION_DENIED
        assert registry.get(node.id) is not None

    @pytest.mark.asyncio
    async def test_lifecycle_verbs_accept_a_node_admin_token(self) -> None:
        registry = _registry()
        auth = _authenticator()
        node = (await _register_one(ClusterServiceImpl(registry), None)).node
        service = ClusterServiceImpl(registry, auth)
        token = auth.issue_node_token("operator", scopes=[SCOPE_NODE_ADMIN])

        resp = await service.CordonNode(cluster_pb2.CordonRequest(node_id=node.id), _bearer(token))

        assert resp.status == cluster_pb2.NodeStatus.NODE_STATUS_CORDONED
        assert registry.get(node.id).status.value == "cordoned"

    @pytest.mark.asyncio
    async def test_reads_stay_unauthenticated_like_the_rest_routes(self) -> None:
        registry = _registry()
        await _register_one(ClusterServiceImpl(registry), None)
        service = ClusterServiceImpl(registry, _authenticator())

        listed = await service.ListNodes(cluster_pb2.ListNodesRequest(), FakeContext())
        status = await service.GetClusterStatus(cluster_pb2.ClusterStatusRequest(), FakeContext())

        assert len(listed.nodes) == 1
        assert status.total_nodes == 1

    @pytest.mark.asyncio
    async def test_handlers_are_open_when_no_authenticator_is_wired(self) -> None:
        registry = _registry()
        service = ClusterServiceImpl(registry)

        await service.RegisterNode(_register_request(), FakeContext())

        assert len(registry.list_nodes()) == 1

    @pytest.mark.asyncio
    async def test_handlers_are_open_when_require_auth_is_off(self) -> None:
        registry = _registry()
        service = ClusterServiceImpl(registry, _authenticator(require_auth=False))

        await service.RegisterNode(_register_request(), FakeContext())

        assert len(registry.list_nodes()) == 1

    @pytest.mark.asyncio
    async def test_shared_secret_authenticates_a_registration(self) -> None:
        registry = _registry()
        service = ClusterServiceImpl(registry, _authenticator())

        await service.RegisterNode(_register_request(), _bearer(SECRET))

        assert len(registry.list_nodes()) == 1

    @pytest.mark.asyncio
    async def test_refused_call_never_falls_through_when_abort_does_not_raise(self) -> None:
        # A context that only records the abort must not let the handler run on
        # into the mutation it was refused.
        from bernstein.core.protocols.cluster.cluster_auth import ClusterAuthError

        class RecordingContext(FakeContext):
            code: grpc.StatusCode | None = None

            async def abort(self, code: grpc.StatusCode, details: str) -> None:
                self.code = code

        registry = _registry()
        service = ClusterServiceImpl(registry, _authenticator())
        context = RecordingContext()

        with pytest.raises(ClusterAuthError, match="Missing Authorization header"):
            await service.RegisterNode(_register_request(), context)

        assert registry.list_nodes() == []
        assert context.code is grpc.StatusCode.UNAUTHENTICATED


class TestRegisterNodeAuthToken:
    @pytest.mark.asyncio
    async def test_register_response_carries_a_token_the_server_accepts(self) -> None:
        registry = _registry()
        auth = _authenticator()
        service = ClusterServiceImpl(registry, auth)

        resp = await service.RegisterNode(_register_request(), _bearer(SECRET))

        assert resp.auth_token
        payload = auth.verify_request(f"Bearer {resp.auth_token}", SCOPE_NODE_HEARTBEAT)
        assert payload.user_id == resp.node.id

    @pytest.mark.asyncio
    async def test_issued_token_authenticates_the_next_heartbeat(self) -> None:
        registry = _registry()
        auth = _authenticator()
        service = ClusterServiceImpl(registry, auth)
        resp = await service.RegisterNode(_register_request(), _bearer(SECRET))

        beat = await service.Heartbeat(
            cluster_pb2.HeartbeatRequest(node_id=resp.node.id),
            _bearer(resp.auth_token),
        )

        assert beat.acknowledged is True
        assert beat.node.id == resp.node.id

    @pytest.mark.asyncio
    async def test_issued_token_does_not_grant_admin_verbs(self) -> None:
        registry = _registry()
        auth = _authenticator()
        service = ClusterServiceImpl(registry, auth)
        resp = await service.RegisterNode(_register_request(), _bearer(SECRET))

        with pytest.raises(Aborted) as excinfo:
            await service.UnregisterNode(
                cluster_pb2.UnregisterNodeRequest(node_id=resp.node.id),
                _bearer(resp.auth_token),
            )

        assert excinfo.value.code is grpc.StatusCode.PERMISSION_DENIED

    @pytest.mark.asyncio
    async def test_auth_token_stays_empty_without_an_authenticator(self) -> None:
        # Nothing could verify a token minted here, so the field is left unset
        # rather than handing back a credential no handler would accept.
        resp = await _register_one(ClusterServiceImpl(_registry()))

        assert resp.auth_token == ""


class TestReRegistrationIsIdempotent:
    @pytest.mark.asyncio
    async def test_reregistration_reuses_the_existing_node_id(self) -> None:
        registry = _registry()
        service = ClusterServiceImpl(registry)

        first = await _register_one(service)
        second = await _register_one(service)

        assert second.node.id == first.node.id
        assert len(registry.list_nodes()) == 1

    @pytest.mark.asyncio
    async def test_restarting_worker_does_not_inflate_cluster_capacity(self) -> None:
        registry = _registry()
        service = ClusterServiceImpl(registry)

        for _ in range(3):
            await _register_one(service, max_agents=4)

        summary = registry.cluster_summary()
        assert summary["total_nodes"] == 1
        assert summary["total_capacity"] == 4

    @pytest.mark.asyncio
    async def test_reregistration_refreshes_capacity_on_the_existing_entry(self) -> None:
        registry = _registry()
        service = ClusterServiceImpl(registry)

        first = await _register_one(service, max_agents=4)
        await _register_one(service, max_agents=8)

        node = registry.get(first.node.id)
        assert node.capacity.max_agents == 8
        assert len(registry.list_nodes()) == 1

    @pytest.mark.asyncio
    async def test_same_name_on_a_different_url_stays_a_separate_node(self) -> None:
        registry = _registry()
        service = ClusterServiceImpl(registry)

        first = await _register_one(service, url="http://worker-a:8052")
        second = await _register_one(service, url="http://worker-a:9052")

        assert first.node.id != second.node.id
        assert len(registry.list_nodes()) == 2

    @pytest.mark.asyncio
    async def test_blank_identity_never_collapses_two_registrations(self) -> None:
        # An empty name/url carries no identity; merging on it would fold
        # unrelated nodes into a single entry.
        registry = _registry()
        service = ClusterServiceImpl(registry)

        first = await _register_one(service, name="", url="")
        second = await _register_one(service, name="", url="")

        assert first.node.id != second.node.id
        assert len(registry.list_nodes()) == 2


class TestNodeRegistryFindByIdentity:
    def test_find_by_identity_matches_name_and_url(self) -> None:
        from bernstein.core.models import NodeInfo

        registry = _registry()
        node = registry.register(NodeInfo(name="worker-a", url="http://worker-a:8052"))

        assert registry.find_by_identity("worker-a", "http://worker-a:8052").id == node.id
        assert registry.find_by_identity("worker-a", "http://worker-a:9052") is None
        assert registry.find_by_identity("worker-b", "http://worker-a:8052") is None

    def test_find_by_identity_refuses_blank_identity(self) -> None:
        from bernstein.core.models import NodeInfo

        registry = _registry()
        registry.register(NodeInfo(name="", url=""))

        assert registry.find_by_identity("", "") is None


class _FakeAioServer:
    def __init__(self) -> None:
        self.insecure_ports: list[str] = []
        self.secure_ports: list[str] = []
        self.started = False

    def add_insecure_port(self, bind: str) -> int:
        self.insecure_ports.append(bind)
        return 1

    def add_secure_port(self, bind: str, _creds: Any) -> int:
        self.secure_ports.append(bind)
        return 1

    async def start(self) -> None:
        self.started = True


class TestInsecurePortFallback:
    @staticmethod
    def _patch_wiring(monkeypatch: pytest.MonkeyPatch) -> tuple[_FakeAioServer, dict[str, Any]]:
        from bernstein.core.grpc_gen import cluster_pb2_grpc, tasks_pb2_grpc
        from bernstein.core.protocols.grpc import grpc_server as module

        fake_server = _FakeAioServer()
        captured: dict[str, Any] = {}

        monkeypatch.setattr(module, "grpc_aio", MagicMock(server=lambda **_kwargs: fake_server))
        monkeypatch.setattr(tasks_pb2_grpc, "add_TaskServiceServicer_to_server", lambda *_args: None)
        monkeypatch.setattr(
            cluster_pb2_grpc,
            "add_ClusterServiceServicer_to_server",
            lambda servicer, _server: captured.__setitem__("servicer", servicer),
        )
        return fake_server, captured

    @pytest.mark.asyncio
    async def test_insecure_fallback_still_enforces_cluster_auth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_server, captured = self._patch_wiring(monkeypatch)
        registry = _registry()
        server = BernsteinGrpcServer(config=GrpcServerConfig(port=50999, enable_reflection=False))

        await server.start(MagicMock(), node_registry=registry, cluster_authenticator=_authenticator())

        assert fake_server.insecure_ports == ["0.0.0.0:50999"]
        assert fake_server.secure_ports == []
        # The servicer mounted on the insecure port is the enforcing one.
        with pytest.raises(Aborted) as excinfo:
            await captured["servicer"].RegisterNode(_register_request(), FakeContext())
        assert excinfo.value.code is grpc.StatusCode.UNAUTHENTICATED
        assert registry.list_nodes() == []

    @pytest.mark.asyncio
    async def test_insecure_fallback_warns_that_credentials_are_in_cleartext(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        self._patch_wiring(monkeypatch)
        server = BernsteinGrpcServer(config=GrpcServerConfig(port=50999, enable_reflection=False))

        with caplog.at_level(logging.WARNING, logger="bernstein.core.protocols.grpc.grpc_server"):
            await server.start(MagicMock(), node_registry=_registry(), cluster_authenticator=_authenticator())

        assert any("cleartext" in record.getMessage() for record in caplog.records)

    @pytest.mark.asyncio
    async def test_no_cleartext_warning_without_cluster_auth(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        self._patch_wiring(monkeypatch)
        server = BernsteinGrpcServer(config=GrpcServerConfig(port=50999, enable_reflection=False))

        with caplog.at_level(logging.WARNING, logger="bernstein.core.protocols.grpc.grpc_server"):
            await server.start(MagicMock(), node_registry=_registry())

        assert caplog.records == []


class TestClusterClientCredentials:
    @staticmethod
    def _client(auth_token: str | None) -> Any:
        from bernstein.core.protocols.grpc.grpc_client import ClusterClient, GrpcClientConfig

        return ClusterClient(config=GrpcClientConfig(auth_token=auth_token))

    def test_metadata_is_empty_without_a_credential(self) -> None:
        assert self._client(None)._metadata() == []

    def test_metadata_carries_the_configured_join_credential(self) -> None:
        assert self._client("join-secret")._metadata() == [("authorization", "Bearer join-secret")]

    @pytest.mark.asyncio
    async def test_client_adopts_the_issued_node_token_after_registration(self) -> None:
        client = self._client("join-secret")
        registered = cluster_pb2.RegisterNodeResponse(auth_token="node-jwt")
        registered.node.id = "node-1"
        sent: list[Any] = []

        class _Stub:
            async def RegisterNode(self, _req: Any, **kwargs: Any) -> Any:  # NOSONAR - gRPC method name
                sent.append(kwargs["metadata"])
                return registered

            async def Heartbeat(self, _req: Any, **kwargs: Any) -> Any:  # NOSONAR - gRPC method name
                sent.append(kwargs["metadata"])
                return cluster_pb2.HeartbeatResponse(acknowledged=True)

        client._stub = _Stub()

        result = await client.register_node(name="worker-a", url="http://worker-a:8052")
        await client.heartbeat("node-1", available_slots=2)

        assert result["auth_token"] == "node-jwt"
        assert sent == [
            [("authorization", "Bearer join-secret")],
            [("authorization", "Bearer node-jwt")],
        ]
