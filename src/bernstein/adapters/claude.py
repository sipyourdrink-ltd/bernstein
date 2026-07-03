"""Claude Code CLI adapter.

The heavy-lifting helpers for MCP config merging, cache-control block
construction, and the inline wrapper-script source live in three sibling
modules extracted by
* :mod:`bernstein.adapters.claude_mcp_loader` - ``load_mcp_config`` / ``_resolve_env_vars``
* :mod:`bernstein.adapters.claude_cache_control` - ``build_cacheable_system_blocks``
* :mod:`bernstein.adapters.claude_wrapper_script` - ``build_wrapper_script``

They are re-exported from this module so existing callers (and tests
that patch symbols via ``bernstein.adapters.claude.<name>``) continue to
work without changes.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import Mapping  # noqa: TC003 - runtime use in ClassVar annotations
from pathlib import Path
from typing import Any, ClassVar, cast

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult, build_worker_cmd
from bernstein.adapters.claude_agents import build_agents_json

# Re-export helpers that were inlined here before split them
# into sibling modules.  Callers and tests import these names directly
# from ``bernstein.adapters.claude`` so the re-exports MUST stay.  The
# explicit ``as`` aliases tell Pyright these are intentional re-exports
# and not unused imports.
from bernstein.adapters.claude_cache_control import (
    build_cacheable_system_blocks as build_cacheable_system_blocks,
)
from bernstein.adapters.claude_mcp_loader import (
    _resolve_env_vars as _resolve_env_vars,  # pyright: ignore[reportPrivateUsage]
)
from bernstein.adapters.claude_mcp_loader import (
    load_mcp_config as load_mcp_config,
)
from bernstein.adapters.claude_wrapper_script import build_wrapper_script
from bernstein.adapters.env_isolation import (
    _embedded_teams_opt_in,
    build_filtered_env,
    record_embedded_teams_opt_in,
)
from bernstein.core.defaults import COST
from bernstein.core.models import ApiTier, ApiTierInfo, ModelConfig, ProviderType, RateLimit
from bernstein.core.platform_compat import kill_process_group_graceful, process_alive

# task-budgets-2026-03-13 propagation. Inlined (rather than imported from
# ``bernstein.core.cost.budget_countdown``) to keep this adapter free of
# scheduler-internal transitive dependencies - see ``.importlinter``
# contract ``adapters-no-scheduler``. Source of truth for both constants
# is :mod:`bernstein.core.cost.budget_countdown` / :mod:`bernstein.core.security.agent_identity`.
_TASK_BUDGETS_OPT_IN_ENV: str = "BERNSTEIN_ANTHROPIC_TASK_BUDGETS"
_TASK_BUDGETS_BETA_VALUE: str = "task-budgets-2026-03-13"


def _task_budgets_opt_in() -> bool:
    """Mirror of :func:`bernstein.core.cost.budget_countdown.is_task_budgets_opt_in`."""
    raw = os.environ.get(_TASK_BUDGETS_OPT_IN_ENV, "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Context-compaction policy (recorded in the replay fingerprint)
# ---------------------------------------------------------------------------
#
# Context compaction (auto-summarising older context between turns) rewrites
# the history the model effectively works from. A run that records "context as
# sent" and later replays it can diverge if compaction changed between the two:
# the replay reconstructs the sent context, but a compaction event altered what
# the recorded run actually consumed.
#
# The load-bearing, verifiable guarantee here is NOT that we can force
# compaction off. It is that the compaction policy state is captured in the
# step fingerprint (the HMAC/Merkle step journal). If compaction behaviour
# changes between record and replay, the fingerprint diverges and the mismatch
# is DETECTED and attributable to a named field rather than surfacing as a
# mysterious, unexplained rewrite. That detection is the deterministic-replay
# lever; it holds regardless of whether any suppression request is honoured.
#
# As a best-effort layer, in deterministic-record or hermetic-replay mode we
# additionally REQUEST that the CLI suppress its client-side auto-compaction.
# This is a best-effort request, not an authoritative guarantee: it targets the
# CLI's own auto-compaction behaviour and cannot promise anything about any
# server-side context handling. The fingerprint is what makes a divergence
# safe; the suppression request only reduces how often one happens.
#
# The env flags below are the source of truth for the run mode. They mirror
# the ones read by the orchestrator (BERNSTEIN_DETERMINISTIC_SEED for record,
# BERNSTEIN_REPLAY_RUN_ID for replay); they are inlined here rather than
# imported so the adapter stays free of scheduler internals (importlinter
# contract ``adapters-no-scheduler``).
_DETERMINISTIC_SEED_ENV: str = "BERNSTEIN_DETERMINISTIC_SEED"
_REPLAY_RUN_ID_ENV: str = "BERNSTEIN_REPLAY_RUN_ID"

#: Best-effort request to suppress the CLI's client-side auto-compaction; not a
#: guarantee. Setting it to "1" asks the CLI not to auto-compact its own
#: context this process; it makes no claim about any server-side handling. The
#: authoritative replay protection is the recorded fingerprint (see
#: :func:`ClaudeAdapter._record_compaction_state`), which detects and
#: attributes any divergence a compaction change would cause.
_DISABLE_COMPACTION_ENV: str = "DISABLE_AUTO_COMPACT"


def _deterministic_run_mode() -> bool:
    """Return whether this process runs in deterministic-record or hermetic-replay mode.

    Reads :data:`_DETERMINISTIC_SEED_ENV` (record) and
    :data:`_REPLAY_RUN_ID_ENV` (replay) from the environment. Either being set
    to a non-empty value means the run must be byte-reconstructable, so the
    compaction policy state is recorded in the fingerprint and a best-effort
    suppression request is emitted.

    Returns:
        ``True`` when a deterministic seed or a replay run id is present.
    """
    seed = os.environ.get(_DETERMINISTIC_SEED_ENV, "").strip()
    replay = os.environ.get(_REPLAY_RUN_ID_ENV, "").strip()
    return bool(seed) or bool(replay)


def apply_compaction_policy(env: dict[str, str]) -> bool:
    """Apply the deterministic context-compaction policy to a spawn *env*.

    In deterministic-record or hermetic-replay mode, sets
    :data:`_DISABLE_COMPACTION_ENV` to "1" as a best-effort request that the
    CLI suppress its client-side auto-compaction. This is a request, not a
    guarantee: it cannot promise the model saw byte-identical context. Outside
    those modes the env is left untouched (default behaviour).

    Mutates *env* in place and returns whether the suppression policy was
    applied, so the caller can record the policy state in the step fingerprint.
    The fingerprint is the load-bearing guarantee: any divergence a compaction
    change causes is detected and attributed to a known, recorded policy field
    rather than surfacing as a mysterious rewrite.

    Args:
        env: The filtered spawn environment dict (mutated in place).

    Returns:
        ``True`` when the suppression policy was applied for this request
        (deterministic modes), ``False`` when it was left at the default.
    """
    if _deterministic_run_mode():
        # Best-effort request to suppress client-side auto-compaction; not a
        # guarantee. The recorded fingerprint is what makes replay safe.
        env[_DISABLE_COMPACTION_ENV] = "1"
        return True
    return False


# ---------------------------------------------------------------------------
# Multimodal attachment encoding (issue #1797)
# ---------------------------------------------------------------------------


def _inject_multimodal_attachments(prompt: str, multimodal_context: Any) -> str:
    """Inline encoded attachments at the head of *prompt*.

    The Claude Code CLI does not accept image attachments as separate
    arguments; we wrap each base64-encoded blob in a structured
    ``<attachment mime="..." sha256="...">`` block before the user
    prompt so the model API receives the bytes inline. Tests assert the
    exact format so downstream replay can reconstruct what was sent.

    The ``sha256`` attribute is computed over the *decoded* base64
    payload -- i.e. the exact bytes the model API receives -- not a
    fresh read of ``content_path``. That ensures the announced digest
    matches the inlined bytes even if the source file changes between
    context construction and spawn time.
    (bot-ack: 3284182744 -- CodeRabbit major.)

    Args:
        prompt: The agent prompt that will be passed to the CLI.
        multimodal_context: A
            :class:`bernstein.core.agents.multimodal.MultiModalContext`.

    Returns:
        Prompt with the encoded blocks prepended, or *prompt* unchanged
        when the context contains no inputs.
    """
    inputs = getattr(multimodal_context, "inputs", ())
    if not inputs:
        return prompt

    import base64 as _base64
    import hashlib as _hashlib

    blocks: list[str] = []
    for inp in inputs:
        b64 = getattr(inp, "content_base64", None) or ""
        mime = getattr(inp, "mime_type", "application/octet-stream")
        if b64:
            try:
                raw = _base64.b64decode(b64, validate=True)
                digest = _hashlib.sha256(raw).hexdigest()
            except (ValueError, TypeError):
                digest = ""
        else:
            digest = ""
        # Format documented in docs/operations/run.md so adapters and
        # tests share the same wire format.
        blocks.append(f'<attachment mime="{mime}" sha256="{digest}">\n{b64}\n</attachment>')
    header = "\n".join(blocks)
    return f"{header}\n\n{prompt}"


# Map short model names to Claude Code CLI model IDs.
# Last verified against upstream @anthropic-ai/claude-code 2.1.x on 2026-05-05.
# Opus 4.7 is GA at the same price as 4.6 (Anthropic news, 2026-04-16); Sonnet
# 4.6 and Haiku 4.5 remain the current production aliases.  Anthropic now
# recommends the native installer (`curl -fsSL https://claude.ai/install.sh | sh`
# or `brew install --cask claude-code`) over the deprecated npm path; the
# adapter still works with whichever binary is on PATH.
_MODEL_MAP: dict[str, str] = {
    "opus": "claude-opus-4-7",
    "opus-4-6": "claude-opus-4-6",  # pinned fallback
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}

# JSON schema for structured agent output - enforced via --json-schema so
# the result is always machine-parseable by the orchestrator.
_RESULT_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["done", "failed", "partial"]},
            "summary": {"type": "string"},
            "files_changed": {"type": "array", "items": {"type": "string"}},
            "exit_reason": {"type": "string"},
        },
        "required": ["status", "summary"],
    }
)


# Shared cast-type constants to avoid string duplication (Sonar S1192).
_CAST_DICT_STR_ANY = "dict[str, Any]"


_logger = logging.getLogger(__name__)


# How long a cached rate-limit probe result stays valid (seconds).
_RATE_LIMIT_CACHE_TTL: float = COST.rate_limit_cache_ttl_s

# Cooldown applied when rate-limiting is detected (seconds).
_RATE_LIMIT_COOLDOWN: float = COST.rate_limit_cooldown_s


class ClaudeCodeAdapter(CLIAdapter):
    """Spawn and monitor Claude Code CLI sessions."""

    # Provider-string aliases this adapter resolves from in
    # ``_infer_adapter_name_for_provider`` (via the registry's
    # provider-alias table). Unchanged from the old substring branch.
    provides = ("claude", "anthropic")

    external_endpoints = (("api.anthropic.com", 443),)
    # Surface the upstream provider on the ``bernstein status``
    # rate-limit panel. Anthropic uses HTTP 429 plus structured
    # ``rate_limit_error`` types on the messages API.
    rate_limit_provider = "anthropic"
    # Claude Code writes a JSONL transcript per session under
    # ``~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl``.
    # ProgressWatch can read this directly as a liveness signal.
    supports_session_log_watch = True

    # Opt into the retry-with-continuation path. Claude Code's CLI
    # exposes ``--resume <session-id>`` to re-enter a prior conversation
    # without paying the full setup cost. See
    # :mod:`bernstein.core.orchestration.commit_completion`.
    supports_session_continuation = True

    # Track Popen objects for reliable is_alive() via poll()
    _procs: ClassVar[dict[int, subprocess.Popen[bytes]]] = {}
    _wrapper_pids: ClassVar[dict[int, int]] = {}  # claude_pid → wrapper_pid

    # Tool allowlists by role - agents only get the tools they need.
    # Reduces attack surface and prevents agents from using tools outside
    # their scope (e.g. qa agent shouldn't use Write to create new files).
    _ROLE_ALLOWED_TOOLS: ClassVar[dict[str, str]] = {
        "qa": "Bash Read Grep Glob Agent",
        "reviewer": "Bash Read Grep Glob",
        "docs": "Read Write Edit Grep Glob",
        "security": "Bash Read Grep Glob",
    }

    def __init__(self) -> None:
        super().__init__()
        # Timestamp until which the provider is assumed rate-limited.
        self._rate_limit_until: float = 0.0
        # Timestamp of last successful (non-rate-limited) probe, for caching.
        self._rate_limit_checked_at: float = 0.0

    def is_rate_limited(self) -> bool:
        """Probe ``claude --version`` to detect provider-side rate limiting.

        Returns a cached result for ``_RATE_LIMIT_CACHE_TTL`` seconds to avoid
        spamming the CLI.  When rate limiting is detected, sets a cooldown of
        ``_RATE_LIMIT_COOLDOWN`` seconds during which all spawns are skipped.
        """
        now = time.time()

        # Active cooldown - provider is known rate-limited.
        if now < self._rate_limit_until:
            return True

        # Cached negative result - recently checked and was fine.
        if now - self._rate_limit_checked_at < _RATE_LIMIT_CACHE_TTL:
            return False

        # Probe with a real API call - `claude --version` doesn't hit the API
        # and can't detect account-level rate limits like "You've hit your limit".
        try:
            result = subprocess.run(
                ["claude", "--print", "--max-turns", "1", "--output-format", "text", "-p", "say ok"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=COST.rate_limit_probe_timeout_s,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            _logger.debug("Rate-limit probe failed: %s", exc)
            self._rate_limit_checked_at = now
            return False

        combined = (result.stdout + result.stderr).lower()
        if "hit your limit" in combined or "rate limit" in combined or "resets" in combined:
            self._rate_limit_until = now + _RATE_LIMIT_COOLDOWN
            _logger.warning(
                "Claude Code rate-limited; blocking spawns for %.0fs",
                _RATE_LIMIT_COOLDOWN,
            )
            # Record on the per-adapter meter so ``bernstein status`` can
            # surface the hit before the spawn loop reaches ``_probe_fast_exit``.
            with contextlib.suppress(Exception):
                self.record_rate_limit_hit(error_code="anthropic_rate_limit_probe")
            return True

        self._rate_limit_checked_at = now
        return False

    # Max turns used for batch-mode agents, which must cover research +
    # decompose + spawn workers + track completion.
    BATCH_MAX_TURNS: int = COST.batch_max_turns

    # Scope → base budget mapping.  Opus tasks get a 2x multiplier because
    # opus input/output tokens cost roughly twice as much as sonnet.
    # Widened to ``Mapping`` because ``defaults.COST`` returns a
    # ``MappingProxyType`` view.
    _SCOPE_BUDGET_USD: ClassVar[Mapping[str, float]] = COST.scope_budget_usd

    # Scope multipliers: large tasks get proportionally more turns so they
    # don't die prematurely.
    _SCOPE_MULTIPLIERS: ClassVar[Mapping[str, float]] = COST.scope_multipliers

    def _build_command(
        self,
        model_config: ModelConfig,
        mcp_config: dict[str, Any] | None,
        prompt: str,
        *,
        role: str = "",
        workdir: Path | None = None,
        agents_json: dict[str, Any] | None = None,
        system_addendum: str = "",
        batch_mode: bool = False,
        task_scope: str = "medium",
        budget_multiplier: float = 1.0,
    ) -> list[str]:
        """Build the claude CLI command with effort mapping.

        Uses ``--permission-mode bypassPermissions`` instead of the deprecated
        ``--dangerously-skip-permissions`` flag, adds ``--fallback-model``
        for automatic failover, ``--allowedTools`` for role-scoped
        tool access, and ``--agents`` for per-task subagent definitions.

        Args:
            model_config: Model and effort configuration.
            mcp_config: MCP server definitions to inject.
            prompt: The task prompt.
            role: Agent role (used for tool allowlisting).
            workdir: Project working directory (used for CLAUDE.md context dirs).
            agents_json: Custom subagent definitions for ``--agents`` flag.
                When provided, Claude Code's Agent tool will use these
                definitions instead of generic defaults.
            system_addendum: Orchestration context to inject via
                ``--append-system-prompt``.  Keeps signal-check instructions,
                completion protocol, heartbeat commands, etc. out of the user
                prompt so the agent focuses on the task goal.
            batch_mode: When True, sets ``--max-turns`` to
                :attr:`BATCH_MAX_TURNS` (200) so the agent has enough turns
                to research, decompose, spawn workers, and track their
                completion via the ``/batch`` skill.
            task_scope: Task scope ("small", "medium", "large") used to
                compute a per-task budget cap.  Opus models get a 2x
                multiplier because their token costs are roughly double.
            budget_multiplier: Additional multiplier applied on top of the
                scope-based budget (e.g. 2.0 when retrying after hitting the
                budget cap in a previous attempt).
        """
        model_id = _MODEL_MAP.get(model_config.model, model_config.model)
        effort = getattr(model_config, "effort", "high")
        base_turns = COST.effort_base_turns.get(effort, 50)
        scope_multiplier = self._SCOPE_MULTIPLIERS.get(task_scope, 1.5)
        max_turns = self.BATCH_MAX_TURNS if batch_mode else int(base_turns * scope_multiplier)
        claude_effort = ({"max": "max", "high": "high", "medium": "medium", "normal": "medium", "low": "low"}).get(
            effort, "high"
        )

        # Choose fallback model: opus-4-7 → opus-4-6 → sonnet → haiku
        fallback_model = (
            {
                "claude-opus-4-7": "claude-opus-4-6",
                "claude-opus-4-6": "claude-sonnet-4-6",
                "claude-sonnet-4-6": "claude-haiku-4-5-20251001",
            }
        ).get(model_id)

        cmd = [
            "claude",
            "--model",
            model_id,
            "--effort",
            claude_effort,
            "--permission-mode",
            "bypassPermissions",
            "--max-turns",
            str(max_turns),
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-hook-events",
            "--no-session-persistence",
        ]
        if fallback_model:
            cmd.extend(["--fallback-model", fallback_model])

        # Role-scoped tool allowlisting: restrict non-coding roles to read-only
        # tools to prevent unintended modifications.
        allowed_tools = self._ROLE_ALLOWED_TOOLS.get(role)
        if allowed_tools:
            cmd.extend(["--allowedTools", allowed_tools])

        # Inject project CLAUDE.md context so the agent picks up coding standards,
        # architecture notes, and any task-specific instructions automatically.
        # --add-dir tells Claude Code to load CLAUDE.md from the given directory.
        if workdir is not None:
            claude_md = workdir / "CLAUDE.md"
            if claude_md.exists():
                cmd.extend(["--add-dir", str(workdir)])

        # Inject per-task subagent definitions so Claude Code's Agent tool
        # spawns role-scoped subagents instead of generic defaults.
        if agents_json:
            cmd.extend(["--agents", json.dumps(agents_json)])

        # Per-task budget cap - scope-aware to avoid killing large tasks mid-work.
        # Opus models get a 2x multiplier because their tokens cost ~2x more.
        # Retry budget_multiplier (e.g. 2.0 after budget-cap failure) stacks on top.
        base_budget = self._SCOPE_BUDGET_USD.get(task_scope, 5.0)
        is_opus = "opus" in model_id.lower()
        budget_usd = base_budget * (COST.opus_budget_multiplier if is_opus else 1.0) * budget_multiplier
        cmd.extend(["--max-budget-usd", f"{budget_usd:.2f}"])

        # Enforce structured output so the orchestrator can always parse results
        cmd.extend(["--json-schema", _RESULT_SCHEMA])

        if mcp_config:
            cmd.extend(["--mcp-config", json.dumps(mcp_config)])

        if system_addendum:
            cmd.extend(["--append-system-prompt", system_addendum])

        cmd.extend(["-p", prompt])
        return cmd

    @staticmethod
    def _wrapper_script(
        session_id: str = "",
        tokens_path: str = "",
        heartbeat_path: str = "",
        completion_path: str = "",
    ) -> str:
        """Return the stream-json → human-readable log converter script.

        Thin delegator to :func:`build_wrapper_script` in
        :mod:`bernstein.adapters.claude_wrapper_script`.  Kept as a
        ``@staticmethod`` on the adapter so tests and external callers
        that reference ``ClaudeCodeAdapter._wrapper_script`` continue
        to work unchanged after the extraction.

        Args:
            session_id: Agent session ID, accepted for API parity.
            tokens_path: Absolute path to the ``.tokens`` sidecar file.
            heartbeat_path: Absolute path to the heartbeat file (touched on each event).
            completion_path: Absolute path to the completion marker file.
        """
        return build_wrapper_script(
            session_id=session_id,
            tokens_path=tokens_path,
            heartbeat_path=heartbeat_path,
            completion_path=completion_path,
        )

    @staticmethod
    def _record_compaction_state(workdir: Path, session_id: str, *, compaction_disabled: bool) -> None:
        """Persist the compaction policy state applied to this spawn.

        Writes a small sidecar JSON at
        ``.sdd/runtime/compaction/<session_id>.json`` recording whether the
        best-effort suppression policy was applied. The spawner folds this flag
        into the step journal so that if compaction behaviour changes between
        record and replay, the resulting divergence is DETECTED and attributable
        to a known, recorded policy field rather than surfacing as a mysterious
        context rewrite. This recording is the load-bearing replay guarantee;
        the suppression request is only best-effort. Writing is itself
        best-effort: a failure here must never break spawn.

        Args:
            workdir: Agent working directory (root of ``.sdd``).
            session_id: Session id used as the sidecar filename.
            compaction_disabled: ``True`` when the suppression policy was
                applied for this request (deterministic / hermetic modes).
        """
        try:
            state_dir = workdir / ".sdd" / "runtime" / "compaction"
            state_dir.mkdir(parents=True, exist_ok=True)
            state_path = state_dir / f"{session_id}.json"
            payload = {
                "session_id": session_id,
                # ``compaction_enabled`` is the policy value folded into the
                # step fingerprint: True = default behaviour (no suppression
                # requested), False = best-effort suppression requested for
                # this deterministic/replay run. It records the policy we
                # applied, not a proof the provider honoured it.
                "compaction_enabled": not compaction_disabled,
            }
            state_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        except OSError as exc:  # pragma: no cover - best-effort, never blocks spawn
            _logger.debug("Could not record compaction state for %s: %s", session_id, exc)

    def _launch_process(
        self,
        cmd: list[str],
        wrapper: str,
        workdir: Path,
        log_path: Path,
        env: dict[str, str] | None = None,
    ) -> tuple[subprocess.Popen[bytes], subprocess.Popen[bytes]]:
        """Launch claude piped through wrapper, writing output to log_path.

        The parent closes its copies of the log/stderr file handles after
        the subprocesses have inherited them.  On failure the handles are
        closed in the ``except`` path so they are never leaked.

        Args:
            cmd: The CLI command to execute (wrapped by bernstein-worker).
            wrapper: Python source for the stream-json log converter script.
            workdir: Working directory for both processes.
            log_path: Path where the wrapper writes decoded output.
            env: Filtered environment dict.  When provided, both child
                processes receive only these variables; when None the full
                parent environment is inherited (legacy behaviour).
        """
        log_file = log_path.open("w")
        stderr_file = log_path.with_suffix(".stderr.log").open("w")
        preexec_fn = self._get_preexec_fn()
        try:
            try:
                claude_proc = subprocess.Popen(
                    cmd,
                    cwd=workdir,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=stderr_file,
                    start_new_session=True,
                    preexec_fn=preexec_fn,
                )
            except FileNotFoundError as exc:
                raise RuntimeError("claude not found in PATH. Install Claude Code: https://claude.ai/code") from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing claude: {exc}") from exc

            try:
                wrapper_proc = subprocess.Popen(
                    [sys.executable, "-c", wrapper],
                    stdin=claude_proc.stdout,
                    stdout=log_file,
                    stderr=stderr_file,
                    start_new_session=True,
                    cwd=workdir,
                    env=env,
                )
            except Exception:
                claude_proc.kill()
                raise
        except Exception:
            # Subprocesses never started (or only partially); close handles
            # so they don't leak.
            log_file.close()
            stderr_file.close()
            raise

        # Both subprocesses are running and own the inherited FDs.
        # Close the parent's copies so the FDs aren't kept alive
        # longer than necessary, but the children can still write.
        log_file.close()
        stderr_file.close()

        # Allow claude_proc to receive SIGPIPE if wrapper dies
        if claude_proc.stdout:
            claude_proc.stdout.close()

        return claude_proc, wrapper_proc

    @staticmethod
    def _inject_hooks_config(
        workdir: Path,
        session_id: str,
        server_url: str = "http://127.0.0.1:8052",
    ) -> None:
        """Write Claude Code hooks config to ``.claude/settings.local.json``.

        Injects HTTP hooks for PostToolUse, Stop, PreCompact, SubagentStart,
        and SubagentStop events.  Each hook POSTs to the Bernstein task server
        so the orchestrator gets real-time visibility into agent activity.

        Each hook request is signed with HMAC-SHA256 over the raw body, using
        the shared secret in ``BERNSTEIN_HOOK_SECRET`` (or
        ``BERNSTEIN_AUTH_TOKEN``).  The server rejects unsigned requests.

        If the settings file already exists, the hooks key is merged in
        (preserving any other settings the user may have configured).

        Args:
            workdir: Project working directory (worktree root).
            session_id: Agent session identifier, embedded in the hook URL.
            server_url: Task server base URL (default localhost:8052).
        """
        settings_dir = workdir / ".claude"
        settings_dir.mkdir(parents=True, exist_ok=True)
        settings_path = settings_dir / "settings.local.json"

        hook_url = f"{server_url}/hooks/{session_id}"
        # Sign the body with HMAC-SHA256 before posting.  ``openssl`` is in the
        # base image on every supported OS; we read the secret from the env
        # var the adapter also exports when spawning the agent.
        curl_cmd = (
            "sh -c '"
            'SECRET="${BERNSTEIN_HOOK_SECRET:-$BERNSTEIN_AUTH_TOKEN}"; '
            "BODY=$(cat); "
            'SIG=$(printf "%s" "$BODY" | openssl dgst -sha256 -hmac "$SECRET" -hex | awk "{print \\$2}"); '
            'curl -sS -X POST -H "Content-Type: application/json" '
            f'-H "X-Bernstein-Hook-Signature-256: sha256=$SIG" -d "$BODY" {hook_url}'
            "'"
        )
        hook_entry = {"type": "command", "command": curl_cmd}

        hook_events = ["PostToolUse", "Stop", "PreCompact", "SubagentStart", "SubagentStop"]
        hooks_config: dict[str, list[dict[str, Any]]] = {}
        for event_name in hook_events:
            hooks_config[event_name] = [
                {"matcher": "", "hooks": [hook_entry]},
            ]

        # Merge with existing settings if present
        existing: dict[str, Any] = {}
        if settings_path.exists():
            with contextlib.suppress(json.JSONDecodeError, OSError):
                raw = json.loads(settings_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    existing = cast(_CAST_DICT_STR_ANY, raw)

        existing["hooks"] = hooks_config

        # We own the merged settings file handed to the worker, so pin the
        # embedded agent-team gate off unless the operator explicitly opts in.
        # Teammates a worker spawns inherit the lead's permission mode and run
        # outside this worker's HMAC audit trail; a stray truthy value in a
        # pre-existing settings file must not silently re-open that hole. The
        # env-var path is denied in build_filtered_env(); this covers the
        # settings.json path Claude Code also reads.
        if not _embedded_teams_opt_in():
            env_block = existing.get("env")
            if not isinstance(env_block, dict):
                env_block = {}
            env_block["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] = "false"
            existing["env"] = env_block

        try:
            settings_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
        except OSError:
            _logger.debug("Failed to write hooks config to %s", settings_path)

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
        self.enforce_network_policy()
        # Issue #1797: encode any attached images into the prompt body
        # as base64 with the correct MIME type so the upstream model API
        # sees them. The Claude Code CLI does not accept attachments
        # directly; we inline them in a structured ``<attachment>`` block
        # at the head of the prompt so the model picks them up.
        if multimodal_context is not None:
            prompt = _inject_multimodal_attachments(prompt, multimodal_context)
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        role = session_id.rsplit("-", 1)[0]  # e.g. "qa" from "qa-abc12345"

        # Inject Claude Code hooks for real-time tool-use and lifecycle monitoring.
        # Must happen before process launch so the agent picks up the config.
        self._inject_hooks_config(workdir, session_id)

        # Inject Bernstein MCP bridge so agents can report progress, check
        # sibling status, and read the bulletin board.  Uses the existing
        # bernstein.mcp.server module in stdio transport mode.
        bridge_server: dict[str, Any] = {
            "command": sys.executable,
            "args": ["-m", "bernstein.mcp.server"],
        }
        effective_mcp: dict[str, Any] = {}
        if mcp_config:
            if "mcpServers" in mcp_config:
                effective_mcp = {**mcp_config}
                effective_mcp["mcpServers"] = mcp_config["mcpServers"] | {"bernstein": bridge_server}
            else:
                effective_mcp = {"mcpServers": mcp_config | {"bernstein": bridge_server}}
        else:
            effective_mcp = {"mcpServers": {"bernstein": bridge_server}}

        # Auto-detect batch mode: prompts starting with "/batch" are delegated
        # to Claude Code's built-in /batch skill, which requires more turns to
        # cover the full research → decompose → spawn-workers → track lifecycle.
        batch_mode = prompt.lstrip().startswith("/batch")
        if batch_mode:
            _logger.info("Batch mode detected for session %s - using %d max-turns", session_id, self.BATCH_MAX_TURNS)

        agents_json = build_agents_json(role)
        cmd = self._build_command(
            model_config,
            effective_mcp,
            prompt,
            role=role,
            workdir=workdir,
            agents_json=agents_json,
            system_addendum=system_addendum,
            batch_mode=batch_mode,
            task_scope=task_scope,
            budget_multiplier=budget_multiplier,
        )

        # Wrap with bernstein-worker for process visibility
        pid_dir = workdir / ".sdd" / "runtime" / "pids"
        model_id = _MODEL_MAP.get(model_config.model, model_config.model)
        wrapped_cmd = build_worker_cmd(
            cmd,
            role=role,
            session_id=session_id,
            pid_dir=pid_dir,
            workdir=workdir,
            log_path=log_path,
            model=model_id,
        )

        tokens_path = workdir / ".sdd" / "runtime" / f"{session_id}.tokens"
        heartbeat_dir = workdir / ".sdd" / "runtime" / "heartbeats"
        heartbeat_dir.mkdir(parents=True, exist_ok=True)
        heartbeat_path = heartbeat_dir / f"{session_id}.json"
        completed_dir = workdir / ".sdd" / "runtime" / "completed"
        completed_dir.mkdir(parents=True, exist_ok=True)
        completion_path = completed_dir / session_id
        wrapper = self._wrapper_script(
            session_id=session_id,
            tokens_path=str(tokens_path),
            heartbeat_path=str(heartbeat_path),
            completion_path=str(completion_path),
        )
        # Allow operator-set ANTHROPIC_BETA through so we can extend it
        # rather than overwrite. Filter is empty-safe when the env var
        # is unset.
        env = build_filtered_env(["ANTHROPIC_API_KEY", "ANTHROPIC_BETA"])
        # Embedded agent-team spawning is denied by default in
        # build_filtered_env(); when an operator re-permits it via
        # BERNSTEIN_ALLOW_EMBEDDED_AGENT_TEAMS we attest the decision to this
        # worker's HMAC audit chain so the teammate hierarchy stays auditable
        # rather than invisible.
        if _embedded_teams_opt_in():
            record_embedded_teams_opt_in(
                adapter="claude",
                session_id=session_id,
                audit_dir=workdir / ".sdd" / "audit",
            )
        # Anthropic Opus 4.7 ``task-budgets-2026-03-13`` beta header. Set
        # via ``ANTHROPIC_BETA`` so the Claude Code CLI forwards it to
        # every API call. Gated behind ``BERNSTEIN_ANTHROPIC_TASK_BUDGETS``
        # (see :data:`bernstein.core.cost.budget_countdown.TASK_BUDGETS_OPT_IN_ENV`)
        # until GA per the upstream beta-flag convention.
        if _task_budgets_opt_in():
            existing_beta = env.get("ANTHROPIC_BETA", "").strip()
            if _TASK_BUDGETS_BETA_VALUE in existing_beta:
                pass  # already present (operator set it explicitly)
            elif existing_beta:
                env["ANTHROPIC_BETA"] = f"{existing_beta},{_TASK_BUDGETS_BETA_VALUE}"
            else:
                env["ANTHROPIC_BETA"] = _TASK_BUDGETS_BETA_VALUE
        # In deterministic-record / hermetic-replay mode, emit a best-effort
        # request to suppress client-side auto-compaction (no-op otherwise).
        # The returned policy flag is recorded in the fingerprint so any
        # compaction-driven divergence between record and replay is detected
        # and stays attributable rather than mysterious. The recording, not the
        # suppression request, is the load-bearing guarantee.
        compaction_disabled = apply_compaction_policy(env)
        self._record_compaction_state(workdir, session_id, compaction_disabled=compaction_disabled)
        claude_proc, wrapper_proc = self._launch_process(wrapped_cmd, wrapper, workdir, log_path, env=env)

        # Track the worker process (wraps claude) for is_alive/kill
        self._procs[claude_proc.pid] = claude_proc
        # Also track wrapper so we can kill both
        self._wrapper_pids[claude_proc.pid] = wrapper_proc.pid

        try:
            self._probe_fast_exit(claude_proc, log_path, provider_name="claude")
        except Exception:
            self._wrapper_pids.pop(claude_proc.pid, None)
            self._procs.pop(claude_proc.pid, None)
            with contextlib.suppress(Exception):
                wrapper_proc.wait(timeout=1)
            raise

        result = SpawnResult(pid=claude_proc.pid, log_path=log_path, proc=claude_proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(claude_proc.pid, timeout_seconds, session_id)
        return result

    def is_alive(self, pid: int) -> bool:
        # Use poll() to detect zombies - process_alive can't
        proc = self._procs.get(pid)
        if proc is not None:
            return proc.poll() is None
        # Fallback for processes we didn't spawn
        return process_alive(pid)

    def kill(self, pid: int) -> None:
        # The claude process is spawned with start_new_session=True, so
        # its PID equals its PGID.  Use the PID directly as PGID instead
        # of os.getpgid() which fails when the process is already dead -
        # this ensures we kill the entire session group including any
        # child processes (the actual claude CLI) that outlive the wrapper.
        #
        # ``kill_process_group_graceful`` sends SIGTERM, polls briefly, and
        # escalates to SIGKILL if the group is still alive.  Without the
        # escalation, agents that trap SIGTERM survive reap paths - see
        # .
        kill_process_group_graceful(pid)
        # Also kill the wrapper process with the same TERM→KILL escalation
        wrapper_pid = self._wrapper_pids.pop(pid, None)
        if wrapper_pid:
            kill_process_group_graceful(wrapper_pid)
        self._procs.pop(pid, None)

    def name(self) -> str:
        return "Claude Code"

    def continuation_args(self, _session_id: str) -> list[str]:
        """Return the CLI flags that re-enter the most recent Claude session.

        Claude Code exposes ``--continue`` to re-enter the most recent
        conversation in the current workdir without paying the full
        setup cost. The orchestrator launches the retry from the same
        worktree as the original spawn, so ``--continue`` resolves to
        the correct prior session without explicit id plumbing.
        """
        return ["--continue"]

    def session_log_path_for(self, session_id: str) -> Path | None:
        """Return the Claude Code JSONL transcript path for this session.

        Claude Code writes per-session transcripts under
        ``~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl``. The
        ``<session-uuid>`` is assigned by Claude itself and is not the
        Bernstein ``session_id``, so we return the most recently modified
        JSONL under the encoded-cwd directory that matches the worktree
        the agent is running in.

        Args:
            session_id: Bernstein session id. Currently unused for path
                resolution but accepted for forward compatibility.

        Returns:
            Absolute path to the latest JSONL transcript, or ``None`` if
            the projects directory or any transcript file is missing.
        """
        del session_id
        try:
            home = Path.home()
        except (OSError, RuntimeError):
            return None
        # Claude encodes the current working directory by replacing path
        # separators with dashes. We accept any subdirectory under
        # ``~/.claude/projects/`` because the spawn cwd may be a worktree
        # whose encoding the caller does not know.
        projects_dir = home / ".claude" / "projects"
        if not projects_dir.is_dir():
            return None
        cwd = Path.cwd().resolve()
        # Best-effort: match a directory whose name encodes ``cwd``.
        encoded = str(cwd).replace("/", "-")
        candidates = list(projects_dir.glob(f"*{encoded}*/*.jsonl"))
        if not candidates:
            # Fall back to the newest JSONL across all projects so the
            # watcher still has a signal even if encoding differs.
            candidates = list(projects_dir.glob("*/*.jsonl"))
        if not candidates:
            return None
        try:
            return max(candidates, key=lambda p: p.stat().st_mtime)
        except OSError:
            return None

    def detect_tier(self) -> ApiTierInfo | None:
        """Detect Claude API tier based on environment and API key type.

        Checks ANTHROPIC_API_KEY prefix to determine tier:
        - sk-ant-api03... = Pro tier
        - sk-ant-api01... = Plus tier
        - Other = Free tier

        Returns:
            ApiTierInfo with detected tier and rate limits.
        """
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")

        if not api_key:
            return None

        # Determine tier from API key prefix
        if api_key.startswith("sk-ant-api03"):
            tier = ApiTier.PRO
            rate_limit = RateLimit(
                requests_per_minute=1000,
                tokens_per_minute=50000,
            )
        elif api_key.startswith("sk-ant-api01"):
            tier = ApiTier.PLUS
            rate_limit = RateLimit(
                requests_per_minute=100,
                tokens_per_minute=10000,
            )
        else:
            tier = ApiTier.FREE
            rate_limit = RateLimit(
                requests_per_minute=20,
                tokens_per_minute=2000,
            )

        return ApiTierInfo(
            provider=ProviderType.CLAUDE,
            tier=tier,
            rate_limit=rate_limit,
            is_active=True,
        )
