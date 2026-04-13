"""Tests for lifecycle_diagrams — state machine extraction and rendering."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from bernstein.core.tasks.lifecycle_diagrams import (
    State,
    StateMachine,
    Transition,
    extract_agent_lifecycle,
    extract_task_lifecycle,
    generate_all_diagrams,
    render_ascii,
    render_mermaid,
)

# ---------------------------------------------------------------------------
# Dataclass tests
# ---------------------------------------------------------------------------


class TestState:
    def test_frozen(self) -> None:
        s = State(name="open", description="Available", is_terminal=False)
        with pytest.raises(AttributeError):
            s.name = "closed"  # type: ignore[misc]

    def test_fields(self) -> None:
        s = State(name="done", description="Completed", is_terminal=True)
        assert s.name == "done"
        assert s.description == "Completed"
        assert s.is_terminal is True


class TestTransition:
    def test_frozen(self) -> None:
        t = Transition(from_state="a", to_state="b", trigger="go", description="a->b")
        with pytest.raises(AttributeError):
            t.from_state = "x"  # type: ignore[misc]

    def test_fields(self) -> None:
        t = Transition(from_state="open", to_state="claimed", trigger="claim", description="claim it")
        assert t.from_state == "open"
        assert t.to_state == "claimed"
        assert t.trigger == "claim"
        assert t.description == "claim it"


class TestStateMachine:
    def test_frozen(self) -> None:
        sm = StateMachine(name="test", states=(), transitions=())
        with pytest.raises(AttributeError):
            sm.name = "other"  # type: ignore[misc]

    def test_tuples_not_lists(self) -> None:
        sm = StateMachine(name="test", states=(), transitions=())
        assert isinstance(sm.states, tuple)
        assert isinstance(sm.transitions, tuple)


# ---------------------------------------------------------------------------
# Extraction tests
# ---------------------------------------------------------------------------


class TestExtractTaskLifecycle:
    def test_returns_state_machine(self) -> None:
        sm = extract_task_lifecycle()
        assert isinstance(sm, StateMachine)
        assert sm.name == "Task Lifecycle"

    def test_has_expected_states(self) -> None:
        sm = extract_task_lifecycle()
        names = {s.name for s in sm.states}
        for expected in ("open", "claimed", "in_progress", "done", "failed", "closed"):
            assert expected in names, f"Missing state: {expected}"

    def test_has_transitions(self) -> None:
        sm = extract_task_lifecycle()
        assert len(sm.transitions) > 0

    def test_transitions_reference_valid_states(self) -> None:
        sm = extract_task_lifecycle()
        state_names = {s.name for s in sm.states}
        for tr in sm.transitions:
            assert tr.from_state in state_names, f"Unknown from_state: {tr.from_state}"
            assert tr.to_state in state_names, f"Unknown to_state: {tr.to_state}"

    def test_terminal_states_identified(self) -> None:
        sm = extract_task_lifecycle()
        terminal = {s.name for s in sm.states if s.is_terminal}
        # closed and cancelled have no outbound transitions
        assert "closed" in terminal
        assert "cancelled" in terminal

    def test_open_to_claimed_exists(self) -> None:
        sm = extract_task_lifecycle()
        pairs = {(t.from_state, t.to_state) for t in sm.transitions}
        assert ("open", "claimed") in pairs

    def test_done_to_closed_exists(self) -> None:
        sm = extract_task_lifecycle()
        pairs = {(t.from_state, t.to_state) for t in sm.transitions}
        assert ("done", "closed") in pairs


class TestExtractAgentLifecycle:
    def test_returns_state_machine(self) -> None:
        sm = extract_agent_lifecycle()
        assert isinstance(sm, StateMachine)
        assert sm.name == "Agent Lifecycle"

    def test_has_expected_states(self) -> None:
        sm = extract_agent_lifecycle()
        names = {s.name for s in sm.states}
        for expected in ("starting", "working", "idle", "dead"):
            assert expected in names, f"Missing state: {expected}"

    def test_dead_is_terminal(self) -> None:
        sm = extract_agent_lifecycle()
        dead_states = [s for s in sm.states if s.name == "dead"]
        assert len(dead_states) == 1
        assert dead_states[0].is_terminal is True

    def test_starting_is_not_terminal(self) -> None:
        sm = extract_agent_lifecycle()
        starting = [s for s in sm.states if s.name == "starting"]
        assert len(starting) == 1
        assert starting[0].is_terminal is False

    def test_transitions_count(self) -> None:
        sm = extract_agent_lifecycle()
        # AGENT_TRANSITIONS has 6 entries
        assert len(sm.transitions) == 6


# ---------------------------------------------------------------------------
# Mermaid renderer tests
# ---------------------------------------------------------------------------


class TestRenderMermaid:
    def test_starts_with_header(self) -> None:
        sm = StateMachine(
            name="Test",
            states=(State("a", "State A", False),),
            transitions=(),
        )
        output = render_mermaid(sm)
        assert output.startswith("stateDiagram-v2")

    def test_contains_transition_arrow(self) -> None:
        sm = StateMachine(
            name="Test",
            states=(
                State("a", "State A", False),
                State("b", "State B", False),
            ),
            transitions=(Transition("a", "b", "go", "a->b"),),
        )
        output = render_mermaid(sm)
        assert "a --> b : go" in output

    def test_terminal_class_applied(self) -> None:
        sm = StateMachine(
            name="Test",
            states=(State("end", "The end", True),),
            transitions=(),
        )
        output = render_mermaid(sm)
        assert "classDef terminal" in output
        assert "class end terminal" in output

    def test_task_lifecycle_renders(self) -> None:
        sm = extract_task_lifecycle()
        output = render_mermaid(sm)
        assert "stateDiagram-v2" in output
        assert "open" in output
        assert "-->" in output

    def test_agent_lifecycle_renders(self) -> None:
        sm = extract_agent_lifecycle()
        output = render_mermaid(sm)
        assert "stateDiagram-v2" in output
        assert "dead" in output


# ---------------------------------------------------------------------------
# ASCII renderer tests
# ---------------------------------------------------------------------------


class TestRenderAscii:
    def test_header_present(self) -> None:
        sm = StateMachine(name="My SM", states=(), transitions=())
        output = render_ascii(sm)
        assert "=== My SM ===" in output

    def test_states_listed(self) -> None:
        sm = StateMachine(
            name="Test",
            states=(State("x", "State X", False),),
            transitions=(),
        )
        output = render_ascii(sm)
        assert "[x]" in output
        assert "State X" in output

    def test_terminal_marker(self) -> None:
        sm = StateMachine(
            name="Test",
            states=(State("end", "Final", True),),
            transitions=(),
        )
        output = render_ascii(sm)
        assert "[TERMINAL]" in output

    def test_transition_arrow(self) -> None:
        sm = StateMachine(
            name="Test",
            states=(
                State("a", "A", False),
                State("b", "B", False),
            ),
            transitions=(Transition("a", "b", "go", "a->b"),),
        )
        output = render_ascii(sm)
        assert "a --(go)--> b" in output


# ---------------------------------------------------------------------------
# File generation tests
# ---------------------------------------------------------------------------


class TestGenerateAllDiagrams:
    def test_creates_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = generate_all_diagrams(tmpdir)
            assert len(paths) == 2
            for p in paths:
                assert p.exists()
                assert p.suffix == ".md"

    def test_filenames(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = generate_all_diagrams(tmpdir)
            names = {p.name for p in paths}
            assert "task_lifecycle.md" in names
            assert "agent_lifecycle.md" in names

    def test_content_has_mermaid(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = generate_all_diagrams(tmpdir)
            for p in paths:
                content = p.read_text(encoding="utf-8")
                assert "```mermaid" in content
                assert "stateDiagram-v2" in content

    def test_creates_output_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            nested = Path(tmpdir) / "a" / "b" / "c"
            paths = generate_all_diagrams(nested)
            assert nested.is_dir()
            assert len(paths) == 2

    def test_tables_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = generate_all_diagrams(tmpdir)
            for p in paths:
                content = p.read_text(encoding="utf-8")
                assert "| State |" in content
                assert "| From |" in content
