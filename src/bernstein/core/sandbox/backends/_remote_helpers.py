"""Shared helpers for remote sandbox backends (E2B, Modal).

The E2B and Modal :class:`SandboxBackend` implementations are structurally
very similar because they both wrap a third-party Python SDK behind the
same :class:`~bernstein.core.sandbox.backend.SandboxSession` protocol. A
handful of patterns repeat across them verbatim:

1. POSIX path resolution against a per-session workdir.
2. Session-id allocation with an optional explicit hint.
3. Probing a provider SDK for the first attribute name that exists (the
   SDKs rename methods across versions, so we accept either form).
4. Encoding SDK return values (``str`` or ``bytes``) as ``bytes``.
5. The exec-preamble: closed-session guard, empty-argv guard, cwd /
   timeout / env merging, and the timeout-aware
   :func:`asyncio.wait_for` + :func:`asyncio.to_thread` dance that turns
   a blocking SDK call into an :class:`ExecResult`.

Centralising these primitives here lets each backend module focus on
the truly provider-specific bits (the SDK import, the Sandbox-class
lookup, the exec signature) without repeating shared scaffolding. It
also keeps SonarCloud duplication noise off backends that must
necessarily conform to the same protocol shape.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from bernstein.core.sandbox.backend import ExecResult

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


def resolve_posix_path(workdir: str, path: str) -> str:
    """Resolve ``path`` inside a POSIX sandbox rooted at ``workdir``.

    Absolute paths are returned unchanged; relative paths are joined
    onto ``workdir``. Mirrors the behaviour every remote backend needs
    so it does not get re-implemented per module.

    Args:
        workdir: Absolute POSIX path of the sandbox working directory.
        path: Caller-supplied path, absolute or relative.

    Returns:
        The fully-qualified POSIX path.
    """
    candidate = PurePosixPath(path)
    if candidate.is_absolute():
        return str(candidate)
    return str(PurePosixPath(workdir) / candidate)


def allocate_session_id(prefix: str, hint: str | None = None) -> str:
    """Return a deterministic-looking session id.

    If ``hint`` is provided it is used verbatim (callers supply this to
    pin a specific identifier, typically for resume). Otherwise a fresh
    ``{prefix}-{rand12}`` identifier is minted.

    Args:
        prefix: Backend name used as the id prefix (e.g. ``"bernstein-e2b"``).
        hint: Optional explicit id. When non-empty, returned unchanged.

    Returns:
        The chosen session identifier.
    """
    if hint:
        return hint
    return f"{prefix}-{secrets.token_hex(6)}"


def resolve_sdk_attr(obj: Any, *names: str) -> Any | None:
    """Return the first non-``None`` attribute from ``obj`` by name.

    Provider SDKs rename methods across versions (``files.read`` vs
    ``filesystem.read``, ``commands.run`` vs ``process.run``, etc.). We
    probe each candidate in order and return the first hit, or ``None``
    if no candidate is exposed. Callers raise a friendly
    ``RuntimeError`` when ``None`` comes back.

    Args:
        obj: The provider SDK handle to probe.
        *names: Candidate attribute names, tried in order.

    Returns:
        The resolved attribute, or ``None`` if none exist.
    """
    for name in names:
        candidate = getattr(obj, name, None)
        if candidate is not None:
            return candidate
    return None


def encode_as_bytes(value: Any) -> bytes:
    """Normalise a ``str``/``bytes`` value returned by a provider SDK.

    Many SDK calls (file reads, stdout streams) return either ``str`` or
    ``bytes`` depending on the SDK version. The Bernstein contract
    surfaces bytes; this helper hides the provider-side variability.

    Args:
        value: Value returned by the SDK, typically ``str`` or ``bytes``.

    Returns:
        The value coerced to ``bytes``.
    """
    if isinstance(value, str):
        return value.encode("utf-8")
    return bytes(value)


def merge_exec_env(
    base_env: Mapping[str, str],
    extra: Mapping[str, str] | None,
) -> dict[str, str]:
    """Merge per-call env overrides on top of the session's base env.

    Args:
        base_env: Base environment captured at session-create time.
        extra: Optional per-call overrides. ``None`` leaves the base
            environment unchanged.

    Returns:
        A fresh dict; callers own the result.
    """
    merged = dict(base_env)
    if extra:
        merged.update(extra)
    return merged


def guard_exec_preconditions(closed: bool, session_id: str, cmd: list[str]) -> None:
    """Raise if the session is closed or ``cmd`` is empty.

    Every backend runs this guard before touching the SDK. Centralising
    it means all backends produce identical error messages.

    Args:
        closed: ``True`` if :meth:`SandboxSession.shutdown` has run.
        session_id: Session identifier for the error message.
        cmd: Argv list the caller wants to execute.

    Raises:
        RuntimeError: If the session has been shut down.
        ValueError: If ``cmd`` is empty.
    """
    if closed:
        raise RuntimeError(f"Session {session_id} is closed")
    if not cmd:
        raise ValueError("cmd must be a non-empty argv list")


async def run_exec_with_timeout(
    runner: Callable[[], tuple[int, bytes, bytes]],
    *,
    cmd: list[str],
    timeout: int,
    timeout_slack: int = 5,
) -> ExecResult:
    """Run a blocking SDK call in a worker thread with timeout handling.

    The provider SDKs expose synchronous call patterns; every remote
    backend wraps them with :func:`asyncio.to_thread` and enforces a
    wall-clock cap via :func:`asyncio.wait_for`. This helper wires that
    up and converts the result into an :class:`ExecResult` so callers
    only supply the provider-specific ``runner`` closure.

    Args:
        runner: Blocking callable returning ``(exit_code, stdout_b,
            stderr_b)``.
        cmd: Argv list, used solely for the error message on timeout.
        timeout: Caller-visible timeout, in seconds.
        timeout_slack: Extra seconds to give the worker thread over the
            SDK-side timeout. Defaults to 5s (matches the prior
            hand-rolled implementations). Pass ``0`` when the SDK does
            not have its own timeout (Docker-style).

    Returns:
        An :class:`ExecResult` with the runner's output and the measured
        wall-clock duration.

    Raises:
        TimeoutError: When the runner does not complete within
            ``timeout + timeout_slack`` seconds.
    """
    start = time.monotonic()
    wait_for = timeout + timeout_slack if timeout_slack else timeout
    try:
        exit_code, stdout_b, stderr_b = await asyncio.wait_for(asyncio.to_thread(runner), timeout=wait_for)
    except TimeoutError:
        raise TimeoutError(f"Command {cmd!r} timed out after {timeout}s") from None
    return ExecResult(
        exit_code=exit_code,
        stdout=stdout_b,
        stderr=stderr_b,
        duration_seconds=time.monotonic() - start,
    )


__all__ = [
    "allocate_session_id",
    "encode_as_bytes",
    "guard_exec_preconditions",
    "merge_exec_env",
    "resolve_posix_path",
    "resolve_sdk_attr",
    "run_exec_with_timeout",
]
