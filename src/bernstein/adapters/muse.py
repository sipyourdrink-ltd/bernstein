"""Muse Code CLI adapter for Bernstein.

Adapter for Muse Code, Meta's terminal coding agent
(https://dev.meta.ai/docs/muse-code). Bernstein drives its documented
headless mode - ``muse exec "<prompt>"`` runs a single prompt to
completion, intended for scripts and CI - so a Muse Code run gets the
same worktree isolation, journaling, and receipts as every other
adapter-managed worker.

Last verified against vendor docs on 2026-08-10.
Install: ``curl -fsSL https://dev.meta.ai/install.sh | sh``
(static binary, macOS/Linux only; no native Windows support).
Auth: ``META_API_KEY`` env var for non-interactive environments.
Default model: ``muse-spark-1.2``. Version probe: ``muse --version``.

Flags used (verified from the vendor configuration/permissions pages):

* ``--model <id>`` - common launch flag, passed before the subcommand.
* ``--disable-approval`` - skips approval prompts while keeping the
  CLI's own sandbox containment; required for unattended runs (the
  interactive approval prompt would hang a headless worker forever).
  ``--yolo`` also exists but additionally disables the vendor sandbox,
  which this adapter deliberately does not do.

TODO-verify (documented but not consumed yet - re-check at
https://dev.meta.ai/docs/muse-code/extending.md before wiring up):

* ``--json`` (headless-only JSONL event stream) - unused; the adapter
  reads plain text output through the standard text-signal channel.
* ``--session-id <uuid>`` resumes an existing session non-interactively;
  resume stays declared unsupported until the resume path supplies one.
"""

from __future__ import annotations

import logging
import subprocess
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from bernstein.adapters.base import (
    DEFAULT_TIMEOUT_SECONDS,
    CLIAdapter,
    SpawnResult,
    build_worker_cmd,
)
from bernstein.adapters.env_isolation import build_filtered_env

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig

logger = logging.getLogger(__name__)

#: Documented default model (single-model vendor lineup today).
DEFAULT_MODEL = "muse-spark-1.2"

# Model mapping: Bernstein logical names -> Muse Code model IDs.
# Deliberately tiny: the vendor ships one model family. Unknown logical
# names fail loudly in :func:`_resolve_model` instead of silently
# remapping onto the default.
_MODEL_MAP: dict[str, str] = {
    "spark": DEFAULT_MODEL,
    DEFAULT_MODEL: DEFAULT_MODEL,
}

# The batch / heuristic selectors emit Claude cascade tier names
# (opus / sonnet / haiku) on almost every run - including the shared
# ``sonnet`` default that plain ``run --cli muse`` and ``test-adapter``
# inherit. They are not Muse model ids, so map them onto the vendor
# default instead of failing before Popen (the same last-resort safety
# net the Codex and Copilot adapters use). The real selection fix lives
# in the spawner; genuinely unknown names still fail loudly below.
_CLAUDE_TIER_MODELS: frozenset[str] = frozenset({"opus", "sonnet", "haiku"})
_tier_notice_emitted = False


def _resolve_model(model: str) -> str:
    """Resolve a Bernstein model name onto a Muse Code model id.

    Args:
        model: Logical name or explicit vendor model id. Empty selects
            the documented default; a Claude cascade tier name maps onto
            the default with a once-per-process notice.

    Returns:
        The vendor model id to pass via ``--model``.

    Raises:
        ValueError: When ``model`` is neither a known logical name, a
            Claude cascade tier name, nor an explicit ``muse-*`` vendor
            id. The vendor lineup is a single model family, so any other
            foreign name is a routing mistake to surface, not remap.
    """
    if not model:
        return DEFAULT_MODEL
    if model.lower() in _CLAUDE_TIER_MODELS:
        global _tier_notice_emitted
        if not _tier_notice_emitted:
            logger.info(
                "MuseAdapter: model %r is a Claude tier name Muse Code cannot run; "
                "using %r instead. Set role_model_policy.<role>.model or "
                "default_model to a 'muse-*' model id to choose explicitly.",
                model,
                DEFAULT_MODEL,
            )
            _tier_notice_emitted = True
        return DEFAULT_MODEL
    mapped = _MODEL_MAP.get(model)
    if mapped is not None:
        return mapped
    if model.startswith("muse-"):
        return model
    raise ValueError(
        f"Unknown model {model!r} for the Muse Code adapter. "
        f"Use {DEFAULT_MODEL!r} (the vendor default), the logical name 'spark', "
        "or an explicit 'muse-*' vendor model id."
    )


class MuseAdapter(CLIAdapter):
    """Spawn and monitor Muse Code (``muse``) headless sessions.

    Runs ``muse --model <id> --disable-approval exec "<prompt>"``:
    common launch flags precede the ``exec`` subcommand, matching the
    vendor's documented invocation shapes.
    """

    registry_name = "muse"

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
        """Launch a Muse Code headless run.

        Args:
            prompt: The task prompt, passed positionally to ``muse exec``.
            workdir: Working directory for the agent process.
            model_config: Model selection; resolved via :func:`_resolve_model`.
            session_id: Unique session identifier.
            mcp_config: Optional MCP server definitions (unused).
            timeout_seconds: Process timeout in seconds.
            task_scope: Task scope hint (unused by Muse Code).
            budget_multiplier: Multiplier on scope budget (unused).
            system_addendum: Protocol-critical system instructions
                (completion, signal-check, heartbeat). Muse Code has no
                separate system-prompt channel, so a non-empty addendum
                is appended to the user prompt - the documented fallback
                in :meth:`CLIAdapter.spawn`.
            multimodal_context: Attachments; refused, Muse Code runs text-only here.

        Returns:
            SpawnResult with the spawned PID and log path.

        Raises:
            ValueError: If ``model_config.model`` does not resolve to a
                Muse Code model id (see :func:`_resolve_model`).
            RuntimeError: If the ``muse`` binary is missing or not
                executable on the configured PATH.
        """
        self.refuse_multimodal_if_needed(multimodal_context)
        model_id = _resolve_model(model_config.model)

        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        # No separate system-prompt channel: merge the protocol-critical
        # addendum into the prompt so completion/signal/heartbeat
        # instructions actually reach the agent (base contract fallback).
        full_prompt = f"{prompt}\n\n{system_addendum}".rstrip() if system_addendum else prompt
        cmd = ["muse", "--model", model_id, "--disable-approval", "exec", full_prompt]

        pid_dir = workdir / ".sdd" / "runtime" / "pids"
        wrapped_cmd = build_worker_cmd(
            cmd,
            role=session_id.rsplit("-", 1)[0],
            session_id=session_id,
            pid_dir=pid_dir,
            workdir=workdir,
            log_path=log_path,
            model=model_id,
        )

        # Explicit env= always: inheriting the orchestrator environment is
        # a credential-leak vector. META_API_KEY is the vendor-documented
        # auth variable for non-interactive runs.
        env = build_filtered_env(["META_API_KEY"])
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
                msg = "muse not found in PATH. Install: curl -fsSL https://dev.meta.ai/install.sh | sh"
                raise RuntimeError(msg) from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing muse: {exc}") from exc

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result

    def name(self) -> str:
        """Human-readable adapter name shown in bernstein ps and logs."""
        return "muse"

    def get_version(self) -> str | None:
        """Return the Muse Code CLI version string, or None if unavailable."""
        with suppress(Exception):
            result = subprocess.run(
                ["muse", "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        return None

    def is_available(self) -> bool:
        """Return True if the Muse Code CLI is installed and accessible."""
        try:
            result = subprocess.run(
                ["muse", "--help"],
                capture_output=True,
                timeout=10,
                check=False,
            )
            return result.returncode == 0
        except Exception:
            return False
