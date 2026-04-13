"""TUI screen for side-by-side branch diff comparison.

Pushed onto the Textual app stack when a user selects two tasks to compare
in the dashboard (key ``d`` to mark, then ``d`` again to compare).
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any, ClassVar

from rich.syntax import Syntax
from rich.text import Text
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Static

if TYPE_CHECKING:
    from pathlib import Path

    from textual.app import ComposeResult

# ---------------------------------------------------------------------------
# Lightweight git helpers (no import from diff_cmd to avoid heavy deps in TUI)
# ---------------------------------------------------------------------------


def _run_git(args: list[str], cwd: Path) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return ""


def _resolve_task_diff(
    task_id: str, agents: list[dict[str, Any]], root: Path, base: str = "main"
) -> tuple[str, str, dict[str, Any] | None]:
    """Resolve diff text and stat for a task ID.

    Returns:
        (diff_text, stat_text, agent_dict)
    """
    # Find agent for task
    agent: dict[str, Any] | None = None
    for a in agents:
        for tid in a.get("task_ids", []):
            if tid == task_id or tid.startswith(task_id) or task_id.startswith(tid[:8]):
                agent = a
                break
        if agent:
            break

    if not agent:
        # Try as session ID
        for a in agents:
            aid = a.get("id", "")
            if aid == task_id or aid.startswith(task_id) or task_id.startswith(aid[:8]):
                agent = a
                break

    if not agent:
        return "", "", None

    session_id = agent.get("id", "")
    branches = [f"agent/{session_id}", f"task/{task_id}"]
    if len(task_id) > 8:
        branches.append(f"task/{task_id[:8]}")

    worktree_path = root / ".sdd" / "worktrees" / session_id

    diff_text = ""
    stat_text = ""

    # 1. Try worktree first (most live data)
    if worktree_path.exists() and (worktree_path / ".git").exists():
        diff_text = _run_git(["diff", f"{base}...HEAD", "--"], worktree_path)
        stat_text = _run_git(["diff", f"{base}...HEAD", "--stat"], worktree_path)

    # 2. Try branches in order
    if not diff_text:
        for branch in branches:
            check = _run_git(["branch", "--list", branch], root)
            if check.strip():
                diff_text = _run_git(["diff", f"{base}...{branch}", "--"], root)
                stat_text = _run_git(["diff", f"{base}...{branch}", "--stat"], root)
                if diff_text:
                    break

    return diff_text, stat_text, agent


def _parse_diff_files(diff_text: str) -> dict[str, str]:
    """Parse unified diff into {filepath: diff_content}."""
    files: dict[str, str] = {}
    current_file: str | None = None
    lines: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("diff --git"):
            if current_file is not None:
                files[current_file] = "\n".join(lines)
            parts = line.split(" b/", 1)
            current_file = parts[1] if len(parts) > 1 else line
            lines = []
        elif current_file is not None:
            lines.append(line)
    if current_file is not None:
        files[current_file] = "\n".join(lines)
    return files


# ---------------------------------------------------------------------------
# Compare Screen
# ---------------------------------------------------------------------------


class CompareScreen(Screen[None]):
    """Side-by-side diff comparison of two agent branches."""

    CSS = """
    CompareScreen {
        background: $background;
    }

    #compare-header {
        height: 3;
        background: $accent 15%;
        padding: 0 2;
    }

    #compare-body {
        height: 1fr;
    }

    #compare-left, #compare-right {
        width: 1fr;
        padding: 0 1;
    }

    #compare-left {
        border-right: heavy $border;
    }

    .file-header {
        background: $surface;
        text-style: bold;
        padding: 0 1;
        height: 1;
    }

    .diff-content {
        padding: 0 1;
    }

    .summary-row {
        height: auto;
        max-height: 6;
        background: $surface;
        border-bottom: solid $border;
        padding: 0 2;
    }

    Footer {
        background: $surface;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "pop_screen", "Back"),
        Binding("q", "pop_screen", "Back"),
    ]

    def __init__(
        self,
        left_id: str,
        right_id: str,
        agents: list[dict[str, Any]],
        root: Path,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._left_id = left_id
        self._right_id = right_id
        self._agents = agents
        self._root = root

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static("", id="compare-header")
        yield Static("", classes="summary-row", id="summary")
        with Horizontal(id="compare-body"):
            yield VerticalScroll(id="compare-left")
            yield VerticalScroll(id="compare-right")
        yield Footer()

    def on_mount(self) -> None:
        self.run_worker(self._load_diffs, thread=True)

    def _load_diffs(self) -> None:
        left_diff, left_stat, left_agent = _resolve_task_diff(
            self._left_id,
            self._agents,
            self._root,
        )
        right_diff, right_stat, right_agent = _resolve_task_diff(
            self._right_id,
            self._agents,
            self._root,
        )
        self.app.call_from_thread(
            self._render_comparison,
            left_diff,
            left_stat,
            left_agent,
            right_diff,
            right_stat,
            right_agent,
        )

    def _render_comparison(
        self,
        left_diff: str,
        left_stat: str,
        left_agent: dict[str, Any] | None,
        right_diff: str,
        right_stat: str,
        right_agent: dict[str, Any] | None,
    ) -> None:
        # Header
        hdr = self.query_one("#compare-header", Static)

        def _label(agent: dict[str, Any] | None, ident: str) -> str:
            if agent:
                role = agent.get("role", "")
                model = agent.get("model", "")
                return f"{ident[:12]}  role={role} model={model}"
            return ident[:12]

        left_label = _label(left_agent, self._left_id)
        right_label = _label(right_agent, self._right_id)
        hdr.update(
            Text.assemble(
                ("COMPARE  ", "bold"),
                (left_label, "cyan"),
                ("  vs  ", "dim"),
                (right_label, "magenta"),
            )
        )

        # Summary
        summary = self.query_one("#summary", Static)
        left_files = _parse_diff_files(left_diff)
        right_files = _parse_diff_files(right_diff)
        all_files = sorted(set(left_files) | set(right_files))

        summary_parts: list[str] = []
        both = sum(1 for f in all_files if f in left_files and f in right_files)
        left_only = sum(1 for f in all_files if f in left_files and f not in right_files)
        right_only = sum(1 for f in all_files if f not in left_files and f in right_files)
        summary_parts.append(f"{len(all_files)} files total")
        if both:
            summary_parts.append(f"{both} in both")
        if left_only:
            summary_parts.append(f"{left_only} left only")
        if right_only:
            summary_parts.append(f"{right_only} right only")
        summary.update(Text(" | ".join(summary_parts), style="dim"))

        # Populate panels
        left_panel = self.query_one("#compare-left", VerticalScroll)
        right_panel = self.query_one("#compare-right", VerticalScroll)

        if not all_files:
            left_panel.mount(Static("[dim]No changes[/dim]"))
            right_panel.mount(Static("[dim]No changes[/dim]"))
            return

        for f in all_files:
            left_content = left_files.get(f, "")
            right_content = right_files.get(f, "")

            in_left = f in left_files
            in_right = f in right_files

            if in_left and in_right:
                marker = "[yellow](both)[/yellow]"
            elif in_left:
                marker = "[cyan](left only)[/cyan]"
            else:
                marker = "[magenta](right only)[/magenta]"

            left_panel.mount(Static(f"[bold]{f}[/bold] {marker}", classes="file-header"))
            right_panel.mount(Static(f"[bold]{f}[/bold] {marker}", classes="file-header"))

            if left_content:
                left_panel.mount(
                    Static(Syntax(left_content, "diff", theme="monokai", line_numbers=False), classes="diff-content")
                )
            else:
                left_panel.mount(Static("[dim](no changes)[/dim]", classes="diff-content"))

            if right_content:
                right_panel.mount(
                    Static(Syntax(right_content, "diff", theme="monokai", line_numbers=False), classes="diff-content")
                )
            else:
                right_panel.mount(Static("[dim](no changes)[/dim]", classes="diff-content"))

    def action_pop_screen(self) -> None:
        self.app.pop_screen()
