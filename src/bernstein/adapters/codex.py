"""OpenAI Codex CLI adapter.

Last verified against upstream @openai/codex 0.152.1 on 2026-09-02.
Install: ``npm i -g @openai/codex`` (or ``brew install --cask codex``).

.. important::
   **codex >= 0.152 speaks only the Responses API.** ``wire_api = "chat"`` in a
   custom provider block is a hard startup error, not a fallback::

       Error loading config.toml: `wire_api = "chat"` is no longer supported.
       How to fix: set `wire_api = "responses"` in your provider config.

   This adapter allow-lists ``OPENAI_BASE_URL``, which advertises support for
   custom OpenAI-compatible endpoints. That support is narrower than it looks:
   an endpoint serving only ``/v1/chat/completions`` **cannot drive codex at
   all**, however compatible it is otherwise. Point ``OPENAI_BASE_URL`` at a
   deployment that implements ``/v1/responses`` (issue #5314).
Recommended models: ``gpt-5.5`` (GA 2026-04-24), which is also the pinned
fallback, or ``gpt-5.4-mini`` for cheap work.  ``gpt-5.4`` is no longer served
on the ChatGPT-account auth path.  The o-series reasoning models (``o3``,
``o4-mini``) are also accepted by the CLI.

Sandbox posture is derived from the adapter's declared
:class:`~bernstein.adapters._contract.DangerousModeStrategy` rather than
hardcoded, because the right answer depends on where the CLI runs.

``codex exec --sandbox workspace-write`` is implemented with bubblewrap on
Linux, and bubblewrap needs an unprivileged user namespace to start. A runner
that already provides isolation typically denies exactly that: a container
started with ``--cap-drop ALL --security-opt no-new-privileges:true``, or a
host with unprivileged user namespaces disabled, makes every model-issued
shell command fail with ``bwrap: No permissions to create a new namespace``.
The failure is silent from the orchestrator's side -- ``codex exec`` still
emits ``turn.completed`` and exits 0 after producing an empty diff -- so the
run reads as a model that had nothing to do rather than as a sandbox that
could not initialise.

An operator whose runner is already isolated therefore declares the escalated
strategy, and the spawn passes ``--dangerously-bypass-approvals-and-sandbox``
instead. Upstream's own help text scopes that flag the same way: "Intended
solely for running in environments that are externally sandboxed." The
un-escalated default stays ``--sandbox workspace-write``, so a spawn on a
plain host keeps the vendor sandbox.

The escalated strategy is a blunt instrument, though: it means "no permission
surface exists to skip" and says nothing about *why* skipping is safe. The
narrower route is the host-isolation declaration (issue #5341). An operator
running inside a container or VM they control states the isolation tier the
host applies and the evidence for it -- ``host_isolation_tier`` and
``host_isolation_evidence``, resolved through the normal config precedence
chain -- and the spawner injects it into this adapter, which advertises that
it consumes one via :attr:`CodexAdapter.consumes_host_isolation`. A declared
``container`` or ``vm`` tier is a boundary that replaces what bubblewrap would
have supplied, so the vendor sandbox is dropped; ``process`` and ``none`` are
not, so it stays. The declaration is written to the HMAC audit chain at the
dispatch seam, which is what makes it an operator statement on the record
rather than an unexplained flag flip.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

from bernstein.adapters._contract import DangerousModeStrategy
from bernstein.adapters.base import (
    DEFAULT_TIMEOUT_SECONDS,
    CLIAdapter,
    SpawnResult,
    append_system_addendum,
    build_worker_cmd,
)
from bernstein.adapters.env_isolation import build_filtered_env
from bernstein.core.models import ApiTier, ApiTierInfo, ModelConfig, ProviderType, RateLimit
from bernstein.core.platform_compat import process_group_popen_kwargs

logger = logging.getLogger(__name__)

# Codex authenticates via either OPENAI_API_KEY or a ChatGPT OAuth session that
# ``codex login`` stores in ~/.codex/auth.json. ~/.codex is already the canonical
# Codex config dir (see agent_discovery and preflight), so its auth.json sibling
# is the right signal for "an OAuth session exists".
_CODEX_AUTH_FILE = Path.home() / ".codex" / "auth.json"

# Claude cascade tier names are not valid Codex model identifiers. If an upstream
# selector hands one to this adapter (e.g. the high-stakes-role default), fall
# back to a Codex model so ``codex exec -m`` receives something the CLI accepts.
# The real selection fix lives in the spawner; this is a last-resort safety net.
#
# ``gpt-5.4`` was the pin until 2026-09-02 and no longer works on the
# ChatGPT-account auth path: the backend rejects it with HTTP 400
# ``invalid_request_error`` -- "The 'gpt-5.4' model is not supported when using
# Codex with a ChatGPT account" -- and the account's own model catalogue lists
# only ``gpt-5.5`` and ``gpt-5.4-mini``. A last-resort fallback that 400s is
# worse than no fallback, so the pin follows the recommended GA model, which
# both auth paths accept.
_DEFAULT_CODEX_MODEL = "gpt-5.5"
_CLAUDE_TIER_MODELS = frozenset({"opus", "sonnet", "haiku"})

#: Sandbox argv for a spawn that keeps the vendor sandbox. Codex implements
#: this profile with bubblewrap on Linux, so it needs an unprivileged user
#: namespace the host must allow.
_SANDBOXED_ARGS: tuple[str, ...] = ("--sandbox", "workspace-write")

#: Sandbox argv for a spawn whose runner already provides isolation. Upstream
#: scopes the flag to exactly that case: "Intended solely for running in
#: environments that are externally sandboxed."
_BYPASS_SANDBOX_FLAG = "--dangerously-bypass-approvals-and-sandbox"

#: Declared host-isolation tiers that make the vendor sandbox redundant (#5341).
#:
#: These are ``SandboxTier`` values, held as plain strings because
#: ``SandboxTier`` lives in :mod:`bernstein.adapters.capability_profile` and the
#: ``adapters-independent`` import-linter contract forbids one adapter module
#: from reaching another. ``SandboxTier`` is a ``StrEnum``, so a member
#: assigned to :attr:`CodexAdapter.host_isolation` compares equal to its value
#: here; ``tests/unit/test_adapter_codex.py`` pins these names against the enum
#: so a rename cannot leave this set silently stale.
#:
#: Which tiers belong is an adapter judgement, not a vocabulary one: the vendor
#: sandbox is dropped only for a boundary that replaces what bubblewrap would
#: have supplied. ``container`` and ``vm`` do. ``process`` does not -- seccomp
#: or a restricted user confines the agent's own commands without giving codex
#: the user namespace it needs -- and neither does ``none``.
_TIERS_REPLACING_VENDOR_SANDBOX = frozenset({"container", "vm"})

#: The tier assumed when no operator declaration reaches the adapter: no
#: isolation, so every vendor sandbox stays on.
_UNDECLARED_HOST_ISOLATION = "none"


def _has_codex_auth() -> bool:
    """Return True when Codex has a usable credential: an API key or OAuth session."""
    return bool(os.environ.get("OPENAI_API_KEY")) or _CODEX_AUTH_FILE.exists()


def _codex_model(model: str) -> str:
    """Map a Claude cascade tier name to the Codex default; pass any other model through."""
    if model in _CLAUDE_TIER_MODELS:
        logger.warning(
            "CodexAdapter: model %r is a Claude tier name Codex cannot run; using %r "
            "instead. Set role_model_policy.<role>.model or default_model to a Codex "
            "model (e.g. gpt-5.5) to choose explicitly.",
            model,
            _DEFAULT_CODEX_MODEL,
        )
        return _DEFAULT_CODEX_MODEL
    return model


#: Bubblewrap's refusal when the kernel disallows unprivileged user namespaces.
#: Codex implements ``--sandbox workspace-write`` with bubblewrap, so in a
#: capability-dropped container every shell call the model issues fails with
#: this while ``codex exec`` still emits ``turn.completed`` and exits 0.
_BWRAP_DENIED = "No permissions to create a new namespace"


def detect_sandbox_failure(log_text: str) -> tuple[str, int, int] | None:
    """Return ``(detail, failed, total)`` when EVERY shell call was refused.

    Issue #5314: a run in which all 16 shell commands failed, nothing changed
    and ~194k tokens were spent still exited 0 and reported ``turn.completed``.
    That is indistinguishable from a model that had nothing to do, which makes
    it the worst of the available failure modes.

    ``_probe_fast_exit`` cannot catch this: it treats an early NON-ZERO exit as
    a spawn failure, and here the exit code is zero. So the signal has to come
    from the event stream rather than the status.

    Deliberately narrow, because the cost of a false positive is aborting a run
    that actually worked:

    * at least one ``command_execution`` item must be present -- a run that
      shelled out zero times is not evidence of anything;
    * EVERY one of them must have failed;
    * at least one must carry bubblewrap's specific refusal, so an agent whose
      commands merely returned non-zero (a failing test suite, a missing file)
      is not reported as a sandbox failure.

    Returns ``None`` when the run does not match, so the caller leaves the
    result untouched.
    """
    if not log_text or _BWRAP_DENIED not in log_text:
        return None

    total = 0
    failed = 0
    saw_bwrap = False
    for line in log_text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        nested = event.get("item")
        item: dict[str, Any] = nested if isinstance(nested, dict) else event
        if "command_execution" not in (item.get("item_type"), item.get("type")):
            continue
        total += 1
        exit_code = item.get("exit_code")
        output = str(item.get("aggregated_output") or item.get("output") or "")
        if _BWRAP_DENIED in output:
            saw_bwrap = True
        if exit_code not in (0, None):
            failed += 1

    if total == 0 or failed != total or not saw_bwrap:
        return None

    detail = (
        f"every shell command was refused by the sandbox ({failed}/{total}). "
        "codex implements --sandbox workspace-write with bubblewrap, which "
        "cannot start in a capability-dropped container on a kernel without "
        "unprivileged user namespaces. The run exited 0 and reported success "
        "while changing nothing. Re-run with a sandbox mode the environment "
        "supports, or enable unprivileged user namespaces on the host."
    )
    return detail, failed, total


class CodexAdapter(CLIAdapter):
    """Spawn and monitor OpenAI Codex CLI sessions."""

    registry_name = "codex"
    # Provider-string aliases this adapter resolves from in
    # ``_infer_adapter_name_for_provider``. NOTE: "openai" and "gpt" are
    # broad aliases that historically also matched the openai_agents
    # provider string via substring search (see 042bcbd0). The registry
    # requires exact provider-name matches, so this alias set only ever
    # matches a provider literally named "codex", "openai", or "gpt" --
    # it can no longer swallow "openai_agents".
    provides = ("codex", "openai", "gpt")
    # Default model when no operator-pinned model reaches this adapter. Read by
    # the spawner to substitute Claude tier names for non-Claude adapters.
    default_model = _DEFAULT_CODEX_MODEL
    external_endpoints = (("api.openai.com", 443),)
    # OpenAI returns HTTP 429 with ``rate_limit_exceeded`` /
    # ``insufficient_quota`` error codes; the meter records both under
    # the same provider label.
    rate_limit_provider = "openai"
    #: Marker the spawner looks for before injecting the operator's
    #: host-isolation declaration (#5341). Only an adapter that owns a vendor
    #: sandbox has anything to do with one; every other adapter leaves this at
    #: the inherited default and is never touched by the injection.
    consumes_host_isolation: bool = True

    def __init__(self) -> None:
        super().__init__()
        #: Isolation the host is declared to apply to this process. Carries a
        #: ``SandboxTier`` value (see ``_TIERS_REPLACING_VENDOR_SANDBOX`` for
        #: why it is typed as the string rather than the enum). Set by the
        #: spawner from resolved config; left at "no declaration" otherwise, so
        #: an adapter constructed directly keeps the vendor sandbox.
        self.host_isolation: str = _UNDECLARED_HOST_ISOLATION
        #: The operator's description of that isolation, recorded verbatim.
        self.host_isolation_evidence: str = ""
        # The drop is one posture decision, not a per-spawn one: warning on
        # every spawn would bury the single line an operator has to read.
        self._host_isolation_warned = False

    def _dangerous_mode(self) -> DangerousModeStrategy:
        """Return the declared dangerous-mode strategy for this adapter."""
        declared = getattr(self.strategy(), "dangerous_mode", DangerousModeStrategy.UNSUPPORTED)
        return declared if isinstance(declared, DangerousModeStrategy) else DangerousModeStrategy.UNSUPPORTED

    def _sandbox_bypassed(self) -> bool:
        """Whether this spawn runs Codex without its own sandbox.

        Only :attr:`DangerousModeStrategy.ALWAYS_ON` bypasses. That value
        means "no permission surface exists to skip", which is what the
        bypass flag produces: no approval prompt and no vendor sandbox. The
        shipped declaration for this adapter is
        :attr:`DangerousModeStrategy.CLI_FLAG` -- a flag pins the posture,
        and the posture it pins is the sandboxed one -- so a default spawn
        keeps ``--sandbox workspace-write``. An operator whose runner is
        already isolated declares ``ALWAYS_ON`` instead.

        The second route is the operator's host-isolation declaration
        (#5341): a host declared at ``container`` or ``vm`` supplies the
        boundary the vendor sandbox would have supplied, so the vendor
        sandbox is redundant rather than merely inconvenient. ``process``
        and ``none`` are not that boundary and keep it.
        """
        return self._dangerous_mode() is DangerousModeStrategy.ALWAYS_ON or self.host_isolation_drops_vendor_sandbox()

    def host_isolation_drops_vendor_sandbox(self) -> bool:
        """Whether the declared host isolation supersedes Codex's own sandbox.

        Part of the ``consumes_host_isolation`` contract rather than an
        internal detail: the spawner records whether the declaration it
        injected actually dropped a sandbox, and only the adapter knows which
        tiers replace the one it ships.
        """
        return str(self.host_isolation) in _TIERS_REPLACING_VENDOR_SANDBOX

    def _warn_host_isolation_drop_once(self) -> None:
        """Name the declaration the drop rests on, once per adapter instance."""
        if self._host_isolation_warned:
            return
        self._host_isolation_warned = True
        logger.warning(
            "codex: vendor sandbox dropped; host isolation declared tier=%s evidence=%s",
            str(self.host_isolation),
            self.host_isolation_evidence or "none given",
        )

    def _sandbox_args(self) -> tuple[str, ...]:
        """Return the sandbox argv for one spawn, derived from the declaration."""
        if not self._sandbox_bypassed():
            return _SANDBOXED_ARGS
        if self.host_isolation_drops_vendor_sandbox():
            self._warn_host_isolation_drop_once()
        if self._dangerous_mode() is DangerousModeStrategy.ALWAYS_ON:
            logger.warning(
                "CodexAdapter: dangerous_mode=%s, so this spawn passes %s -- model-issued "
                "shell commands run with no Codex sandbox. Declare this only when the "
                "runner itself provides the isolation.",
                DangerousModeStrategy.ALWAYS_ON,
                _BYPASS_SANDBOX_FLAG,
            )
        return (_BYPASS_SANDBOX_FLAG,)

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
        self.refuse_multimodal_if_needed(multimodal_context)
        self.enforce_network_policy()
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        output_path = workdir / ".sdd" / "runtime" / f"{session_id}.last-message.txt"

        if not _has_codex_auth():
            logger.warning(
                "CodexAdapter: no OPENAI_API_KEY and no Codex OAuth session "
                "(~/.codex/auth.json) detected - spawn may fail until `codex login` is "
                "run or OPENAI_API_KEY is set",
            )

        model = _codex_model(model_config.model)
        cmd = [
            "codex",
            "exec",
            *self._sandbox_args(),
            "-m",
            model,
            "--json",
            "-o",
            str(output_path),
        ]
        # Session-id binding is contract-driven: the argv gains a flag only
        # when the contract names one. ``codex exec`` exposes no flag that
        # accepts a caller-supplied session id -- only a ``resume
        # <SESSION_ID>`` subcommand, which reattaches to an existing session
        # and cannot bind one at spawn time -- so the codex contract names no
        # flag and this stays an empty list (issue #4135). The derived id is
        # still recorded in orchestrator state for cross-reference.
        cmd.extend(self.session_id_args(session_id))
        # No separate system-prompt channel -- graft any addendum onto the prompt so
        # completion / heartbeat instructions still reach the agent. Empty addenda are no-ops.
        cmd.append(append_system_addendum(prompt, system_addendum))

        # Wrap with bernstein-worker for process visibility
        pid_dir = workdir / ".sdd" / "runtime" / "pids"
        wrapped_cmd = build_worker_cmd(
            cmd,
            role=session_id.rsplit("-", 1)[0],
            session_id=session_id,
            pid_dir=pid_dir,
            workdir=workdir,
            log_path=log_path,
            model=model,
        )

        env = build_filtered_env(["OPENAI_API_KEY", "OPENAI_ORG_ID", "OPENAI_BASE_URL"])
        with log_path.open("w") as log_file:
            try:
                proc = subprocess.Popen(
                    wrapped_cmd,
                    cwd=workdir,
                    env=env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    **process_group_popen_kwargs(),
                )
            except FileNotFoundError as exc:
                raise RuntimeError("codex not found in PATH. Install it with: npm install -g @openai/codex") from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing codex: {exc}") from exc

        self._probe_fast_exit(proc, log_path, provider_name="codex")

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)

        # #5314 - the fast-exit probe above only catches an early NON-ZERO exit.
        # A sandbox that refuses every shell call still exits 0, so the run has
        # to be judged from the event stream after it finishes.
        thread = threading.Thread(target=self._flag_sandbox_failure, args=(proc, log_path, result), daemon=True)
        thread.start()
        result.post_exit_thread = thread
        return result

    def _flag_sandbox_failure(self, proc: subprocess.Popen, log_path: Path, result: SpawnResult) -> None:
        """Mark a run whose every shell call the sandbox refused (#5314).

        Runs after the process exits. Sets ``abort_reason`` so a caller sees a
        refused run rather than a successful one that happened to change
        nothing; it does not raise, because by this point the process is gone
        and there is nothing left to fail.
        """
        try:
            proc.wait()
            detected = detect_sandbox_failure(log_path.read_text(encoding="utf-8", errors="replace"))
        except Exception as exc:  # bookkeeping must never wedge the worker
            logger.debug("codex: sandbox-failure check skipped (%s)", type(exc).__name__)
            return

        if detected is None:
            return
        from bernstein.core.models import AbortReason

        detail, failed, total = detected
        result.abort_reason = AbortReason.PERMISSION_DENIED
        result.abort_detail = detail
        logger.error("CodexAdapter: %s", detail)
        logger.error(
            "CodexAdapter: %d/%d shell commands refused; the run reported success regardless",
            failed,
            total,
        )

    def name(self) -> str:
        return "Codex"

    def detect_tier(self) -> ApiTierInfo | None:
        """Detect Codex API tier based on environment configuration.

        Checks OPENAI_API_KEY and OPENAI_ORG_ID to determine tier:
        - With organization ID = Enterprise tier
        - With paid account (sk-proj...) = Pro tier
        - Default = Free tier

        Returns:
            ApiTierInfo with detected tier and rate limits.
        """
        api_key = os.environ.get("OPENAI_API_KEY", "")
        org_id = os.environ.get("OPENAI_ORG_ID", "")

        if not api_key:
            return None

        # Determine tier from environment and key format
        if org_id:
            tier = ApiTier.ENTERPRISE
            rate_limit = RateLimit(
                requests_per_minute=500,
                tokens_per_minute=90000,
            )
        elif api_key.startswith("sk-proj"):
            tier = ApiTier.PRO
            rate_limit = RateLimit(
                requests_per_minute=100,
                tokens_per_minute=10000,
            )
        elif api_key.startswith("sk-"):
            tier = ApiTier.PLUS
            rate_limit = RateLimit(
                requests_per_minute=60,
                tokens_per_minute=5000,
            )
        else:
            tier = ApiTier.FREE
            rate_limit = RateLimit(
                requests_per_minute=20,
                tokens_per_minute=2000,
            )

        return ApiTierInfo(
            provider=ProviderType.CODEX,
            tier=tier,
            rate_limit=rate_limit,
            is_active=True,
        )
