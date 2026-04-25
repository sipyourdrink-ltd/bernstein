"""Shell-command notification sink.

Spawns an arbitrary command per event with the JSON event payload on
stdin and a pinned environment. Designed for power users who want
custom logic (paging, on-call rotation, archival) without writing a
pluggy plugin.

Security posture:

  * The command is executed via :func:`asyncio.create_subprocess_exec`
    — never shell-interpolated — so a malicious event field can't
    inject extra commands.
  * The environment is whitelisted (``PATH``, ``HOME``, ``USER`` and
    every ``BERNSTEIN_*`` variable) plus user-declared keys. Anything
    else from the parent process is stripped.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from typing import Any

from bernstein.core.notifications.protocol import (
    NotificationDeliveryError,
    NotificationEvent,
    NotificationPermanentError,
)

__all__ = ["ShellSink"]

_ENV_WHITELIST: tuple[str, ...] = ("PATH", "HOME", "USER")


class ShellSink:
    """Run an arbitrary command with the event JSON on stdin.

    Required config keys::

        id: <unique sink id>
        kind: shell
        command: ["/usr/local/bin/page", "--severity"]

    Optional::

        env: {EXTRA_KEY: value}
        timeout_s: 30
        non_zero_exit_is_permanent: false
    """

    kind: str = "shell"

    def __init__(self, config: dict[str, Any]) -> None:
        self.sink_id = str(config["id"])
        command = config.get("command")
        if not isinstance(command, list) or not command:
            raise NotificationPermanentError(
                f"shell sink {self.sink_id!r} requires non-empty 'command' list",
            )
        self._command: list[str] = [str(part) for part in command]
        self._timeout = float(config.get("timeout_s", 30.0))
        raw_env = config.get("env") or {}
        if not isinstance(raw_env, dict):
            raise NotificationPermanentError(
                f"shell sink {self.sink_id!r} env must be a mapping",
            )
        self._extra_env = {str(k): str(v) for k, v in raw_env.items()}
        self._non_zero_permanent = bool(config.get("non_zero_exit_is_permanent", False))

    async def deliver(self, event: NotificationEvent) -> None:
        """Spawn the command and pipe ``event`` JSON to its stdin."""
        env = self._build_env(event)
        payload = json.dumps(event.to_payload()).encode("utf-8")
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError as exc:
            raise NotificationPermanentError(
                f"shell sink {self.sink_id!r} command not found: {self._command[0]}",
            ) from exc
        except OSError as exc:
            raise NotificationDeliveryError(f"shell sink spawn error: {exc}") from exc

        try:
            _, stderr = await asyncio.wait_for(proc.communicate(input=payload), timeout=self._timeout)
        except TimeoutError as exc:
            proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            raise NotificationDeliveryError(f"shell sink {self.sink_id!r} timed out") from exc

        if proc.returncode == 0:
            return
        stderr_text = (stderr or b"").decode("utf-8", errors="replace").strip()[:500]
        msg = f"shell sink {self.sink_id!r} exited {proc.returncode}: {stderr_text}"
        if self._non_zero_permanent:
            raise NotificationPermanentError(msg)
        raise NotificationDeliveryError(msg)

    async def close(self) -> None:
        """No-op."""

    def _build_env(self, event: NotificationEvent) -> dict[str, str]:
        env: dict[str, str] = {}
        for key in _ENV_WHITELIST:
            value = os.environ.get(key)
            if value is not None:
                env[key] = value
        for key, value in os.environ.items():
            if key.startswith("BERNSTEIN_"):
                env[key] = value
        env["BERNSTEIN_NOTIFY_EVENT_ID"] = event.event_id
        env["BERNSTEIN_NOTIFY_KIND"] = event.kind.value
        env["BERNSTEIN_NOTIFY_SEVERITY"] = event.severity
        env["BERNSTEIN_NOTIFY_SINK_ID"] = self.sink_id
        env.update(self._extra_env)
        return env
