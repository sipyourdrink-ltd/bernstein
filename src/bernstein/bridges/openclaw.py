"""OpenClaw Gateway-backed RuntimeBridge implementation."""

from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING, Any, cast

from bernstein.bridges.base import (
    AgentState,
    AgentStatus,
    BridgeConfig,
    BridgeError,
    RuntimeBridge,
    SpawnRequest,
)
from bernstein.bridges.openclaw_gateway import GatewayWaitResult, OpenClawGatewayClient
from bernstein.bridges.openclaw_state import OpenClawRunRecord, OpenClawRunStore

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_SAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9_.:-]+")
_TERMINAL_STATES = frozenset({AgentState.COMPLETED, AgentState.FAILED, AgentState.CANCELLED})


def _sanitize_segment(raw: str) -> str:
    """Return a session-key-safe identifier fragment."""
    cleaned = _SAFE_SEGMENT_RE.sub("-", raw).strip("-")
    return cleaned or "run"


class OpenClawBridge(RuntimeBridge):
    """Runtime bridge that drives OpenClaw agent turns over Gateway WS.

    This bridge is intentionally scoped to ``shared_workspace`` deployments:
    OpenClaw executes against the same repo/shared filesystem Bernstein later
    verifies and merges. No remote artifact sync or diff import is attempted.
    """

    def __init__(self, config: BridgeConfig, *, workdir: Path) -> None:
        """Initialise the OpenClaw Gateway bridge.

        Args:
            config: Runtime bridge configuration with OpenClaw-specific extras.
            workdir: Bernstein workspace root used for durable bridge state.

        Raises:
            BridgeError: If the config is malformed for the OpenClaw bridge.
        """
        if config.bridge_type != "openclaw":
            raise BridgeError(f"OpenClawBridge requires bridge_type='openclaw', got {config.bridge_type!r}")
        if not config.endpoint:
            raise BridgeError("OpenClawBridge requires a non-empty Gateway WebSocket endpoint")
        if not config.api_key:
            raise BridgeError("OpenClawBridge requires a non-empty API key")
        extra = config.extra
        agent_id = extra.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise BridgeError("OpenClawBridge requires extra.agent_id")
        workspace_mode = extra.get("workspace_mode", "shared_workspace")
        if workspace_mode != "shared_workspace":
            raise BridgeError("OpenClawBridge supports workspace_mode='shared_workspace' only")

        super().__init__(config)
        self._workdir = workdir
        self._agent_id = agent_id.strip()
        self._session_prefix = str(extra.get("session_prefix", "bernstein-")).strip() or "bernstein-"
        self._model_override = self._optional_str(extra.get("model_override"))
        self._run_store = OpenClawRunStore(workdir)
        self._gateway = OpenClawGatewayClient(
            url=config.endpoint,
            api_key=config.api_key,
            connect_timeout_s=float(extra.get("connect_timeout_s", config.timeout_seconds)),
            request_timeout_s=float(extra.get("request_timeout_s", config.timeout_seconds)),
            identity_dir=self._run_store.identity_dir,
        )
        logger.info("OpenClawBridge initialised endpoint=%s agent=%s", config.endpoint, self._agent_id)

    def name(self) -> str:
        """Return the runtime bridge identifier."""
        return "openclaw"

    async def spawn(self, request: SpawnRequest) -> AgentStatus:
        """Submit a Bernstein task batch as an OpenClaw agent turn."""
        if not request.prompt.strip():
            raise BridgeError("OpenClawBridge requires SpawnRequest.prompt")

        session_key = self._derive_session_key(request)
        log_path = str(self._run_store.log_path(request.agent_id, request.log_path))
        pending = OpenClawRunRecord(
            agent_id=request.agent_id,
            session_key=session_key,
            state=AgentState.PENDING,
            gateway_url=self.config.endpoint,
            log_path=log_path,
            message="Awaiting OpenClaw acceptance",
        )
        self._run_store.save(pending)
        try:
            accepted = await self._gateway.submit_agent_run(
                session_key=session_key,
                agent_id=self._agent_id,
                message=request.prompt,
                timeout_seconds=request.timeout_seconds,
                thinking=self._thinking_for_request(request),
                model=self._model_override or request.model or None,
                metadata=self._metadata_for_request(request),
            )
        except Exception:
            self._run_store.delete(request.agent_id)
            raise

        running = OpenClawRunRecord(
            agent_id=request.agent_id,
            session_key=accepted.session_key,
            run_id=accepted.run_id,
            state=AgentState.RUNNING,
            gateway_url=self.config.endpoint,
            log_path=log_path,
            accepted_at=accepted.accepted_at,
            started_at=accepted.accepted_at,
            message="Accepted by OpenClaw gateway",
        )
        self._run_store.save(running)
        return self._to_agent_status(running)

    async def status(self, agent_id: str) -> AgentStatus:
        """Return the current Bernstein view of a remote OpenClaw run."""
        record = self._require_record(agent_id)
        if record.state in _TERMINAL_STATES and record.transcript_synced:
            return self._to_agent_status(record)
        if record.state == AgentState.CANCELLED:
            return self._to_agent_status(record)
        if not record.run_id:
            return self._to_agent_status(record)

        wait_result = await self._gateway.wait_for_run(
            session_key=record.session_key,
            run_id=record.run_id,
            timeout_ms=200,
        )
        updated = self._apply_wait_result(record, wait_result)
        if updated.state in _TERMINAL_STATES and not updated.transcript_synced:
            updated = await self._sync_transcript(updated)
        return self._to_agent_status(updated)

    async def cancel(self, agent_id: str) -> None:
        """Best-effort abort for an active OpenClaw run."""
        record = self._require_record(agent_id)
        if record.state in _TERMINAL_STATES:
            return
        if record.run_id is not None:
            await self._gateway.abort_run(session_key=record.session_key, run_id=record.run_id)
        self._run_store.update(
            agent_id,
            state=AgentState.CANCELLED,
            cancelled_at=time.time(),
            finished_at=time.time(),
            exit_code=130,
            message="Cancellation requested",
        )

    async def logs(self, agent_id: str, *, tail: int | None = None) -> bytes:
        """Return Bernstein-captured transcript bytes for a run."""
        record = self._require_record(agent_id)
        if record.state in _TERMINAL_STATES and not record.transcript_synced:
            await self._sync_transcript(record)
        return self._run_store.read_logs(agent_id, max_bytes=self.config.max_log_bytes, tail=tail)

    def _derive_session_key(self, request: SpawnRequest) -> str:
        """Build a deterministic, Bernstein-owned OpenClaw session key."""
        explicit = request.labels.get("openclaw.session_key", "")
        if explicit:
            return explicit
        suffix = _sanitize_segment(f"{self._session_prefix}{request.agent_id}")
        return f"agent:{self._agent_id}:{suffix}"

    def _thinking_for_request(self, request: SpawnRequest) -> str:
        """Map Bernstein effort labels onto OpenClaw thinking levels."""
        effort = request.effort.strip().lower()
        if effort in {"off", "minimal", "low", "medium", "high", "xhigh"}:
            return effort
        if effort == "max":
            return "xhigh"
        return "high"

    def _metadata_for_request(self, request: SpawnRequest) -> dict[str, str]:
        """Build auditable metadata for the remote run request."""
        metadata = dict(request.labels)
        metadata.setdefault("bernstein.agent_id", request.agent_id)
        metadata.setdefault("bernstein.role", request.role)
        metadata.setdefault("bernstein.workdir", request.workdir)
        if request.model:
            metadata.setdefault("bernstein.model", request.model)
        if request.effort:
            metadata.setdefault("bernstein.effort", request.effort)
        return metadata

    def _apply_wait_result(self, record: OpenClawRunRecord, result: GatewayWaitResult) -> OpenClawRunRecord:
        """Persist one ``agent.wait`` result and map it onto bridge state."""
        if result.status == "timeout":
            return self._run_store.update(
                record.agent_id,
                state=AgentState.RUNNING,
                started_at=result.started_at or record.started_at,
                message=record.message or "Run accepted by OpenClaw",
            )

        if result.status == "ok":
            return self._run_store.update(
                record.agent_id,
                state=AgentState.COMPLETED,
                started_at=result.started_at or record.started_at,
                finished_at=result.ended_at or time.time(),
                exit_code=0,
                message="OpenClaw run completed",
            )

        error_text = result.error or f"OpenClaw run ended with status={result.status}"
        is_cancelled = "abort" in error_text.lower() or "cancel" in error_text.lower()
        state = AgentState.CANCELLED if is_cancelled else AgentState.FAILED
        exit_code = 130 if state == AgentState.CANCELLED else 1
        return self._run_store.update(
            record.agent_id,
            state=state,
            started_at=result.started_at or record.started_at,
            finished_at=result.ended_at or time.time(),
            exit_code=exit_code,
            message=error_text,
        )

    async def _sync_transcript(self, record: OpenClawRunRecord) -> OpenClawRunRecord:
        """Fetch final transcript history into Bernstein's local log path."""
        try:
            history = await self._gateway.fetch_history(
                session_key=record.session_key,
                max_chars=self.config.max_log_bytes * 2,
            )
        except Exception as exc:
            logger.warning("Failed to fetch OpenClaw transcript for %s: %s", record.agent_id, exc)
            return record
        transcript = self._render_history(history)
        if transcript:
            log_path = self._run_store.append_log(record.agent_id, transcript, preferred_path=record.log_path)
            return self._run_store.update(record.agent_id, transcript_synced=True, log_path=str(log_path))
        return self._run_store.update(record.agent_id, transcript_synced=True)

    def _render_history(self, history: list[dict[str, Any]]) -> str:
        """Render gateway transcript history into a deterministic log format."""
        lines: list[str] = []
        for item in history:
            role = item.get("role")
            role_text = str(role) if role else str(item.get("type", "event"))
            text = self._extract_text(item)
            if not text:
                continue
            lines.append(f"[{role_text}] {text}")
        return "\n".join(lines) + ("\n" if lines else "")

    @staticmethod
    def _extract_content_list(content: list[object]) -> str:
        """Join text from a list of content blocks."""
        chunks: list[str] = []
        for block_raw in content:
            if isinstance(block_raw, str) and block_raw.strip():
                chunks.append(block_raw.strip())
            elif isinstance(block_raw, dict):
                block = cast("dict[str, object]", block_raw)
                block_text = block.get("text") or block.get("content")
                if isinstance(block_text, str) and block_text.strip():
                    chunks.append(block_text.strip())
        return "\n".join(chunks) if chunks else ""

    def _extract_text(self, item: dict[str, Any]) -> str:
        """Extract text content from a gateway transcript item."""
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
        content = item.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            result = self._extract_content_list(cast("list[object]", content))
            if result:
                return result
        payload = item.get("payload")
        if isinstance(payload, dict):
            payload_dict = cast("dict[str, object]", payload)
            payload_text = payload_dict.get("text") or payload_dict.get("message")
            if isinstance(payload_text, str) and payload_text.strip():
                return payload_text.strip()
        return ""

    def _to_agent_status(self, record: OpenClawRunRecord) -> AgentStatus:
        """Convert a stored run record into the RuntimeBridge status shape."""
        return AgentStatus(
            agent_id=record.agent_id,
            state=record.state,
            exit_code=record.exit_code,
            started_at=record.started_at or record.accepted_at,
            finished_at=record.finished_at,
            message=record.message,
            metadata={
                "session_key": record.session_key,
                "run_id": record.run_id or "",
            },
        )

    def _require_record(self, agent_id: str) -> OpenClawRunRecord:
        """Load a persisted run record or raise a bridge error."""
        record = self._run_store.load(agent_id)
        if record is None:
            raise BridgeError(f"Unknown OpenClaw run {agent_id}", agent_id=agent_id)
        return record

    def _optional_str(self, raw: object) -> str | None:
        """Normalize optional string config values."""
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        return None


_BRIDGE_CLASS = OpenClawBridge
