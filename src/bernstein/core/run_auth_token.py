"""Persist and read the auto-generated run Bearer token (issue #2794).

When the launcher starts a server without an operator-configured
``BERNSTEIN_AUTH_TOKEN`` it auto-generates an ephemeral token. Historically
that token lived only in the launcher process environment, so any CLI monitor
invoked from a *different* shell (``status``/``recap``/``checkpoint``) or the
TUI poller could not authenticate and the server answered ``401`` - which the
client misreported as "server unreachable".

This module gives the token a workspace-local home: a ``0600`` file under
``.sdd/runtime`` that the launcher writes and out-of-process clients read as a
fallback. The file is created with restrictive permissions *from the start*
(never widen-then-narrow) following the same pattern as
:func:`bernstein.core.security.audit.load_or_create_audit_key`, and the token
value is never logged (see #2762 / #2763).
"""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path

from bernstein.core.defaults import SDD_AUTH_TOKEN

logger = logging.getLogger(__name__)

_TOKEN_FILE_MODE = 0o600


def run_auth_token_path(workdir: Path) -> Path:
    """Return the token file path for *workdir*.

    Args:
        workdir: Project root that owns the ``.sdd`` workspace.

    Returns:
        The workspace-relative ``.sdd/runtime/auth.token`` path resolved
        against *workdir*.
    """
    return workdir / SDD_AUTH_TOKEN


def persist_run_auth_token(workdir: Path, token: str) -> Path | None:
    """Write *token* to the run token file with ``0600`` permissions.

    The file is created restrictive-from-the-start via a temporary sibling
    opened with ``O_WRONLY|O_CREAT|O_EXCL`` and mode ``0600``, then atomically
    renamed over the target. This never exposes a widened-permission window
    and never leaves readers observing a half-written token. A fresh token
    supersedes any previous session's file. The token value is never logged.

    Args:
        workdir: Project root that owns the ``.sdd`` workspace.
        token: The Bearer token to persist.

    Returns:
        The path written, or ``None`` if persistence failed (best-effort:
        the launcher still keeps the token in its own environment).
    """
    target = run_auth_token_path(workdir)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("could not create runtime dir for auth token: %s", exc)
        return None

    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, _TOKEN_FILE_MODE)
    except OSError as exc:
        logger.warning("could not create auth token file: %s", exc)
        return None
    try:
        os.write(fd, token.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.replace(str(tmp), str(target))
    except OSError as exc:
        logger.warning("could not finalize auth token file: %s", exc)
        with contextlib.suppress(OSError):
            os.unlink(str(tmp))
        return None
    return target


def read_run_auth_token(workdir: Path | None = None) -> str | None:
    """Read the persisted run token, if present.

    Args:
        workdir: Project root to resolve the token file against. Defaults to
            the current working directory - the workspace a CLI monitor runs
            in.

    Returns:
        The stripped token string, or ``None`` when the file is absent,
        empty, or unreadable.
    """
    path = run_auth_token_path(workdir if workdir is not None else Path.cwd())
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None
