"""Tests for ``print_dry_run_table`` ticket discovery (issue #952)."""

from __future__ import annotations

import io
from pathlib import Path

from rich.console import Console

from bernstein.cli import helpers


def _setup_backlog(workdir: Path) -> None:
    """Create a backlog with one ``.md``-frontmatter ticket and one ``.yaml`` ticket."""
    open_dir = workdir / ".sdd" / "backlog" / "open"
    open_dir.mkdir(parents=True)

    (open_dir / "001-md-ticket.md").write_text(
        """---
title: "Add hello function"
role: backend
priority: 2
scope: small
complexity: low
---
# Add hello function

Body of the markdown ticket.
""",
        encoding="utf-8",
    )

    (open_dir / "002-yaml-ticket.yaml").write_text(
        """---
title: "Improve CLI output"
role: frontend
priority: 3
scope: medium
complexity: medium
---
# Improve CLI output

A YAML-frontmatter ticket.
""",
        encoding="utf-8",
    )


def test_print_dry_run_table_includes_md_and_yaml_tickets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Both ``.md`` and ``.yaml`` tickets should appear in the dry-run table.

    Regression test for issue #952: the previous implementation only globbed
    ``*.yaml`` from ``.sdd/backlog/open/``, hiding any ``.md``-frontmatter
    ticket from the dry-run preview even though the orchestrator would
    happily ingest it.
    """
    _setup_backlog(tmp_path)

    buf = io.StringIO()
    fake_console = Console(file=buf, width=200, force_terminal=False, record=False)
    monkeypatch.setattr(helpers, "console", fake_console)

    helpers.print_dry_run_table(tmp_path)

    output = buf.getvalue()
    assert "Add hello function" in output, output
    assert "Improve CLI output" in output, output
    assert "No open tasks found" not in output, output
    assert "Total: 2 task(s)" in output, output


def test_print_dry_run_table_includes_issues_dir_tickets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Tickets under ``.sdd/backlog/issues/`` are picked up too (orchestrator parity)."""
    issues_dir = tmp_path / ".sdd" / "backlog" / "issues"
    issues_dir.mkdir(parents=True)
    (issues_dir / "010-issue.md").write_text(
        """---
title: "Investigate flaky test"
role: qa
priority: 1
scope: small
complexity: low
---
# Investigate flaky test
""",
        encoding="utf-8",
    )

    buf = io.StringIO()
    fake_console = Console(file=buf, width=200, force_terminal=False, record=False)
    monkeypatch.setattr(helpers, "console", fake_console)

    helpers.print_dry_run_table(tmp_path)

    output = buf.getvalue()
    assert "Investigate flaky test" in output, output
    assert "No open tasks found" not in output, output


def test_print_dry_run_table_no_backlog(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """With no backlog directories, the helper reports no open tasks."""
    buf = io.StringIO()
    fake_console = Console(file=buf, width=200, force_terminal=False, record=False)
    monkeypatch.setattr(helpers, "console", fake_console)

    helpers.print_dry_run_table(tmp_path)

    assert "No open tasks found" in buf.getvalue()
