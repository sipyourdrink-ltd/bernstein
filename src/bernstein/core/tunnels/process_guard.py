"""Guarded process termination for persisted tunnel handles.

Persisted tunnel state in ``.sdd/runtime/tunnels.json`` stores an integer PID,
which is neither an exclusive capability nor a durable process identity.  Every
tunnel teardown path (``gui/cli.py``, ``tunnel_cmd.py``, ``stop_cmd.py``, and
``preview/tunnel_bridge.py``) routes through :func:`terminate_tunnel_process` or
:func:`terminate_tunnel_process_with_reason` so that:

* non-process / special POSIX PIDs (``bool``, ``<= 1`` including ``0``, ``-1``,
  process-group negative targets, and init/container PID ``1``), our own PID
  (``os.getpid()``), and our own process-group leader (``os.getpgrp()``) are
  never signaled;
* providers that manage no child daemon (``tailscale``) are never signaled;
* dead or zombie processes are skipped via :func:`is_process_alive`;
* process argv is inspected via ``/proc/<pid>/cmdline`` first on Linux and
  falls back to ``ps`` on other POSIX systems, failing closed whenever the
  command line cannot be read (including Windows, missing ``ps``, or errors);
  and
* process identity matches the exact basename of ``argv[0]`` (or the script
  basename when launched through an interpreter) rather than an unanchored
  substring anywhere in the command line.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shlex
import signal
import subprocess
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Final, Literal

from bernstein.core.config.platform_compat import IS_WINDOWS
from bernstein.core.orchestration.process_utils import (
    is_process_alive,
    pid_command_line,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from bernstein.core.tunnels.protocol import TunnelHandle, TunnelProvider

logger = logging.getLogger(__name__)

#: Outcome reason returned by :func:`terminate_tunnel_process_with_reason`.
TerminateReason = Literal[
    "signaled",
    "no_signal_provider",
    "invalid_pid",
    "missing_markers",
    "not_alive",
    "unreadable_cmdline",
    "cmdline_mismatch",
    "signal_failed",
]

#: Root of the Linux procfs tree (overridable in unit tests).
_PROC_ROOT: Path = Path("/proc")

#: Providers that manage tunnels out-of-process (no child daemon PID to kill).
_NO_SIGNAL_PROVIDERS: Final[frozenset[str]] = frozenset({"tailscale"})

#: Default binary markers for shipped stream-process tunnel drivers.
_KNOWN_PROVIDER_BINARIES: Final[dict[str, tuple[str, ...]]] = {
    "cloudflared": ("cloudflared",),
    "bore": ("bore",),
    "ngrok": ("ngrok",),
}

#: Executable suffixes normalized away when matching binary basenames.
_EXECUTABLE_SUFFIXES: Final[tuple[str, ...]] = (".exe", ".cmd", ".bat")

_SHELL_INTERPRETERS: Final[frozenset[str]] = frozenset({"sh", "bash", "dash", "zsh", "ksh", "ash", "fish"})
_SHELL_VALUE_OPTS: Final[frozenset[str]] = frozenset({"-o", "+o"})
_SHELL_STOP_FLAGS: Final[frozenset[str]] = frozenset({"c", "s"})

_PYTHON_INTERPRETER_RE: Final[re.Pattern[str]] = re.compile(r"^(?:python|pypy)(?:\d+(?:\.\d+)*)?$")
_PYTHON_VALUE_OPTS: Final[frozenset[str]] = frozenset({"-W", "-X", "--check-hash-based-pycs"})

_NODE_INTERPRETERS: Final[frozenset[str]] = frozenset({"node", "nodejs", "deno", "bun"})
_NODE_STOP_OPTS: Final[frozenset[str]] = frozenset({"-e", "--eval", "-p", "--print"})
_NODE_VALUE_OPTS: Final[frozenset[str]] = frozenset(
    {"-r", "--require", "--import", "--loader", "--experimental-loader", "-C", "--conditions"}
)

_SCRIPT_INTERPRETERS: Final[frozenset[str]] = frozenset({"ruby", "perl"})
_SCRIPT_STOP_OPTS: Final[frozenset[str]] = frozenset({"-e", "-E"})
_SCRIPT_VALUE_OPTS: Final[frozenset[str]] = frozenset({"-I", "-r", "-C"})

_ENV_VALUE_OPTS: Final[frozenset[str]] = frozenset({"-u", "--unset", "-C", "--chdir", "-P"})


def is_signallable_tunnel_pid(pid: object) -> bool:
    """Return ``True`` only for a regular PID that is safe to target.

    Rejects ``bool`` (``bool`` subclasses ``int``), ``<= 1`` (init/container
    PID 1, process-group 0, broadcast -1, negative PGIDs), the current process
    PID, and (on POSIX) the current process-group leader PID.
    """
    if isinstance(pid, bool) or not isinstance(pid, int):
        return False
    if pid <= 1:
        return False
    if pid == os.getpid():
        return False
    if hasattr(os, "getpgrp"):
        with contextlib.suppress(OSError):
            if pid == os.getpgrp():
                return False
    return True


def expected_tunnel_markers(
    handle: TunnelHandle,
    providers: Mapping[str, TunnelProvider] | None = None,
) -> tuple[str, ...]:
    """Return expected binary names for *handle*'s tunnel provider."""
    markers: list[str] = []
    provider_name = (handle.provider or "").strip()
    if providers is not None and provider_name in providers:
        binary = getattr(providers[provider_name], "binary", "")
        if isinstance(binary, str) and binary.strip():
            markers.append(binary.strip())
    elif provider_name in _KNOWN_PROVIDER_BINARIES:
        markers.extend(_KNOWN_PROVIDER_BINARIES[provider_name])
    if provider_name and provider_name not in markers:
        markers.append(provider_name)
    return tuple(markers)


def _read_proc_cmdline_argv(pid: int) -> tuple[str, ...] | None:
    """Read NUL-delimited argv from ``/proc/<pid>/cmdline`` when available."""
    if pid <= 0 or IS_WINDOWS:
        return None
    proc_cmdline = _PROC_ROOT / str(pid) / "cmdline"
    try:
        raw = proc_cmdline.read_bytes()
    except OSError:
        return None
    if not raw:
        return None
    parts = [part.decode("utf-8", errors="replace") for part in raw.split(b"\x00") if part]
    return tuple(parts) if parts else None


def _ps_comm(pid: int) -> str | None:
    """Return the executable path from ``ps -o comm=`` on POSIX, if available."""
    if pid <= 0 or IS_WINDOWS:
        return None
    try:
        result = subprocess.run(
            ["ps", "-o", "comm=", "-p", str(pid)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _split_cmdline(cmdline: str, *, comm: str | None = None) -> tuple[str, ...]:
    """Split a ``ps -o command=`` string into argv tokens.

    Preserves space-containing ``argv[0]`` paths when ``ps -o comm=`` supplies
    a matching prefix, and falls back to plain whitespace splitting if a live
    command line contains unmatched quotes.
    """
    text = cmdline.strip()
    if not text:
        return ()
    if comm and any(ch.isspace() for ch in comm) and (text == comm or text.startswith(f"{comm} ")):
        remainder = text[len(comm) :].strip()
        if not remainder:
            return (comm,)
        try:
            tail = shlex.split(remainder, posix=True)
        except ValueError:
            tail = remainder.split()
        return (comm, *tail)
    try:
        return tuple(shlex.split(text, posix=True))
    except ValueError:
        return tuple(text.split())


def pid_command_argv(pid: int) -> tuple[str, ...] | None:
    """Return *pid*'s argv tuple, or ``None`` when identity cannot be verified.

    Reads ``/proc/<pid>/cmdline`` first (works on Linux containers without
    ``procps``/``ps`` and preserves NUL-delimited ``argv[0]``), then falls back
    to ``ps`` on other POSIX systems.  Always returns ``None`` on Windows so
    teardown fails closed without a verified process identity.
    """
    if pid <= 0 or IS_WINDOWS:
        return None
    proc_argv = _read_proc_cmdline_argv(pid)
    if proc_argv:
        return proc_argv
    cmdline = pid_command_line(pid)
    if cmdline is None:
        return None
    comm = _ps_comm(pid)
    argv = _split_cmdline(cmdline, comm=comm)
    return argv or None


def _normalize_executable_basename(token: str) -> str:
    """Normalize a path or binary name to a lowercase executable basename."""
    cleaned = token.strip()
    if not cleaned:
        return ""
    name = PurePosixPath(cleaned.replace("\\", "/")).name.lower()
    for suffix in _EXECUTABLE_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _peel_env(argv: Sequence[str]) -> Sequence[str]:
    """Strip leading ``env`` wrapper tokens and ``KEY=VALUE`` assignments."""
    current: Sequence[str] = argv
    while current and _normalize_executable_basename(current[0]) == "env":
        i = 1
        while i < len(current):
            tok = current[i]
            if tok == "--":
                current = current[i + 1 :]
                break
            if tok in _ENV_VALUE_OPTS:
                i += 2
                continue
            if tok.startswith("-"):
                i += 1
                continue
            if "=" in tok and not tok.startswith(("/", "./", "../")):
                i += 1
                continue
            current = current[i:]
            break
        else:
            return ()
    return current


def _first_script_token(
    args: Sequence[str],
    *,
    stop_opts: frozenset[str] = frozenset(),
    stop_prefixes: tuple[str, ...] = (),
    value_opts: frozenset[str] = frozenset(),
    skip_prefixes: tuple[str, ...] = (),
    shell_mode: bool = False,
    python_mode: bool = False,
) -> str | None:
    """Return the first script-path positional argument for an interpreter, if any."""
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--":
            return args[i + 1] if i + 1 < len(args) else None
        if tok == "-":
            return None
        if tok in value_opts:
            i += 2
            continue
        if skip_prefixes and tok.startswith(skip_prefixes):
            i += 1
            continue
        if tok in stop_opts or (stop_prefixes and tok.startswith(stop_prefixes)):
            return None
        if shell_mode:
            if tok.startswith("--"):
                i += 1
                continue
            if tok.startswith("-") and len(tok) > 1:
                if any(ch in _SHELL_STOP_FLAGS for ch in tok[1:]):
                    return None
                i += 1
                continue
            if tok.startswith("+") and len(tok) > 1:
                i += 1
                continue
        elif python_mode:
            if tok.startswith("-"):
                if "c" in tok[1:] or "m" in tok[1:]:
                    return None
                i += 1
                continue
        elif tok.startswith("-"):
            i += 1
            continue
        return tok
    return None


def _extract_target_basenames(argv: Sequence[str]) -> tuple[str, ...]:
    """Extract executable basename (and script basename for interpreter wrappers)."""
    unwrapped = _peel_env(argv)
    if not unwrapped:
        return ()
    exe = _normalize_executable_basename(unwrapped[0])
    if not exe:
        return ()

    candidates: list[str] = [exe]
    rest = unwrapped[1:]
    script: str | None = None
    if exe.lstrip("-") in _SHELL_INTERPRETERS:
        script = _first_script_token(rest, value_opts=_SHELL_VALUE_OPTS, shell_mode=True)
    elif _PYTHON_INTERPRETER_RE.match(exe):
        script = _first_script_token(
            rest,
            value_opts=_PYTHON_VALUE_OPTS,
            skip_prefixes=("-W", "-X", "--"),
            python_mode=True,
        )
    elif exe in _NODE_INTERPRETERS:
        script = _first_script_token(
            rest,
            stop_opts=_NODE_STOP_OPTS,
            stop_prefixes=("--eval=", "--print="),
            value_opts=_NODE_VALUE_OPTS,
        )
    elif exe in _SCRIPT_INTERPRETERS:
        script = _first_script_token(
            rest,
            stop_opts=_SCRIPT_STOP_OPTS,
            stop_prefixes=("-e", "-E"),
            value_opts=_SCRIPT_VALUE_OPTS,
        )

    if script is not None:
        script_name = _normalize_executable_basename(script)
        if script_name:
            candidates.append(script_name)
    return tuple(candidates)


def argv_matches_markers(argv: Sequence[str], markers: Sequence[str]) -> bool:
    """Return ``True`` only when *argv*'s executable or script basename matches *markers*."""
    expected = {_normalize_executable_basename(marker) for marker in markers}
    expected.discard("")
    if not expected:
        return False
    return any(candidate in expected for candidate in _extract_target_basenames(argv))


def terminate_tunnel_process_with_reason(
    handle: TunnelHandle,
    *,
    providers: Mapping[str, TunnelProvider] | None = None,
    sig: int = signal.SIGTERM,
) -> tuple[bool, TerminateReason]:
    """Send *sig* to *handle*'s tunnel process and report the exact outcome reason.

    Fails closed whenever the process's command line cannot be read or does not
    match the expected tunnel executable basename.
    """
    if handle.provider in _NO_SIGNAL_PROVIDERS:
        return False, "no_signal_provider"
    if not is_signallable_tunnel_pid(handle.pid):
        return False, "invalid_pid"
    markers = expected_tunnel_markers(handle, providers)
    if not markers:
        return False, "missing_markers"
    if not is_process_alive(handle.pid):
        return False, "not_alive"
    argv = pid_command_argv(handle.pid)
    if argv is None:
        logger.warning(
            "Skipping signal for tunnel %s (pid %d): process command line could not be read",
            handle.name,
            handle.pid,
        )
        return False, "unreadable_cmdline"
    if not argv_matches_markers(argv, markers):
        logger.debug(
            "Skipping signal for tunnel %s: pid %d argv %r does not match %r",
            handle.name,
            handle.pid,
            argv,
            markers,
        )
        return False, "cmdline_mismatch"
    try:
        # PID passed non-process/self/pgrp, liveness, and executable-basename
        # identity checks above (Sonar python:S4828).
        os.kill(handle.pid, sig)  # NOSONAR python:S4828
    except OSError as exc:
        logger.debug("Signal %s to tunnel %s (pid %d) failed: %s", sig, handle.name, handle.pid, exc)
        return False, "signal_failed"
    return True, "signaled"


def terminate_tunnel_process(
    handle: TunnelHandle,
    *,
    providers: Mapping[str, TunnelProvider] | None = None,
    sig: int = signal.SIGTERM,
) -> bool:
    """Send *sig* to *handle*'s tunnel process after PID and identity checks.

    Returns ``True`` if ``os.kill`` succeeded, or ``False`` when signaling was
    refused by a guard, the process was not alive, its command line could not
    be read or did not match the tunnel binary, or ``os.kill`` raised
    ``OSError``.
    """
    signaled, _reason = terminate_tunnel_process_with_reason(handle, providers=providers, sig=sig)
    return signaled


__all__ = [
    "TerminateReason",
    "argv_matches_markers",
    "expected_tunnel_markers",
    "is_signallable_tunnel_pid",
    "pid_command_argv",
    "terminate_tunnel_process",
    "terminate_tunnel_process_with_reason",
]
