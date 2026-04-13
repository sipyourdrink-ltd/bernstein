"""Futuristic execution plan display — mission briefing style.

Renders the TaskPlan as a Rich-based panel with the same dark/cyan/green
aesthetic used by the boot splash screen.  Replaces the previous plain
Markdown + click.confirm() flow with a visually consistent, keyboard-driven
approval prompt.

Non-TTY safe: auto-approves when stdout is piped or in CI.
"""

from __future__ import annotations

import datetime
import os
import sys

# Platform-specific imports for terminal handling
if sys.platform == "win32":
    import msvcrt
    _IS_WINDOWS = True
else:
    import select
    import termios
    import tty
    _IS_WINDOWS = False
from contextlib import contextmanager
from typing import TYPE_CHECKING

from rich.console import Console
from rich.text import Text

from bernstein.core.models import PlanStatus, Task, TaskCostEstimate, TaskPlan

if TYPE_CHECKING:
    from collections.abc import Iterator

# ---------------------------------------------------------------------------
# Color palette (matches splash_screen.py)
# ---------------------------------------------------------------------------

C_GREEN = "#00ff41"
C_CYAN = "#00d4ff"
C_DIM = "#555555"
C_WARN = "#ffaa00"
C_ERR = "#ff3333"
C_WHITE = "#cccccc"
C_BRIGHT = "#ffffff"

# ---------------------------------------------------------------------------
# Risk display helpers
# ---------------------------------------------------------------------------

_RISK_ICON: dict[str, str] = {
    "low": "\u2713",
    "medium": "\u26a0",
    "high": "\u26a1",
    "critical": "\u2b24",
}

_RISK_COLOR: dict[str, str] = {
    "low": C_GREEN,
    "medium": C_WARN,
    "high": C_ERR,
    "critical": C_ERR,
}

_STATUS_LABEL: dict[str, str] = {
    PlanStatus.PENDING.value: "pending \u2014 awaiting approval",
    PlanStatus.APPROVED.value: "approved",
    PlanStatus.REJECTED.value: "rejected",
    PlanStatus.EXPIRED.value: "expired",
}

# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _fmt_cost(usd: float) -> str:
    """Format a USD cost for display."""
    if usd < 0.01:
        return f"${usd:.4f}"
    return f"${usd:.2f}"


def _fmt_minutes(minutes: int) -> str:
    """Format minutes as 'Xh Ym' or just 'Ym'."""
    if minutes >= 60:
        h, m = divmod(minutes, 60)
        return f"{h}h {m}m" if m else f"{h}h"
    return f"{minutes}m"


def _pad_right(text: str, width: int) -> str:
    """Pad a plain-text string to a fixed width with spaces."""
    return text + " " * max(0, width - len(text))


def _truncate(text: str, max_len: int) -> str:
    """Truncate text with ellipsis if it exceeds max_len."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "\u2026"


# ---------------------------------------------------------------------------
# Terminal raw mode helpers (same pattern as splash_screen.py)
# ---------------------------------------------------------------------------


@contextmanager
def _raw_mode() -> Iterator[None]:
    """Put stdin in raw mode for keypress detection.

    Restores original settings on exit.  No-ops on non-TTY.
    """
    if not sys.stdin.isatty():
        yield
        return

    if _IS_WINDOWS:
        # Windows does not need raw mode setup - msvcrt handles it
        yield
        return

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def _read_key() -> str:
    """Block until a single keypress and return it.

    Returns lowercase letter for alpha keys, or special names:
    'enter', 'escape', 'q', 'y', 'n'.
    """
    if not sys.stdin.isatty():
        return "y"  # auto-approve for non-TTY

    if _IS_WINDOWS:
        # Use msvcrt for Windows keypress detection
        ch = msvcrt.getch()
        if ch == b"\r" or ch == b"\n":
            return "enter"
        if ch == b"\x1b":
            return "escape"
        try:
            decoded = ch.decode("utf-8", errors="replace").lower()
        except Exception:
            return ""
        return decoded

    fd = sys.stdin.fileno()
    # Wait for input
    select.select([sys.stdin], [], [])
    ch = os.read(fd, 1)

    if ch == b"\r" or ch == b"\n":
        return "enter"
    if ch == b"\x1b":
        return "escape"
    try:
        decoded = ch.decode("utf-8", errors="replace").lower()
    except Exception:
        return ""
    return decoded


def _drain_input() -> None:
    """Consume any buffered input so it doesn't leak to the next prompt."""
    if not sys.stdin.isatty():
        return
    try:
        if _IS_WINDOWS:
            # Drain buffered input on Windows using msvcrt
            while msvcrt.kbhit():
                msvcrt.getch()
        else:
            while select.select([sys.stdin], [], [], 0.0)[0]:
                sys.stdin.read(1)
    except (OSError, ValueError):
        pass


# ---------------------------------------------------------------------------
# Box-drawing helpers
# ---------------------------------------------------------------------------


def _hline(width: int) -> str:
    """Horizontal rule inside the outer box."""
    return f"[{C_DIM}]{'─' * width}[/{C_DIM}]"


def _box_top(width: int) -> str:
    """Top border: ┌──...──┐"""
    inner = "─" * (width - 2)
    return f"[{C_DIM}]┌{inner}┐[/{C_DIM}]"


def _box_bottom(width: int) -> str:
    """Bottom border: └──...──┘"""
    inner = "─" * (width - 2)
    return f"[{C_DIM}]└{inner}┘[/{C_DIM}]"


def _box_row(content: str, width: int) -> str:
    """Row inside the outer box: │  content  │  (content is Rich markup)."""
    # We can't measure Rich markup width exactly, so we pad the raw line
    # and let the outer borders frame it visually.
    return f"[{C_DIM}]│[/{C_DIM}]  {content}"


def _inner_box_top(label: str, width: int) -> str:
    """Inner box top: ┌─ Label ─────...─┐"""
    prefix = f"─ {label} "
    fill = "─" * max(0, width - len(prefix) - 2)
    return f"  [{C_DIM}]┌{prefix}{fill}┐[/{C_DIM}]"


def _inner_box_row(content: str) -> str:
    """Inner box row: │  content  │"""
    return f"  [{C_DIM}]│[/{C_DIM}]  {content}"


def _inner_box_bottom(width: int) -> str:
    """Inner box bottom: └─────...─┘"""
    inner = "─" * (width - 2)
    return f"  [{C_DIM}]└{inner}┘[/{C_DIM}]"


# ---------------------------------------------------------------------------
# Plan renderer
# ---------------------------------------------------------------------------


class _PlanRenderer:
    """Assembles the plan display as Rich markup lines."""

    def __init__(
        self,
        plan: TaskPlan,
        tasks: list[Task] | None,
        width: int,
    ) -> None:
        self.plan = plan
        self.tasks_by_id: dict[str, Task] = {}
        if tasks:
            self.tasks_by_id = {t.id: t for t in tasks}
        self.width = min(width, 100)
        self.inner_width = self.width - 6  # accounting for outer box + indent
        self.lines: list[str] = []

    def _add(self, line: str) -> None:
        self.lines.append(line)

    def _blank(self) -> None:
        self.lines.append("")

    def build(self) -> list[str]:
        """Build all lines and return them."""
        self._header()
        self._summary_box()
        self._task_table()
        self._agent_assignments()
        self._footer()
        return self.lines

    # -- Sections --

    def _header(self) -> None:
        """Plan title, goal, status, timestamp."""
        plan = self.plan
        w = self.width

        self._add(_box_top(w))
        self._blank()

        # Title + plan ID (right-aligned)
        plan_id_short = plan.id[:12] if len(plan.id) > 12 else plan.id
        title = f"[bold {C_CYAN}]EXECUTION PLAN[/bold {C_CYAN}]"
        # Approximate: title is 14 chars, we want the ID on the right
        gap = max(1, w - 14 - len(plan_id_short) - 8)
        self._add(
            _box_row(
                f"{title}{' ' * gap}[{C_DIM}]{plan_id_short}[/{C_DIM}]",
                w,
            )
        )

        # Separator
        self._add(_box_row(_hline(w - 6), w))
        self._blank()

        # Goal
        goal_text = _truncate(plan.goal, w - 16)
        self._add(
            _box_row(
                f"[{C_WHITE}]Goal:[/{C_WHITE}] [{C_BRIGHT}]{goal_text}[/{C_BRIGHT}]",
                w,
            )
        )

        # Status
        status_label = _STATUS_LABEL.get(plan.status.value, plan.status.value)
        status_color = C_WARN if plan.status == PlanStatus.PENDING else C_GREEN
        self._add(
            _box_row(
                f"[{C_WHITE}]Status:[/{C_WHITE}] [{status_color}]{status_label}[/{status_color}]",
                w,
            )
        )

        # Created timestamp
        created = datetime.datetime.fromtimestamp(plan.created_at, tz=datetime.UTC)
        created_str = created.strftime("%Y-%m-%d %H:%M UTC")
        self._add(
            _box_row(
                f"[{C_WHITE}]Created:[/{C_WHITE}] [{C_DIM}]{created_str}[/{C_DIM}]",
                w,
            )
        )

        self._blank()

    def _summary_box(self) -> None:
        """Summary statistics inner box."""
        plan = self.plan
        box_w = 40

        high_risk = len(plan.high_risk_tasks)
        risk_color = C_ERR if high_risk > 0 else C_GREEN

        rows: list[tuple[str, str, str]] = [
            ("Tasks", str(len(plan.task_estimates)), C_BRIGHT),
            ("Est. cost", f"{_fmt_cost(plan.total_estimated_cost_usd)} (±20%)", C_CYAN),
            ("Est. time", _fmt_minutes(plan.total_estimated_minutes), C_WHITE),
            ("High-risk", str(high_risk), risk_color),
        ]

        # Budget warning
        budget_usd = getattr(plan, "budget_usd", 0.0)
        if budget_usd > 0 and plan.total_estimated_cost_usd > budget_usd:
            warning = (
                f"Estimated cost (${plan.total_estimated_cost_usd:.2f}) exceeds configured budget (${budget_usd:.2f})."
            )
            self._add(_box_row(f"[bold {C_ERR}]⚠ BUDGET WARNING[/{C_ERR}]", self.width))
            self._add(_box_row(f"[{C_ERR}]{_truncate(warning, self.width - 6)}[/{C_ERR}]", self.width))
            self._blank()

        self._add(_inner_box_top("Summary", box_w))
        for label, value, color in rows:
            padded_label = _pad_right(label, 16)
            self._add(_inner_box_row(f"[{C_WHITE}]{padded_label}[/{C_WHITE}][{color}]{value}[/{color}]"))
        self._add(_inner_box_bottom(box_w))
        self._blank()

    def _task_table(self) -> None:
        """Task list inner box with table layout."""
        plan = self.plan
        estimates = plan.task_estimates
        if not estimates:
            return

        # Compute column widths based on available space
        # #(3) Task(dynamic) Role(10) Model(8) Cost(7) Risk(4)
        box_w = min(self.inner_width, 68)
        task_col_w = max(20, box_w - 3 - 10 - 8 - 7 - 6 - 5)  # remaining for task

        # Header
        self._add(_inner_box_top("Tasks", box_w))

        header = (
            f"[bold {C_DIM}]"
            f"{'#':>2}  "
            f"{_pad_right('Task', task_col_w)}  "
            f"{_pad_right('Role', 10)}"
            f"{_pad_right('Model', 8)}"
            f"{'Cost':>7}  "
            f"Risk"
            f"[/bold {C_DIM}]"
        )
        self._add(_inner_box_row(header))

        for i, est in enumerate(estimates, start=1):
            self._add(self._task_row(i, est, task_col_w))

        self._add(_inner_box_bottom(box_w))
        self._blank()

    def _task_row(
        self,
        index: int,
        est: TaskCostEstimate,
        task_col_w: int,
    ) -> str:
        """Render a single task row."""
        title = _truncate(est.title, task_col_w)
        padded_title = _pad_right(title, task_col_w)

        risk_icon = _RISK_ICON.get(est.risk_level, "?")
        risk_color = _RISK_COLOR.get(est.risk_level, C_WHITE)

        cost_str = _fmt_cost(est.estimated_cost_usd)

        row = (
            f"[{C_WHITE}]{index:>2}[/{C_WHITE}]  "
            f"[{C_BRIGHT}]{padded_title}[/{C_BRIGHT}]  "
            f"[{C_CYAN}]{_pad_right(est.role, 10)}[/{C_CYAN}]"
            f"[{C_DIM}]{_pad_right(est.model, 8)}[/{C_DIM}]"
            f"[{C_WHITE}]{cost_str:>7}[/{C_WHITE}]  "
            f"[{risk_color}]{risk_icon}[/{risk_color}]"
        )
        return _inner_box_row(row)

    def _agent_assignments(self) -> None:
        """Agent assignments inner box."""
        plan = self.plan
        # Collect unique role -> (agent, model) mappings
        role_agents: dict[str, tuple[str, str]] = {}
        for est in plan.task_estimates:
            task = self.tasks_by_id.get(est.task_id)
            agent = (task.assigned_agent or "auto") if task else "auto"
            model = est.model
            existing = role_agents.get(est.role)
            if existing is None or (existing[0] == "auto" and agent != "auto"):
                role_agents[est.role] = (agent, model)

        if not role_agents:
            return

        box_w = 40
        self._add(_inner_box_top("Agent Assignments", box_w))
        for role, (agent, model) in sorted(role_agents.items()):
            padded_role = _pad_right(role, 12)
            assignment = f"{agent} ({model})" if model else agent
            self._add(_inner_box_row(f"[{C_CYAN}]{padded_role}[/{C_CYAN}][{C_WHITE}]{assignment}[/{C_WHITE}]"))
        self._add(_inner_box_bottom(box_w))
        self._blank()

    def _footer(self) -> None:
        """Separator and action buttons."""
        w = self.width
        self._add(_box_row(_hline(w - 6), w))
        self._blank()

        # Buttons
        approve_btn = f"[bold {C_GREEN}][ \u25b6 APPROVE ][/bold {C_GREEN}]"
        cancel_btn = f"[bold {C_ERR}][ \u2715 CANCEL ][/bold {C_ERR}]"
        # Center the buttons
        btn_text = f"{approve_btn}        {cancel_btn}"
        # Approximate centering (markup is invisible to width calc)
        pad = max(0, (w - 36) // 2)
        self._add(f"{' ' * pad}{btn_text}")
        self._blank()
        self._add(_box_bottom(w))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_plan(
    plan: TaskPlan,
    tasks: list[Task] | None = None,
    *,
    console: Console | None = None,
) -> None:
    """Render the execution plan in futuristic style without prompting.

    Useful when you want to display the plan but handle confirmation
    separately.

    Args:
        plan: The TaskPlan to render.
        tasks: Optional Task objects for richer detail.
        console: Rich Console instance (creates one if not provided).
    """
    if console is None:
        console = Console()

    width = min(console.size.width, 100)
    renderer = _PlanRenderer(plan, tasks, width)
    lines = renderer.build()
    markup = "\n".join(lines)

    console.print(Text.from_markup(markup))


def display_plan_and_confirm(
    plan: TaskPlan,
    tasks: list[Task] | None = None,
    *,
    console: Console | None = None,
) -> bool:
    """Display the execution plan in futuristic style and ask for approval.

    Renders the plan using Rich panels with the dark/cyan/green aesthetic
    matching the boot splash screen, then waits for a keypress to approve
    or cancel.

    Keyboard controls:
        Enter / Y / y  -- approve
        N / n / Esc / q -- cancel

    Non-TTY environments (CI, pipes) auto-approve.

    Args:
        plan: The TaskPlan to render.
        tasks: Optional Task objects for richer detail (agent assignments,
            effort levels, dependency info).
        console: Rich Console instance.  Creates one if not provided.

    Returns:
        True if the user approved, False if cancelled.
    """
    if console is None:
        console = Console()

    # Non-TTY: auto-approve
    is_tty = console.is_terminal and sys.stdin.isatty()
    is_ci = bool(os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"))

    if not is_tty or is_ci:
        render_plan(plan, tasks, console=console)
        console.print(f"\n  [{C_DIM}]Non-interactive mode \u2014 auto-approved[/{C_DIM}]")
        return True

    # Interactive: render plan, then wait for keypress
    render_plan(plan, tasks, console=console)

    # Prompt hint
    console.print(
        f"\n  [{C_DIM}]Press[/{C_DIM}] "
        f"[bold {C_GREEN}]Enter[/bold {C_GREEN}] "
        f"[{C_DIM}]or[/{C_DIM}] "
        f"[bold {C_GREEN}]Y[/bold {C_GREEN}] "
        f"[{C_DIM}]to approve,[/{C_DIM}] "
        f"[bold {C_ERR}]N[/bold {C_ERR}] "
        f"[{C_DIM}]or[/{C_DIM}] "
        f"[bold {C_ERR}]Esc[/bold {C_ERR}] "
        f"[{C_DIM}]to cancel[/{C_DIM}]"
    )

    _approve_keys: set[str] = {"enter", "y"}
    _cancel_keys: set[str] = {"n", "escape", "q"}

    try:
        with _raw_mode():
            _drain_input()
            key = _read_key()
    except (OSError, ValueError):
        # Terminal issues -- fall back to approve
        return True

    _drain_input()

    approved = key in _approve_keys
    if approved:
        console.print(f"\n  [bold {C_GREEN}]\u2713 Plan approved[/bold {C_GREEN}]")
    else:
        console.print(f"\n  [{C_DIM}]\u2715 Cancelled[/{C_DIM}]")

    return approved


def format_plan_status(status: PlanStatus) -> tuple[str, str]:
    """Return (label, color) for a plan status.

    Exposed as a helper for other modules that need consistent status
    formatting without rendering the full plan.

    Args:
        status: The plan status enum value.

    Returns:
        Tuple of (human-readable label, Rich color string).
    """
    label = _STATUS_LABEL.get(status.value, status.value)
    if status == PlanStatus.PENDING:
        return label, C_WARN
    if status == PlanStatus.APPROVED:
        return label, C_GREEN
    if status == PlanStatus.REJECTED:
        return label, C_ERR
    return label, C_DIM
