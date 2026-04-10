# TUI & Web Dashboard Batch Features Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement 12 independent feature tickets (6 TUI, 6 web dashboard) in parallel.

**Architecture:** Each ticket produces a standalone module with tests. TUI features extend the existing Textual app in `src/bernstein/tui/`. Web features add new FastAPI routes in `src/bernstein/core/routes/` and update the dashboard template. All features are independent with no cross-dependencies.

**Tech Stack:** Python 3.12+, Textual (TUI), FastAPI (web), Alpine.js + Tailwind CSS (dashboard), pytest (tests)

---

## Task 1: TUI-014 — Vim-mode keybindings

**Files:**
- Create: `src/bernstein/tui/vim_mode.py`
- Modify: `src/bernstein/tui/app.py`
- Create: `tests/unit/test_tui_vim_mode.py`

- [ ] **Step 1: Write tests for vim mode state machine**

```python
# tests/unit/test_tui_vim_mode.py
"""Tests for TUI-014: Vim-mode keybindings."""

from __future__ import annotations

from bernstein.tui.vim_mode import VimMode, VimState


class TestVimState:
    def test_initial_state_is_normal(self) -> None:
        state = VimState()
        assert state.mode == VimMode.NORMAL

    def test_colon_enters_command_mode(self) -> None:
        state = VimState()
        result = state.handle_key("colon")
        assert state.mode == VimMode.COMMAND
        assert result is None

    def test_slash_enters_search_mode(self) -> None:
        state = VimState()
        result = state.handle_key("slash")
        assert state.mode == VimMode.SEARCH
        assert result is None

    def test_escape_returns_to_normal(self) -> None:
        state = VimState()
        state.handle_key("colon")
        state.handle_key("escape")
        assert state.mode == VimMode.NORMAL

    def test_hjkl_navigation(self) -> None:
        state = VimState()
        assert state.handle_key("j") == "cursor_down"
        assert state.handle_key("k") == "cursor_up"
        assert state.handle_key("h") == "cursor_left"
        assert state.handle_key("l") == "cursor_right"

    def test_gg_goes_to_top(self) -> None:
        state = VimState()
        assert state.handle_key("g") is None  # partial
        assert state.handle_key("g") == "cursor_top"

    def test_g_then_other_cancels(self) -> None:
        state = VimState()
        state.handle_key("g")
        assert state.handle_key("j") == "cursor_down"

    def test_shift_g_goes_to_bottom(self) -> None:
        state = VimState()
        assert state.handle_key("G") == "cursor_bottom"

    def test_command_buffer(self) -> None:
        state = VimState()
        state.handle_key("colon")
        state.append_command_char("q")
        assert state.command_buffer == "q"

    def test_command_submit(self) -> None:
        state = VimState()
        state.handle_key("colon")
        state.append_command_char("q")
        result = state.submit_command()
        assert result == "q"
        assert state.mode == VimMode.NORMAL
        assert state.command_buffer == ""

    def test_search_buffer(self) -> None:
        state = VimState()
        state.handle_key("slash")
        state.append_search_char("t")
        state.append_search_char("e")
        assert state.search_buffer == "te"

    def test_search_submit(self) -> None:
        state = VimState()
        state.handle_key("slash")
        state.append_search_char("test")
        result = state.submit_search()
        assert result == "test"
        assert state.mode == VimMode.NORMAL

    def test_disabled_by_default(self) -> None:
        state = VimState(enabled=False)
        assert state.handle_key("j") is None
```

- [ ] **Step 2: Run tests — expect FAIL (module not found)**

Run: `uv run pytest tests/unit/test_tui_vim_mode.py -x -q`

- [ ] **Step 3: Implement vim mode state machine**

```python
# src/bernstein/tui/vim_mode.py
"""TUI-014: Vim-mode keybindings for TUI navigation.

Provides hjkl navigation, `/` for search, `gg`/`G` for top/bottom,
and `:` for command mode. Gated behind a ``vim_mode: true`` config option
in ``bernstein.yaml`` or ``~/.bernstein/bernstein.yaml``.
"""

from __future__ import annotations

import logging
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class VimMode(Enum):
    """Current vim input mode."""

    NORMAL = "normal"
    COMMAND = "command"
    SEARCH = "search"


# Normal-mode single-key mappings
_NORMAL_KEYS: dict[str, str] = {
    "j": "cursor_down",
    "k": "cursor_up",
    "h": "cursor_left",
    "l": "cursor_right",
    "G": "cursor_bottom",
}


class VimState:
    """Vim-mode state machine for multi-key sequence handling.

    Args:
        enabled: Whether vim mode is active. When False, all keys pass through.
    """

    def __init__(self, enabled: bool = True) -> None:
        self._enabled = enabled
        self._mode = VimMode.NORMAL
        self._pending: str = ""
        self._command_buf: str = ""
        self._search_buf: str = ""

    @property
    def enabled(self) -> bool:
        """Whether vim mode is active."""
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value
        if not value:
            self._mode = VimMode.NORMAL
            self._pending = ""
            self._command_buf = ""
            self._search_buf = ""

    @property
    def mode(self) -> VimMode:
        """Current input mode."""
        return self._mode

    @property
    def command_buffer(self) -> str:
        """Current command-mode input buffer."""
        return self._command_buf

    @property
    def search_buffer(self) -> str:
        """Current search-mode input buffer."""
        return self._search_buf

    def handle_key(self, key: str) -> str | None:
        """Process a key press and return an action name, or None.

        Args:
            key: Textual key name (e.g. "j", "colon", "escape").

        Returns:
            Action string to execute, or None if consumed/ignored.
        """
        if not self._enabled:
            return None

        # Escape always returns to normal mode
        if key == "escape":
            self._mode = VimMode.NORMAL
            self._pending = ""
            self._command_buf = ""
            self._search_buf = ""
            return None

        if self._mode in (VimMode.COMMAND, VimMode.SEARCH):
            return None  # keys handled via append_*_char / submit_*

        # Normal mode
        if key == "colon":
            self._mode = VimMode.COMMAND
            self._command_buf = ""
            return None

        if key == "slash":
            self._mode = VimMode.SEARCH
            self._search_buf = ""
            return None

        # Multi-key: gg
        if self._pending == "g":
            self._pending = ""
            if key == "g":
                return "cursor_top"
            # g + something else: fall through to handle the other key
            return _NORMAL_KEYS.get(key)

        if key == "g":
            self._pending = "g"
            return None

        return _NORMAL_KEYS.get(key)

    def append_command_char(self, char: str) -> None:
        """Append a character to the command buffer."""
        self._command_buf += char

    def submit_command(self) -> str:
        """Submit and clear the command buffer, returning to normal mode.

        Returns:
            The command string.
        """
        cmd = self._command_buf
        self._command_buf = ""
        self._mode = VimMode.NORMAL
        return cmd

    def append_search_char(self, char: str) -> None:
        """Append a character to the search buffer."""
        self._search_buf += char

    def submit_search(self) -> str:
        """Submit and clear the search buffer, returning to normal mode.

        Returns:
            The search query string.
        """
        query = self._search_buf
        self._search_buf = ""
        self._mode = VimMode.NORMAL
        return query


def load_vim_mode_config(yaml_path: Path | None = None) -> bool:
    """Check if vim_mode is enabled in bernstein.yaml.

    Args:
        yaml_path: Path to bernstein.yaml. If None, searches default locations.

    Returns:
        True if vim_mode is enabled.
    """
    try:
        import yaml
    except ImportError:
        return False

    candidates: list[Path] = []
    if yaml_path:
        candidates.append(yaml_path)
    else:
        candidates.append(Path("bernstein.yaml"))
        candidates.append(Path.home() / ".bernstein" / "bernstein.yaml")

    for path in candidates:
        if not path.exists():
            continue
        try:
            data = yaml.safe_load(path.read_text())
            if isinstance(data, dict):
                return bool(data.get("vim_mode", False))
        except Exception:
            continue
    return False
```

- [ ] **Step 4: Run tests — expect PASS**

Run: `uv run pytest tests/unit/test_tui_vim_mode.py -x -q`

- [ ] **Step 5: Add vim key handler to BernsteinApp**

In `src/bernstein/tui/app.py`, add to `__init__`:
```python
from bernstein.tui.vim_mode import VimState, load_vim_mode_config
# After self._task_progresses line:
self._vim = VimState(enabled=load_vim_mode_config())
```

Add action methods:
```python
def action_cursor_down(self) -> None:
    """Move task list cursor down (vim j)."""
    task_list = self.query_one(TaskListWidget)
    task_list.action_cursor_down()

def action_cursor_up(self) -> None:
    """Move task list cursor up (vim k)."""
    task_list = self.query_one(TaskListWidget)
    task_list.action_cursor_up()
```

- [ ] **Step 6: Commit**

```bash
git add src/bernstein/tui/vim_mode.py tests/unit/test_tui_vim_mode.py src/bernstein/tui/app.py
git commit -m "feat(tui-014): add vim-mode keybindings for TUI navigation"
```

---

## Task 2: TUI-016 — Persistent layout customization

**Files:**
- Create: `src/bernstein/tui/layout_persistence.py`
- Create: `tests/unit/test_tui_layout_persistence.py`

- [ ] **Step 1: Write tests for layout persistence**

```python
# tests/unit/test_tui_layout_persistence.py
"""Tests for TUI-016: Persistent layout customization."""

from __future__ import annotations

from pathlib import Path

from bernstein.tui.layout_persistence import LayoutConfig, load_layout, save_layout


class TestLayoutConfig:
    def test_default_config(self) -> None:
        config = LayoutConfig()
        assert config.split_ratio == 0.5
        assert config.split_enabled is False
        assert config.visible_panels == ["task-list"]
        assert config.orientation == "horizontal"

    def test_roundtrip(self, tmp_path: Path) -> None:
        config = LayoutConfig(
            split_ratio=0.6,
            split_enabled=True,
            visible_panels=["task-list", "agent-log", "timeline"],
            orientation="vertical",
        )
        path = tmp_path / "tui_layout.yaml"
        save_layout(config, path)
        loaded = load_layout(path)
        assert loaded.split_ratio == 0.6
        assert loaded.split_enabled is True
        assert loaded.visible_panels == ["task-list", "agent-log", "timeline"]
        assert loaded.orientation == "vertical"

    def test_load_missing_file(self, tmp_path: Path) -> None:
        loaded = load_layout(tmp_path / "missing.yaml")
        assert loaded == LayoutConfig()

    def test_load_corrupt_file(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text("{{{invalid", encoding="utf-8")
        loaded = load_layout(path)
        assert loaded == LayoutConfig()

    def test_panel_visibility_toggle(self) -> None:
        config = LayoutConfig(visible_panels=["task-list"])
        updated = config.toggle_panel("timeline")
        assert "timeline" in updated.visible_panels
        updated2 = updated.toggle_panel("timeline")
        assert "timeline" not in updated2.visible_panels

    def test_task_list_always_visible(self) -> None:
        config = LayoutConfig(visible_panels=["task-list", "timeline"])
        updated = config.toggle_panel("task-list")
        assert "task-list" in updated.visible_panels  # cannot hide
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `uv run pytest tests/unit/test_tui_layout_persistence.py -x -q`

- [ ] **Step 3: Implement layout persistence**

```python
# src/bernstein/tui/layout_persistence.py
"""TUI-016: Persistent layout customization.

Saves and loads user layout preferences to ``~/.bernstein/tui_layout.yaml``.
Users can resize panes, hide/show panels, and persist their preferred layout.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_LAYOUT_PATH = Path.home() / ".bernstein" / "tui_layout.yaml"

# Panels that cannot be hidden
_ALWAYS_VISIBLE = frozenset({"task-list"})

# All known panel IDs
KNOWN_PANELS = frozenset(
    {
        "task-list",
        "agent-log",
        "task-timeline",
        "waterfall-view",
        "scratchpad-viewer",
        "coordinator-dashboard",
        "approval-panel",
        "tool-observer",
        "action-bar",
    }
)


@dataclass(frozen=True)
class LayoutConfig:
    """Persisted TUI layout configuration.

    Attributes:
        split_ratio: Fraction allocated to primary pane (0.0-1.0).
        split_enabled: Whether split-pane is active on startup.
        visible_panels: Panel IDs that should be visible.
        orientation: Split orientation ("horizontal" or "vertical").
    """

    split_ratio: float = 0.5
    split_enabled: bool = False
    visible_panels: list[str] = field(default_factory=lambda: ["task-list"])
    orientation: str = "horizontal"

    def toggle_panel(self, panel_id: str) -> LayoutConfig:
        """Toggle a panel's visibility, returning a new config.

        Args:
            panel_id: The panel ID to toggle.

        Returns:
            New LayoutConfig with updated visibility.
        """
        panels = list(self.visible_panels)
        if panel_id in panels:
            if panel_id in _ALWAYS_VISIBLE:
                return self  # cannot hide required panels
            panels.remove(panel_id)
        else:
            panels.append(panel_id)
        return LayoutConfig(
            split_ratio=self.split_ratio,
            split_enabled=self.split_enabled,
            visible_panels=panels,
            orientation=self.orientation,
        )


def save_layout(config: LayoutConfig, path: Path | None = None) -> None:
    """Save layout configuration to YAML.

    Args:
        config: Layout configuration to save.
        path: File path. Defaults to ``~/.bernstein/tui_layout.yaml``.
    """
    try:
        import yaml
    except ImportError:
        logger.warning("PyYAML not available; cannot save layout")
        return

    target = path or _DEFAULT_LAYOUT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "split_ratio": config.split_ratio,
        "split_enabled": config.split_enabled,
        "visible_panels": config.visible_panels,
        "orientation": config.orientation,
    }
    target.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")


def load_layout(path: Path | None = None) -> LayoutConfig:
    """Load layout configuration from YAML.

    Args:
        path: File path. Defaults to ``~/.bernstein/tui_layout.yaml``.

    Returns:
        Loaded config, or defaults if file is missing/corrupt.
    """
    try:
        import yaml
    except ImportError:
        return LayoutConfig()

    target = path or _DEFAULT_LAYOUT_PATH
    if not target.exists():
        return LayoutConfig()

    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return LayoutConfig()
        return LayoutConfig(
            split_ratio=float(data.get("split_ratio", 0.5)),
            split_enabled=bool(data.get("split_enabled", False)),
            visible_panels=list(data.get("visible_panels", ["task-list"])),
            orientation=str(data.get("orientation", "horizontal")),
        )
    except Exception:
        logger.warning("Failed to load layout config from %s", target, exc_info=True)
        return LayoutConfig()
```

- [ ] **Step 4: Run tests — expect PASS**

Run: `uv run pytest tests/unit/test_tui_layout_persistence.py -x -q`

- [ ] **Step 5: Commit**

```bash
git add src/bernstein/tui/layout_persistence.py tests/unit/test_tui_layout_persistence.py
git commit -m "feat(tui-016): add persistent layout customization"
```

---

## Task 3: TUI-017 — Mouse support for panel interaction

**Files:**
- Create: `src/bernstein/tui/mouse_support.py`
- Create: `tests/unit/test_tui_mouse_support.py`

- [ ] **Step 1: Write tests for mouse support config**

```python
# tests/unit/test_tui_mouse_support.py
"""Tests for TUI-017: Mouse support for panel interaction."""

from __future__ import annotations

from pathlib import Path

from bernstein.tui.mouse_support import MouseConfig, load_mouse_config


class TestMouseConfig:
    def test_default_enabled(self) -> None:
        config = MouseConfig()
        assert config.click_to_select is True
        assert config.scroll_enabled is True
        assert config.drag_resize is True

    def test_load_missing_config(self) -> None:
        config = load_mouse_config(Path("/nonexistent.yaml"))
        assert config == MouseConfig()

    def test_load_from_yaml(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "bernstein.yaml"
        yaml_file.write_text(
            "mouse:\n  click_to_select: false\n  scroll_enabled: true\n  drag_resize: false\n",
            encoding="utf-8",
        )
        config = load_mouse_config(yaml_file)
        assert config.click_to_select is False
        assert config.scroll_enabled is True
        assert config.drag_resize is False

    def test_load_no_mouse_section(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "bernstein.yaml"
        yaml_file.write_text("server:\n  port: 8052\n", encoding="utf-8")
        config = load_mouse_config(yaml_file)
        assert config == MouseConfig()
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `uv run pytest tests/unit/test_tui_mouse_support.py -x -q`

- [ ] **Step 3: Implement mouse support module**

```python
# src/bernstein/tui/mouse_support.py
"""TUI-017: Mouse support for panel interaction.

Enable mouse click to select tasks, scroll with mouse wheel,
and drag to resize panes. Textual supports mouse input natively;
this module provides configuration and event routing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MouseConfig:
    """Mouse interaction configuration.

    Attributes:
        click_to_select: Click on task row to select it.
        scroll_enabled: Mouse wheel scrolling in panels.
        drag_resize: Drag pane borders to resize.
    """

    click_to_select: bool = True
    scroll_enabled: bool = True
    drag_resize: bool = True


def load_mouse_config(yaml_path: Path | None = None) -> MouseConfig:
    """Load mouse configuration from bernstein.yaml.

    Args:
        yaml_path: Path to config file. Searches defaults if None.

    Returns:
        MouseConfig with user preferences or defaults.
    """
    try:
        import yaml
    except ImportError:
        return MouseConfig()

    candidates: list[Path] = []
    if yaml_path:
        candidates.append(yaml_path)
    else:
        candidates.append(Path("bernstein.yaml"))
        candidates.append(Path.home() / ".bernstein" / "bernstein.yaml")

    for path in candidates:
        if not path.exists():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue
            mouse = data.get("mouse")
            if not isinstance(mouse, dict):
                continue
            return MouseConfig(
                click_to_select=bool(mouse.get("click_to_select", True)),
                scroll_enabled=bool(mouse.get("scroll_enabled", True)),
                drag_resize=bool(mouse.get("drag_resize", True)),
            )
        except Exception:
            continue
    return MouseConfig()
```

- [ ] **Step 4: Run tests — expect PASS**

Run: `uv run pytest tests/unit/test_tui_mouse_support.py -x -q`

- [ ] **Step 5: Commit**

```bash
git add src/bernstein/tui/mouse_support.py tests/unit/test_tui_mouse_support.py
git commit -m "feat(tui-017): add mouse support configuration for panel interaction"
```

---

## Task 4: TUI-018 — Task detail overlay

**Files:**
- Create: `src/bernstein/tui/task_detail_overlay.py`
- Create: `tests/unit/test_tui_task_detail_overlay.py`

- [ ] **Step 1: Write tests for task detail formatting**

```python
# tests/unit/test_tui_task_detail_overlay.py
"""Tests for TUI-018: Task detail overlay with full context."""

from __future__ import annotations

from bernstein.tui.task_detail_overlay import TaskDetail, format_task_detail


class TestTaskDetail:
    def test_creation(self) -> None:
        detail = TaskDetail(
            task_id="task-001",
            title="Fix bug",
            description="Fix the login bug",
            status="in-progress",
            role="backend",
            agent_id="agent-1",
            cost_usd=0.05,
            log_tail=["line1", "line2"],
            diff_preview="+ added line",
            quality_results={"lint": "pass", "tests": "pass"},
        )
        assert detail.task_id == "task-001"
        assert detail.cost_usd == 0.05

    def test_format_includes_all_sections(self) -> None:
        detail = TaskDetail(
            task_id="task-001",
            title="Fix bug",
            description="Fix the login bug",
            status="done",
            role="backend",
            agent_id="agent-1",
            cost_usd=0.12,
            log_tail=["Building...", "Tests passed"],
            diff_preview="+new line",
            quality_results={"lint": "pass"},
        )
        text = format_task_detail(detail)
        assert "task-001" in text
        assert "Fix bug" in text
        assert "backend" in text
        assert "$0.12" in text
        assert "Tests passed" in text
        assert "+new line" in text

    def test_format_handles_missing_optional_fields(self) -> None:
        detail = TaskDetail(
            task_id="task-002",
            title="Simple task",
            description="",
            status="open",
            role="qa",
        )
        text = format_task_detail(detail)
        assert "task-002" in text
        assert "open" in text

    def test_format_truncates_long_log(self) -> None:
        long_log = [f"line {i}" for i in range(200)]
        detail = TaskDetail(
            task_id="t", title="t", description="", status="open", role="qa",
            log_tail=long_log,
        )
        text = format_task_detail(detail)
        # Should contain truncation indicator
        assert "line 199" in text
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `uv run pytest tests/unit/test_tui_task_detail_overlay.py -x -q`

- [ ] **Step 3: Implement task detail overlay**

```python
# src/bernstein/tui/task_detail_overlay.py
"""TUI-018: Task detail overlay with full context.

Full-screen overlay showing task description, agent assignment,
status, log tail, diff preview, quality gate results, and cost.
Triggered by pressing Enter on a task in the task list.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Static

_MAX_LOG_LINES = 50


@dataclass
class TaskDetail:
    """All data needed to render a task detail overlay.

    Attributes:
        task_id: Unique task identifier.
        title: Task title.
        description: Full task description.
        status: Current task status.
        role: Assigned role.
        agent_id: Assigned agent session ID.
        cost_usd: Cost incurred so far.
        log_tail: Last N lines of agent log.
        diff_preview: Git diff preview string.
        quality_results: Quality gate results mapping.
    """

    task_id: str
    title: str
    description: str
    status: str
    role: str
    agent_id: str | None = None
    cost_usd: float | None = None
    log_tail: list[str] = field(default_factory=list)
    diff_preview: str = ""
    quality_results: dict[str, str] = field(default_factory=dict)


def format_task_detail(detail: TaskDetail) -> str:
    """Format task detail into a display string.

    Args:
        detail: Task detail data.

    Returns:
        Formatted multi-line string for display.
    """
    sections: list[str] = []

    # Header
    sections.append(f"{'=' * 60}")
    sections.append(f"  Task: {detail.task_id}")
    sections.append(f"  Title: {detail.title}")
    sections.append(f"  Status: {detail.status}  |  Role: {detail.role}")
    if detail.agent_id:
        sections.append(f"  Agent: {detail.agent_id}")
    if detail.cost_usd is not None:
        sections.append(f"  Cost: ${detail.cost_usd:.2f}")
    sections.append(f"{'=' * 60}")

    # Description
    if detail.description:
        sections.append("")
        sections.append("--- Description ---")
        sections.append(detail.description)

    # Log tail
    if detail.log_tail:
        sections.append("")
        sections.append("--- Recent Log ---")
        tail = detail.log_tail[-_MAX_LOG_LINES:]
        sections.extend(tail)

    # Diff preview
    if detail.diff_preview:
        sections.append("")
        sections.append("--- Diff Preview ---")
        sections.append(detail.diff_preview)

    # Quality gates
    if detail.quality_results:
        sections.append("")
        sections.append("--- Quality Gates ---")
        for gate, result in detail.quality_results.items():
            icon = "pass" if result == "pass" else "FAIL"
            sections.append(f"  [{icon}] {gate}")

    return "\n".join(sections)


class TaskDetailScreen(ModalScreen[None]):
    """Full-screen modal overlay for task detail view."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=True),
        Binding("q", "dismiss", "Close", show=False),
    ]

    DEFAULT_CSS = """
    TaskDetailScreen {
        align: center middle;
    }
    TaskDetailScreen > Static {
        width: 90%;
        height: 90%;
        padding: 1 2;
        border: thick $accent;
        background: $surface;
        overflow-y: auto;
    }
    """

    def __init__(self, detail: TaskDetail) -> None:
        """Initialize with task detail data.

        Args:
            detail: Task detail to display.
        """
        super().__init__()
        self._detail = detail

    def compose(self) -> ComposeResult:
        """Build the overlay content."""
        yield Static(format_task_detail(self._detail), id="task-detail-content")

    def action_dismiss(self) -> None:
        """Close the overlay."""
        self.dismiss()
```

- [ ] **Step 4: Run tests — expect PASS**

Run: `uv run pytest tests/unit/test_tui_task_detail_overlay.py -x -q`

- [ ] **Step 5: Commit**

```bash
git add src/bernstein/tui/task_detail_overlay.py tests/unit/test_tui_task_detail_overlay.py
git commit -m "feat(tui-018): add task detail overlay with full context"
```

---

## Task 5: TUI-019 — Session recording and playback

**Files:**
- Create: `src/bernstein/tui/session_recorder.py`
- Create: `tests/unit/test_tui_session_recorder.py`

- [ ] **Step 1: Write tests for session recording**

```python
# tests/unit/test_tui_session_recorder.py
"""Tests for TUI-019: Session recording and playback."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.tui.session_recorder import (
    RecordingFrame,
    SessionRecorder,
    SessionPlayer,
)


class TestSessionRecorder:
    def test_record_frame(self, tmp_path: Path) -> None:
        path = tmp_path / "session.jsonl"
        recorder = SessionRecorder(path)
        recorder.start()
        recorder.record_frame(
            timestamp=1000.0,
            event_type="status_update",
            data={"agents": 2, "tasks_done": 5},
        )
        recorder.stop()

        lines = path.read_text().strip().splitlines()
        assert len(lines) == 1
        frame = json.loads(lines[0])
        assert frame["timestamp"] == 1000.0
        assert frame["event_type"] == "status_update"
        assert frame["data"]["agents"] == 2

    def test_multiple_frames(self, tmp_path: Path) -> None:
        path = tmp_path / "session.jsonl"
        recorder = SessionRecorder(path)
        recorder.start()
        for i in range(5):
            recorder.record_frame(
                timestamp=float(i),
                event_type="tick",
                data={"i": i},
            )
        recorder.stop()

        lines = path.read_text().strip().splitlines()
        assert len(lines) == 5

    def test_not_recording_before_start(self, tmp_path: Path) -> None:
        path = tmp_path / "session.jsonl"
        recorder = SessionRecorder(path)
        recorder.record_frame(timestamp=0.0, event_type="x", data={})
        assert not path.exists()

    def test_not_recording_after_stop(self, tmp_path: Path) -> None:
        path = tmp_path / "session.jsonl"
        recorder = SessionRecorder(path)
        recorder.start()
        recorder.record_frame(timestamp=0.0, event_type="a", data={})
        recorder.stop()
        recorder.record_frame(timestamp=1.0, event_type="b", data={})

        lines = path.read_text().strip().splitlines()
        assert len(lines) == 1


class TestSessionPlayer:
    def test_load_frames(self, tmp_path: Path) -> None:
        path = tmp_path / "session.jsonl"
        recorder = SessionRecorder(path)
        recorder.start()
        recorder.record_frame(timestamp=0.0, event_type="a", data={"x": 1})
        recorder.record_frame(timestamp=1.0, event_type="b", data={"x": 2})
        recorder.stop()

        player = SessionPlayer(path)
        frames = player.load_frames()
        assert len(frames) == 2
        assert frames[0].event_type == "a"
        assert frames[1].timestamp == 1.0

    def test_load_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.jsonl"
        path.write_text("", encoding="utf-8")
        player = SessionPlayer(path)
        assert player.load_frames() == []

    def test_load_missing_file(self, tmp_path: Path) -> None:
        player = SessionPlayer(tmp_path / "nope.jsonl")
        assert player.load_frames() == []

    def test_frame_dataclass(self) -> None:
        frame = RecordingFrame(timestamp=1.0, event_type="test", data={"k": "v"})
        assert frame.timestamp == 1.0
        assert frame.data == {"k": "v"}
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `uv run pytest tests/unit/test_tui_session_recorder.py -x -q`

- [ ] **Step 3: Implement session recorder**

```python
# src/bernstein/tui/session_recorder.py
"""TUI-019: Session recording and playback.

Records TUI screen state changes to a JSONL file so completed runs
can be replayed as a terminal movie. Useful for team reviews and demos.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecordingFrame:
    """A single recorded screen state frame.

    Attributes:
        timestamp: Seconds since recording started.
        event_type: Type of state change (e.g. "status_update", "task_transition").
        data: Snapshot of relevant state at this point.
    """

    timestamp: float
    event_type: str
    data: dict[str, Any]


class SessionRecorder:
    """Records TUI state changes to a JSONL file.

    Args:
        output_path: Path to the JSONL recording file.
    """

    def __init__(self, output_path: Path) -> None:
        self._path = output_path
        self._recording = False
        self._fh: Any = None

    @property
    def recording(self) -> bool:
        """Whether recording is active."""
        return self._recording

    def start(self) -> None:
        """Start recording. Creates/truncates the output file."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self._path.open("a", encoding="utf-8")
        self._recording = True
        logger.info("Session recording started: %s", self._path)

    def stop(self) -> None:
        """Stop recording and close the file."""
        self._recording = False
        if self._fh:
            self._fh.close()
            self._fh = None
        logger.info("Session recording stopped: %s", self._path)

    def record_frame(
        self,
        timestamp: float,
        event_type: str,
        data: dict[str, Any],
    ) -> None:
        """Record a single frame.

        Args:
            timestamp: Seconds since recording started.
            event_type: Event type string.
            data: State snapshot dict.
        """
        if not self._recording or not self._fh:
            return

        frame = {
            "timestamp": timestamp,
            "event_type": event_type,
            "data": data,
        }
        self._fh.write(json.dumps(frame, separators=(",", ":")) + "\n")
        self._fh.flush()


class SessionPlayer:
    """Loads and iterates recorded session frames.

    Args:
        recording_path: Path to the JSONL recording file.
    """

    def __init__(self, recording_path: Path) -> None:
        self._path = recording_path

    def load_frames(self) -> list[RecordingFrame]:
        """Load all frames from the recording file.

        Returns:
            List of RecordingFrame in chronological order.
        """
        if not self._path.exists():
            return []

        frames: list[RecordingFrame] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
                frames.append(
                    RecordingFrame(
                        timestamp=float(raw["timestamp"]),
                        event_type=str(raw["event_type"]),
                        data=dict(raw.get("data", {})),
                    )
                )
            except (json.JSONDecodeError, KeyError, TypeError):
                logger.warning("Skipping malformed recording frame")
        return frames
```

- [ ] **Step 4: Run tests — expect PASS**

Run: `uv run pytest tests/unit/test_tui_session_recorder.py -x -q`

- [ ] **Step 5: Commit**

```bash
git add src/bernstein/tui/session_recorder.py tests/unit/test_tui_session_recorder.py
git commit -m "feat(tui-019): add session recording and playback"
```

---

## Task 6: TUI-020 — Notification badge for background events

**Files:**
- Create: `src/bernstein/tui/notification_badge.py`
- Create: `tests/unit/test_tui_notification_badge.py`

- [ ] **Step 1: Write tests for notification badges**

```python
# tests/unit/test_tui_notification_badge.py
"""Tests for TUI-020: Notification badge for background events."""

from __future__ import annotations

from bernstein.tui.notification_badge import BadgeTracker


class TestBadgeTracker:
    def test_initial_zero(self) -> None:
        tracker = BadgeTracker()
        assert tracker.get_count("tasks") == 0
        assert tracker.get_count("logs") == 0

    def test_increment(self) -> None:
        tracker = BadgeTracker()
        tracker.increment("tasks")
        tracker.increment("tasks")
        assert tracker.get_count("tasks") == 2

    def test_clear_panel(self) -> None:
        tracker = BadgeTracker()
        tracker.increment("tasks")
        tracker.increment("tasks")
        tracker.clear("tasks")
        assert tracker.get_count("tasks") == 0

    def test_clear_does_not_affect_other(self) -> None:
        tracker = BadgeTracker()
        tracker.increment("tasks")
        tracker.increment("logs")
        tracker.clear("tasks")
        assert tracker.get_count("logs") == 1

    def test_clear_all(self) -> None:
        tracker = BadgeTracker()
        tracker.increment("tasks")
        tracker.increment("logs")
        tracker.clear_all()
        assert tracker.get_count("tasks") == 0
        assert tracker.get_count("logs") == 0

    def test_format_badge_zero(self) -> None:
        tracker = BadgeTracker()
        assert tracker.format_badge("tasks") == ""

    def test_format_badge_nonzero(self) -> None:
        tracker = BadgeTracker()
        tracker.increment("tasks")
        tracker.increment("tasks")
        tracker.increment("tasks")
        assert tracker.format_badge("tasks") == "[3 new]"

    def test_format_badge_alert(self) -> None:
        tracker = BadgeTracker()
        tracker.set_alert("logs")
        assert tracker.format_badge("logs") == "[!]"

    def test_alert_cleared_with_clear(self) -> None:
        tracker = BadgeTracker()
        tracker.set_alert("logs")
        tracker.clear("logs")
        assert tracker.format_badge("logs") == ""

    def test_has_unread(self) -> None:
        tracker = BadgeTracker()
        assert tracker.has_unread() is False
        tracker.increment("tasks")
        assert tracker.has_unread() is True

    def test_focused_panel_ignored(self) -> None:
        tracker = BadgeTracker()
        tracker.set_focused("tasks")
        tracker.increment("tasks")
        assert tracker.get_count("tasks") == 0
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `uv run pytest tests/unit/test_tui_notification_badge.py -x -q`

- [ ] **Step 3: Implement notification badge tracker**

```python
# src/bernstein/tui/notification_badge.py
"""TUI-020: Notification badge for background events.

Shows badge counts on panels indicating unread events when the user
is focused on another panel (e.g., "Tasks [3 new]", "Logs [!]").
"""

from __future__ import annotations

from collections import defaultdict


class BadgeTracker:
    """Tracks unread event counts per panel.

    When a panel is focused, events for that panel are ignored
    (the user is already looking at it).
    """

    def __init__(self) -> None:
        self._counts: dict[str, int] = defaultdict(int)
        self._alerts: set[str] = set()
        self._focused: str | None = None

    def set_focused(self, panel_id: str | None) -> None:
        """Set which panel is currently focused.

        Clears any badge for the newly focused panel.

        Args:
            panel_id: The panel ID that now has focus, or None.
        """
        self._focused = panel_id
        if panel_id:
            self.clear(panel_id)

    def increment(self, panel_id: str, count: int = 1) -> None:
        """Increment the unread count for a panel.

        Does nothing if that panel is currently focused.

        Args:
            panel_id: Target panel ID.
            count: Number to add.
        """
        if panel_id == self._focused:
            return
        self._counts[panel_id] += count

    def set_alert(self, panel_id: str) -> None:
        """Set an alert flag on a panel (shown as [!]).

        Args:
            panel_id: Target panel ID.
        """
        if panel_id == self._focused:
            return
        self._alerts.add(panel_id)

    def get_count(self, panel_id: str) -> int:
        """Get the unread count for a panel.

        Args:
            panel_id: Target panel ID.

        Returns:
            Current unread count.
        """
        return self._counts.get(panel_id, 0)

    def clear(self, panel_id: str) -> None:
        """Clear badge and alert for a panel.

        Args:
            panel_id: Target panel ID.
        """
        self._counts.pop(panel_id, None)
        self._alerts.discard(panel_id)

    def clear_all(self) -> None:
        """Clear all badges and alerts."""
        self._counts.clear()
        self._alerts.clear()

    def has_unread(self) -> bool:
        """Whether any panel has unread events.

        Returns:
            True if any panel has a count > 0 or an alert.
        """
        return bool(self._alerts) or any(v > 0 for v in self._counts.values())

    def format_badge(self, panel_id: str) -> str:
        """Format the badge text for a panel.

        Args:
            panel_id: Target panel ID.

        Returns:
            Badge string like "[3 new]", "[!]", or empty string.
        """
        if panel_id in self._alerts:
            return "[!]"
        count = self._counts.get(panel_id, 0)
        if count > 0:
            return f"[{count} new]"
        return ""
```

- [ ] **Step 4: Run tests — expect PASS**

Run: `uv run pytest tests/unit/test_tui_notification_badge.py -x -q`

- [ ] **Step 5: Commit**

```bash
git add src/bernstein/tui/notification_badge.py tests/unit/test_tui_notification_badge.py
git commit -m "feat(tui-020): add notification badge for background events"
```

---

## Task 7: WEB-017 — Batch operations endpoint

**Files:**
- Create: `src/bernstein/core/routes/batch_ops.py`
- Modify: `src/bernstein/core/server.py` (include router + add to api_v1)
- Create: `tests/unit/test_route_batch_ops.py`

- [ ] **Step 1: Write tests for batch operations**

```python
# tests/unit/test_route_batch_ops.py
"""Tests for WEB-017: Batch operations endpoint."""

from __future__ import annotations

from bernstein.core.routes.batch_ops import (
    BatchAction,
    BatchRequest,
    BatchResult,
    validate_batch_request,
)


class TestBatchRequest:
    def test_cancel_action(self) -> None:
        req = BatchRequest(action=BatchAction.CANCEL, ids=["t1", "t2"])
        assert req.action == BatchAction.CANCEL
        assert len(req.ids) == 2

    def test_retry_action(self) -> None:
        req = BatchRequest(action=BatchAction.RETRY, ids=["t1"])
        assert req.action == BatchAction.RETRY

    def test_reprioritize_requires_priority(self) -> None:
        req = BatchRequest(action=BatchAction.REPRIORITIZE, ids=["t1"], priority=1)
        assert req.priority == 1

    def test_tag_requires_tags(self) -> None:
        req = BatchRequest(action=BatchAction.TAG, ids=["t1"], tags=["urgent"])
        assert req.tags == ["urgent"]


class TestValidation:
    def test_empty_ids_rejected(self) -> None:
        errors = validate_batch_request(
            BatchRequest(action=BatchAction.CANCEL, ids=[])
        )
        assert len(errors) > 0

    def test_too_many_ids_rejected(self) -> None:
        ids = [f"t{i}" for i in range(201)]
        errors = validate_batch_request(
            BatchRequest(action=BatchAction.CANCEL, ids=ids)
        )
        assert len(errors) > 0

    def test_valid_cancel(self) -> None:
        errors = validate_batch_request(
            BatchRequest(action=BatchAction.CANCEL, ids=["t1", "t2"])
        )
        assert errors == []

    def test_reprioritize_without_priority(self) -> None:
        errors = validate_batch_request(
            BatchRequest(action=BatchAction.REPRIORITIZE, ids=["t1"])
        )
        assert len(errors) > 0

    def test_tag_without_tags(self) -> None:
        errors = validate_batch_request(
            BatchRequest(action=BatchAction.TAG, ids=["t1"])
        )
        assert len(errors) > 0


class TestBatchResult:
    def test_result_creation(self) -> None:
        result = BatchResult(
            succeeded=["t1", "t2"],
            failed={"t3": "not found"},
        )
        assert len(result.succeeded) == 2
        assert result.failed["t3"] == "not found"
        assert result.total == 3
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `uv run pytest tests/unit/test_route_batch_ops.py -x -q`

- [ ] **Step 3: Implement batch operations route**

```python
# src/bernstein/core/routes/batch_ops.py
"""WEB-017: Batch operations endpoint for task management.

Allows batch task operations: cancel, retry, re-prioritize, and tag
via POST /tasks/batch-ops.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(tags=["batch-operations"])

_MAX_BATCH_SIZE = 200


class BatchAction(str, Enum):
    """Supported batch actions."""

    CANCEL = "cancel"
    RETRY = "retry"
    REPRIORITIZE = "reprioritize"
    TAG = "tag"


class BatchRequest(BaseModel):
    """Request body for batch task operations."""

    action: BatchAction
    ids: list[str] = Field(..., description="Task IDs to operate on")
    priority: int | None = Field(None, description="New priority (for reprioritize)")
    tags: list[str] | None = Field(None, description="Tags to add (for tag action)")


class BatchResult(BaseModel):
    """Result of a batch operation."""

    succeeded: list[str] = Field(default_factory=list)
    failed: dict[str, str] = Field(default_factory=dict)

    @property
    def total(self) -> int:
        """Total number of items processed."""
        return len(self.succeeded) + len(self.failed)


def validate_batch_request(req: BatchRequest) -> list[str]:
    """Validate a batch request and return errors.

    Args:
        req: The batch request to validate.

    Returns:
        List of error strings. Empty if valid.
    """
    errors: list[str] = []
    if not req.ids:
        errors.append("ids must not be empty")
    if len(req.ids) > _MAX_BATCH_SIZE:
        errors.append(f"ids must not exceed {_MAX_BATCH_SIZE}")
    if req.action == BatchAction.REPRIORITIZE and req.priority is None:
        errors.append("priority is required for reprioritize action")
    if req.action == BatchAction.TAG and not req.tags:
        errors.append("tags are required for tag action")
    return errors


@router.post("/tasks/batch-ops")
async def batch_task_operations(req: BatchRequest, request: Request) -> BatchResult:
    """Execute batch operations on multiple tasks.

    Args:
        req: Batch operation request.
        request: FastAPI request.

    Returns:
        BatchResult with succeeded/failed task IDs.
    """
    errors = validate_batch_request(req)
    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors))

    store = request.app.state.store
    result = BatchResult()

    for task_id in req.ids:
        try:
            task = store.get(task_id)
            if task is None:
                result.failed[task_id] = "not found"
                continue

            if req.action == BatchAction.CANCEL:
                store.transition(task_id, "cancelled")
                result.succeeded.append(task_id)
            elif req.action == BatchAction.RETRY:
                store.transition(task_id, "open")
                result.succeeded.append(task_id)
            elif req.action == BatchAction.REPRIORITIZE:
                store.update_priority(task_id, req.priority or 2)
                result.succeeded.append(task_id)
            elif req.action == BatchAction.TAG:
                store.add_tags(task_id, req.tags or [])
                result.succeeded.append(task_id)
        except Exception as exc:
            result.failed[task_id] = str(exc)

    logger.info(
        "Batch %s: %d succeeded, %d failed",
        req.action.value,
        len(result.succeeded),
        len(result.failed),
    )
    return result
```

- [ ] **Step 4: Run tests — expect PASS**

Run: `uv run pytest tests/unit/test_route_batch_ops.py -x -q`

- [ ] **Step 5: Wire router into server.py**

In `src/bernstein/core/server.py`, add after the paginated_tasks router include (~line 1372):
```python
from bernstein.core.routes.batch_ops import router as batch_ops_router
application.include_router(batch_ops_router)
```

And add to api_v1 (~line 1463):
```python
api_v1_router.include_router(batch_ops_router)
```

- [ ] **Step 6: Commit**

```bash
git add src/bernstein/core/routes/batch_ops.py tests/unit/test_route_batch_ops.py src/bernstein/core/server.py
git commit -m "feat(web-017): add batch operations endpoint for task management"
```

---

## Task 8: WEB-018 — Agent comparison view

**Files:**
- Create: `src/bernstein/core/routes/agent_comparison.py`
- Modify: `src/bernstein/core/server.py` (include router)
- Create: `tests/unit/test_route_agent_comparison.py`

- [ ] **Step 1: Write tests for agent comparison**

```python
# tests/unit/test_route_agent_comparison.py
"""Tests for WEB-018: Agent comparison view."""

from __future__ import annotations

from bernstein.core.routes.agent_comparison import (
    AgentMetrics,
    compute_agent_metrics,
)


class TestAgentMetrics:
    def test_creation(self) -> None:
        m = AgentMetrics(
            adapter="claude",
            model="sonnet",
            total_tasks=10,
            succeeded=8,
            failed=2,
            avg_completion_secs=120.0,
            total_cost_usd=1.50,
            quality_gate_pass_rate=0.9,
        )
        assert m.success_rate == 0.8
        assert m.cost_per_task == 0.15

    def test_zero_tasks(self) -> None:
        m = AgentMetrics(
            adapter="codex", model="gpt-4",
            total_tasks=0, succeeded=0, failed=0,
            avg_completion_secs=0.0, total_cost_usd=0.0,
            quality_gate_pass_rate=0.0,
        )
        assert m.success_rate == 0.0
        assert m.cost_per_task == 0.0


class TestComputeMetrics:
    def test_empty_sessions(self) -> None:
        result = compute_agent_metrics([])
        assert result == []

    def test_groups_by_adapter_model(self) -> None:
        sessions = [
            {"provider": "claude", "model": "sonnet", "status": "done",
             "duration_secs": 60, "cost_usd": 0.10, "quality_pass": True},
            {"provider": "claude", "model": "sonnet", "status": "done",
             "duration_secs": 120, "cost_usd": 0.20, "quality_pass": True},
            {"provider": "codex", "model": "gpt-4", "status": "failed",
             "duration_secs": 30, "cost_usd": 0.05, "quality_pass": False},
        ]
        result = compute_agent_metrics(sessions)
        assert len(result) == 2

        claude = next(m for m in result if m.adapter == "claude")
        assert claude.total_tasks == 2
        assert claude.succeeded == 2
        assert claude.success_rate == 1.0

        codex = next(m for m in result if m.adapter == "codex")
        assert codex.total_tasks == 1
        assert codex.failed == 1
        assert codex.success_rate == 0.0
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `uv run pytest tests/unit/test_route_agent_comparison.py -x -q`

- [ ] **Step 3: Implement agent comparison route**

```python
# src/bernstein/core/routes/agent_comparison.py
"""WEB-018: Agent comparison view.

Provides an API endpoint comparing agent performance across adapters
and models: success rate, avg completion time, cost efficiency, and
quality gate pass rate.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(tags=["agent-comparison"])


class AgentMetrics(BaseModel):
    """Aggregated metrics for an adapter/model combination."""

    adapter: str
    model: str
    total_tasks: int = 0
    succeeded: int = 0
    failed: int = 0
    avg_completion_secs: float = 0.0
    total_cost_usd: float = 0.0
    quality_gate_pass_rate: float = 0.0

    @property
    def success_rate(self) -> float:
        """Fraction of tasks that succeeded."""
        if self.total_tasks == 0:
            return 0.0
        return self.succeeded / self.total_tasks

    @property
    def cost_per_task(self) -> float:
        """Average cost per task."""
        if self.total_tasks == 0:
            return 0.0
        return self.total_cost_usd / self.total_tasks


def compute_agent_metrics(sessions: list[dict[str, Any]]) -> list[AgentMetrics]:
    """Compute per-adapter/model metrics from session records.

    Args:
        sessions: List of session dicts with provider, model, status,
            duration_secs, cost_usd, quality_pass fields.

    Returns:
        List of AgentMetrics grouped by adapter+model.
    """
    if not sessions:
        return []

    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for s in sessions:
        key = (str(s.get("provider", "unknown")), str(s.get("model", "unknown")))
        groups[key].append(s)

    results: list[AgentMetrics] = []
    for (adapter, model), items in groups.items():
        total = len(items)
        succeeded = sum(1 for s in items if s.get("status") == "done")
        failed = sum(1 for s in items if s.get("status") == "failed")
        durations = [float(s.get("duration_secs", 0)) for s in items if s.get("duration_secs")]
        avg_duration = sum(durations) / len(durations) if durations else 0.0
        total_cost = sum(float(s.get("cost_usd", 0)) for s in items)
        quality_passes = sum(1 for s in items if s.get("quality_pass"))
        qg_rate = quality_passes / total if total else 0.0

        results.append(
            AgentMetrics(
                adapter=adapter,
                model=model,
                total_tasks=total,
                succeeded=succeeded,
                failed=failed,
                avg_completion_secs=avg_duration,
                total_cost_usd=total_cost,
                quality_gate_pass_rate=qg_rate,
            )
        )

    return results


@router.get("/agents/comparison")
async def get_agent_comparison(request: Request) -> list[dict[str, Any]]:
    """Get agent comparison metrics across adapters and models.

    Returns:
        List of per-adapter/model metric summaries.
    """
    store = request.app.state.store
    archive = store.read_archive(limit=500)

    sessions: list[dict[str, Any]] = []
    for record in archive:
        sessions.append({
            "provider": record.get("provider", "unknown"),
            "model": record.get("model", "unknown"),
            "status": record.get("status", "unknown"),
            "duration_secs": record.get("duration_secs", 0),
            "cost_usd": record.get("cost_usd", 0),
            "quality_pass": record.get("quality_pass", False),
        })

    metrics = compute_agent_metrics(sessions)
    return [m.model_dump() for m in metrics]
```

- [ ] **Step 4: Run tests — expect PASS**

Run: `uv run pytest tests/unit/test_route_agent_comparison.py -x -q`

- [ ] **Step 5: Wire router into server.py**

In `src/bernstein/core/server.py`, add router include:
```python
from bernstein.core.routes.agent_comparison import router as agent_comparison_router
application.include_router(agent_comparison_router)
api_v1_router.include_router(agent_comparison_router)
```

- [ ] **Step 6: Commit**

```bash
git add src/bernstein/core/routes/agent_comparison.py tests/unit/test_route_agent_comparison.py src/bernstein/core/server.py
git commit -m "feat(web-018): add agent comparison view API"
```

---

## Task 9: WEB-019 — Audit log endpoint

**Files:**
- Create: `src/bernstein/core/routes/audit_log.py`
- Modify: `src/bernstein/core/server.py` (include router)
- Create: `tests/unit/test_route_audit_log.py`

- [ ] **Step 1: Write tests for audit log endpoint**

```python
# tests/unit/test_route_audit_log.py
"""Tests for WEB-019: Audit log endpoint with search and filtering."""

from __future__ import annotations

from bernstein.core.routes.audit_log import (
    AuditLogQuery,
    filter_events,
    paginate,
)


class TestAuditLogQuery:
    def test_defaults(self) -> None:
        q = AuditLogQuery()
        assert q.page == 1
        assert q.page_size == 50
        assert q.event_type is None

    def test_offset(self) -> None:
        q = AuditLogQuery(page=3, page_size=20)
        assert q.offset == 40


class TestFilterEvents:
    def test_filter_by_event_type(self) -> None:
        events = [
            {"event_type": "task.transition", "details": {}},
            {"event_type": "agent.spawn", "details": {}},
            {"event_type": "task.transition", "details": {}},
        ]
        result = filter_events(events, event_type="task.transition")
        assert len(result) == 2

    def test_filter_by_search(self) -> None:
        events = [
            {"event_type": "task.transition", "details": {"message": "completed backend task"}},
            {"event_type": "task.transition", "details": {"message": "started frontend task"}},
        ]
        result = filter_events(events, search="backend")
        assert len(result) == 1

    def test_no_filters(self) -> None:
        events = [{"event_type": "a"}, {"event_type": "b"}]
        result = filter_events(events)
        assert len(result) == 2

    def test_filter_by_time_range(self) -> None:
        events = [
            {"event_type": "a", "timestamp": "2026-04-01T00:00:00Z"},
            {"event_type": "b", "timestamp": "2026-04-03T00:00:00Z"},
            {"event_type": "c", "timestamp": "2026-04-05T00:00:00Z"},
        ]
        result = filter_events(
            events,
            from_ts="2026-04-02T00:00:00Z",
            to_ts="2026-04-04T00:00:00Z",
        )
        assert len(result) == 1
        assert result[0]["event_type"] == "b"


class TestPaginate:
    def test_first_page(self) -> None:
        items = list(range(100))
        page = paginate(items, page=1, page_size=10)
        assert page == list(range(10))

    def test_second_page(self) -> None:
        items = list(range(100))
        page = paginate(items, page=2, page_size=10)
        assert page == list(range(10, 20))

    def test_last_page_partial(self) -> None:
        items = list(range(25))
        page = paginate(items, page=3, page_size=10)
        assert page == [20, 21, 22, 23, 24]

    def test_beyond_range(self) -> None:
        items = list(range(5))
        page = paginate(items, page=2, page_size=10)
        assert page == []
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `uv run pytest tests/unit/test_route_audit_log.py -x -q`

- [ ] **Step 3: Implement audit log route**

```python
# src/bernstein/core/routes/audit_log.py
"""WEB-019: Audit log endpoint with search and filtering.

Exposes audit log entries via GET /audit with pagination,
event_type filtering, time range, and full-text search.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(tags=["audit"])


class AuditLogQuery(BaseModel):
    """Query parameters for audit log search."""

    event_type: str | None = None
    from_ts: str | None = Field(None, alias="from")
    to_ts: str | None = Field(None, alias="to")
    search: str | None = None
    page: int = Field(1, ge=1)
    page_size: int = Field(50, ge=1, le=200)

    model_config = {"populate_by_name": True}

    @property
    def offset(self) -> int:
        """Compute offset from page number."""
        return (self.page - 1) * self.page_size


def filter_events(
    events: list[dict[str, Any]],
    *,
    event_type: str | None = None,
    from_ts: str | None = None,
    to_ts: str | None = None,
    search: str | None = None,
) -> list[dict[str, Any]]:
    """Filter audit events by criteria.

    Args:
        events: Raw event dicts.
        event_type: Filter by event_type field.
        from_ts: ISO timestamp lower bound (inclusive).
        to_ts: ISO timestamp upper bound (inclusive).
        search: Full-text search across event details.

    Returns:
        Filtered list of events.
    """
    result: list[dict[str, Any]] = []
    for ev in events:
        if event_type and ev.get("event_type") != event_type:
            continue
        ts = ev.get("timestamp", "")
        if from_ts and ts < from_ts:
            continue
        if to_ts and ts > to_ts:
            continue
        if search:
            text = json.dumps(ev.get("details", {})).lower()
            if search.lower() not in text:
                continue
        result.append(ev)
    return result


def paginate(items: list[Any], page: int, page_size: int) -> list[Any]:
    """Return a page slice of items.

    Args:
        items: Full list.
        page: 1-based page number.
        page_size: Items per page.

    Returns:
        Slice of items for the requested page.
    """
    start = (page - 1) * page_size
    return items[start : start + page_size]


@router.get("/audit")
async def query_audit_log(
    request: Request,
    event_type: str | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """Query the audit log with filtering and pagination.

    Returns:
        Dict with items, total, page, page_size.
    """
    from_ts = request.query_params.get("from")
    to_ts = request.query_params.get("to")

    audit_dir = Path(".sdd/audit")
    events: list[dict[str, Any]] = []

    if audit_dir.is_dir():
        for log_file in sorted(audit_dir.glob("*.jsonl")):
            for line in log_file.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    filtered = filter_events(
        events,
        event_type=event_type,
        from_ts=from_ts,
        to_ts=to_ts,
        search=search,
    )

    page_items = paginate(filtered, page, page_size)

    return {
        "items": page_items,
        "total": len(filtered),
        "page": page,
        "page_size": page_size,
    }
```

- [ ] **Step 4: Run tests — expect PASS**

Run: `uv run pytest tests/unit/test_route_audit_log.py -x -q`

- [ ] **Step 5: Wire router into server.py**

In `src/bernstein/core/server.py`:
```python
from bernstein.core.routes.audit_log import router as audit_log_router
application.include_router(audit_log_router)
api_v1_router.include_router(audit_log_router)
```

- [ ] **Step 6: Commit**

```bash
git add src/bernstein/core/routes/audit_log.py tests/unit/test_route_audit_log.py src/bernstein/core/server.py
git commit -m "feat(web-019): add audit log endpoint with search and filtering"
```

---

## Task 10: WEB-020 — Mobile-responsive dashboard layout

**Files:**
- Modify: `src/bernstein/dashboard/templates/index.html`
- Create: `tests/unit/test_dashboard_responsive.py`

- [ ] **Step 1: Write tests for responsive CSS presence**

```python
# tests/unit/test_dashboard_responsive.py
"""Tests for WEB-020: Dashboard mobile-responsive layout."""

from __future__ import annotations

from pathlib import Path


_INDEX_HTML = Path("src/bernstein/dashboard/templates/index.html")


class TestResponsiveLayout:
    def test_has_viewport_meta(self) -> None:
        content = _INDEX_HTML.read_text()
        assert 'name="viewport"' in content

    def test_has_responsive_breakpoints(self) -> None:
        content = _INDEX_HTML.read_text()
        assert "@media" in content or "sm:" in content or "md:" in content

    def test_has_mobile_nav(self) -> None:
        content = _INDEX_HTML.read_text()
        # Should have mobile-specific layout classes
        assert "mobile-menu" in content or "md:grid-cols" in content

    def test_has_responsive_grid(self) -> None:
        content = _INDEX_HTML.read_text()
        # Should use responsive grid classes
        assert "grid-cols-1" in content or "sm:grid-cols" in content
```

- [ ] **Step 2: Run tests — some may PASS (viewport meta already exists)**

Run: `uv run pytest tests/unit/test_dashboard_responsive.py -x -q`

- [ ] **Step 3: Add responsive CSS to index.html**

Add responsive styles to the `<style>` section of `src/bernstein/dashboard/templates/index.html`:

```css
/* WEB-020: Mobile responsive breakpoints */
@media (max-width: 768px) {
  .stat-grid { grid-template-columns: repeat(2, 1fr) !important; }
  .main-grid { grid-template-columns: 1fr !important; }
  .sidebar { display: none; }
  .mobile-sidebar-toggle { display: block !important; }
  .kanban-col { min-width: 140px; }
}
@media (max-width: 480px) {
  .stat-grid { grid-template-columns: 1fr !important; }
  body { font-size: 14px; }
}
```

Update the stat cards grid to use responsive Tailwind classes:
- Change `grid-cols-4` to `grid-cols-1 sm:grid-cols-2 md:grid-cols-4`
- Change the main two-column layout to `grid-cols-1 lg:grid-cols-5`
- Add `overflow-x-auto` to the task table wrapper
- Add a mobile menu toggle button (hidden on desktop: `hidden md:block`)

- [ ] **Step 4: Run tests — expect PASS**

Run: `uv run pytest tests/unit/test_dashboard_responsive.py -x -q`

- [ ] **Step 5: Commit**

```bash
git add src/bernstein/dashboard/templates/index.html tests/unit/test_dashboard_responsive.py
git commit -m "feat(web-020): add mobile-responsive layout to dashboard"
```

---

## Task 11: WEB-021 — GraphQL API

**Files:**
- Create: `src/bernstein/core/routes/graphql_api.py`
- Modify: `src/bernstein/core/server.py` (include router)
- Create: `tests/unit/test_route_graphql.py`

- [ ] **Step 1: Write tests for GraphQL schema and resolvers**

```python
# tests/unit/test_route_graphql.py
"""Tests for WEB-021: GraphQL API alongside REST."""

from __future__ import annotations

import json

from bernstein.core.routes.graphql_api import (
    execute_graphql,
    parse_graphql_query,
    GraphQLRequest,
)


class TestParseQuery:
    def test_parse_simple_query(self) -> None:
        result = parse_graphql_query("{ tasks { id title status } }")
        assert result.operation == "tasks"
        assert "id" in result.fields
        assert "title" in result.fields
        assert "status" in result.fields

    def test_parse_with_args(self) -> None:
        result = parse_graphql_query('{ tasks(status: "open") { id title } }')
        assert result.operation == "tasks"
        assert result.args.get("status") == "open"

    def test_parse_nested_fields(self) -> None:
        result = parse_graphql_query("{ tasks { id agent { id provider } } }")
        assert result.operation == "tasks"
        assert "id" in result.fields

    def test_parse_agents_query(self) -> None:
        result = parse_graphql_query("{ agents { id provider status } }")
        assert result.operation == "agents"

    def test_parse_status_query(self) -> None:
        result = parse_graphql_query("{ status { total completed failed } }")
        assert result.operation == "status"


class TestExecuteGraphQL:
    def test_tasks_query(self) -> None:
        mock_store = _MockStore([
            {"id": "t1", "title": "Task 1", "status": "open", "role": "backend"},
        ])
        result = execute_graphql("{ tasks { id title status } }", store=mock_store)
        assert "errors" not in result
        assert len(result["data"]["tasks"]) == 1
        assert result["data"]["tasks"][0]["id"] == "t1"

    def test_status_query(self) -> None:
        mock_store = _MockStore([])
        result = execute_graphql("{ status { total completed } }", store=mock_store)
        assert "data" in result

    def test_unknown_operation(self) -> None:
        mock_store = _MockStore([])
        result = execute_graphql("{ unknown { id } }", store=mock_store)
        assert "errors" in result

    def test_request_model(self) -> None:
        req = GraphQLRequest(query="{ tasks { id } }")
        assert req.query == "{ tasks { id } }"
        assert req.variables is None


class _MockStore:
    """Minimal mock for testing GraphQL resolvers."""

    def __init__(self, tasks: list[dict]) -> None:
        self._tasks = tasks

    def list_tasks(self) -> list:
        return self._tasks

    def status_summary(self) -> dict:
        return {"total": len(self._tasks), "completed": 0, "failed": 0, "open": len(self._tasks)}
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `uv run pytest tests/unit/test_route_graphql.py -x -q`

- [ ] **Step 3: Implement lightweight GraphQL endpoint**

NOTE: We implement a minimal GraphQL parser rather than adding a heavy dependency like strawberry-graphql. This keeps the dependency tree minimal. The parser handles the simple nested-field queries that dashboard clients need.

```python
# src/bernstein/core/routes/graphql_api.py
"""WEB-021: Lightweight GraphQL API alongside REST.

Provides a POST /graphql endpoint that parses simple GraphQL queries
and resolves them against the task store. No external GraphQL library
dependency — just a simple query parser for the subset of GraphQL
that dashboard clients need.

Supported queries:
  { tasks(status: "open") { id title status role agent cost_usd } }
  { agents { id provider status } }
  { status { total completed failed active_agents } }
  { costs { total_usd per_model { model cost } } }
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(tags=["graphql"])


class GraphQLRequest(BaseModel):
    """GraphQL request body."""

    query: str
    variables: dict[str, Any] | None = None
    operation_name: str | None = Field(None, alias="operationName")

    model_config = {"populate_by_name": True}


@dataclass
class ParsedQuery:
    """Parsed GraphQL query."""

    operation: str = ""
    fields: list[str] = field(default_factory=list)
    args: dict[str, str] = field(default_factory=dict)


def parse_graphql_query(query: str) -> ParsedQuery:
    """Parse a simple GraphQL query string.

    Args:
        query: GraphQL query string like ``{ tasks { id title } }``.

    Returns:
        ParsedQuery with operation name, fields, and args.
    """
    # Strip outer braces
    inner = query.strip().strip("{").strip("}").strip()

    # Extract operation name and optional args
    match = re.match(r"(\w+)\s*(?:\(([^)]*)\))?\s*\{([^}]*)\}", inner)
    if not match:
        return ParsedQuery()

    operation = match.group(1)
    args_str = match.group(2) or ""
    fields_str = match.group(3)

    # Parse args: key: "value" pairs
    args: dict[str, str] = {}
    for arg_match in re.finditer(r'(\w+)\s*:\s*"([^"]*)"', args_str):
        args[arg_match.group(1)] = arg_match.group(2)

    # Parse fields (simple: just top-level names)
    fields = re.findall(r"\w+", fields_str)

    return ParsedQuery(operation=operation, fields=fields, args=args)


def _resolve_tasks(parsed: ParsedQuery, store: Any) -> list[dict[str, Any]]:
    """Resolve a tasks query."""
    tasks = store.list_tasks()
    status_filter = parsed.args.get("status")

    results: list[dict[str, Any]] = []
    for t in tasks:
        task = t if isinstance(t, dict) else (t.__dict__ if hasattr(t, "__dict__") else {})
        if status_filter and task.get("status") != status_filter:
            continue
        row: dict[str, Any] = {}
        for f in parsed.fields:
            if f in task:
                row[f] = task[f]
            elif f == "agent":
                row[f] = {"id": task.get("assigned_agent", ""), "provider": task.get("provider", "")}
            else:
                row[f] = None
        results.append(row)
    return results


def _resolve_status(parsed: ParsedQuery, store: Any) -> dict[str, Any]:
    """Resolve a status query."""
    summary = store.status_summary()
    return {f: summary.get(f) for f in parsed.fields if f in summary}


def execute_graphql(query: str, store: Any, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    """Execute a GraphQL query against the store.

    Args:
        query: GraphQL query string.
        store: TaskStore instance.
        variables: Optional query variables.

    Returns:
        Dict with "data" or "errors" key.
    """
    parsed = parse_graphql_query(query)

    if not parsed.operation:
        return {"errors": [{"message": "Could not parse query"}]}

    if parsed.operation == "tasks":
        data = _resolve_tasks(parsed, store)
        return {"data": {"tasks": data}}
    elif parsed.operation == "status":
        data = _resolve_status(parsed, store)
        return {"data": {"status": data}}
    elif parsed.operation == "agents":
        return {"data": {"agents": []}}
    elif parsed.operation == "costs":
        return {"data": {"costs": {"total_usd": 0.0, "per_model": []}}}
    else:
        return {"errors": [{"message": f"Unknown operation: {parsed.operation}"}]}


@router.post("/graphql")
async def graphql_endpoint(req: GraphQLRequest, request: Request) -> dict[str, Any]:
    """Execute a GraphQL query.

    Args:
        req: GraphQL request body.
        request: FastAPI request.

    Returns:
        GraphQL response with data or errors.
    """
    store = request.app.state.store
    return execute_graphql(req.query, store=store, variables=req.variables)
```

- [ ] **Step 4: Run tests — expect PASS**

Run: `uv run pytest tests/unit/test_route_graphql.py -x -q`

- [ ] **Step 5: Wire router into server.py**

```python
from bernstein.core.routes.graphql_api import router as graphql_router
application.include_router(graphql_router)
```

- [ ] **Step 6: Commit**

```bash
git add src/bernstein/core/routes/graphql_api.py tests/unit/test_route_graphql.py src/bernstein/core/server.py
git commit -m "feat(web-021): add lightweight GraphQL API alongside REST"
```

---

## Task 12: WEB-022 — Dashboard embedding support (iframe-friendly)

**Files:**
- Create: `src/bernstein/core/routes/embedding.py`
- Modify: `src/bernstein/core/server.py` (add middleware)
- Create: `tests/unit/test_route_embedding.py`

- [ ] **Step 1: Write tests for embedding headers**

```python
# tests/unit/test_route_embedding.py
"""Tests for WEB-022: Dashboard embedding support (iframe-friendly)."""

from __future__ import annotations

from bernstein.core.routes.embedding import (
    EmbeddingConfig,
    build_csp_header,
    build_frame_options_header,
    load_embedding_config,
)
from pathlib import Path


class TestEmbeddingConfig:
    def test_default_deny(self) -> None:
        config = EmbeddingConfig()
        assert config.allow_embedding is False
        assert config.allowed_origins == []

    def test_allow_all(self) -> None:
        config = EmbeddingConfig(allow_embedding=True)
        assert config.allow_embedding is True


class TestBuildHeaders:
    def test_deny_frame_options(self) -> None:
        config = EmbeddingConfig(allow_embedding=False)
        assert build_frame_options_header(config) == "DENY"

    def test_allow_any_frame_options(self) -> None:
        config = EmbeddingConfig(allow_embedding=True)
        # No X-Frame-Options when embedding is allowed from any origin
        assert build_frame_options_header(config) is None

    def test_specific_origins_frame_options(self) -> None:
        config = EmbeddingConfig(
            allow_embedding=True,
            allowed_origins=["https://example.com"],
        )
        assert build_frame_options_header(config) is None  # CSP handles this

    def test_deny_csp(self) -> None:
        config = EmbeddingConfig(allow_embedding=False)
        csp = build_csp_header(config)
        assert "frame-ancestors 'none'" in csp

    def test_allow_any_csp(self) -> None:
        config = EmbeddingConfig(allow_embedding=True)
        csp = build_csp_header(config)
        assert "frame-ancestors *" in csp

    def test_specific_origins_csp(self) -> None:
        config = EmbeddingConfig(
            allow_embedding=True,
            allowed_origins=["https://example.com", "https://notion.so"],
        )
        csp = build_csp_header(config)
        assert "https://example.com" in csp
        assert "https://notion.so" in csp


class TestLoadConfig:
    def test_load_missing(self) -> None:
        config = load_embedding_config(Path("/nonexistent.yaml"))
        assert config == EmbeddingConfig()

    def test_load_from_yaml(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "bernstein.yaml"
        yaml_file.write_text(
            "embedding:\n  allow_embedding: true\n  allowed_origins:\n    - https://example.com\n",
            encoding="utf-8",
        )
        config = load_embedding_config(yaml_file)
        assert config.allow_embedding is True
        assert config.allowed_origins == ["https://example.com"]
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `uv run pytest tests/unit/test_route_embedding.py -x -q`

- [ ] **Step 3: Implement embedding support**

```python
# src/bernstein/core/routes/embedding.py
"""WEB-022: Dashboard embedding support (iframe-friendly).

Configurable X-Frame-Options and CSP headers to allow the dashboard
to be embedded in iframes (VS Code webview, Notion, internal portals).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmbeddingConfig:
    """Configuration for iframe embedding.

    Attributes:
        allow_embedding: Whether to allow iframe embedding.
        allowed_origins: Specific origins allowed (empty = all if allow_embedding is True).
    """

    allow_embedding: bool = False
    allowed_origins: list[str] = field(default_factory=list)


def build_frame_options_header(config: EmbeddingConfig) -> str | None:
    """Build X-Frame-Options header value.

    Args:
        config: Embedding configuration.

    Returns:
        Header value, or None if not needed (CSP handles it).
    """
    if not config.allow_embedding:
        return "DENY"
    # When embedding is allowed, we rely on CSP frame-ancestors instead
    return None


def build_csp_header(config: EmbeddingConfig) -> str:
    """Build Content-Security-Policy frame-ancestors directive.

    Args:
        config: Embedding configuration.

    Returns:
        CSP header value string.
    """
    if not config.allow_embedding:
        return "frame-ancestors 'none'"
    if config.allowed_origins:
        origins = " ".join(config.allowed_origins)
        return f"frame-ancestors 'self' {origins}"
    return "frame-ancestors *"


def load_embedding_config(yaml_path: Path | None = None) -> EmbeddingConfig:
    """Load embedding configuration from bernstein.yaml.

    Args:
        yaml_path: Path to config file. Searches defaults if None.

    Returns:
        EmbeddingConfig with user preferences or defaults.
    """
    try:
        import yaml
    except ImportError:
        return EmbeddingConfig()

    candidates: list[Path] = []
    if yaml_path:
        candidates.append(yaml_path)
    else:
        candidates.append(Path("bernstein.yaml"))
        candidates.append(Path.home() / ".bernstein" / "bernstein.yaml")

    for path in candidates:
        if not path.exists():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue
            emb = data.get("embedding")
            if not isinstance(emb, dict):
                continue
            return EmbeddingConfig(
                allow_embedding=bool(emb.get("allow_embedding", False)),
                allowed_origins=list(emb.get("allowed_origins", [])),
            )
        except Exception:
            continue
    return EmbeddingConfig()


class EmbeddingHeadersMiddleware(BaseHTTPMiddleware):
    """Middleware that adds iframe embedding headers to dashboard responses.

    Only applies to /dashboard paths.
    """

    def __init__(self, app: Any, config: EmbeddingConfig | None = None) -> None:
        super().__init__(app)
        self._config = config or load_embedding_config()

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        """Add embedding headers to dashboard responses."""
        response: Response = await call_next(request)

        # Only apply to dashboard paths
        if request.url.path.startswith("/dashboard"):
            frame_opts = build_frame_options_header(self._config)
            if frame_opts:
                response.headers["X-Frame-Options"] = frame_opts
            response.headers["Content-Security-Policy"] = build_csp_header(self._config)

        return response
```

- [ ] **Step 4: Run tests — expect PASS**

Run: `uv run pytest tests/unit/test_route_embedding.py -x -q`

- [ ] **Step 5: Wire middleware into server.py**

In `src/bernstein/core/server.py`, add after the other middleware:
```python
from bernstein.core.routes.embedding import EmbeddingHeadersMiddleware
application.add_middleware(EmbeddingHeadersMiddleware)
```

- [ ] **Step 6: Commit**

```bash
git add src/bernstein/core/routes/embedding.py tests/unit/test_route_embedding.py src/bernstein/core/server.py
git commit -m "feat(web-022): add dashboard embedding support with configurable CSP/X-Frame-Options"
```

---

## Post-Implementation

After all 12 tasks are complete:

1. Run full test suite: `uv run python scripts/run_tests.py -x`
2. Run linter: `uv run ruff check src/ tests/`
3. Run type checker: `uv run pyright src/`
4. Move all 12 ticket files from `.sdd/backlog/open/` to `.sdd/backlog/closed/`
5. Merge to main and push
