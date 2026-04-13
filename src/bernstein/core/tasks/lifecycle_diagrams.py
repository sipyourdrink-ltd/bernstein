"""State machine diagram generation for task and agent lifecycles.

Extracts transition tables from the lifecycle governance kernel and renders
them as Mermaid stateDiagram-v2 or ASCII art for documentation and terminal
display.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class State:
    """A single state in a finite state machine."""

    name: str
    description: str
    is_terminal: bool


@dataclass(frozen=True)
class Transition:
    """A directed edge between two states."""

    from_state: str
    to_state: str
    trigger: str
    description: str


@dataclass(frozen=True)
class StateMachine:
    """A complete state machine definition."""

    name: str
    states: tuple[State, ...]
    transitions: tuple[Transition, ...]


# ---------------------------------------------------------------------------
# Task lifecycle descriptions
# ---------------------------------------------------------------------------

_TASK_STATE_DESCRIPTIONS: dict[str, str] = {
    "planned": "Awaiting human approval before execution",
    "open": "Available for claiming by an agent",
    "claimed": "Assigned to an agent, not yet started",
    "in_progress": "Agent is actively working on the task",
    "done": "Task completed successfully",
    "closed": "Verified and archived",
    "failed": "Task execution failed",
    "blocked": "Waiting on external dependency",
    "waiting_for_subtasks": "Parent task waiting for children",
    "cancelled": "Task was cancelled",
    "orphaned": "Agent crashed mid-task, pending recovery",
    "pending_approval": "Completed, awaiting human approval",
}

_TASK_TRANSITION_TRIGGERS: dict[tuple[str, str], str] = {
    ("planned", "open"): "approve",
    ("planned", "cancelled"): "cancel",
    ("open", "claimed"): "claim",
    ("open", "waiting_for_subtasks"): "split",
    ("open", "cancelled"): "cancel",
    ("claimed", "in_progress"): "start_work",
    ("claimed", "open"): "unclaim",
    ("claimed", "done"): "fast_complete",
    ("claimed", "failed"): "fail",
    ("claimed", "cancelled"): "cancel",
    ("claimed", "waiting_for_subtasks"): "split",
    ("claimed", "blocked"): "block",
    ("in_progress", "done"): "complete",
    ("in_progress", "failed"): "fail",
    ("in_progress", "blocked"): "block",
    ("in_progress", "waiting_for_subtasks"): "split",
    ("in_progress", "open"): "requeue",
    ("in_progress", "cancelled"): "cancel",
    ("in_progress", "orphaned"): "agent_crash",
    ("orphaned", "done"): "recover_complete",
    ("orphaned", "failed"): "recover_fail",
    ("orphaned", "open"): "recover_requeue",
    ("blocked", "open"): "unblock",
    ("blocked", "cancelled"): "cancel",
    ("waiting_for_subtasks", "done"): "subtasks_done",
    ("waiting_for_subtasks", "blocked"): "subtask_timeout",
    ("waiting_for_subtasks", "cancelled"): "cancel",
    ("failed", "open"): "retry",
    ("done", "closed"): "verify_close",
    ("done", "failed"): "verification_fail",
}


# ---------------------------------------------------------------------------
# Agent lifecycle descriptions
# ---------------------------------------------------------------------------

_AGENT_STATE_DESCRIPTIONS: dict[str, str] = {
    "starting": "Agent process is spawning",
    "working": "Agent is executing a task",
    "idle": "Agent finished task, awaiting next assignment",
    "dead": "Agent process has terminated",
}

_AGENT_TRANSITION_TRIGGERS: dict[tuple[str, str], str] = {
    ("starting", "working"): "spawn_success",
    ("starting", "dead"): "spawn_failure",
    ("working", "idle"): "task_complete",
    ("working", "dead"): "kill_or_crash",
    ("idle", "working"): "assign_task",
    ("idle", "dead"): "recycle",
}


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------


def extract_task_lifecycle() -> StateMachine:
    """Build a StateMachine from the canonical TASK_TRANSITIONS table."""
    from bernstein.core.tasks.lifecycle import TASK_TRANSITIONS, TERMINAL_TASK_STATUSES
    from bernstein.core.tasks.models import TaskStatus

    states: list[State] = []
    for ts in TaskStatus:
        states.append(
            State(
                name=ts.value,
                description=_TASK_STATE_DESCRIPTIONS.get(ts.value, ts.value),
                is_terminal=ts in TERMINAL_TASK_STATUSES,
            )
        )

    transitions: list[Transition] = []
    for from_ts, to_ts in TASK_TRANSITIONS:
        key = (from_ts.value, to_ts.value)
        trigger = _TASK_TRANSITION_TRIGGERS.get(key, f"{from_ts.value}_to_{to_ts.value}")
        desc = f"{from_ts.value} -> {to_ts.value}"
        transitions.append(
            Transition(
                from_state=from_ts.value,
                to_state=to_ts.value,
                trigger=trigger,
                description=desc,
            )
        )

    return StateMachine(
        name="Task Lifecycle",
        states=tuple(states),
        transitions=tuple(transitions),
    )


def extract_agent_lifecycle() -> StateMachine:
    """Build a StateMachine from the canonical AGENT_TRANSITIONS table."""
    from bernstein.core.tasks.lifecycle import AGENT_TRANSITIONS

    # Collect all states from transition keys.
    state_names: list[str] = []
    for from_s, to_s in AGENT_TRANSITIONS:
        if from_s not in state_names:
            state_names.append(from_s)
        if to_s not in state_names:
            state_names.append(to_s)

    # Terminal = no outbound transitions.
    sources = {from_s for from_s, _ in AGENT_TRANSITIONS}
    states: list[State] = []
    for sn in state_names:
        states.append(
            State(
                name=sn,
                description=_AGENT_STATE_DESCRIPTIONS.get(sn, sn),
                is_terminal=sn not in sources,
            )
        )

    transitions: list[Transition] = []
    for from_s, to_s in AGENT_TRANSITIONS:
        key = (from_s, to_s)
        trigger = _AGENT_TRANSITION_TRIGGERS.get(key, f"{from_s}_to_{to_s}")
        desc = f"{from_s} -> {to_s}"
        transitions.append(
            Transition(
                from_state=from_s,
                to_state=to_s,
                trigger=trigger,
                description=desc,
            )
        )

    return StateMachine(
        name="Agent Lifecycle",
        states=tuple(states),
        transitions=tuple(transitions),
    )


# ---------------------------------------------------------------------------
# Mermaid renderer
# ---------------------------------------------------------------------------


def render_mermaid(sm: StateMachine) -> str:
    """Render a StateMachine as Mermaid stateDiagram-v2 syntax."""
    lines: list[str] = ["stateDiagram-v2"]

    # State descriptions (notes) and terminal classDef.
    terminal_names: list[str] = []
    for st in sm.states:
        safe = _mermaid_state_id(st.name)
        lines.append(f"    {safe} : {st.name}")
        if st.is_terminal:
            terminal_names.append(safe)

    lines.append("")

    # Transitions.
    for tr in sm.transitions:
        from_id = _mermaid_state_id(tr.from_state)
        to_id = _mermaid_state_id(tr.to_state)
        lines.append(f"    {from_id} --> {to_id} : {tr.trigger}")

    # Terminal styling.
    if terminal_names:
        lines.append("")
        lines.append("    classDef terminal fill:#f96,stroke:#333,stroke-width:2px")
        for tn in terminal_names:
            lines.append(f"    class {tn} terminal")

    return "\n".join(lines) + "\n"


def _mermaid_state_id(name: str) -> str:
    """Convert a state name to a valid Mermaid identifier."""
    return name.replace(" ", "_").replace("-", "_")


# ---------------------------------------------------------------------------
# ASCII renderer
# ---------------------------------------------------------------------------


def render_ascii(sm: StateMachine) -> str:
    """Render a simple ASCII diagram for terminal display."""
    lines: list[str] = [f"=== {sm.name} ===", ""]

    # States section.
    lines.append("States:")
    for st in sm.states:
        marker = " [TERMINAL]" if st.is_terminal else ""
        lines.append(f"  [{st.name}]{marker} - {st.description}")

    lines.append("")
    lines.append("Transitions:")
    for tr in sm.transitions:
        lines.append(f"  {tr.from_state} --({tr.trigger})--> {tr.to_state}")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Diagram file generation
# ---------------------------------------------------------------------------


def generate_all_diagrams(output_dir: str | Path) -> list[Path]:
    """Generate Mermaid .md diagram files for all lifecycles.

    Args:
        output_dir: Directory to write diagram files into.

    Returns:
        List of paths to generated files.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    generated: list[Path] = []

    for extractor, filename, title in [
        (extract_task_lifecycle, "task_lifecycle.md", "Task Lifecycle"),
        (extract_agent_lifecycle, "agent_lifecycle.md", "Agent Lifecycle"),
    ]:
        sm = extractor()
        mermaid = render_mermaid(sm)
        # Indent mermaid body inside the fenced block.
        content = f"# {title} State Machine\n\n"
        content += f"```mermaid\n{mermaid.rstrip()}\n```\n\n"
        content += "## States\n\n"
        content += "| State | Description | Terminal |\n"
        content += "|-------|-------------|----------|\n"
        for st in sm.states:
            terminal_marker = "Yes" if st.is_terminal else "No"
            content += f"| {st.name} | {st.description} | {terminal_marker} |\n"

        content += "\n## Transitions\n\n"
        content += "| From | To | Trigger |\n"
        content += "|------|----|---------|\n"
        for tr in sm.transitions:
            content += f"| {tr.from_state} | {tr.to_state} | {tr.trigger} |\n"

        path = out / filename
        path.write_text(content, encoding="utf-8")
        generated.append(path)

    return generated
