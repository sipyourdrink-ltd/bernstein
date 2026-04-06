"""Rich-based fallback display for unsupported terminals (TUI-003).

When the terminal does not support Textual features (e.g. SSH sessions,
``screen``/``tmux`` with degraded caps, CI environments, or piped output),
this module provides a simple Rich Live-based status display that shows the
same information as the full TUI but without Textual's interactive widgets.

Usage::

    from bernstein.tui.fallback import FallbackDisplay
    display = FallbackDisplay(server_url="http://127.0.0.1:8052")
    display.run()  # blocks until Ctrl+C
"""

from __future__ import annotations

import os
import time
from typing import Any, cast

import httpx
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


def _auth_headers() -> dict[str, str]:
    """Return Authorization header dict if BERNSTEIN_AUTH_TOKEN is set."""
    token = os.environ.get("BERNSTEIN_AUTH_TOKEN")
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


class FallbackDisplay:
    """Simple Rich Live-based status display for degraded terminals (TUI-003).

    Polls the Bernstein task server and renders a compact table showing
    agent and task status.  No Textual dependency -- only uses Rich, which
    works on virtually any terminal (including dumb terminals, SSH, CI).

    Args:
        server_url: Base URL of the Bernstein task server.
        interval: Polling interval in seconds.
        console: Optional Rich Console instance.
    """

    def __init__(
        self,
        server_url: str = "http://127.0.0.1:8052",
        interval: float = 2.0,
        console: Console | None = None,
    ) -> None:
        self._server_url = server_url
        self._interval = interval
        self._console = console or Console()
        self._start_ts = time.time()

    def _get(self, path: str) -> dict[str, Any] | list[Any] | None:
        """GET from the task server.

        Args:
            path: API path.

        Returns:
            Parsed JSON or None on error.
        """
        try:
            resp = httpx.get(
                f"{self._server_url}{path}",
                timeout=5.0,
                headers=_auth_headers(),
            )
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]
        except Exception:
            return None

    def _render(self) -> Group:
        """Build a Rich renderable from current server state.

        Returns:
            A Rich Group renderable.
        """
        raw = self._get("/status")
        if raw is None or not isinstance(raw, dict):
            return Group(
                Text("bernstein -- server offline", style="bold red"),
            )

        data: dict[str, Any] = raw
        agents_active = int(data.get("active_agents", 0))
        tasks_done = int(data.get("completed", 0))
        tasks_total = int(data.get("total", 0))
        tasks_failed = int(data.get("failed", 0))

        elapsed = time.time() - self._start_ts
        minutes = int(elapsed) // 60
        seconds = int(elapsed) % 60

        # Status line
        status_parts: list[str] = [
            "[bold]bernstein[/bold]",
            f"{agents_active} agents",
            f"{tasks_done}/{tasks_total} tasks",
        ]
        if tasks_failed:
            status_parts.append(f"[red]{tasks_failed} failed[/red]")
        status_parts.append(f"{minutes}m{seconds:02d}s")
        status_line = " | ".join(status_parts)

        # Task table
        task_table = Table(
            show_header=True,
            header_style="bold cyan",
            expand=True,
            title="Tasks",
        )
        task_table.add_column("Status", min_width=12)
        task_table.add_column("Role", min_width=8)
        task_table.add_column("Title")

        per_role: list[Any] = data.get("per_role", [])
        for t in per_role:
            if not isinstance(t, dict):
                continue
            task_dict = cast("dict[str, Any]", t)
            status = str(task_dict.get("status", "open"))
            role = str(task_dict.get("role", ""))
            title = str(task_dict.get("title", ""))
            color = {
                "done": "green",
                "failed": "red",
                "in_progress": "yellow",
                "claimed": "cyan",
            }.get(status, "white")
            task_table.add_row(
                f"[{color}]{status}[/{color}]",
                role,
                title[:60],
            )

        return Group(
            Panel(Text.from_markup(status_line), border_style="cyan"),
            task_table,
        )

    def run(self) -> None:
        """Start the fallback display, blocking until Ctrl+C.

        Uses Rich Live for auto-refreshing output that works on all
        terminals including dumb/pipe.
        """
        from rich.live import Live

        try:
            with Live(
                Text("Connecting...", style="dim"),
                console=self._console,
                refresh_per_second=2,
            ) as live:
                while True:
                    live.update(self._render())
                    time.sleep(self._interval)
        except KeyboardInterrupt:
            pass
        self._console.print("\n[dim]Display stopped.[/dim]")
