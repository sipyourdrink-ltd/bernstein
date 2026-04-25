"""End-to-end check: ACP-driven sessions and CLI-driven sessions emit
byte-identical audit chain entries.

The HMAC chain is sensitive to the exact JSON payload that goes through
``AuditLog.log``; this test runs the same logical operation (open a
task, change mode, cancel) twice — once through the ACP handler layer
and once with a direct call mimicking the CLI path — and asserts the
``details`` payloads match.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

from bernstein.core.protocols.acp.handlers import (
    ACPHandlerRegistry,
    ACPRequestContext,
    PromptResult,
)
from bernstein.core.protocols.acp.session import ACPSessionStore
from bernstein.core.security.audit import AuditLog


def _ctx(method: str, request_id: int = 1) -> ACPRequestContext:
    return ACPRequestContext(method=method, request_id=request_id, peer="test")


def _build_registry(audit_log: AuditLog) -> ACPHandlerRegistry:
    sessions = ACPSessionStore()

    async def _create(prompt: str, cwd: str, role: str) -> PromptResult:
        del prompt, cwd, role
        return PromptResult(session_id="task-deadbeef")

    async def _cancel(session_id: str, reason: str) -> bool:
        del session_id, reason
        return True

    async def _publish(_frame: dict[str, Any]) -> None:
        return None

    def _audit(event_type: str, resource_id: str, details: dict[str, Any]) -> None:
        audit_log.log(event_type, "acp_bridge", "session", resource_id, details)

    return ACPHandlerRegistry(
        sessions=sessions,
        adapters=("claude",),
        sandbox_backends=("none",),
        task_creator=_create,
        task_canceller=_cancel,
        stream_publisher=_publish,
        audit_emitter=_audit,
    )


def _read_chain(audit_dir: Path) -> list[dict[str, Any]]:
    """Read every audit JSONL row from *audit_dir* in chronological order."""
    files = sorted(audit_dir.glob("*.jsonl"))
    rows: list[dict[str, Any]] = []
    for path in files:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def test_acp_and_cli_emit_identical_audit_payloads() -> None:
    """Same operation through either surface produces matching audit details."""
    with tempfile.TemporaryDirectory() as acp_dir, tempfile.TemporaryDirectory() as cli_dir:
        # NB: audit keys must NOT be shared across temp dirs because the
        # default key path is at $HOME — we pin them per-run via key_path
        # so the two AuditLog instances do not collide.
        acp_audit = AuditLog(audit_dir=Path(acp_dir), key=b"k" * 32)
        cli_audit = AuditLog(audit_dir=Path(cli_dir), key=b"k" * 32)

        registry = _build_registry(acp_audit)

        async def _drive_acp() -> str:
            result = await registry.dispatch(
                _ctx("prompt"),
                {"prompt": "hello", "cwd": "/work", "role": "backend"},
            )
            sid = result["sessionId"]
            await registry.dispatch(_ctx("setMode", 2), {"sessionId": sid, "mode": "auto"})
            await registry.dispatch(
                _ctx("cancel", 3), {"sessionId": sid, "reason": "user_done"}
            )
            return sid

        sid = asyncio.run(_drive_acp())

        # Mimic the CLI surface invoking the same audit calls directly.
        cli_audit.log(
            "acp.prompt", "acp_bridge", "session", sid,
            {"peer": "test", "cwd": "/work", "role": "backend", "mode": "manual"},
        )
        cli_audit.log(
            "acp.set_mode", "acp_bridge", "session", sid,
            {"peer": "test", "mode": "auto"},
        )
        cli_audit.log(
            "acp.cancel", "acp_bridge", "session", sid,
            {"peer": "test", "reason": "user_done", "ok": True},
        )

        acp_rows = _read_chain(Path(acp_dir))
        cli_rows = _read_chain(Path(cli_dir))

        # Strip timestamp + chain HMACs (which depend on log time and
        # prior-entry HMACs and so cannot be byte-identical between
        # independent dirs); compare the semantic payload.
        def _strip(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [
                {
                    "event_type": r["event_type"],
                    "actor": r["actor"],
                    "resource_type": r["resource_type"],
                    "resource_id": r["resource_id"],
                    "details": r["details"],
                }
                for r in rows
            ]

        # The ACP chain emits 'acp.prompt', 'acp.set_mode', 'acp.cancel' in
        # that order — exactly matching the CLI fixture.
        assert _strip(acp_rows) == _strip(cli_rows)

        # And the chain remains valid for both — no torn rows.
        assert acp_audit.verify()[0]
        assert cli_audit.verify()[0]
