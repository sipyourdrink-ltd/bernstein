"""Behavioral tests for the backlog DAG checkbox parser and priority scaling.

The existing ``test_backlog_parser`` suite covers YAML frontmatter and the
markdown ``**Field:**`` form. This file targets the previously-untested
surfaces: the ``- [ ] [Txxx] [P] [USn] desc -> deps`` task-line grammar,
priority-scale normalisation (0-4 ticket scale -> 1-3 internal scale), and
scope/complexity clamping.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.tasks.backlog_parser import (
    ParsedBacklogTask,
    _parse_complexity,
    _parse_priority,
    _parse_scope,
    parse_backlog_path,
    parse_backlog_text,
    parse_task_line,
    parse_task_lines,
)

# ---------------------------------------------------------------------------
# parse_task_line - the DAG checkbox grammar
# ---------------------------------------------------------------------------


def test_parse_task_line_full_grammar() -> None:
    parsed = parse_task_line("- [ ] [T001] [P] [US1] Add YAML loader -> T000")
    assert parsed is not None
    assert parsed.task_id == "T001"
    assert parsed.parallel_safe is True
    assert parsed.story_id == "US1"
    assert parsed.description == "Add YAML loader"
    assert parsed.depends_on == ("T000",)


def test_parse_task_line_minimal_id_only() -> None:
    parsed = parse_task_line("- [ ] [T002] Just a task")
    assert parsed is not None
    assert parsed.task_id == "T002"
    assert parsed.parallel_safe is False
    assert parsed.story_id is None
    assert parsed.depends_on == ()
    assert parsed.description == "Just a task"


def test_parse_task_line_checked_box_still_parses() -> None:
    parsed = parse_task_line("- [x] [T003] Completed item")
    assert parsed is not None
    assert parsed.task_id == "T003"
    assert parsed.description == "Completed item"


def test_parse_task_line_star_bullet_accepted() -> None:
    parsed = parse_task_line("* [ ] [T004] Star bullet")
    assert parsed is not None
    assert parsed.task_id == "T004"


def test_parse_task_line_multiple_dependencies() -> None:
    parsed = parse_task_line("- [ ] [T005] Build it -> T001, T002, T003")
    assert parsed is not None
    assert parsed.depends_on == ("T001", "T002", "T003")
    assert parsed.description == "Build it"


def test_parse_task_line_unicode_arrow_dependency() -> None:
    parsed = parse_task_line("- [ ] [T006] Wire it → T001")
    assert parsed is not None
    assert parsed.depends_on == ("T001",)
    assert parsed.description == "Wire it"


def test_parse_task_line_story_id_is_uppercased() -> None:
    parsed = parse_task_line("- [ ] [T007] [us2] lowercase story marker")
    assert parsed is not None
    assert parsed.story_id == "US2"


def test_parse_task_line_markers_in_any_order() -> None:
    parsed = parse_task_line("- [ ] [US3] [P] [T008] reordered markers")
    assert parsed is not None
    assert parsed.task_id == "T008"
    assert parsed.parallel_safe is True
    assert parsed.story_id == "US3"


def test_parse_task_line_prose_returns_none() -> None:
    assert parse_task_line("This is just a paragraph of prose.") is None


def test_parse_task_line_checkbox_without_task_id_returns_none() -> None:
    # A checkbox line that lacks a [Txxx] marker is not a DAG task row.
    assert parse_task_line("- [ ] just some description, no id") is None


def test_parse_task_line_blank_returns_none() -> None:
    assert parse_task_line("    ") is None


def test_parse_task_line_unknown_marker_stops_consumption() -> None:
    # An unrecognised bracket token (e.g. [WIP]) stops marker parsing; the
    # token then becomes part of the description text.
    parsed = parse_task_line("- [ ] [T009] [WIP] still going")
    assert parsed is not None
    assert parsed.task_id == "T009"
    assert parsed.description == "[WIP] still going"


# ---------------------------------------------------------------------------
# parse_task_lines - whole-document parsing
# ---------------------------------------------------------------------------


def test_parse_task_lines_skips_non_task_rows() -> None:
    doc = """# My Plan

Some intro prose.

- [ ] [T001] First task
- [ ] [T002] Second task -> T001
- not a checkbox
- [ ] no id here
"""
    rows = parse_task_lines(doc)
    assert [r.task_id for r in rows] == ["T001", "T002"]
    assert rows[1].depends_on == ("T001",)


def test_parse_task_lines_empty_document_returns_empty_list() -> None:
    assert parse_task_lines("") == []


@pytest.mark.parametrize(
    ("opening", "inside", "closing"),
    [
        ("```markdown", "~~~", "```"),
        ("~~~markdown", "~~~still code", "~~~"),
        ("  ````markdown", "```", "   `````  "),
        ("~~~~markdown", "~~~", "~~~~"),
    ],
)
def test_parse_task_lines_skips_fenced_examples(opening: str, inside: str, closing: str) -> None:
    doc = f"{opening}\n{inside}\n- [ ] [T001] Example\n{closing}\n- [ ] [T002] Ship"
    assert [row.task_id for row in parse_task_lines(doc)] == ["T002"]


# ---------------------------------------------------------------------------
# _parse_priority - 0-4 ticket scale collapses to 1-3 internal scale
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0, 1),  # p0 critical -> 1
        (1, 1),  # p1 -> 1
        (2, 2),  # p2 normal -> 2
        (3, 3),  # p3 -> 3
        (4, 3),  # p4 future -> 3
        ("p0", 1),
        ("p4", 3),
        ("priority: 2", 2),
    ],
)
def test_parse_priority_scale_collapse(raw: object, expected: int) -> None:
    assert _parse_priority(raw) == expected


def test_parse_priority_non_numeric_defaults_to_two() -> None:
    assert _parse_priority("high") == 2


# ---------------------------------------------------------------------------
# _parse_scope / _parse_complexity - clamp to known vocabulary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("small", "small"),
        ("MEDIUM", "medium"),
        ("  Large ", "large"),
        ("enormous", "medium"),  # unknown -> default
        ("", "medium"),
    ],
)
def test_parse_scope_clamps_to_known(raw: str, expected: str) -> None:
    assert _parse_scope(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("low", "low"),
        ("HIGH", "high"),
        (" Medium ", "medium"),
        ("extreme", "medium"),  # unknown -> default
        ("", "medium"),
    ],
)
def test_parse_complexity_clamps_to_known(raw: str, expected: str) -> None:
    assert _parse_complexity(raw) == expected


# ---------------------------------------------------------------------------
# parse_backlog_text / parse_backlog_path - whole-file dispatch
# ---------------------------------------------------------------------------


def test_parse_backlog_text_empty_returns_none() -> None:
    assert parse_backlog_text("f.md", "   \n\n  ") is None


def test_parse_backlog_path_missing_file_returns_none(tmp_path: Path) -> None:
    assert parse_backlog_path(tmp_path / "does-not-exist.md") is None


def test_parse_backlog_text_markdown_priority_uses_scale_collapse() -> None:
    content = "# Title\n\n**Role:** backend\n**Priority:** 0\n**Scope:** large\n"
    parsed = parse_backlog_text("t.md", content)
    assert parsed is not None
    assert parsed.priority == 1  # p0 -> 1
    assert parsed.scope == "large"
    assert parsed.role == "backend"


def test_parse_backlog_text_yaml_frontmatter_priority_collapse() -> None:
    content = "---\ntitle: Do thing\nrole: qa\npriority: 4\n---\nBody text.\n"
    parsed = parse_backlog_text("t.md", content)
    assert parsed is not None
    assert isinstance(parsed, ParsedBacklogTask)
    assert parsed.priority == 3  # p4 -> 3
    assert parsed.role == "qa"


def test_parse_backlog_path_reads_yaml_from_disk(tmp_path: Path) -> None:
    f = tmp_path / "ticket.md"
    f.write_text("---\ntitle: From disk\nrole: docs\n---\nDetailed body.\n", encoding="utf-8")
    parsed = parse_backlog_path(f)
    assert parsed is not None
    assert parsed.title == "From disk"
    assert parsed.role == "docs"
    assert parsed.source_file == "ticket.md"
