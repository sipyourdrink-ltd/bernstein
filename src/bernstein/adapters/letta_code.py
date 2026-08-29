"""Letta Code CLI adapter."""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import threading
from typing import TYPE_CHECKING, Any

from bernstein.adapters._contract import (
    AdapterStrategy,
    DangerousModeStrategy,
    EventChannel,
    SessionState,
)
from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env
from bernstein.core.security.path_containment import slug_identifier

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig


logger = logging.getLogger(__name__)


class LettaCodeAdapter(CLIAdapter):
    """Spawn and monitor Letta Code CLI sessions.

    The CLI is invoked as ``letta --output-format stream-json``
    ``-p <prompt> [--permission-mode unrestricted]``
    ``--new-agent --conversation <derived_id>`` where ``-p`` runs
    a one-off prompt in headless mode and
    ``--permission-mode unrestricted`` is used when the strategy's
    dangerous_mode is CLI_FLAG. The binary ships as ``letta`` from the
    npm package ``@letta-ai/letta-code``.

    Letta Code's defining feature is *cross-task memory* persisted via
    Letta Cloud (``LETTA_API_KEY``) -- the agent maintains long-lived
    state across separate invocations. Bernstein wraps Letta Code as a
    leaf-node, one-shot agent: each task spawns a fresh ``letta -p``
    process and exits when the prompt completes. Bernstein does not
    coordinate Letta's cross-task memory, agent IDs, or memory blocks;
    that machinery still operates in Letta's own backend, but it is
    opaque to Bernstein's orchestrator. If you want Bernstein-level
    state to survive across tasks, use Bernstein's ``.sdd/`` files,
    not Letta's memory.

    Event channel: stream-json (structured NDJSON events on stdout).
    """

    strategy_override: AdapterStrategy = AdapterStrategy(
        dangerous_mode=DangerousModeStrategy.CLI_FLAG,
        event_channel=EventChannel.STREAM_JSON,
        session_state=SessionState.PERSISTENT_AGENT,
    )

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
        """Launch a Letta Code CLI session.

        Args:
            prompt: The headless prompt supplied via ``-p``.
            workdir: Working directory for the agent process.
            model_config: Model and effort configuration (retained for
                interface compatibility; Letta Code resolves the model
                via ``/connect`` config or ``--model``, not via the
                Bernstein scope mapping).
            session_id: Unique session identifier.
            mcp_config: Optional MCP server definitions (unused).
            timeout_seconds: Process timeout in seconds.
            task_scope: Task scope hint (unused by Letta Code).
            budget_multiplier: Multiplier on scope budget (unused).
            system_addendum: Protocol-critical system instructions (unused).

        Returns:
            SpawnResult with the spawned PID and log path.

        Raises:
            RuntimeError: If the ``letta`` binary is missing from PATH
                or cannot be executed.
        """
        self.refuse_multimodal_if_needed(multimodal_context)
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        # Derive conversation ID from session_id
        conversation_id = slug_identifier(session_id)

        # Build the letta command
        cmd = ["letta", "--output-format", "stream-json"]
        strategy = self.strategy()
        if strategy.dangerous_mode == DangerousModeStrategy.CLI_FLAG:
            cmd.extend(["--permission-mode", "unrestricted"])
        cmd.extend(["-p", prompt, "--new-agent", "--conversation", conversation_id])

        pid_dir = workdir / ".sdd" / "runtime" / "pids"
        wrapped_cmd = build_worker_cmd(
            cmd,
            role=session_id.rsplit("-", 1)[0],
            session_id=session_id,
            pid_dir=pid_dir,
            workdir=workdir,
            log_path=log_path,
            model=model_config.model,
        )

        env = build_filtered_env(
            [
                "LETTA_API_KEY",
                "LETTA_BASE_URL",
                "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY",
            ]
        )
        with log_path.open("w") as log_file:
            try:
                proc = subprocess.Popen(
                    wrapped_cmd,
                    cwd=workdir,
                    env=env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                msg = "letta not found in PATH. Install: npm install -g @letta-ai/letta-code"
                raise RuntimeError(msg) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing letta: {exc}") from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)

        # Start a daemon thread to handle post-exit tasks
        thread = threading.Thread(target=self._post_exit, args=(proc, workdir, session_id, log_path, result))
        thread.daemon = True
        thread.start()

        return result

    def _post_exit(
        self,
        proc: subprocess.Popen,
        workdir: Path,
        session_id: str,
        log_path: Path,
        spawn_result: SpawnResult,
    ) -> None:
        """Handle tasks after the Letta Code process exits.

        This includes:
        1. Waiting for the process to exit.
        2. Parsing the log for the envelope (agent_id, conversation_id, token_usage).
        3. Running `letta memory export` for the agent.
        4. Computing SHA-256 of the exported memory and writing to a sidecar.
        5. Writing the envelope data to a metadata sidecar file.
        """
        try:
            # Wait for the process to finish
            proc.wait()

            # Parse the log file to extract the envelope
            agent_id = None
            conv_id_from_log = None
            token_usage = None

            with log_path.open("r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        # Check if this line contains the envelope fields
                        if all(k in data for k in ("agent_id", "conversation_id", "token_usage")):
                            agent_id = data["agent_id"]
                            conv_id_from_log = data["conversation_id"]
                            token_usage = data["token_usage"]
                    except json.JSONDecodeError:
                        # Skip lines that are not valid JSON
                        continue

            if agent_id is None:
                raise RuntimeError("Envelope not found in Letta Code log")

            # Derive the conversation ID for file naming (from session_id, same as used in --conversation)
            conversation_id = slug_identifier(session_id)

            # Define paths for memory export and its sidecar
            runtime_dir = workdir / ".sdd" / "runtime"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            export_path = runtime_dir / f"letta_memory_{conversation_id}.json"
            export_sha256_path = export_path.with_suffix(export_path.suffix + ".sha256")

            # Run letta memory export
            export_cmd = [
                "letta",
                "memory",
                "export",
                "--agent",
                agent_id,
                "--out",
                str(export_path),
            ]
            export_proc = subprocess.Popen(
                export_cmd,
                cwd=workdir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            _, export_stderr = export_proc.communicate()
            if export_proc.returncode != 0:
                logger.warning(f"Letta memory export failed for agent {agent_id}: {export_stderr.decode()}")
                # We'll continue to try to write the metadata, but note the export failed.
                # We'll set the export_path to None so we skip the SHA-256 step.
                export_path = None

            # Compute SHA-256 of the export file if it exists and was created successfully
            if export_path and export_path.exists():
                sha256_hash = hashlib.sha256()
                with export_path.open("rb") as f:
                    for byte_block in iter(lambda: f.read(4096), b""):
                        sha256_hash.update(byte_block)
                digest = sha256_hash.hexdigest()
                with export_sha256_path.open("w") as f:
                    f.write(digest)
            else:
                logger.warning(f"Letta memory export file not found or export failed for agent {agent_id}")

            # Write the envelope data to a metadata sidecar file
            meta_path = runtime_dir / f"{session_id}.letta_meta.json"
            meta_data = {
                "agent_id": agent_id,
                "conversation_id": conv_id_from_log,
                "token_usage": token_usage,
            }
            with meta_path.open("w") as f:
                json.dump(meta_data, f, indent=2)

        except Exception as e:
            logger.warning(f"Failed to post-process Letta Code session for {session_id}: {e}")
            spawn_result.finish_reason = f"Post-processing failed: {e}"

    def name(self) -> str:
        """Return the human-readable adapter name."""
        return "Letta Code"
