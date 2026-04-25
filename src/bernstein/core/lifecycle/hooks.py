"""Lifecycle-hook registry, event enum, and context dataclass.

This module is self-contained; the pluggy bridge lives in a sibling
module so this file can be imported from contexts where pluggy is
unavailable or undesired.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import MappingProxyType

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_STDOUT_BYTES",
    "HookFailure",
    "HookRegistry",
    "LifecycleContext",
    "LifecycleEvent",
]


DEFAULT_TIMEOUT_SECONDS: int = 30
"""Default subprocess timeout applied to script hooks."""

MAX_STDOUT_BYTES: int = 10 * 1024 * 1024
"""Maximum captured stdout for a script hook (10 MB). Output is truncated."""

# Whitelisted parent environment variables. Anything not listed here is
# stripped before launching a script hook, so secrets and unrelated
# settings do not leak into user-supplied processes.
_ENV_WHITELIST: tuple[str, ...] = ("PATH", "HOME", "USER")


class LifecycleEvent(StrEnum):
    """Named lifecycle events a hook may subscribe to."""

    PRE_TASK = "pre_task"
    POST_TASK = "post_task"
    PRE_MERGE = "pre_merge"
    POST_MERGE = "post_merge"
    PRE_SPAWN = "pre_spawn"
    POST_SPAWN = "post_spawn"
    PRE_ARCHIVE = "pre_archive"
    POST_ARCHIVE = "post_archive"


@dataclass(frozen=True, slots=True)
class LifecycleContext:
    """Immutable payload passed to every hook invocation.

    Attributes:
        event: The lifecycle event being dispatched.
        task: Task identifier, when the event is task-scoped.
        session_id: Agent session identifier, when relevant.
        workdir: Working directory the caller considers current.
        env: Extra environment variables to merge into script hooks
            (on top of the whitelisted parent env).
        timestamp: Unix timestamp when the context was built.
    """

    event: LifecycleEvent
    task: str | None = None
    session_id: str | None = None
    workdir: Path = field(default_factory=Path.cwd)
    env: dict[str, str] = field(default_factory=dict[str, str])
    timestamp: float = field(default_factory=time.time)

    def to_payload(self) -> dict[str, Any]:
        """Serialise the context for transport to a subprocess."""
        return {
            "event": self.event.value,
            "task": self.task,
            "session_id": self.session_id,
            "workdir": str(self.workdir),
            "env": dict(self.env),
            "timestamp": self.timestamp,
        }


class HookFailure(RuntimeError):
    """Raised when a script hook exits non-zero or a callable raises.

    Attributes:
        event: The event whose dispatch failed.
        hook: Human-readable description of the failing hook.
        exit_code: Subprocess exit code, or ``None`` for callables.
        stderr: Captured standard error, when available.
    """

    def __init__(
        self,
        event: LifecycleEvent,
        hook: str,
        *,
        exit_code: int | None = None,
        stderr: str = "",
        cause: BaseException | None = None,
    ) -> None:
        detail = f"exit_code={exit_code}" if exit_code is not None else "raised"
        super().__init__(f"Lifecycle hook failed for {event.value}: {hook} ({detail})")
        self.event = event
        self.hook = hook
        self.exit_code = exit_code
        self.stderr = stderr
        self.__cause__ = cause


@dataclass(frozen=True, slots=True)
class _ScriptHook:
    path: Path
    timeout: int


@dataclass(frozen=True, slots=True)
class _CallableHook:
    fn: Callable[[LifecycleContext], None]

    @property
    def label(self) -> str:
        name = getattr(self.fn, "__qualname__", None) or getattr(self.fn, "__name__", None) or repr(self.fn)
        return f"callable:{name}"


class HookRegistry:
    """Registers and dispatches lifecycle hooks.

    Hooks fire in registration order. The registry deliberately keeps
    pluggy integration in a bridge module so this class can be used
    standalone in tests and by callers that do not want to take a
    dependency on pluggy.
    """

    def __init__(self) -> None:
        self._scripts: dict[LifecycleEvent, list[_ScriptHook]] = {event: [] for event in LifecycleEvent}
        self._callables: dict[LifecycleEvent, list[_CallableHook]] = {event: [] for event in LifecycleEvent}
        # Insertion-order ledger so we can dispatch scripts and callables
        # in the exact order a user registered them.
        self._order: dict[LifecycleEvent, list[tuple[str, int]]] = {event: [] for event in LifecycleEvent}
        self._executor: ThreadPoolExecutor | None = None
        # The pluggy bridge attaches itself here when installed.
        self._pluggy_dispatcher: Callable[[LifecycleEvent, LifecycleContext], None] | None = None

    # ------------------------------------------------------------------ registration

    def register_script(
        self,
        event: LifecycleEvent,
        path: str | os.PathLike[str],
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """Register a shell script to run when ``event`` fires.

        Args:
            event: Lifecycle event to subscribe to.
            path: Filesystem path to the script; need not exist yet.
            timeout: Maximum wall-clock seconds before the subprocess is killed.
        """
        hook = _ScriptHook(path=Path(path), timeout=timeout)
        idx = len(self._scripts[event])
        self._scripts[event].append(hook)
        self._order[event].append(("script", idx))

    def register_callable(
        self,
        event: LifecycleEvent,
        fn: Callable[[LifecycleContext], None],
    ) -> None:
        """Register a Python callable for ``event``.

        The callable receives a single :class:`LifecycleContext` argument.
        Any exception it raises surfaces as :class:`HookFailure`.
        """
        hook = _CallableHook(fn=fn)
        idx = len(self._callables[event])
        self._callables[event].append(hook)
        self._order[event].append(("callable", idx))

    def attach_pluggy_dispatcher(
        self,
        dispatcher: Callable[[LifecycleEvent, LifecycleContext], None],
    ) -> None:
        """Install the pluggy bridge's dispatcher.

        Called by :mod:`bernstein.core.lifecycle.pluggy_bridge`. The
        dispatcher is invoked after callables and scripts so that
        plugin-supplied hookimpls see the same context.
        """
        self._pluggy_dispatcher = dispatcher

    # ------------------------------------------------------------------ introspection

    def registered(self, event: LifecycleEvent) -> list[str]:
        """Return labels of all hooks registered for ``event``, in order."""
        labels: list[str] = []
        for kind, idx in self._order[event]:
            if kind == "script":
                labels.append(f"script:{self._scripts[event][idx].path}")
            else:
                labels.append(self._callables[event][idx].label)
        return labels

    # ------------------------------------------------------------------ dispatch

    def run(self, event: LifecycleEvent, context: LifecycleContext) -> None:
        """Run all hooks for ``event`` synchronously, in registration order.

        Raises:
            HookFailure: On the first failure; subsequent hooks are not run.
        """
        for kind, idx in self._order[event]:
            if kind == "script":
                self._run_script(event, self._scripts[event][idx], context)
            else:
                self._run_callable(event, self._callables[event][idx], context)
        if self._pluggy_dispatcher is not None:
            try:
                self._pluggy_dispatcher(event, context)
            except HookFailure:
                raise
            except Exception as exc:
                raise HookFailure(event, "pluggy", cause=exc) from exc

    def run_async(self, event: LifecycleEvent, context: LifecycleContext) -> Future[None]:
        """Schedule ``run`` on a background thread and return a Future.

        Use for post-events where the caller should not block on I/O.
        The caller owns the future; failures surface via ``future.exception()``.
        """
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="bernstein-hooks")
        return self._executor.submit(self.run, event, context)

    def shutdown(self, wait: bool = True) -> None:
        """Tear down the background executor, if one was started."""
        if self._executor is not None:
            self._executor.shutdown(wait=wait)
            self._executor = None

    # ------------------------------------------------------------------ internals

    def _run_callable(
        self,
        event: LifecycleEvent,
        hook: _CallableHook,
        context: LifecycleContext,
    ) -> None:
        try:
            hook.fn(context)
        except Exception as exc:
            raise HookFailure(event, hook.label, cause=exc) from exc

    def _run_script(
        self,
        event: LifecycleEvent,
        hook: _ScriptHook,
        context: LifecycleContext,
    ) -> None:
        env = _build_script_env(context)
        payload = json.dumps(context.to_payload()).encode("utf-8")
        label = f"script:{hook.path}"
        try:
            proc = subprocess.run(
                [str(hook.path)],
                input=payload,
                env=env,
                cwd=str(context.workdir),
                capture_output=True,
                timeout=hook.timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise HookFailure(event, label, cause=exc) from exc
        except subprocess.TimeoutExpired as exc:
            raise HookFailure(event, label, stderr="timeout", cause=exc) from exc

        stdout = proc.stdout or b""
        if len(stdout) > MAX_STDOUT_BYTES:
            truncated = stdout[:MAX_STDOUT_BYTES]
            log.warning(
                "hook stdout truncated from %d to %d bytes for %s",
                len(stdout),
                MAX_STDOUT_BYTES,
                hook.path,
            )
            stdout = truncated

        if proc.returncode != 0:
            stderr_text = (proc.stderr or b"").decode("utf-8", errors="replace")
            raise HookFailure(event, label, exit_code=proc.returncode, stderr=stderr_text)


def _build_script_env(context: LifecycleContext) -> dict[str, str]:
    """Construct the environment for a script subprocess.

    Only whitelisted parent variables are forwarded. Anything callers
    want visible to hooks must live on ``context.env`` or be explicit
    ``BERNSTEIN_*`` values.
    """
    env: dict[str, str] = {}
    parent = _read_parent_env()
    for key in _ENV_WHITELIST:
        value = parent.get(key)
        if value is not None:
            env[key] = value
    # Forward all BERNSTEIN_* variables already on the parent.
    for key, value in parent.items():
        if key.startswith("BERNSTEIN_"):
            env[key] = value

    env["BERNSTEIN_EVENT"] = context.event.value
    if context.task is not None:
        env["BERNSTEIN_TASK_ID"] = context.task
    if context.session_id is not None:
        env["BERNSTEIN_SESSION_ID"] = context.session_id
    env["BERNSTEIN_WORKDIR"] = str(context.workdir)

    # Context.env wins over anything inherited so callers can override
    # a whitelisted value deliberately.
    env.update(context.env)
    return env


def _read_parent_env() -> MappingProxyType[str, str] | dict[str, str]:
    """Indirection point so tests can monkeypatch environment inheritance."""
    return dict(os.environ)
