"""Durable local state for OpenClaw-backed agent runs."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, cast

from bernstein.bridges.base import AgentState, BridgeError


@dataclass(frozen=True)
class OpenClawRunRecord:
    """Bernstein-owned snapshot of a remote OpenClaw run.

    Attributes:
        agent_id: Bernstein session identifier.
        session_key: OpenClaw session key used for the run.
        run_id: Gateway-assigned run identifier once accepted.
        state: Current bridge lifecycle state.
        gateway_url: Gateway endpoint that owns the run.
        log_path: Bernstein-side transcript capture path.
        accepted_at: Unix timestamp when the gateway accepted the run.
        started_at: Unix timestamp when the run started remotely.
        finished_at: Unix timestamp when the run finished remotely.
        cancelled_at: Unix timestamp when Bernstein requested cancellation.
        exit_code: Synthetic exit code (0 success, 1 remote failure, 130 cancelled).
        message: Human-readable status detail.
        transcript_synced: Whether the final transcript was fetched locally.
        last_update: Unix timestamp of the last local record update.
    """

    agent_id: str
    session_key: str
    run_id: str | None = None
    state: AgentState = AgentState.PENDING
    gateway_url: str = ""
    log_path: str = ""
    accepted_at: float | None = None
    started_at: float | None = None
    finished_at: float | None = None
    cancelled_at: float | None = None
    exit_code: int | None = None
    message: str = ""
    transcript_synced: bool = False
    last_update: float = field(default_factory=time.time)


class OpenClawRunStore:
    """Persist bridge state so remote sessions survive Bernstein restarts."""

    def __init__(self, workdir: Path) -> None:
        self._root = workdir / ".sdd" / "runtime" / "openclaw"
        self._runs_dir = self._root / "runs"
        self._identity_dir = self._root / "identity"
        self._runs_dir.mkdir(parents=True, exist_ok=True)
        self._identity_dir.mkdir(parents=True, exist_ok=True)

    @property
    def identity_dir(self) -> Path:
        """Return the device-identity storage directory."""
        return self._identity_dir

    def run_path(self, agent_id: str) -> Path:
        """Return the state-file path for a Bernstein agent session."""
        return self._runs_dir / f"{agent_id}.json"

    def log_path(self, agent_id: str, preferred_path: str = "") -> Path:
        """Return the Bernstein-side transcript path for a run."""
        if preferred_path:
            return Path(preferred_path)
        logs_dir = self._root / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        return logs_dir / f"{agent_id}.log"

    def load(self, agent_id: str) -> OpenClawRunRecord | None:
        """Load a stored run record if it exists."""
        path = self.run_path(agent_id)
        if not path.exists():
            return None
        try:
            data_raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BridgeError(f"Cannot read OpenClaw run state for {agent_id}: {exc}", agent_id=agent_id) from exc
        if not isinstance(data_raw, dict):
            raise BridgeError(f"Malformed OpenClaw run state for {agent_id}", agent_id=agent_id)
        data = cast("dict[str, object]", data_raw)
        state_raw = data.get("state", AgentState.PENDING.value)
        try:
            state = AgentState(str(state_raw))
        except ValueError as exc:
            raise BridgeError(f"Unknown OpenClaw run state {state_raw!r}", agent_id=agent_id) from exc
        run_id_raw = data.get("run_id")
        session_key_raw = data.get("session_key", "")
        gateway_url_raw = data.get("gateway_url", "")
        log_path_raw = data.get("log_path", "")
        accepted_at_raw = data.get("accepted_at")
        started_at_raw = data.get("started_at")
        finished_at_raw = data.get("finished_at")
        cancelled_at_raw = data.get("cancelled_at")
        exit_code_raw = data.get("exit_code")
        message_raw = data.get("message", "")
        transcript_synced_raw = data.get("transcript_synced", False)
        last_update_raw = data.get("last_update", time.time())
        return OpenClawRunRecord(
            agent_id=str(data.get("agent_id", agent_id)),
            session_key=str(session_key_raw),
            run_id=str(run_id_raw) if isinstance(run_id_raw, str) else None,
            state=state,
            gateway_url=str(gateway_url_raw),
            log_path=str(log_path_raw),
            accepted_at=float(accepted_at_raw) if isinstance(accepted_at_raw, (int, float)) else None,
            started_at=float(started_at_raw) if isinstance(started_at_raw, (int, float)) else None,
            finished_at=float(finished_at_raw) if isinstance(finished_at_raw, (int, float)) else None,
            cancelled_at=float(cancelled_at_raw) if isinstance(cancelled_at_raw, (int, float)) else None,
            exit_code=int(exit_code_raw) if isinstance(exit_code_raw, int) else None,
            message=str(message_raw),
            transcript_synced=bool(transcript_synced_raw),
            last_update=float(last_update_raw) if isinstance(last_update_raw, (int, float)) else time.time(),
        )

    def save(self, record: OpenClawRunRecord) -> None:
        """Persist a run record atomically."""
        path = self.run_path(record.agent_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(record)
        payload["state"] = record.state.value
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(path)

    def delete(self, agent_id: str) -> None:
        """Delete a run record after a pre-accept failure."""
        self.run_path(agent_id).unlink(missing_ok=True)

    def update(self, agent_id: str, **changes: Any) -> OpenClawRunRecord:
        """Load, mutate, and persist a run record."""
        record = self.load(agent_id)
        if record is None:
            raise BridgeError(f"Unknown OpenClaw run {agent_id}", agent_id=agent_id)
        updated = replace(record, last_update=time.time(), **changes)
        self.save(updated)
        return updated

    def append_log(self, agent_id: str, content: str, *, preferred_path: str = "") -> Path:
        """Append transcript content to the Bernstein-side log file."""
        path = self.log_path(agent_id, preferred_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(content)
        return path

    def read_logs(self, agent_id: str, *, max_bytes: int, tail: int | None = None) -> bytes:
        """Return locally captured transcript bytes for a run."""
        record = self.load(agent_id)
        if record is None:
            raise BridgeError(f"Unknown OpenClaw run {agent_id}", agent_id=agent_id)
        path = self.log_path(agent_id, record.log_path)
        if not path.exists():
            return b""
        data = path.read_bytes()
        if tail is not None:
            lines = data.splitlines()
            return b"\n".join(lines[-tail:])
        return data[-max_bytes:]
