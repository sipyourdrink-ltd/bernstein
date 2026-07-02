"""Environment variable isolation for spawned agents.

Agents should only receive the variables they need to function.
This prevents credential leakage when the orchestrator process has many
secrets loaded (database credentials, CI tokens, API keys for other
services, etc.).

Usage::

    from bernstein.adapters.env_isolation import build_filtered_env

    env = build_filtered_env(extra_keys=["ANTHROPIC_API_KEY"])
    subprocess.Popen(cmd, env=env, ...)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from bernstein.core.credential_scoping import AgentCredentialPolicy
    from bernstein.core.secrets import SecretsConfig

logger = logging.getLogger(__name__)

# Variables always passed to every spawned agent, regardless of role.
# This is the minimal set required for any CLI coding agent to function correctly.
_BASE_ALLOWLIST: frozenset[str] = frozenset(
    {
        # --- Executable discovery ---
        "PATH",
        # --- User home directory ---
        # Claude Code, aider, gemini CLI etc. read ~/.config, ~/.claude, ~/.cache
        "HOME",
        # --- Windows system variables ---
        # Without SYSTEMROOT/WINDIR, Windows processes fail to locate system DLLs
        # and exit with code -1 (0xFFFFFFFF) before even starting
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",  # Path to cmd.exe, needed for shell operations
        "APPDATA",
        "LOCALAPPDATA",
        "USERPROFILE",  # Windows equivalent of HOME
        "SystemDrive",
        "ProgramFiles",
        "ProgramFiles(x86)",
        "ProgramData",
        "CommonProgramFiles",
        "CommonProgramFiles(x86)",
        "USERNAME",  # Windows equivalent of USER
        # --- Locale / text encoding ---
        # Many CLIs break on non-UTF-8 terminals without these
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LC_MESSAGES",
        # --- User identity ---
        # git uses USER/LOGNAME as fallback for commit authorship
        "USER",
        "LOGNAME",
        # --- Shell ---
        # Some CLIs invoke subshells for hooks or build steps
        "SHELL",
        # --- Terminal ---
        # Controls colour output, readline behaviour, column width
        "TERM",
        "COLORTERM",
        "COLUMNS",
        "LINES",
        # --- Temporary directory ---
        # macOS uses a per-user path like /var/folders/…; without TMPDIR
        # some tools fall back to /tmp which may not be writable
        "TMPDIR",
        "TMP",
        "TEMP",
        # --- XDG base directories (Linux standard) ---
        "XDG_RUNTIME_DIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        # --- Git authoring ---
        # Agents commit code; these ensure correct attribution in git history
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        # --- SSH for git operations ---
        # Needed when agents push code over SSH
        "SSH_AUTH_SOCK",
        "GIT_SSH_COMMAND",
        "GIT_SSH",
        # --- Python runtime ---
        # The bernstein-worker subprocess uses sys.executable to run itself;
        # PYTHONPATH / VIRTUAL_ENV ensure the bernstein package stays importable
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "CONDA_DEFAULT_ENV",
        "CONDA_PREFIX",
        # --- Node.js version managers ---
        # Claude Code, Codex, Gemini CLI are Node.js-based; NVM mangles PATH
        # so NVM_DIR / NVM_BIN must be preserved alongside PATH
        "NVM_DIR",
        "NVM_BIN",
        "NVM_PATH",
        "NODE_PATH",
        # --- Bernstein hook auth ---
        # Spawned agents post tool events to ``/hooks/{session_id}`` signed
        # with HMAC-SHA256 keyed by BERNSTEIN_HOOK_SECRET (falling back to
        # BERNSTEIN_AUTH_TOKEN - see adapters/claude.py:568 and
        # core/routes/hooks.py:60). Without these in the allowlist the
        # agent's env has an empty secret, openssl HMACs over "", the server
        # rejects every hook POST with 401, and the orchestrator can't see
        # heartbeats/completion markers until the watchdog fires (up to
        # 120s after work actually finished).  These are NOT provider
        # credentials - they're the agent's own return channel, so passing
        # them through the allowlist is safe.
        "BERNSTEIN_AUTH_TOKEN",
        "BERNSTEIN_HOOK_SECRET",
        # ``BERNSTEIN_AUTH_DISABLED`` must propagate so the agent's hook
        # runner skips signing in dev mode (server likewise skips HMAC
        # verification).  Without it the agent signs while the server
        # bypasses verification - not a failure, but keeps behaviour
        # symmetric.
        "BERNSTEIN_AUTH_DISABLED",
        # ``BERNSTEIN_SERVER_URL`` tells the agent's server-facing plumbing
        # (CLI helpers, hook posts) where the central server lives.  Remote
        # workers export it before spawning; without it in the allowlist
        # the agent falls back to http://localhost:8052 and its progress
        # reporting never reaches the central server.  It is a URL, not a
        # credential, so passing it through is safe.
        "BERNSTEIN_SERVER_URL",
    }
)


# Host env var an operator sets to *re-permit* embedded agent-team spawning.
# Off by default: absent (or any value other than the truthy set below) keeps
# the deny-by-default posture.  See :func:`_embedded_teams_opt_in`.
_EMBEDDED_TEAMS_OPT_IN_VAR = "BERNSTEIN_ALLOW_EMBEDDED_AGENT_TEAMS"

# Exact gate variables a spawned worker could use to launch its own autonomous
# teammate instances.  ``CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`` is the Claude
# Code adapter's own opt-in variable; teammates it spawns inherit the lead's
# permission mode and run OUTSIDE the per-worker HMAC audit trail, breaking the
# one-worker-one-audit-trail invariant our replay guarantee depends on.  These
# are stripped unconditionally (even if an operator widens ``extra_keys`` to
# include them) unless the host explicitly opts in.
_EMBEDDED_TEAMS_DENYLIST: frozenset[str] = frozenset(
    {
        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS",
    }
)

# Forward-looking suffix match: any variable ending in one of these is treated
# as an embedded-orchestration gate and denied by default, so a future adapter
# introducing e.g. ``<VENDOR>_EXPERIMENTAL_AGENT_TEAMS`` or ``<VENDOR>_AGENT_TEAMS``
# is covered without a code change.  Kept narrow to avoid stripping unrelated
# vars that merely mention "agent".
_EMBEDDED_TEAMS_DENY_SUFFIXES: tuple[str, ...] = (
    "_EXPERIMENTAL_AGENT_TEAMS",
    "_AGENT_TEAMS",
)


def _embedded_teams_opt_in() -> bool:
    """Return True only when the operator explicitly re-permits embedded teams.

    Truthy set is deliberately narrow (``1``/``true``/``yes``/``on``, case
    insensitive) so a stray empty or ``0`` value keeps deny-by-default.
    """
    raw = os.environ.get(_EMBEDDED_TEAMS_OPT_IN_VAR, "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _is_embedded_teams_gate(key: str) -> bool:
    """Return True when ``key`` is an embedded agent-team spawning gate."""
    if key in _EMBEDDED_TEAMS_DENYLIST:
        return True
    return any(key.endswith(suffix) for suffix in _EMBEDDED_TEAMS_DENY_SUFFIXES)


def record_embedded_teams_opt_in(
    *,
    adapter: str,
    session_id: str,
    audit_dir: Path | None = None,
) -> None:
    """Attest an operator opt-in that re-permits embedded agent-team spawning.

    When ``BERNSTEIN_ALLOW_EMBEDDED_AGENT_TEAMS`` re-permits the gate, the
    embedded team must be *attested* rather than invisible: we append an
    ``adapter.embedded_agent_teams_enabled`` event to the per-worker HMAC audit
    chain so an auditor replaying the log can see exactly which spawn opted a
    teammate hierarchy back in.  Removing the audit chain removes the meaning
    of this feature, not just its log - the whole point is that a re-permitted
    team stays inside the one-worker-one-audit-trail invariant.

    Failures (missing key, disk full) are caught and logged; audit emission
    must never mask the spawn.

    Args:
        adapter: Adapter name performing the spawn (e.g. ``"claude"``).
        session_id: Spawn session identifier (becomes the audit resource id).
        audit_dir: Audit directory override.  Defaults to ``.sdd/audit`` under
            the current working directory, matching the spawner convention.
    """
    try:
        from bernstein.core.security.audit import AuditLog

        target_dir = audit_dir if audit_dir is not None else Path.cwd() / ".sdd" / "audit"
        audit = AuditLog(audit_dir=target_dir)
        audit.log(
            event_type="adapter.embedded_agent_teams_enabled",
            actor=adapter,
            resource_type="agent_session",
            resource_id=session_id,
            details={
                "adapter": adapter,
                "opt_in_var": _EMBEDDED_TEAMS_OPT_IN_VAR,
            },
        )
    except Exception as exc:  # audit must never mask the spawn - log and move on
        logger.warning(
            "Could not emit embedded_agent_teams_enabled audit event for %s: %s",
            session_id,
            exc,
        )


def build_filtered_env(
    extra_keys: Iterable[str] = (),
    *,
    secrets_config: SecretsConfig | None = None,
    agent_id: str | None = None,
    role: str | None = None,
    credential_policy: AgentCredentialPolicy | None = None,
) -> dict[str, str]:
    """Build a filtered copy of the environment safe for agent subprocesses.

    Only variables in the base allowlist or ``extra_keys`` are included.
    All other variables (database credentials, CI tokens, secrets for
    unrelated services, etc.) are excluded.

    When ``secrets_config`` is provided, secrets are loaded from the
    configured provider and injected into the returned environment.
    If the provider is unavailable, falls back to env vars silently.

    When ``agent_id`` is provided, ``extra_keys`` is further filtered
    through the :class:`~bernstein.core.credential_scoping.AgentCredentialPolicy`
    so each agent only inherits the credential env vars it is
    authorised to see. If no ``credential_policy`` is
    supplied the module-level default is consulted.

    Args:
        extra_keys: Additional variable names to include beyond the base
            allowlist.  Pass the adapter-specific API key name(s) here,
            e.g. ``["ANTHROPIC_API_KEY"]``.
        secrets_config: Optional secrets manager configuration. When set,
            API keys are loaded from the external provider instead of
            (or in addition to) environment variables.
        agent_id: Optional agent identifier for per-agent credential
            scoping.  When supplied, the credential policy is consulted
            to narrow ``extra_keys`` to the subset this agent may see.
        role: Optional role name used as a fallback when the agent has
            no explicit rule in the policy.
        credential_policy: Explicit policy override.  Primarily used in
            tests; production code should rely on the module-level
            default installed at orchestrator startup.

    Returns:
        A fresh dict containing only the allowed variables that are currently
        set in ``os.environ``, plus any secrets from the provider.

    Raises:
        bernstein.core.credential_scoping.UnknownCredentialKeyError: If
            any requested ``extra_keys`` entry is not declared in the
            active policy's ``known_keys``.
        bernstein.core.credential_scoping.AgentNotScopedError: If the
            active policy is enabled and ``agent_id`` has no matching
            rule.

    Example::

        env = build_filtered_env(["ANTHROPIC_API_KEY"])
        # env contains PATH, HOME, LANG, ANTHROPIC_API_KEY (if set), etc.
        # env does NOT contain DATABASE_URL, AWS_SECRET_ACCESS_KEY, etc.
    """
    requested_extra = frozenset(extra_keys)

    # Narrow credential env vars through the per-agent policy when an
    # agent identity is supplied.  Policy is consulted eagerly so typos
    # surface during spawn rather than silently granting nothing.
    if agent_id is not None:
        from bernstein.core.credential_scoping import (
            get_default_policy,
            scoped_credential_keys,
        )

        effective_policy = credential_policy if credential_policy is not None else get_default_policy()
        scoped = scoped_credential_keys(
            agent_id,
            requested_extra,
            role=role,
            policy=effective_policy,
        )
        requested_extra = frozenset(scoped)

    allowed = _BASE_ALLOWLIST | requested_extra
    env = {k: v for k, v in os.environ.items() if k in allowed}

    # Deny-by-default: strip embedded agent-team spawning gates even if an
    # operator-widened allowlist (or a future permissive path) let one through.
    # A teammate spawned by a worker inherits the lead's permission mode and
    # runs outside this worker's HMAC audit trail, so we close the hole here
    # rather than trusting every caller's ``extra_keys``.  An explicit host
    # opt-in re-permits it; the spawn path is expected to attest the decision
    # via :func:`record_embedded_teams_opt_in` so the team stays auditable.
    if not _embedded_teams_opt_in():
        env = {k: v for k, v in env.items() if not _is_embedded_teams_gate(k)}

    # Ensure PYTHONPATH includes directories needed by bernstein-worker.
    # When the orchestrator runs via ``uv run``, sys.executable may point
    # to the framework Python rather than the venv Python.  Without an
    # explicit PYTHONPATH the worker subprocess cannot import bernstein.
    if "PYTHONPATH" not in env:
        import sys

        src_dirs = [p for p in sys.path if p and Path(p).is_dir()]
        if src_dirs:
            env["PYTHONPATH"] = os.pathsep.join(src_dirs)

    # Overlay secrets from external provider (if configured).
    if secrets_config is not None:
        from bernstein.core.secrets import load_secrets

        provider_secrets = load_secrets(secrets_config)
        if provider_secrets:
            # Only the count and provider name are logged, not secret values.
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
            logger.debug(
                "Injecting %d secret(s) from %s into agent env",
                len(provider_secrets),
                secrets_config.provider,
            )
            env.update(provider_secrets)

    return env
