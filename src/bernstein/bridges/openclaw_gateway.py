"""OpenClaw Gateway WebSocket transport for Bernstein runtime bridging."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sys
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from websockets.asyncio.client import ClientConnection, connect

from bernstein.bridges.base import BridgeError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

_PROTOCOL_VERSION = 3
_CONNECT_SCOPES = ("operator.read", "operator.write")
_KEYPAIR_FILE = "keypair.json"
_DEVICE_TOKEN_FILE = "device-token"


def _b64url_encode(raw: bytes) -> str:
    """Encode bytes using unpadded base64url."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(raw: str) -> bytes:
    """Decode unpadded base64url bytes."""
    padding = "=" * ((4 - len(raw) % 4) % 4)
    return base64.urlsafe_b64decode(raw + padding)


def _now_ms() -> int:
    """Return the current time in milliseconds."""
    return int(time.time() * 1000)


def _timestamp_to_seconds(raw: object) -> float | None:
    """Convert a gateway timestamp into Unix seconds.

    Args:
        raw: Gateway timestamp value, typically seconds, milliseconds, or ISO.

    Returns:
        Unix seconds when conversion is possible, otherwise None.
    """
    if isinstance(raw, (int, float)):
        value = float(raw)
        if value > 1_000_000_000_000:
            return value / 1000.0
        return value
    if isinstance(raw, str):
        try:
            value = float(raw)
        except ValueError:
            return None
        if value > 1_000_000_000_000:
            return value / 1000.0
        return value
    return None


@dataclass(frozen=True)
class GatewayAcceptedRun:
    """Accepted OpenClaw agent run returned by the gateway."""

    run_id: str
    session_key: str
    accepted_at: float


@dataclass(frozen=True)
class GatewayWaitResult:
    """Result of polling ``agent.wait``."""

    status: str
    started_at: float | None
    ended_at: float | None
    error: str = ""


@dataclass
class _DeviceIdentity:
    """Stable per-workspace OpenClaw device identity."""

    device_id: str
    public_key_b64url: str
    private_key: Ed25519PrivateKey


class _IdentityStore:
    """Persist the Ed25519 device identity used for Gateway auth."""

    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._dir.mkdir(parents=True, exist_ok=True)

    def load_or_generate(self) -> _DeviceIdentity:
        """Load the current identity or create a new one on first use."""
        keypair_path = self._dir / _KEYPAIR_FILE
        if keypair_path.exists():
            try:
                data_raw = json.loads(keypair_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise BridgeError(f"Cannot read OpenClaw identity store: {exc}") from exc
            if not isinstance(data_raw, dict):
                raise BridgeError("Malformed OpenClaw identity store")
            data = cast("dict[str, object]", data_raw)
            seed_raw = data.get("privateKey")
            public_raw = data.get("publicKey")
            if not isinstance(seed_raw, str) or not isinstance(public_raw, str):
                raise BridgeError("Malformed OpenClaw identity keypair payload")
            private_key = Ed25519PrivateKey.from_private_bytes(_b64url_decode(seed_raw))
            public_key = private_key.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
            device_id = hashlib.sha256(public_key).hexdigest()
            return _DeviceIdentity(
                device_id=device_id,
                public_key_b64url=_b64url_encode(public_key),
                private_key=private_key,
            )

        private_key = Ed25519PrivateKey.generate()
        private_seed = private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        public_key = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        device_id = hashlib.sha256(public_key).hexdigest()
        payload = {
            "deviceId": device_id,
            "publicKey": _b64url_encode(public_key),
            "privateKey": _b64url_encode(private_seed),
        }
        keypair_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return _DeviceIdentity(
            device_id=device_id,
            public_key_b64url=payload["publicKey"],
            private_key=private_key,
        )

    def load_device_token(self) -> str:
        """Return the cached device token, if any."""
        token_path = self._dir / _DEVICE_TOKEN_FILE
        if not token_path.exists():
            return ""
        try:
            return token_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def save_device_token(self, token: str) -> None:
        """Persist a gateway-issued device token."""
        token_path = self._dir / _DEVICE_TOKEN_FILE
        token_path.write_text(token.strip(), encoding="utf-8")


class OpenClawGatewayClient:
    """Thin, typed WebSocket RPC client for the OpenClaw Gateway protocol."""

    def __init__(
        self,
        *,
        url: str,
        api_key: str,
        connect_timeout_s: float,
        request_timeout_s: float,
        identity_dir: Path,
    ) -> None:
        self._url = url
        self._api_key = api_key
        self._connect_timeout_s = connect_timeout_s
        self._request_timeout_s = request_timeout_s
        self._identity_store = _IdentityStore(identity_dir)
        self._identity = self._identity_store.load_or_generate()

    async def submit_agent_run(
        self,
        *,
        session_key: str,
        agent_id: str,
        message: str,
        timeout_seconds: int,
        thinking: str,
        model: str | None,
        metadata: dict[str, str] | None = None,
    ) -> GatewayAcceptedRun:
        """Submit an ``agent`` RPC and return the accepted run metadata."""
        params: dict[str, object] = {
            "agentId": agent_id,
            "sessionKey": session_key,
            "message": message,
            "thinking": thinking,
            "timeoutSeconds": timeout_seconds,
            "deliver": False,
        }
        if model:
            params["model"] = model
        if metadata:
            params["metadata"] = metadata

        websocket = await self._open_connection()
        try:
            payload = await self._request(websocket, "agent", params)
        finally:
            await websocket.close()
        run_id = payload.get("runId")
        if not isinstance(run_id, str) or not run_id:
            raise BridgeError("OpenClaw agent response did not include runId")
        accepted_at = _timestamp_to_seconds(payload.get("acceptedAt")) or time.time()
        session_key_value = payload.get("sessionKey", session_key)
        if not isinstance(session_key_value, str) or not session_key_value:
            session_key_value = session_key
        return GatewayAcceptedRun(run_id=run_id, session_key=session_key_value, accepted_at=accepted_at)

    async def wait_for_run(
        self,
        *,
        session_key: str,
        run_id: str,
        timeout_ms: int,
    ) -> GatewayWaitResult:
        """Poll ``agent.wait`` without blocking the orchestrator for long."""
        params: dict[str, object] = {"sessionKey": session_key, "runId": run_id, "timeoutMs": timeout_ms}
        websocket = await self._open_connection()
        try:
            payload = await self._request(websocket, "agent.wait", params)
        finally:
            await websocket.close()
        status_raw = payload.get("status", "timeout")
        status_text = str(status_raw)
        error_text = self._format_error(payload.get("error"))
        return GatewayWaitResult(
            status=status_text,
            started_at=_timestamp_to_seconds(payload.get("startedAt")),
            ended_at=_timestamp_to_seconds(payload.get("endedAt")),
            error=error_text,
        )

    async def abort_run(self, *, session_key: str, run_id: str) -> None:
        """Attempt to abort an active run via ``chat.abort``."""
        params: dict[str, object] = {"sessionKey": session_key, "runId": run_id}
        websocket = await self._open_connection()
        try:
            await self._request(websocket, "chat.abort", params)
        finally:
            await websocket.close()

    async def fetch_history(self, *, session_key: str, max_chars: int) -> list[dict[str, Any]]:
        """Fetch transcript history for the session backing a run."""
        params: dict[str, object] = {"sessionKey": session_key, "maxChars": max_chars}
        websocket = await self._open_connection()
        try:
            payload = await self._request(websocket, "chat.history", params)
        finally:
            await websocket.close()
        candidates: object = payload.get("messages")
        if candidates is None:
            candidates = payload.get("history")
        if candidates is None:
            candidates = payload.get("items")
        if candidates is None:
            candidates = payload
        if not isinstance(candidates, list):
            return []
        result: list[dict[str, Any]] = []
        for item_raw in cast("list[object]", candidates):
            if isinstance(item_raw, dict):
                result.append(cast("dict[str, Any]", item_raw))
        return result

    async def _open_connection(self, *, _retry_with_device_token: bool = True) -> ClientConnection:
        """Open and authenticate a Gateway WebSocket connection."""
        try:
            websocket = await connect(
                self._url,
                open_timeout=self._connect_timeout_s,
                close_timeout=self._request_timeout_s,
                ping_interval=20,
                max_size=None,
            )
        except Exception as exc:
            raise BridgeError(f"OpenClaw gateway connect failed: {exc}") from exc
        try:
            await self._authenticate(websocket, use_device_token=False)
            return websocket
        except BridgeError as exc:
            if _retry_with_device_token and self._is_auth_token_mismatch(exc):
                await websocket.close()
                cached_device_token = self._identity_store.load_device_token()
                if cached_device_token:
                    try:
                        retry_socket = await connect(
                            self._url,
                            open_timeout=self._connect_timeout_s,
                            close_timeout=self._request_timeout_s,
                            ping_interval=20,
                            max_size=None,
                        )
                    except Exception as retry_exc:
                        raise BridgeError(f"OpenClaw gateway reconnect failed: {retry_exc}") from retry_exc
                    try:
                        await self._authenticate(retry_socket, use_device_token=True)
                        return retry_socket
                    except Exception:
                        await retry_socket.close()
                        raise
            await websocket.close()
            raise

    async def _authenticate(self, websocket: ClientConnection, *, use_device_token: bool) -> None:
        """Complete the Gateway ``connect`` handshake."""
        challenge = await self._recv_json(websocket)
        if challenge.get("type") != "event" or challenge.get("event") != "connect.challenge":
            raise BridgeError("OpenClaw gateway did not send connect.challenge")
        payload_raw = challenge.get("payload")
        if not isinstance(payload_raw, dict):
            raise BridgeError("OpenClaw connect.challenge payload missing")
        payload = cast("dict[str, object]", payload_raw)
        nonce = payload.get("nonce")
        if not isinstance(nonce, str) or not nonce:
            raise BridgeError("OpenClaw connect.challenge nonce missing")

        auth_token = self._identity_store.load_device_token() if use_device_token else self._api_key
        if not auth_token:
            raise BridgeError("OpenClaw gateway authentication token is empty")

        request_id = uuid.uuid4().hex
        signed_at = _now_ms()
        device = self._build_device_payload(nonce=nonce, token=auth_token, signed_at_ms=signed_at)
        client_mode = "operator"
        params = {
            "minProtocol": _PROTOCOL_VERSION,
            "maxProtocol": _PROTOCOL_VERSION,
            "client": {
                "id": "bernstein-bridge",
                "version": "1.0",
                "platform": self._platform_name(),
                "mode": client_mode,
            },
            "role": "operator",
            "scopes": list(_CONNECT_SCOPES),
            "caps": [],
            "commands": [],
            "permissions": {},
            "auth": {"token": auth_token},
            "locale": "en-US",
            "userAgent": "bernstein-openclaw-bridge/1.0",
            "device": device,
        }
        await websocket.send(
            json.dumps(
                {
                    "type": "req",
                    "id": request_id,
                    "method": "connect",
                    "params": params,
                }
            )
        )
        response = await self._await_response(websocket, request_id)
        payload_raw = response.get("payload")
        payload_dict = cast("dict[str, object]", payload_raw) if isinstance(payload_raw, dict) else {}
        auth_payload = payload_dict.get("auth")
        if isinstance(auth_payload, dict):
            auth_dict = cast("dict[str, object]", auth_payload)
            device_token = auth_dict.get("deviceToken")
            if isinstance(device_token, str) and device_token:
                self._identity_store.save_device_token(device_token)

    async def _request(self, websocket: ClientConnection, method: str, params: Mapping[str, object]) -> dict[str, Any]:
        """Send one RPC request and return the decoded payload."""
        request_id = uuid.uuid4().hex
        frame = {"type": "req", "id": request_id, "method": method, "params": params}
        await websocket.send(json.dumps(frame))
        response = await self._await_response(websocket, request_id)
        payload = response.get("payload")
        if not isinstance(payload, dict):
            return {}
        return cast("dict[str, Any]", payload)

    async def _await_response(self, websocket: ClientConnection, request_id: str) -> dict[str, object]:
        """Wait for the response matching a request id."""
        deadline = time.monotonic() + self._request_timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BridgeError("Timed out waiting for OpenClaw gateway response")
            frame = await self._recv_json(websocket, timeout_s=remaining)
            if frame.get("type") != "res" or frame.get("id") != request_id:
                continue
            if frame.get("ok") is True:
                return frame
            error = frame.get("error")
            raise self._rpc_error(error)

    async def _recv_json(self, websocket: ClientConnection, *, timeout_s: float | None = None) -> dict[str, object]:
        """Receive and decode one JSON frame from the gateway."""
        try:
            raw = await asyncio.wait_for(
                websocket.recv(),
                timeout=self._request_timeout_s if timeout_s is None else timeout_s,
            )
        except TimeoutError as exc:
            raise BridgeError("Timed out waiting for OpenClaw gateway frame") from exc
        except Exception as exc:
            raise BridgeError(f"OpenClaw gateway recv failed: {exc}") from exc
        if not isinstance(raw, str):
            raise BridgeError("OpenClaw gateway returned a non-text frame")
        try:
            data_raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BridgeError(f"OpenClaw gateway sent malformed JSON: {exc}") from exc
        if not isinstance(data_raw, dict):
            raise BridgeError("OpenClaw gateway frame must be a JSON object")
        return cast("dict[str, object]", data_raw)

    def _build_device_payload(self, *, nonce: str, token: str, signed_at_ms: int) -> dict[str, object]:
        """Build the v2 device-auth payload expected by the gateway."""
        scope_text = ",".join(_CONNECT_SCOPES)
        payload = (
            f"v2|{self._identity.device_id}|bernstein-bridge|operator|operator|{scope_text}|"
            f"{signed_at_ms}|{token}|{nonce}"
        )
        signature = self._identity.private_key.sign(payload.encode("utf-8"))
        return {
            "id": self._identity.device_id,
            "publicKey": self._identity.public_key_b64url,
            "signature": _b64url_encode(signature),
            "signedAt": signed_at_ms,
            "nonce": nonce,
        }

    def _rpc_error(self, raw: object) -> BridgeError:
        """Convert a gateway RPC error payload into a BridgeError."""
        if isinstance(raw, dict):
            error_dict = cast("dict[str, object]", raw)
            message_raw = error_dict.get("message") or error_dict.get("code") or "OpenClaw gateway request failed"
            message = str(message_raw)
            details = error_dict.get("details")
            detail_text = ""
            if isinstance(details, dict):
                detail_dict = cast("dict[str, object]", details)
                code = detail_dict.get("code")
                reason = detail_dict.get("reason")
                detail_text = " ".join(str(part) for part in (code, reason) if part)
            return BridgeError(
                f"{message}{f' ({detail_text})' if detail_text else ''}",
                status_code=None,
            )
        return BridgeError(str(raw) or "OpenClaw gateway request failed")

    def _format_error(self, raw: object) -> str:
        """Render a compact error string for ``agent.wait`` payloads."""
        if isinstance(raw, dict):
            error_dict = cast("dict[str, object]", raw)
            message = error_dict.get("message") or error_dict.get("code") or error_dict.get("reason")
            return str(message) if message else ""
        if raw is None:
            return ""
        return str(raw)

    def _is_auth_token_mismatch(self, exc: BridgeError) -> bool:
        """Return True when a connect failure recommends device-token retry."""
        text = str(exc)
        return "AUTH_TOKEN_MISMATCH" in text or "retry_with_device_token" in text

    def _platform_name(self) -> str:
        """Return a stable, protocol-friendly platform string."""
        if sys.platform.startswith("darwin"):
            return "macos"
        if sys.platform.startswith("linux"):
            return "linux"
        if sys.platform.startswith("win"):
            return "windows"
        return "unknown"
