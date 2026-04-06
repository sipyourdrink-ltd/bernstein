"""Tests for the FastTracker II style task monitoring widget."""

from __future__ import annotations

from bernstein.tui.tracker_view import (
    NOTE_NAMES,
    TrackerRow,
    TrackerView,
    format_effect,
    render_vu,
    task_id_to_note,
)

# --- Note mapping ---


def test_note_mapping_deterministic() -> None:
    """Same task_id always produces the same note."""
    note_a = task_id_to_note("task-abc-123")
    note_b = task_id_to_note("task-abc-123")
    assert note_a == note_b
    assert len(note_a) == 3


def test_note_mapping_all_notes_reachable() -> None:
    """Different IDs should cover all 12 note names."""
    seen: set[str] = set()
    # Try a wide range of IDs to hit all 12 notes.
    for i in range(500):
        note = task_id_to_note(f"task-{i}")
        # Extract note name (everything before the octave digit).
        name = note[:-1]  # e.g. "C-" or "C#"
        # Normalize: strip trailing dash for single-char names.
        name = name.rstrip("-")
        seen.add(name)
    assert seen == set(NOTE_NAMES)


def test_note_format_single_char() -> None:
    """Single-character note names get a dash separator."""
    # Find a task_id that hashes to a single-char note name.
    for i in range(500):
        note = task_id_to_note(f"id-{i}")
        name_part = note[:-1]
        if "-" in name_part:
            # This is a single-char name like "C-4".
            assert len(note) == 3
            assert note[1] == "-"
            assert note[2].isdigit()
            return
    # Should always find at least one in 500 tries.
    raise AssertionError("No single-char note name found")  # pragma: no cover


def test_note_format_sharp() -> None:
    """Sharp note names have no dash, e.g. 'C#4'."""
    for i in range(500):
        note = task_id_to_note(f"id-{i}")
        if "#" in note:
            assert len(note) == 3
            assert note[1] == "#"
            assert note[2].isdigit()
            return
    raise AssertionError("No sharp note found")  # pragma: no cover


# --- Effect formatting ---


def test_effect_format_files() -> None:
    """8 files changed -> 'F08'."""
    assert format_effect(files_changed=8) == "F08"


def test_effect_format_files_large() -> None:
    """255 files changed -> 'FFF'."""
    assert format_effect(files_changed=255) == "FFF"


def test_effect_format_progress() -> None:
    """64% progress -> 'VA3' (64% scaled to 0-255 range, then hex)."""
    result = format_effect(progress_pct=64.0)
    # 64% -> int(64 * 255 / 100) = 163 -> 0xA3
    assert result == "VA3"


def test_effect_format_tests() -> None:
    """12 tests passing -> 'T0C'."""
    assert format_effect(tests_passing=12) == "T0C"


def test_effect_format_none() -> None:
    """No data -> middle-dot triplet."""
    assert format_effect() == "\u00b7\u00b7\u00b7"


def test_effect_priority_files_first() -> None:
    """Files changed takes priority over progress and tests."""
    result = format_effect(files_changed=3, progress_pct=50.0, tests_passing=10)
    assert result == "F03"


def test_effect_priority_progress_over_tests() -> None:
    """Progress takes priority over tests when no files."""
    result = format_effect(progress_pct=50.0, tests_passing=10)
    assert result.startswith("V")


# --- VU level rendering ---


def test_vu_level_rendering_zero() -> None:
    """0.0 activity -> spaces."""
    assert render_vu(0.0) == "    "


def test_vu_level_rendering_half() -> None:
    """0.5 activity -> 2 filled chars."""
    vu = render_vu(0.5)
    assert len(vu) == 4
    # First 2 chars should be block characters, rest spaces.
    filled_chars = vu.rstrip()
    assert len(filled_chars) == 2


def test_vu_level_rendering_full() -> None:
    """1.0 activity -> 4 filled block chars."""
    vu = render_vu(1.0)
    assert len(vu) == 4
    assert vu == "\u2591\u2592\u2593\u2588"


def test_vu_level_rendering_ascii() -> None:
    """ASCII mode uses -=#X characters."""
    vu = render_vu(1.0, ascii_mode=True)
    assert len(vu) == 4
    assert vu == "-=#X"


def test_vu_level_clamped() -> None:
    """Values outside [0,1] are clamped."""
    assert render_vu(-1.0) == "    "
    vu_over = render_vu(2.0)
    assert len(vu_over.rstrip()) == 4


# --- TrackerRow dataclass ---


def test_tracker_row_frozen() -> None:
    """TrackerRow is immutable."""
    row = TrackerRow(row_num=0, note="C-4", effect="F08", vu_level=0.5)
    try:
        row.row_num = 1  # type: ignore[misc]
        raised = False
    except AttributeError:
        raised = True
    assert raised


# --- TrackerView widget ---


def test_tracker_render_empty() -> None:
    """No agents -> 'No agents active'."""
    view = TrackerView()
    rendered = view.render()
    assert "No agents active" in rendered.plain


def test_tracker_render_with_agents() -> None:
    """With agents, output contains channel headers and data rows."""
    view = TrackerView()
    view.update_agents([
        {
            "session_id": "agent-1",
            "role": "backend",
            "current_task_id": "task-42",
            "files_changed": 5,
            "progress_pct": 0.0,
            "tests_passing": 0,
            "tokens_per_sec": 50.0,
        },
        {
            "session_id": "agent-2",
            "role": "frontend",
            "current_task_id": None,
            "files_changed": 0,
            "progress_pct": 0.0,
            "tests_passing": 0,
            "tokens_per_sec": 0.0,
        },
    ])
    rendered = view.render()
    plain = rendered.plain
    assert "CH1" in plain
    assert "CH2" in plain
    assert "backend" in plain
    assert "frontend" in plain
    # Should have data rows with row numbers.
    assert "00" in plain


def test_tracker_max_channels() -> None:
    """More than 6 agents only shows 6 channels."""
    view = TrackerView()
    agents = [
        {
            "session_id": f"agent-{i}",
            "role": f"role-{i}",
            "current_task_id": f"task-{i}",
            "files_changed": i,
            "progress_pct": 0.0,
            "tests_passing": 0,
            "tokens_per_sec": 10.0,
        }
        for i in range(10)
    ]
    view.update_agents(agents)
    assert len(view._channels) == 6


def test_tracker_row_counter_wraps() -> None:
    """Row counter wraps at 0xFF to 0x00."""
    view = TrackerView()
    view._row_counter = 0xFF
    agent_data = [
        {
            "session_id": "agent-1",
            "role": "backend",
            "current_task_id": "task-1",
            "files_changed": 1,
            "progress_pct": 0.0,
            "tests_passing": 0,
            "tokens_per_sec": 10.0,
        },
    ]
    # This update uses row_counter=0xFF, then increments.
    view.update_agents(agent_data)
    # After update, counter should have wrapped to 0x00.
    assert view._row_counter == 0x00
    # The row that was just added should have row_num=0xFF.
    assert view._channels[0].rows[-1].row_num == 0xFF


def test_current_row_highlighted() -> None:
    """Most recent row has bright style (current row marker)."""
    view = TrackerView()
    view.update_agents([
        {
            "session_id": "agent-1",
            "role": "backend",
            "current_task_id": "task-1",
            "files_changed": 3,
            "progress_pct": 0.0,
            "tests_passing": 0,
            "tokens_per_sec": 50.0,
        },
    ])
    rendered = view.render()
    plain = rendered.plain
    # The current row marker should be present.
    assert "\u25ba" in plain


def test_channel_dead_agent_shows_idle() -> None:
    """A channel for a dead agent shows '---' on subsequent rows."""
    view = TrackerView()
    # First update with agent active.
    view.update_agents([
        {
            "session_id": "agent-1",
            "role": "backend",
            "current_task_id": "task-1",
            "files_changed": 1,
            "progress_pct": 0.0,
            "tests_passing": 0,
            "tokens_per_sec": 10.0,
        },
    ])
    # Second update without the agent.
    view.update_agents([])
    # Channel should still exist with an idle row.
    assert len(view._channels) == 1
    last_row = view._channels[0].rows[-1]
    assert last_row.note == "---"
    assert last_row.effect == "\u00b7\u00b7\u00b7"


def test_channel_reuse_on_same_session_id() -> None:
    """Same session_id reuses the existing channel."""
    view = TrackerView()
    view.update_agents([
        {
            "session_id": "agent-1",
            "role": "backend",
            "current_task_id": "task-1",
            "files_changed": 1,
            "progress_pct": 0.0,
            "tests_passing": 0,
            "tokens_per_sec": 10.0,
        },
    ])
    view.update_agents([
        {
            "session_id": "agent-1",
            "role": "backend",
            "current_task_id": "task-2",
            "files_changed": 3,
            "progress_pct": 0.0,
            "tests_passing": 0,
            "tokens_per_sec": 20.0,
        },
    ])
    # Should still be one channel with 2 rows.
    assert len(view._channels) == 1
    assert len(view._channels[0].rows) == 2
