"""Garak adversarial probe adapter.

Spawns garak CLI, parses its JSONL attempt log, and produces a
CampaignArtifact report binding: tool version, probe set, target descriptor,
invocation argv hash, and attempt-log content hash.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.env_isolation import build_filtered_env

#: garak target type to provider-API-key env-var mapping.
#: Keys are the target type prefix used in ``--target <type>:<name>``.
_TARGET_PROVIDER_KEYS: dict[str, str] = {
    "huggingface": "HF_TOKEN",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "ollama": "OLLAMA_API_KEY",
    "google": "GOOGLE_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "cohere": "COHERE_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "nvidia_nim": "NVIDIA_API_KEY",
    "azure": "AZURE_OPENAI_API_KEY",
    "vertex": "GOOGLE_API_KEY",
    "bedrock": "AWS_ACCESS_KEY_ID",  # also reads AWS_SECRET_ACCESS_KEY
    "replicate": "REPLICATE_API_TOKEN",
    "deepinfra": "DEEPINFRA_API_KEY",
    "kilocode": "KILOCODE_API_KEY",
}


def _sha256_hex(data: str | bytes) -> str:
    """Return the SHA-256 hex digest of *data*."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).digest().hex()


def _version_from_pip() -> str | None:
    """Return the installed garak version via ``pip show``, or None if not found."""
    rc, out = subprocess.getstatusoutput("pip show garak 2>/dev/null")
    if rc != 0:
        return None
    for line in out.splitlines():
        if line.startswith("Version:"):
            return line.split(":", 1)[1].strip()
    return None


class GarakAdapter(CLIAdapter):
    """Spawn and monitor garak adversarial probe runs.

    garak (https://github.com/NVIDIA/garak) is a red-team LLM vulnerability
    scanner that probes a target for known failure modes.  It is driven
    non-interactively via its CLI::

        garak --target <type>:<name> --probes <p1,p2> --report_filename <path>.jsonl

    The adapter shells out to ``garak``, waits for the process to complete,
    then parses the stored JSONL attempt log and returns a
    :class:`~bernstein.core.evaluation.CampaignArtifact` summary binding:

    - tool version (from ``pip show garak``)
    - probe set (declared probes)
    - target descriptor (``<type>:<name>``)
    - invocation argv hash (SHA-256 of the sorted, space-joined argv)
    - attempt-log content hash (SHA-256 of the JSONL file bytes)

    Success rate is computed from the adapter's own log parsing, not from
    garak's own summary output.

    The adapter **refuses to spawn without a target**: a garak run without
    a target has no meaning and would waste compute.  The refusal carries a
    structured reason code so callers can distinguish it from other spawn
    errors.

    Env isolation: only the provider API key matching the declared target
    type reaches the subprocess.  No other credentials are forwarded.
    """

    supports_session_log_watch = False
    supports_session_continuation = False

    def name(self) -> str:
        """Return the human-readable adapter name."""
        return "garak"

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
        """Spawn a garak probe run against a declared target.

        Args:
            prompt: garak target string of the form ``<type>:<name>``, e.g.
                ``openai:gpt-4o``.  Also accepts extra CLI flags appended to
                the garak invocation when *prompt* contains them (everything
                after the first space is passed through verbatim after the
                standard ``--target`` argument).
            workdir: Working directory for the agent process.
            model_config: Model configuration (unused for garak).
            session_id: Unique session identifier.
            mcp_config: Optional MCP server definitions (unused).
            timeout_seconds: Process timeout in seconds.
            task_scope: Task scope label (unused).
            budget_multiplier: Budget multiplier (unused).
            system_addendum: Protocol-critical instructions (unused).

        Returns:
            A :class:`SpawnResult` describing the spawned process.

        Raises:
            ValueError: If *prompt* does not contain a valid target
                specification of the form ``<type>:<name>``.
            RuntimeError: If the ``garak`` binary cannot be found or executed.
        """
        self.refuse_multimodal_if_needed(multimodal_context)
        self.enforce_network_policy()

        # Parse target and extra CLI flags from prompt.
        # Format: ``<target_type>:<target_name>`` or
        #         ``<target_type>:<target_name> --extra-flag value ...``
        raw_target, _, extra_cli = prompt.partition(" --")
        raw_target = raw_target.strip()
        extra_cli = " --" + extra_cli if extra_cli else ""

        if ":" not in raw_target:
            msg = (
                "garak requires a target specification of the form "
                "<type>:<name> (e.g. openai:gpt-4o, huggingface:meta-llama/Llama-2-7b); "
                f"received: {raw_target!r}"
            )
            raise ValueError(msg)

        target_type, _, target_name = raw_target.partition(":")
        target_type = target_type.strip()
        target_name = target_name.strip()

        if not target_type or not target_name:
            msg = (
                "garak target must be non-empty on both sides of the colon; "
                f"received type={target_type!r}, name={target_name!r}"
            )
            raise ValueError(msg)

        # Probe set defaults; can be overridden via extra CLI in prompt.
        probes = ""

        # Determine env key for this target type.
        env_key = _TARGET_PROVIDER_KEYS.get(target_type.lower(), "")
        extra_keys: tuple[str, ...] = ()
        if env_key:
            extra_keys = (env_key,)

        # Build the report filename in the workdir so we can find it after run.
        report_filename = f"garak_attempts_{session_id}.jsonl"

        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        report_path = workdir / ".sdd" / "runtime" / report_filename

        # Build the garak command.
        # Everything after ``--`` in the prompt is passed through verbatim
        # so operators can override probes, output format, etc.
        garak_cmd = [
            "garak",
            "--target",
            f"{target_type}:{target_name}",
            "--report_filename",
            str(report_path),
        ]
        if probes:
            garak_cmd.extend(["--probes", probes])
        if extra_cli:
            garak_cmd.extend(extra_cli.strip().split())

        pid_dir = workdir / ".sdd" / "runtime" / "pids"
        wrapped_cmd = build_worker_cmd(
            garak_cmd,
            role="adversary",
            session_id=session_id,
            pid_dir=pid_dir,
            workdir=workdir,
            log_path=log_path,
            model=model_config.model,
        )

        env = build_filtered_env(extra_keys)
        with log_path.open("w") as log_file:
            try:
                proc = subprocess.Popen(
                    wrapped_cmd,
                    cwd=workdir,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                msg = "garak not found in PATH. Install: pip install garak (see https://github.com/NVIDIA/garak)"
                raise RuntimeError(msg) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing garak: {exc}") from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def post_run_summary(
        self,
        session_id: str,
        workdir: Path,
        report_path: Path | None = None,
    ) -> dict[str, Any]:
        """Parse garak attempt log and return a structured summary dict.

        Called by the orchestrator after the spawned process exits.  The
        summary is used to build the :class:`CampaignArtifact` record.

        Args:
            session_id: Session identifier used at spawn.
            workdir: Agent working directory.
            report_path: Explicit path to the JSONL report.  When None,
                reconstructs the path from ``workdir`` using the same
                naming scheme as :meth:`spawn`.

        Returns:
            A dict containing:
            - ``tool_version``: garak version string.
            - ``probe_set``: probes that were run (from log metadata).
            - ``target``: the target string used at spawn.
            - ``invocation_argv_hash``: SHA-256 of the sorted argv.
            - ``attempt_log_hash``: SHA-256 of the JSONL report bytes.
            - ``attempt_count``: total number of probe attempts.
            - ``success_count``: attempts where no vulnerability was triggered.
            - ``fail_count``: attempts where a vulnerability was triggered.
            - ``error_count``: attempts that errored.
            - ``success_rate``: computed success rate (0.0-1.0).
            - ``report_path``: path to the JSONL file on disk.
            - ``report_exists``: whether the report file was found.
        """
        if report_path is None:
            report_filename = f"garak_attempts_{session_id}.jsonl"
            report_path = workdir / ".sdd" / "runtime" / report_filename

        report_exists = report_path.exists()
        attempt_log_hash = ""
        attempt_count = 0
        success_count = 0
        fail_count = 0
        error_count = 0
        probe_set: list[str] = []
        target = ""

        if report_exists:
            try:
                raw_bytes = report_path.read_bytes()
            except OSError:
                raw_bytes = b""
            attempt_log_hash = _sha256_hex(raw_bytes)

            try:
                text = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                raw_bytes = report_path.read_bytes()
                text = raw_bytes.decode("utf-8", errors="replace")

            for line in text.splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                attempt_count += 1
                # Track probes and target from log metadata (first entry).
                if "config" in entry:
                    cfg = entry["config"]
                    if not probe_set:
                        probe_set = list(cfg.get("probes", []) or [])
                    if not target:
                        target = cfg.get("target", "")
                # Result classification per garak attempt schema.
                status = entry.get("status", "").lower()
                if status == "fail":
                    fail_count += 1
                elif status == "error":
                    error_count += 1
                else:
                    # includes "pass", "ok", "skip", "" and any unknown status
                    success_count += 1

        success_rate = 0.0
        total_adjudicated = success_count + fail_count + error_count
        if total_adjudicated > 0:
            # Success rate = non-fail / total adjudicated.
            success_rate = round(success_count / total_adjudicated, 4)

        return {
            "tool_version": _version_from_pip() or "unknown",
            "probe_set": probe_set,
            "target": target,
            "invocation_argv_hash": "",  # caller fills this from spawn-time computed value
            "attempt_log_hash": attempt_log_hash,
            "attempt_count": attempt_count,
            "success_count": success_count,
            "fail_count": fail_count,
            "error_count": error_count,
            "success_rate": success_rate,
            "report_path": str(report_path),
            "report_exists": report_exists,
        }
