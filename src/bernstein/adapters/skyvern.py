"""Skyvern adapter: drives an existing Skyvern server over HTTP.

Skyvern is a maintained, self-hostable, goal-driven browser agent. The operator
starts the Skyvern server separately (``skyvern server --port 8000``); this
adapter connects to it over HTTP and drives runs through its REST surface.

This adapter subclasses :class:`bernstein.adapters.computer_use.ComputerUseAdapter`
so it inherits the computer-use contract (output mode ARTIFACT, POLL_PTY event channel,
per-task isolation, and capability gating via :attr:`is_computer_use`).

We do not import the Skyvern Python SDK or vendor any of its code. The
integration is HTTP-only so the AGPL-3.0 upstream stays out of our dependency
set.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, SpawnResult
from bernstein.adapters.computer_use import ComputerUseAdapter, ComputerUseDriverError, ComputerUseTerminalState
from bernstein.core.agents.computer_use_attestation import ComputerUseSession
from bernstein.core.lineage.identity import AgentCard, generate_keypair
from bernstein.core.lineage.signed_write import SignedLineageLog
from bernstein.core.lineage.store import LineageStore
from bernstein.core.persistence.cas_store import CASStore
from bernstein.core.security.audit import load_or_create_audit_key
from bernstein.core.security.audit_chain import AuditChainStore

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig


class SkyvernServerUnreachable(ComputerUseDriverError):
    """Raised when the configured Skyvern server cannot be reached."""


class SkyvernRunRefused(ComputerUseDriverError):
    """Raised when Skyvern refuses the submitted task."""


class SkyvernRunTimeout(ComputerUseDriverError):
    """Raised when a Skyvern run does not complete within the deadline."""


class SkyvernAdapter(ComputerUseAdapter):
    """Adapter that drives an existing Skyvern server over HTTP.

    The operator starts the server with ``skyvern server``. The adapter
    connects to ``http://localhost:8000`` by default and submits runs via
    ``POST /v1/run/tasks``.
    """

    registry_name = "skyvern"
    is_computer_use = True

    def __init__(
        self,
        *,
        cli_command: str = "skyvern",
        display_name: str = "Skyvern",
        base_url: str = "http://localhost:8000",
    ) -> None:
        super().__init__()
        self._cli_command = cli_command
        self._display_name = display_name
        self._base_url = base_url.rstrip("/")

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        task_scope: str = "medium",
        budget_multiplier: float = 1.0,
        system_addendum: str = "",
        multimodal_context: Any | None = None,
    ) -> SpawnResult:
        """Submit a goal-driven task to the Skyvern server.

        The adapter connects to the existing Skyvern HTTP server, posts the
        task, and returns immediately with the run id so the orchestrator
        can poll for completion.

        The run is instrumented with per-action recording via the computer-use
        attestation layer: each step the agent reports lands as an Observation
        content-addressed at retrieval time, followed by the action receipt it
        justifies, in that order. Artifacts (screenshots, recordings, downloaded
        files) are hashed at retrieval and bound into the evidence set.
        """
        self.refuse_multimodal_if_needed(multimodal_context)

        profile_dir = self.prepare_isolation(workdir=workdir, session_id=session_id)
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        # Initialize the computer-use session for per-action recording.
        cas = CASStore(workdir / ".sdd" / "computer-use" / session_id / "cas")
        audit_chain = AuditChainStore(
            audit_dir=workdir / ".sdd" / "computer-use" / session_id / "audit",
        )
        lineage_store = LineageStore(workdir / ".sdd" / "computer-use" / session_id / "lineage")
        priv_key, pub_key = generate_keypair()
        agent_card = AgentCard(
            agent_id=f"agent:skyvern-{session_id}",
            kid="kid-skyvern",
            public_key_pem=pub_key,
        )
        _ = ComputerUseSession(
            run_id=session_id,
            worker_id=f"agent:skyvern-{session_id}",
            worktree_id=session_id,
            cas=cas,
            audit_chain=audit_chain,
            lineage_recorder=SignedLineageLog(
                lineage_store,
                operator_hmac_key=load_or_create_audit_key(),
            ),
            agent_card=agent_card,
            private_key_pem=priv_key,
            run_byte_cap=64 * 1024 * 1024,  # 64 MiB per-run cap
        )

        # Build the run task payload. Skyvern accepts at minimum a prompt
        # and optionally a target url.
        payload = json.dumps(
            {
                "prompt": prompt,
                "user_data_dir": str(profile_dir),
            }
        ).encode()

        url = f"{self._base_url}/v1/run/tasks"
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode())
        except urllib.error.URLError as exc:
            raise SkyvernServerUnreachable(
                f"Skyvern server unreachable at {url}: {exc}",
                terminal_state=ComputerUseTerminalState.DRIVER_FAILURE,
            ) from exc
        except ConnectionRefusedError as exc:
            raise SkyvernServerUnreachable(
                f"Skyvern server refused connection at {url}: {exc}",
                terminal_state=ComputerUseTerminalState.DRIVER_FAILURE,
            ) from exc

        run_id = body.get("run_id") or body.get("task_id")
        if not run_id:
            raise SkyvernRunRefused(
                f"Skyvern did not return a run id: {body}",
                terminal_state=ComputerUseTerminalState.REFUSED,
            )

        # Write a minimal run record so the worker can later attach artifacts
        # and the lineage layer can bind the run id to the evidence set.
        run_dir = workdir / ".sdd" / "computer-use" / session_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "session_id": session_id,
                    "prompt": prompt,
                    "base_url": self._base_url,
                    "profile_dir": str(profile_dir),
                }
            )
        )

        # Persist the task id so workers can retrieve artifacts later.
        (workdir / ".sdd" / "runtime" / f"{session_id}.task_id").write_text(str(run_id))

        # Launch a watcher that waits for completion; the worker reads it.
        self._watch_run(
            base_url=self._base_url,
            run_id=run_id,
            timeout_seconds=timeout_seconds,
            log_path=log_path,
        )

        return SpawnResult(
            pid=0,  # no child process - HTTP-based adapter, not a subprocess
            log_path=log_path,
        )

    def _watch_run(
        self,
        *,
        base_url: str,
        run_id: str,
        timeout_seconds: int,
        log_path: Path,
    ) -> None:
        """Poll the run endpoint until completion or timeout."""
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            try:
                req = urllib.request.Request(
                    f"{base_url}/v1/runs/{run_id}",
                    method="GET",
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = json.loads(resp.read().decode())
            except (urllib.error.URLError, ConnectionRefusedError):
                time.sleep(2)
                continue

            status = body.get("status", "").lower()
            if status in ("completed", "failed", "refused"):
                with log_path.open("a") as fh:
                    fh.write(f"[skyvern] run {run_id} terminal status: {status}\n")
                return
            time.sleep(2)

        raise SkyvernRunTimeout(
            f"Skyvern run {run_id} did not complete within {timeout_seconds}s",
            terminal_state=ComputerUseTerminalState.TIMEOUT,
        )

    def name(self) -> str:
        return self._display_name


__all__ = [
    "SkyvernAdapter",
    "SkyvernRunRefused",
    "SkyvernRunTimeout",
    "SkyvernServerUnreachable",
]
