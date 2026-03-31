"""Focused tests for backlog ticket parsing helpers."""

from __future__ import annotations

from pathlib import Path

from bernstein.core.backlog_parser import ParsedBacklogTask, parse_backlog_path, parse_backlog_text


def test_parse_yaml_frontmatter_extracts_extended_fields() -> None:
    """YAML frontmatter is parsed into the extended backlog task model."""
    content = """---
id: EXTERNAL-003
title: Coverage Gap Expansion
role: qa
priority: 1
scope: large
complexity: high
model: opus
effort: max
tags: ["testing", "coverage"]
depends_on: ["A", "B"]
affected_paths: ["tests/unit"]
context_files: ["src/bernstein/core/task_store.py"]
estimated_minutes: 480
require_review: true
require_human_approval: false
janitor_signals:
  - type: test_passes
    value: uv run pytest tests/unit -x -q
---

# Coverage Gap Expansion
"""

    parsed = parse_backlog_text("EXTERNAL-003.md", content)

    assert parsed == ParsedBacklogTask(
        title="Coverage Gap Expansion",
        description=content.strip(),
        role="qa",
        priority=1,
        scope="large",
        complexity="high",
        source_file="EXTERNAL-003.md",
        ticket_id="EXTERNAL-003",
        model="opus",
        effort="max",
        ticket_type="feature",
        tags=("testing", "coverage"),
        depends_on=("A", "B"),
        affected_paths=("tests/unit",),
        context_files=("src/bernstein/core/task_store.py",),
        estimated_minutes=480,
        require_review=True,
        require_human_approval=False,
        janitor_signals=({"type": "test_passes", "value": "uv run pytest tests/unit -x -q"},),
    )


def test_parse_markdown_fields_uses_heading_metadata() -> None:
    """Markdown fallback extracts title and inline metadata fields."""
    content = """# Fix backlog parser

**Role:** backend
**Priority:** 1
**Scope:** large
**Complexity:** high
"""

    parsed = parse_backlog_text("ticket.md", content)

    assert parsed is not None
    assert parsed.title == "Fix backlog parser"
    assert parsed.role == "backend"
    assert parsed.priority == 1
    assert parsed.scope == "large"
    assert parsed.complexity == "high"


def test_parse_yaml_frontmatter_falls_back_to_markdown_when_yaml_is_malformed() -> None:
    """Malformed YAML frontmatter does not crash and falls back to markdown parsing."""
    content = """---
title: [bad
role: qa
---

# Broken ticket
"""

    parsed = parse_backlog_text("broken.md", content)

    assert parsed is not None
    assert parsed.title == "Broken ticket"
    assert parsed.role == "backend"


def test_parse_yaml_frontmatter_applies_defaults_for_missing_optional_fields() -> None:
    """Missing optional YAML fields fall back to the parser defaults."""
    content = """---
title: Minimal ticket
---

# Minimal ticket
"""

    parsed = parse_backlog_text("minimal.md", content)

    assert parsed is not None
    assert parsed.role == "backend"
    assert parsed.priority == 2
    assert parsed.scope == "medium"
    assert parsed.complexity == "medium"
    assert parsed.model == "auto"
    assert parsed.effort == "normal"
    assert parsed.estimated_minutes == 45


def test_to_task_payload_omits_auto_defaults() -> None:
    """Payload conversion only includes override fields that differ from defaults."""
    parsed = ParsedBacklogTask(
        title="Focused task",
        description="desc",
        role="backend",
        priority=2,
        scope="medium",
        complexity="medium",
        source_file="task.md",
    )

    assert parsed.to_task_payload() == {
        "title": "Focused task",
        "description": "desc",
        "role": "backend",
        "priority": 2,
        "scope": "medium",
        "complexity": "medium",
    }


def test_parse_backlog_path_reads_utf8_file(tmp_path: Path) -> None:
    """Parsing from disk reads the file and delegates to the text parser."""
    path = tmp_path / "ticket.md"
    path.write_text("# Ticket from disk\n\n**Role:** qa\n", encoding="utf-8")

    parsed = parse_backlog_path(path)

    assert parsed is not None
    assert parsed.title == "Ticket from disk"
    assert parsed.role == "qa"
