"""API gateway bridge for model-routed task execution (#6564).

Routes task payloads to a centralized API gateway that handles model selection,
fallback, cost optimization, and credential rotation.  The gateway returns
structured results with routing metadata (provider, model, cost, latency).

The bridge accepts model-agnostic task payloads from the orchestrator, sends
them to the gateway endpoint with capability requirements, and returns
structured responses.  Fallback handling is transparent: when the gateway
reports a provider as unavailable, the bridge retries once with a fallback
hint.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from bernstein.bridges.base import (
    AgentState,
    AgentStatus,
    BridgeConfig,
    BridgeError,
    RuntimeBridge,
    SpawnRequest,
)
from bernstein.core.credentials import CredentialPool, NoAvailableCredentialError

logger = logging.getLogger(__name__)


def _str_any_dict() -> dict[str, Any]:
    return {}


@dataclass(frozen=True)
class GatewayRoutingMetadata:
    """Metadata returned by the gateway about how a request was routed."""

    provider: str = ""
    model: str = ""
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    fallback_used: bool = False
    cache_hit: bool = False
    extra: dict[str, Any] = field(default_factory=_str_any_dict)


_GATEWAY_STATE_MAP: dict[str, AgentState] = {
    "pending": AgentState.PENDING,
    "queued": AgentState.PENDING,
    "running": AgentState.RUNNING,
    "processing": AgentState.RUNNING,
    "completed": AgentState.COMPLETED,
    "done": AgentState.COMPLETED,
    "failed": AgentState.FAILED,
    "error": AgentState.FAILED,
    "cancelled": AgentState.CANCELLED,
    "canceled": AgentState.CANCELLED,
}


class GatewayBridge(RuntimeBridge):
    """Bridge that dispatches agent work through an API gateway.

    The gateway is expected to expose:
    - ``POST /v1/tasks``  — submit a task payload
    - ``GET  /v1/tasks/{id}`` — poll task status
    - ``POST /v1/tasks/{id}/cancel`` — cancel a running task
    - ``GET  /v1/tasks/{id}/logs`` — fetch captured output

    The ``extra`` field in :class:`BridgeConfig` may carry:
    - ``fallback_model``: model to request on retry after provider-unavailable
    - ``capability_hints``: list of capability tags the gateway should match
    """

    def __init__(
        self,
        config: BridgeConfig,
        *,
        credential_pool: CredentialPool | None = None,
        **_kw: Any,
    ) -> None:
        super().__init__(config)
        if not config.endpoint:
            raise BridgeError("GatewayBridge requires an endpoint", agent_id="", status_code=0)
        self._credential_pool = credential_pool
        self._client = httpx.AsyncClient(
            base_url=config.endpoint.rstrip("/"),
            timeout=config.timeout_seconds,
            headers=self._build_headers(config),
        )
        self._routing_cache: dict[str, GatewayRoutingMetadata] = {}

    @staticmethod
    def _build_headers(config: BridgeConfig) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        return headers

    def _acquire_auth_header(self) -> tuple[str | None, str | None]:
        """Return (key_id, bearer_token) from the credential pool or config."""
        if self._credential_pool is not None:
            try:
                entry = self._credential_pool.acquire()
                return entry.key_id, entry.secret_ref
            except NoAvailableCredentialError:
                logger.warning("credential pool exhausted, falling back to config api_key")
        return None, self._config.api_key or None

    def _release_credential(self, key_id: str | None, *, rate_limited: bool = False) -> None:
        if key_id is not None and self._credential_pool is not None:
            self._credential_pool.release(key_id, rate_limited=rate_limited)

    async def spawn(self, request: SpawnRequest) -> AgentStatus:
        payload = {
            "agent_id": request.agent_id,
            "prompt": request.prompt,
            "model": request.model or self._config.extra.get("default_model", ""),
            "effort": request.effort,
            "role": request.role,
            "timeout_seconds": request.timeout_seconds,
            "labels": request.labels,
            "capability_hints": self._config.extra.get("capability_hints", []),
            "env": request.env,
        }
        cred_key_id, bearer = self._acquire_auth_header()
        extra_headers: dict[str, str] = {}
        if bearer:
            extra_headers["Authorization"] = f"Bearer {bearer}"
        try:
            resp = await self._client.post(
                "/v1/tasks",
                json=payload,
                headers=extra_headers if extra_headers else None,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                self._release_credential(cred_key_id, rate_limited=True)
                raise BridgeError(
                    f"gateway rate limited: {exc}",
                    agent_id=request.agent_id,
                    status_code=429,
                ) from exc
            if exc.response.status_code == 503:
                fallback = self._config.extra.get("fallback_model")
                if fallback and payload.get("model") != fallback:
                    logger.info(
                        "gateway returned 503 for %s, retrying with fallback %s",
                        request.agent_id,
                        fallback,
                    )
                    payload["model"] = fallback
                    resp = await self._client.post(
                        "/v1/tasks",
                        json=payload,
                        headers=extra_headers if extra_headers else None,
                    )
                    resp.raise_for_status()
                else:
                    self._release_credential(cred_key_id)
                    raise BridgeError(
                        f"gateway unavailable: {exc}",
                        agent_id=request.agent_id,
                        status_code=exc.response.status_code,
                    ) from exc
            else:
                self._release_credential(cred_key_id)
                raise BridgeError(
                    f"gateway spawn failed: {exc}",
                    agent_id=request.agent_id,
                    status_code=exc.response.status_code,
                ) from exc
        except httpx.HTTPError as exc:
            self._release_credential(cred_key_id)
            raise BridgeError(
                f"gateway connection error: {exc}",
                agent_id=request.agent_id,
                status_code=0,
            ) from exc

        self._release_credential(cred_key_id)
        data = resp.json()
        self._store_routing_metadata(request.agent_id, data)
        return self._parse_status(request.agent_id, data)

    async def status(self, agent_id: str) -> AgentStatus:
        try:
            resp = await self._client.get(f"/v1/tasks/{agent_id}")
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise BridgeError(
                f"gateway status failed: {exc}",
                agent_id=agent_id,
                status_code=exc.response.status_code,
            ) from exc
        except httpx.HTTPError as exc:
            raise BridgeError(
                f"gateway connection error: {exc}",
                agent_id=agent_id,
                status_code=0,
            ) from exc

        data = resp.json()
        self._store_routing_metadata(agent_id, data)
        return self._parse_status(agent_id, data)

    async def cancel(self, agent_id: str) -> None:
        try:
            resp = await self._client.post(f"/v1/tasks/{agent_id}/cancel")
            if resp.status_code == 404:
                return
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise BridgeError(
                f"gateway cancel failed: {exc}",
                agent_id=agent_id,
                status_code=getattr(getattr(exc, "response", None), "status_code", 0),
            ) from exc

    async def logs(self, agent_id: str, *, tail: int | None = None) -> bytes:
        params: dict[str, Any] = {}
        if tail is not None:
            params["tail"] = tail
        try:
            resp = await self._client.get(f"/v1/tasks/{agent_id}/logs", params=params)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise BridgeError(
                f"gateway logs failed: {exc}",
                agent_id=agent_id,
                status_code=getattr(getattr(exc, "response", None), "status_code", 0),
            ) from exc
        return resp.content

    def name(self) -> str:
        return "gateway"

    def routing_metadata(self, agent_id: str) -> GatewayRoutingMetadata | None:
        """Return cached routing metadata for a completed task."""
        return self._routing_cache.get(agent_id)

    def _store_routing_metadata(self, agent_id: str, data: dict[str, Any]) -> None:
        routing = data.get("routing") or data.get("metadata") or {}
        if not routing:
            return
        self._routing_cache[agent_id] = GatewayRoutingMetadata(
            provider=routing.get("provider", ""),
            model=routing.get("model", ""),
            cost_usd=float(routing.get("cost_usd", 0.0)),
            latency_ms=float(routing.get("latency_ms", 0.0)),
            fallback_used=bool(routing.get("fallback_used", False)),
            cache_hit=bool(routing.get("cache_hit", False)),
            extra={
                k: v
                for k, v in routing.items()
                if k not in ("provider", "model", "cost_usd", "latency_ms", "fallback_used", "cache_hit")
            },
        )

    @staticmethod
    def _parse_status(agent_id: str, data: dict[str, Any]) -> AgentStatus:
        raw_state = str(data.get("status", data.get("state", "pending"))).lower()
        state = _GATEWAY_STATE_MAP.get(raw_state, AgentState.PENDING)
        return AgentStatus(
            agent_id=agent_id,
            state=state,
            exit_code=data.get("exit_code"),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            message=data.get("message", ""),
            metadata={k: str(v) for k, v in (data.get("routing") or {}).items()},
        )


_BRIDGE_CLASS = GatewayBridge
