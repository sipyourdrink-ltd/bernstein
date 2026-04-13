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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

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
    }
)


def build_filtered_env(
    extra_keys: Iterable[str] = (),
    *,
    secrets_config: SecretsConfig | None = None,
) -> dict[str, str]:
    """Build a filtered copy of the environment safe for agent subprocesses.

    Only variables in the base allowlist or ``extra_keys`` are included.
    All other variables (database credentials, CI tokens, secrets for
    unrelated services, etc.) are excluded.

    When ``secrets_config`` is provided, secrets are loaded from the
    configured provider and injected into the returned environment.
    If the provider is unavailable, falls back to env vars silently.

    Args:
        extra_keys: Additional variable names to include beyond the base
            allowlist.  Pass the adapter-specific API key name(s) here,
            e.g. ``["ANTHROPIC_API_KEY"]``.
        secrets_config: Optional secrets manager configuration. When set,
            API keys are loaded from the external provider instead of
            (or in addition to) environment variables.

    Returns:
        A fresh dict containing only the allowed variables that are currently
        set in ``os.environ``, plus any secrets from the provider.

    Example::

        env = build_filtered_env(["ANTHROPIC_API_KEY"])
        # env contains PATH, HOME, LANG, ANTHROPIC_API_KEY (if set), etc.
        # env does NOT contain DATABASE_URL, AWS_SECRET_ACCESS_KEY, etc.
    """
    allowed = _BASE_ALLOWLIST | frozenset(extra_keys)
    env = {k: v for k, v in os.environ.items() if k in allowed}

    # Ensure PYTHONPATH includes directories needed by bernstein-worker.
    # When the orchestrator runs via ``uv run``, sys.executable may point
    # to the framework Python rather than the venv Python.  Without an
    # explicit PYTHONPATH the worker subprocess cannot import bernstein.
    if "PYTHONPATH" not in env:
        import sys

        src_dirs = [p for p in sys.path if p and os.path.isdir(p)]
        if src_dirs:
            env["PYTHONPATH"] = os.pathsep.join(src_dirs)

    # Overlay secrets from external provider (if configured).
    if secrets_config is not None:
        from bernstein.core.secrets import load_secrets

        provider_secrets = load_secrets(secrets_config)
        if provider_secrets:
            logger.debug(
                "Injecting %d secret(s) from %s into agent env",
                len(provider_secrets),
                secrets_config.provider,
            )
            env.update(provider_secrets)

    return env
