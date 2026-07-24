"""Unit tests for the Codex-on-Cloudflare bridge adapter (issue #2969).

Every HTTP interaction here is served by an ``httpx.MockTransport`` whose
payloads were recorded from the bridge shipped in ``@cloudflare/sandbox``
0.12.4 (``package/dist/bridge/index.js``): the route table, the OpenAPI
document it serves (API contract ``1.0.0``, distinct from the package
version), the ``{"error", "code"}`` failure shape, and the SSE frames -
base64 ``stdout``/``stderr`` chunks plus a terminal ``exit`` or ``error``.

:class:`_Bridge` also reproduces two behaviours that shape the adapter's
design, so the tests fail if either assumption is dropped:

* ``GET /v1/sandbox/:id/running`` runs behind the warm-pool middleware and
  *acquires a container* before answering, so probing it after a delete
  restarts what was just torn down.
* ``DELETE /v1/sandbox/:id`` looks the container up without allocating, and
  drops the assignment.

There is no live Cloudflare deployment behind these tests. End-to-end
verification against a real bridge is documented in
``docs/cloudflare/cloudflare-codex-sandbox.md`` and has not been run.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import tarfile
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from bernstein.adapters.codex_cloudflare import (
    BRIDGE_API_CONTRACT_VERSION,
    BRIDGE_SDK_VERSION,
    REMOTE_ISOLATION,
    REQUIRED_BRIDGE_PATHS,
    RETIRED_CONFIG_FIELDS,
    CodexCloudflareAdapter,
    CodexCloudflareBridgeAuthError,
    CodexCloudflareBridgeContractError,
    CodexCloudflareBridgeVersionError,
    CodexCloudflareCancelError,
    CodexCloudflareConfigError,
    CodexCloudflareNotConfiguredError,
    CodexCloudflarePayloadTooLargeError,
    CodexSandboxConfig,
    CodexSandboxResult,
    SandboxEvidence,
    _decode_stream_payload,
    _parse_sse_frames,
)

if TYPE_CHECKING:
    from pathlib import Path

BRIDGE_URL = "https://sandbox-bridge.example.workers.dev"
API_KEY = "bridge-secret"
#: Base32 lowercase, matching the bridge's own ``^[a-z2-7]{1,128}$`` id format.
SANDBOX_ID = "mfrggzdfmy2tqnrzgezdgnbv"
SESSION_ID = "sess-1"

#: Paths the bridge publishes in its OpenAPI document, read from the bundle.
PUBLISHED_PATHS = [
    "/v1/sandbox",
    "/v1/sandbox/{id}",
    "/v1/sandbox/{id}/exec",
    "/v1/sandbox/{id}/file/{path}",
    "/v1/sandbox/{id}/hydrate",
    "/v1/sandbox/{id}/mount",
    "/v1/sandbox/{id}/persist",
    "/v1/sandbox/{id}/pty",
    "/v1/sandbox/{id}/running",
    "/v1/sandbox/{id}/session",
    "/v1/sandbox/{id}/session/{sid}",
    "/v1/sandbox/{id}/tunnel/{port}",
    "/v1/sandbox/{id}/unmount",
    "/v1/openapi.json",
    "/v1/pool/prime",
    "/v1/pool/shutdown-prewarmed",
    "/v1/pool/stats",
]


# ---------------------------------------------------------------------------
# Recorded bridge fixtures
# ---------------------------------------------------------------------------


def _tar(entries: dict[str, bytes]) -> bytes:
    """Build a deterministic in-memory tar, with the ``./`` prefix the bridge emits."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, payload in sorted(entries.items()):
            info = tarfile.TarInfo(name=f"./{name}")
            info.size = len(payload)
            info.mtime = 0
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def _sse(frames: list[tuple[str, str]]) -> bytes:
    """Render SSE frames the way the bridge's ``writeSSE`` helper does."""
    chunks = []
    for event, data in frames:
        body = "\n".join(f"data: {line}" for line in data.split("\n"))
        chunks.append(f"event: {event}\n{body}\n\n")
    return "".join(chunks).encode("utf-8")


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


#: The exec stream a successful run produces, mirroring the documented frames.
EXEC_STREAM_OK = _sse(
    [
        ("stdout", _b64("applying patch\n")),
        ("stderr", _b64("warning: no tests\n")),
        ("stdout", _b64("done\n")),
        ("exit", '{"exit_code":0}'),
    ],
)

SEED_TAR = _tar({"README.md": b"before\n", "src/app.py": b"print(1)\n"})
RESULT_TAR = _tar(
    {
        "README.md": b"before\n",
        "src/app.py": b"print(2)\n",
        "src/new.py": b"added\n",
    },
)


class _Bridge:
    """Scripted stand-in for a deployed bridge Worker.

    Models the warm pool the way the real bridge does: any ``/sandbox/:id/*``
    route acquires a container for the id (starting one if needed), while
    ``DELETE /sandbox/:id`` looks it up without allocating and then drops it.
    ``/pool/stats`` reports the resulting bookkeeping.
    """

    def __init__(
        self,
        *,
        exec_stream: bytes = EXEC_STREAM_OK,
        persist_tar: bytes = RESULT_TAR,
        openapi_version: str = BRIDGE_API_CONTRACT_VERSION,
        published_paths: list[str] | None = None,
        auth_enforced: bool = True,
        exec_status: int = 200,
        delete_status: int = 204,
    ) -> None:
        self.exec_stream = exec_stream
        self.persist_tar = persist_tar
        self.openapi_version = openapi_version
        self.published_paths = PUBLISHED_PATHS if published_paths is None else published_paths
        self.auth_enforced = auth_enforced
        self.exec_status = exec_status
        self.delete_status = delete_status
        self.requests: list[tuple[str, str]] = []
        self.bodies: dict[str, bytes] = {}
        self.deleted: list[str] = []
        #: Sandbox ids the warm pool currently holds a container for.
        self.assigned: set[str] = set()

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def routes(self) -> list[str]:
        return [f"{method} {path}" for method, path in self.requests]

    def _openapi(self) -> dict[str, Any]:
        return {
            "openapi": "3.1.0",
            "info": {"title": "Cloudflare Sandbox Service API", "version": self.openapi_version},
            "paths": {path: {} for path in self.published_paths},
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        method = request.method
        path = request.url.path
        self.requests.append((method, path))
        self.bodies[f"{method} {path}"] = request.content
        authorised = request.headers.get("Authorization") == f"Bearer {API_KEY}"

        if self.auth_enforced and not authorised:
            return httpx.Response(401, json={"error": "Unauthorized", "code": "unauthorized"})

        if method == "POST" and path == "/v1/sandbox":
            # The create route mints an id; it does not allocate a container.
            return httpx.Response(200, json={"id": SANDBOX_ID})
        if method == "GET" and path == "/v1/openapi.json":
            return httpx.Response(200, json=self._openapi())
        if method == "GET" and path == "/v1/pool/stats":
            count = len(self.assigned)
            return httpx.Response(200, json={"warm": 0, "assigned": count, "total": count})
        if method == "DELETE" and path == f"/v1/sandbox/{SANDBOX_ID}":
            # Allocation-free: looks the container up, drops it, never starts one.
            self.deleted.append(path)
            self.assigned.discard(SANDBOX_ID)
            if self.delete_status >= 400:
                return httpx.Response(self.delete_status, json={"error": "pool error", "code": "pool_error"})
            return httpx.Response(204)

        if path.startswith(f"/v1/sandbox/{SANDBOX_ID}/"):
            # Warm-pool middleware: acquires (and would start) a container.
            self.assigned.add(SANDBOX_ID)

        if method == "POST" and path.endswith("/hydrate"):
            return httpx.Response(200, json={"ok": True})
        if method == "POST" and path.endswith("/session"):
            return httpx.Response(200, json={"id": SESSION_ID})
        if method == "POST" and path.endswith("/exec"):
            if self.exec_status >= 400:
                return httpx.Response(self.exec_status, json={"error": "boom", "code": "exec_error"})
            return httpx.Response(
                200,
                content=self.exec_stream,
                headers={"content-type": "text/event-stream"},
            )
        if method == "POST" and path.endswith("/persist"):
            return httpx.Response(200, content=self.persist_tar)
        if method == "GET" and path.endswith("/running"):
            # The handler runs `true` in the container the middleware just
            # acquired, so it answers true even for an id that had none.
            return httpx.Response(200, json={"running": True})
        if method == "PUT" and "/file/" in path:
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"error": "not found", "code": "not_found"})


def _config(**overrides: Any) -> CodexSandboxConfig:
    base: dict[str, Any] = {
        "bridge_url": BRIDGE_URL,
        "bridge_api_key": API_KEY,
        "openai_api_key": "sk-test",
    }
    base.update(overrides)
    return CodexSandboxConfig(**base)


def _adapter(bridge: _Bridge, **overrides: Any) -> CodexCloudflareAdapter:
    return CodexCloudflareAdapter(_config(**overrides), transport=bridge.transport)


# ---------------------------------------------------------------------------
# Configuration surface
# ---------------------------------------------------------------------------


class TestCodexSandboxConfig:
    def test_defaults_describe_only_honourable_settings(self) -> None:
        cfg = CodexSandboxConfig()
        assert cfg.bridge_url == ""
        assert cfg.bridge_api_key == ""
        assert cfg.workdir == "/workspace"
        assert cfg.agent_command == ("codex", "exec")
        assert cfg.max_execution_minutes == 30
        assert cfg.persist_excludes == (".git",)
        assert cfg.require_supported_api_version is True

    def test_retired_fields_are_absent_from_the_dataclass(self) -> None:
        fields = set(CodexSandboxConfig.__dataclass_fields__)
        assert fields.isdisjoint(RETIRED_CONFIG_FIELDS)

    def test_is_configured_requires_url_and_key(self) -> None:
        assert not CodexSandboxConfig(bridge_url=BRIDGE_URL).is_configured
        assert not CodexSandboxConfig(bridge_api_key=API_KEY).is_configured
        assert _config().is_configured

    def test_missing_fields_names_both(self) -> None:
        assert CodexSandboxConfig().missing_fields() == ("bridge_url", "bridge_api_key")

    def test_frozen(self) -> None:
        cfg = CodexSandboxConfig()
        with pytest.raises(AttributeError):
            cfg.bridge_url = "x"  # type: ignore[misc]

    @pytest.mark.parametrize("retired", sorted(RETIRED_CONFIG_FIELDS))
    def test_from_mapping_refuses_each_retired_field_by_name(self, retired: str) -> None:
        with pytest.raises(CodexCloudflareConfigError) as excinfo:
            CodexSandboxConfig.from_mapping({"bridge_url": BRIDGE_URL, retired: 1})
        message = str(excinfo.value)
        assert retired in message
        assert "cannot honour" in message
        assert "ignored" in message

    def test_from_mapping_explains_sizing_is_deploy_time(self) -> None:
        with pytest.raises(CodexCloudflareConfigError, match="instance_type"):
            CodexSandboxConfig.from_mapping({"memory_mb": 1024})

    def test_from_mapping_explains_no_egress_restriction(self) -> None:
        with pytest.raises(CodexCloudflareConfigError, match="no egress restrictions"):
            CodexSandboxConfig.from_mapping({"network_access": "restricted"})

    def test_from_mapping_rejects_unknown_keys(self) -> None:
        with pytest.raises(CodexCloudflareConfigError, match="Unknown"):
            CodexSandboxConfig.from_mapping({"nope": 1})

    def test_from_mapping_normalises_sequences_and_env(self) -> None:
        cfg = CodexSandboxConfig.from_mapping(
            {
                "bridge_url": BRIDGE_URL,
                "bridge_api_key": API_KEY,
                "agent_command": ["codex", "exec", "--full-auto"],
                "persist_excludes": [".git", "node_modules"],
                "extra_env": {"FOO": "bar"},
            },
        )
        assert cfg.agent_command == ("codex", "exec", "--full-auto")
        assert cfg.persist_excludes == (".git", "node_modules")
        assert cfg.extra_env == (("FOO", "bar"),)


# ---------------------------------------------------------------------------
# Unconfigured: refusal, never a silent local fallback
# ---------------------------------------------------------------------------


class TestNotConfigured:
    @pytest.mark.asyncio
    async def test_execute_refuses(self) -> None:
        adapter = CodexCloudflareAdapter(CodexSandboxConfig())
        with pytest.raises(CodexCloudflareNotConfiguredError) as excinfo:
            await adapter.execute("do stuff", "ws-1")
        message = str(excinfo.value)
        assert "not configured" in message
        assert "bridge_url" in message
        assert "does not fall back to local execution" in message

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["get_status", "cancel", "is_running"])
    async def test_lifecycle_methods_refuse(self, method: str) -> None:
        adapter = CodexCloudflareAdapter(CodexSandboxConfig())
        with pytest.raises(CodexCloudflareNotConfiguredError):
            await getattr(adapter, method)(SANDBOX_ID)

    @pytest.mark.asyncio
    async def test_preflight_refuses(self) -> None:
        adapter = CodexCloudflareAdapter(CodexSandboxConfig(bridge_url=BRIDGE_URL))
        with pytest.raises(CodexCloudflareNotConfiguredError, match="bridge_api_key"):
            await adapter.preflight()

    def test_name(self) -> None:
        assert CodexCloudflareAdapter(CodexSandboxConfig()).name == "codex-cloudflare"


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


class TestPreflight:
    @pytest.mark.asyncio
    async def test_accepts_a_healthy_pinned_bridge(self) -> None:
        bridge = _Bridge()
        result = await _adapter(bridge).preflight()
        assert result.auth_enforced is True
        assert result.api_version_supported is True
        assert result.api_version == BRIDGE_API_CONTRACT_VERSION
        assert result.routes_verified == REQUIRED_BRIDGE_PATHS
        assert "POST /v1/sandbox" in bridge.routes()
        assert "GET /v1/openapi.json" in bridge.routes()

    @pytest.mark.asyncio
    async def test_api_contract_version_is_not_the_package_version(self) -> None:
        """A healthy bridge from the pinned package serves contract 1.0.0.

        Comparing that against the package version (0.12.4) would refuse every
        real deployment, so the two version lines stay separate.
        """
        assert BRIDGE_API_CONTRACT_VERSION.split(".")[0] != BRIDGE_SDK_VERSION.split(".")[0]
        bridge = _Bridge(openapi_version="1.0.0")
        result = await _adapter(bridge).preflight()
        assert result.api_version_supported is True

    @pytest.mark.asyncio
    async def test_refuses_bridge_that_skips_authentication(self) -> None:
        bridge = _Bridge(auth_enforced=False)
        with pytest.raises(CodexCloudflareBridgeAuthError) as excinfo:
            await _adapter(bridge).preflight()
        message = str(excinfo.value)
        assert "SANDBOX_API_KEY secret is unset" in message
        assert "wrangler secret put" in message

    @pytest.mark.asyncio
    async def test_unauthenticated_probe_cleans_up_the_sandbox_it_created(self) -> None:
        bridge = _Bridge(auth_enforced=False)
        with pytest.raises(CodexCloudflareBridgeAuthError):
            await _adapter(bridge).preflight()
        assert bridge.deleted == [f"/v1/sandbox/{SANDBOX_ID}"]

    @pytest.mark.asyncio
    async def test_auth_probe_targets_the_route_without_an_id_segment(self) -> None:
        """The bridge validates the sandbox-id format *before* checking auth.

        Probing an id-bearing route would return 400 for a malformed id and
        misreport the auth posture, so the probe uses the create route, which
        carries no id.
        """
        bridge = _Bridge()
        await _adapter(bridge).preflight()
        first_method, first_path = bridge.requests[0]
        assert (first_method, first_path) == ("POST", "/v1/sandbox")

    @pytest.mark.asyncio
    async def test_refuses_a_different_major_contract(self) -> None:
        bridge = _Bridge(openapi_version="2.0.0")
        with pytest.raises(CodexCloudflareBridgeVersionError) as excinfo:
            await _adapter(bridge).preflight()
        message = str(excinfo.value)
        assert "2.0.0" in message
        assert BRIDGE_API_CONTRACT_VERSION in message

    @pytest.mark.asyncio
    async def test_refuses_bridge_without_a_declared_version(self) -> None:
        bridge = _Bridge(openapi_version="")
        with pytest.raises(CodexCloudflareBridgeVersionError, match="<absent>"):
            await _adapter(bridge).preflight()

    @pytest.mark.asyncio
    async def test_refuses_bridge_missing_a_route_the_adapter_drives(self) -> None:
        without_persist = [p for p in PUBLISHED_PATHS if p != "/v1/sandbox/{id}/persist"]
        bridge = _Bridge(published_paths=without_persist)
        with pytest.raises(CodexCloudflareBridgeContractError) as excinfo:
            await _adapter(bridge).preflight()
        assert "/v1/sandbox/{id}/persist" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_route_probe_stays_strict_when_version_drift_is_allowed(self) -> None:
        without_exec = [p for p in PUBLISHED_PATHS if p != "/v1/sandbox/{id}/exec"]
        bridge = _Bridge(openapi_version="2.0.0", published_paths=without_exec)
        adapter = _adapter(bridge, require_supported_api_version=False)
        with pytest.raises(CodexCloudflareBridgeContractError, match="exec"):
            await adapter.preflight()

    @pytest.mark.asyncio
    async def test_version_drift_can_be_opted_into(self) -> None:
        bridge = _Bridge(openapi_version="2.0.0")
        adapter = _adapter(bridge, require_supported_api_version=False)
        result = await adapter.preflight()
        assert result.api_version_supported is False
        assert result.api_version == "2.0.0"

    def test_auth_version_and_contract_failures_are_distinct_errors(self) -> None:
        auth = CodexCloudflareBridgeAuthError(BRIDGE_URL)
        version = CodexCloudflareBridgeVersionError("2.0.0", BRIDGE_URL)
        contract = CodexCloudflareBridgeContractError(("/v1/sandbox/{id}/exec",), BRIDGE_URL)
        assert not isinstance(auth, CodexCloudflareBridgeVersionError)
        assert not isinstance(version, CodexCloudflareBridgeContractError)
        assert len({str(auth), str(version), str(contract)}) == 3


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


class TestExecute:
    @pytest.mark.asyncio
    async def test_full_lifecycle_route_sequence(self) -> None:
        bridge = _Bridge()
        await _adapter(bridge).execute("fix the bug", "ws-1", workspace_tar=SEED_TAR)
        assert bridge.routes() == [
            "POST /v1/sandbox",
            f"POST /v1/sandbox/{SANDBOX_ID}/hydrate",
            f"POST /v1/sandbox/{SANDBOX_ID}/session",
            f"POST /v1/sandbox/{SANDBOX_ID}/exec",
            f"POST /v1/sandbox/{SANDBOX_ID}/persist",
            f"DELETE /v1/sandbox/{SANDBOX_ID}",
        ]

    @pytest.mark.asyncio
    async def test_decodes_base64_output_frames(self) -> None:
        bridge = _Bridge()
        result = await _adapter(bridge).execute("fix the bug", "ws-1")
        assert result.stdout == "applying patch\ndone\n"
        assert result.stderr == "warning: no tests\n"
        assert result.exit_code == 0
        assert result.status == "completed"

    @pytest.mark.asyncio
    async def test_output_is_streamed_before_the_workspace_is_collected(self) -> None:
        bridge = _Bridge()
        seen: list[tuple[str, bytes, tuple[str, ...]]] = []

        def record(stream: str, chunk: bytes) -> None:
            seen.append((stream, chunk, tuple(bridge.routes())))

        await _adapter(bridge).execute("fix the bug", "ws-1", on_output=record)

        assert [(stream, chunk) for stream, chunk, _ in seen] == [
            ("stdout", b"applying patch\n"),
            ("stderr", b"warning: no tests\n"),
            ("stdout", b"done\n"),
        ]
        # Every frame reached the caller while the stream was still open: no
        # /persist had been requested yet at callback time.
        for _, _, routes_at_call in seen:
            assert not any("persist" in route for route in routes_at_call)

    @pytest.mark.asyncio
    async def test_returns_a_diff_against_the_seeded_workspace(self) -> None:
        bridge = _Bridge()
        result = await _adapter(bridge).execute("fix the bug", "ws-1", workspace_tar=SEED_TAR)
        assert result.files_changed == ["src/app.py", "src/new.py"]

    @pytest.mark.asyncio
    async def test_agent_key_is_injected_as_session_env_not_argv(self) -> None:
        bridge = _Bridge()
        await _adapter(bridge).execute("fix the bug", "ws-1")
        session_body = json.loads(bridge.bodies[f"POST /v1/sandbox/{SANDBOX_ID}/session"])
        exec_body = bridge.bodies[f"POST /v1/sandbox/{SANDBOX_ID}/exec"].decode()
        assert session_body == {"cwd": "/workspace", "env": {"OPENAI_API_KEY": "sk-test"}}
        assert "sk-test" not in exec_body

    @pytest.mark.asyncio
    async def test_exec_body_matches_the_published_schema(self) -> None:
        bridge = _Bridge()
        await _adapter(bridge).execute("fix the bug", "ws-1", model="codex-mini", timeout_minutes=2)
        body = json.loads(bridge.bodies[f"POST /v1/sandbox/{SANDBOX_ID}/exec"])
        # ExecRequest publishes exactly argv / timeout_ms / cwd - there is no
        # env field, which is why the key travels on the session instead.
        assert set(body) == {"argv", "timeout_ms", "cwd"}
        assert body["argv"] == ["codex", "exec", "--model", "codex-mini", "fix the bug"]
        assert body["timeout_ms"] == 120_000
        assert body["cwd"] == "/workspace"

    @pytest.mark.asyncio
    async def test_exec_carries_the_session_id_header_and_hydrate_does_not(self) -> None:
        bridge = _Bridge()
        headers: dict[str, dict[str, str]] = {}
        inner = bridge.handle

        def spy(request: httpx.Request) -> httpx.Response:
            key = request.url.path.rsplit("/", 1)[-1]
            headers[key] = dict(request.headers)
            return inner(request)

        adapter = CodexCloudflareAdapter(_config(), transport=httpx.MockTransport(spy))
        await adapter.execute("fix the bug", "ws-1", workspace_tar=SEED_TAR)
        assert headers["exec"]["session-id"] == SESSION_ID
        assert headers["exec"]["accept"] == "text/event-stream"
        # The hydrate handler does not read Session-Id, so none is sent.
        assert "session-id" not in headers["hydrate"]

    @pytest.mark.asyncio
    async def test_persist_forwards_the_excludes_query_parameter(self) -> None:
        bridge = _Bridge()
        captured: list[str] = []
        inner = bridge.handle

        def spy(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/persist"):
                captured.append(request.url.query.decode())
            return inner(request)

        adapter = CodexCloudflareAdapter(
            _config(persist_excludes=(".git", "node_modules")),
            transport=httpx.MockTransport(spy),
        )
        await adapter.execute("fix the bug", "ws-1")
        assert captured == ["excludes=.git%2Cnode_modules"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("message", ["command timed out", "exec timeout after 30000ms"])
    async def test_terminal_timeout_error_is_surfaced(self, message: str) -> None:
        stream = _sse([("error", json.dumps({"error": message, "code": "exec_error"}))])
        bridge = _Bridge(exec_stream=stream)
        result = await _adapter(bridge).execute("fix the bug", "ws-1")
        assert result.status == "timeout"
        assert result.error == message

    @pytest.mark.asyncio
    async def test_terminal_transport_error_is_a_plain_failure(self) -> None:
        stream = _sse([("error", '{"error":"exec failed: boom","code":"exec_transport_error"}')])
        bridge = _Bridge(exec_stream=stream)
        result = await _adapter(bridge).execute("fix the bug", "ws-1")
        assert result.status == "failed"
        assert result.error == "exec failed: boom"

    @pytest.mark.asyncio
    async def test_nonzero_exit_is_failed(self) -> None:
        stream = _sse([("stdout", _b64("nope\n")), ("exit", '{"exit_code":2}')])
        bridge = _Bridge(exec_stream=stream)
        result = await _adapter(bridge).execute("fix the bug", "ws-1")
        assert result.status == "failed"
        assert result.exit_code == 2

    @pytest.mark.asyncio
    async def test_sandbox_is_deleted_even_when_the_run_blows_up(self) -> None:
        bridge = _Bridge(exec_status=500)
        with pytest.raises(RuntimeError, match="HTTP 500"):
            await _adapter(bridge).execute("fix the bug", "ws-1")
        assert bridge.deleted == [f"/v1/sandbox/{SANDBOX_ID}"]
        assert bridge.assigned == set()

    @pytest.mark.asyncio
    async def test_oversized_seed_is_refused_before_upload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("bernstein.adapters.codex_cloudflare.BRIDGE_MAX_PAYLOAD_BYTES", 8)
        bridge = _Bridge()
        with pytest.raises(CodexCloudflarePayloadTooLargeError, match="over the bridge"):
            await _adapter(bridge).execute("fix the bug", "ws-1", workspace_tar=SEED_TAR)
        assert not any("hydrate" in route for route in bridge.routes())
        assert bridge.deleted == [f"/v1/sandbox/{SANDBOX_ID}"]

    @pytest.mark.asyncio
    async def test_get_logs_returns_the_recorded_transcript(self) -> None:
        bridge = _Bridge()
        adapter = _adapter(bridge)
        await adapter.execute("fix the bug", "ws-1")
        assert await adapter.get_logs(SANDBOX_ID) == "applying patch\ndone\nwarning: no tests\n"
        assert await adapter.get_logs("unknown") == ""

    @pytest.mark.asyncio
    async def test_write_file_rejects_oversized_content(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("bernstein.adapters.codex_cloudflare.BRIDGE_MAX_PAYLOAD_BYTES", 4)
        bridge = _Bridge()
        with pytest.raises(CodexCloudflarePayloadTooLargeError):
            await _adapter(bridge).write_file(SANDBOX_ID, "a.txt", b"too long")
        assert bridge.routes() == []


# ---------------------------------------------------------------------------
# Cancellation must stop remote work, not just the local stream
# ---------------------------------------------------------------------------


class TestCancellation:
    @pytest.mark.asyncio
    async def test_cancel_issues_the_explicit_delete(self) -> None:
        bridge = _Bridge()
        bridge.assigned.add(SANDBOX_ID)
        outcome = await _adapter(bridge).cancel(SANDBOX_ID)
        assert outcome.deleted is True
        assert bridge.deleted == [f"/v1/sandbox/{SANDBOX_ID}"]

    @pytest.mark.asyncio
    async def test_teardown_is_confirmed_without_restarting_the_container(self) -> None:
        """After cancel the pool holds no container, and none was started.

        Teardown is read from ``/v1/pool/stats`` rather than
        ``/v1/sandbox/:id/running``: the latter runs behind the warm-pool
        middleware, which acquires a container before answering, so probing it
        here would restart exactly what the delete stopped.
        """
        bridge = _Bridge()
        bridge.assigned.add(SANDBOX_ID)
        outcome = await _adapter(bridge).cancel(SANDBOX_ID)
        assert outcome.pool_after.assigned == 0
        assert outcome.pool_after.total == 0
        assert bridge.assigned == set()
        assert not any(route.endswith("/running") for route in bridge.routes())

    @pytest.mark.asyncio
    async def test_running_probe_after_a_delete_would_restart_the_container(self) -> None:
        """The hazard the cancel path avoids, made explicit.

        This is the bridge behaviour, not a bug in the adapter: asking
        ``/running`` for a deleted id allocates a container and then reports
        it as up. Cancellation must not be built on this signal.
        """
        bridge = _Bridge()
        bridge.assigned.add(SANDBOX_ID)
        adapter = _adapter(bridge)
        await adapter.cancel(SANDBOX_ID)
        assert bridge.assigned == set()

        assert await adapter.is_running(SANDBOX_ID) is True
        assert bridge.assigned == {SANDBOX_ID}

    @pytest.mark.asyncio
    async def test_cancel_raises_when_the_bridge_refuses_the_delete(self) -> None:
        bridge = _Bridge(delete_status=502)
        with pytest.raises(CodexCloudflareCancelError) as excinfo:
            await _adapter(bridge).cancel(SANDBOX_ID)
        message = str(excinfo.value)
        assert "may still be running" in message
        assert SANDBOX_ID in message

    @pytest.mark.asyncio
    async def test_pool_stats_are_readable_on_their_own(self) -> None:
        bridge = _Bridge()
        bridge.assigned.add(SANDBOX_ID)
        stats = await _adapter(bridge).pool_stats()
        assert stats.assigned == 1
        assert stats.total == 1
        assert stats.warm == 0

    @pytest.mark.asyncio
    async def test_reap_retries_once_when_cancelled_mid_teardown(self) -> None:
        bridge = _Bridge()
        adapter = _adapter(bridge)
        calls: list[str] = []

        async def flaky_destroy(_client: httpx.AsyncClient, sandbox_id: str) -> None:
            calls.append(sandbox_id)
            if len(calls) == 1:
                raise asyncio.CancelledError
            bridge.deleted.append(f"/v1/sandbox/{sandbox_id}")

        adapter._destroy = flaky_destroy  # type: ignore[method-assign]
        async with httpx.AsyncClient(transport=bridge.transport) as client:
            with pytest.raises(asyncio.CancelledError):
                await adapter._reap(client, SANDBOX_ID)
        assert calls == [SANDBOX_ID, SANDBOX_ID]
        assert bridge.deleted == [f"/v1/sandbox/{SANDBOX_ID}"]


# ---------------------------------------------------------------------------
# Sandbox evidence: a remote run records where it ran
# ---------------------------------------------------------------------------


class TestSandboxEvidence:
    @pytest.mark.asyncio
    async def test_digest_is_the_content_address_of_the_persisted_workspace(self) -> None:
        bridge = _Bridge()
        result = await _adapter(bridge).execute("fix the bug", "ws-1", workspace_tar=SEED_TAR)
        assert result.evidence is not None
        assert result.evidence.terminal_snapshot_digest == hashlib.sha256(RESULT_TAR).hexdigest()
        assert result.evidence.workspace_bytes == len(RESULT_TAR)
        assert result.workspace_tar == RESULT_TAR

    @pytest.mark.asyncio
    async def test_isolation_names_where_the_task_ran(self) -> None:
        bridge = _Bridge()
        result = await _adapter(bridge).execute("fix the bug", "ws-1")
        assert result.evidence is not None
        assert result.evidence.isolation == REMOTE_ISOLATION
        assert result.evidence.sandbox_id == SANDBOX_ID
        assert result.evidence.bridge_api_version == BRIDGE_API_CONTRACT_VERSION
        assert result.evidence.bridge_sdk_version == BRIDGE_SDK_VERSION

    @pytest.mark.asyncio
    async def test_evidence_signs_and_verifies_as_a_selection_receipt(self, tmp_path: Path) -> None:
        from bernstein.core.sandbox.selection_receipt import (
            build_selection_receipt,
            load_or_create_signing_key,
            sign_receipt,
            snapshot_digests,
            verify_receipt,
        )

        bridge = _Bridge()
        result = await _adapter(bridge).execute("fix the bug", "ws-1", workspace_tar=SEED_TAR)
        assert result.evidence is not None
        candidate = result.evidence.to_race_candidate("task-1", {"correctness": 1.0})

        key = load_or_create_signing_key(tmp_path / "signing.pem")
        receipt = build_selection_receipt(
            base_snapshot_digest=hashlib.sha256(SEED_TAR).hexdigest(),
            candidates=[candidate],
            winner_task_id="task-1",
            ranker_profile={"method": "topsis", "criteria": []},
            public_key=key.public_key(),
        )
        signed = sign_receipt(receipt, private_key=key)

        verification = verify_receipt(signed)
        assert verification.ok, verification.errors
        assert signed.winner_snapshot_digest == result.evidence.terminal_snapshot_digest
        assert result.evidence.terminal_snapshot_digest in snapshot_digests(signed)
        assert signed.candidates[0]["isolation"] == REMOTE_ISOLATION

    def test_evidence_defaults(self) -> None:
        evidence = SandboxEvidence(terminal_snapshot_digest="0" * 64)
        assert evidence.isolation == REMOTE_ISOLATION
        assert evidence.bridge_api_version == BRIDGE_API_CONTRACT_VERSION


# ---------------------------------------------------------------------------
# SSE framing details
# ---------------------------------------------------------------------------


class TestSseFraming:
    def test_multiline_data_is_rejoined(self) -> None:
        frames = _parse_sse_frames(["event: error", "data: line one", "data: line two", ""])
        assert len(frames) == 1
        assert frames[0].event == "error"
        assert frames[0].data == "line one\nline two"

    def test_comment_lines_are_ignored(self) -> None:
        frames = _parse_sse_frames([": keep-alive", "event: exit", 'data: {"exit_code":0}', ""])
        assert [f.event for f in frames] == ["exit"]

    def test_non_base64_payload_is_passed_through(self) -> None:
        assert _decode_stream_payload("not base64!!") == b"not base64!!"
        assert _decode_stream_payload("") == b""
        assert _decode_stream_payload(_b64("ok")) == b"ok"


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


class TestCodexSandboxResult:
    def test_defaults(self) -> None:
        result = CodexSandboxResult(sandbox_id="sb-1", status="completed")
        assert result.files_changed == []
        assert result.stdout == ""
        assert result.exit_code == 0
        assert result.workspace_tar is None
        assert result.evidence is None
        assert result.error == ""
