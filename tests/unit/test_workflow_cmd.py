"""CLI regressions for ``bernstein workflow run``/``list`` manifest routing.

Two manifest schemas share ``.bernstein/workflows/``: the WorkflowSpec
(list-form ``nodes:``) schema and the conditional-DAG DSL (``phases:`` plus
mapping-form ``nodes:``).  ``run``/``list`` must sniff the kind and route
each file to its own reader instead of forcing every file through the
WorkflowSpec loader (#4461).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.workflow_cmd import _one_line_error
from bernstein.cli.main import cli
from bernstein.core.workflows.workflow_spec import WorkflowSpecError, load_workflow_spec_from_text

_DSL_MANIFEST = """\
name: two-phase
version: "1.0.0"
phases:
  - name: build
  - name: check
nodes:
  make:
    phase: build
    role: backend
    description: "Build the thing"
  test:
    phase: check
    role: qa
    description: "Check the thing"
    depends_on:
      - make
"""

# Valid YAML, but neither a WorkflowSpec (no ``nodes`` list) nor a DSL
# (no ``phases``) - the "genuinely broken" case `_detect_kind` calls "unknown".
_BROKEN_MANIFEST = """\
name: nothing-useful
"""


def _write(tmp_path: Path, name: str, text: str) -> Path:
    workflows_dir = tmp_path / ".bernstein" / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    path = workflows_dir / f"{name}.yaml"
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# `workflow run` on DSL-form manifests
# ---------------------------------------------------------------------------


def test_run_dry_run_on_dsl_manifest_prints_plan_not_pydantic_dump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DSL-form manifest dry-runs cleanly instead of hitting the WorkflowSpec wall."""
    _write(tmp_path, "two-phase", _DSL_MANIFEST)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["workflow", "run", "two-phase", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "validation error" not in result.output.lower()
    assert "Field required" not in result.output
    assert "make" in result.output
    assert "test" in result.output


def test_run_without_dry_run_on_dsl_manifest_names_the_working_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real `run` of a DSL manifest fails with one clean line, not a pydantic dump."""
    _write(tmp_path, "two-phase", _DSL_MANIFEST)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["workflow", "run", "two-phase"])

    assert result.exit_code != 0
    assert "validation error" not in result.output.lower()
    assert "Field required" not in result.output
    assert "--dry-run" in result.output


def test_run_still_resolves_spec_form_manifests_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pre-existing WorkflowSpec `run` path is untouched by kind routing."""
    _write(
        tmp_path,
        "spec-only",
        """\
name: spec-only
description: "One command node"
version: "1.0.0"
nodes:
  - id: only
    command: "true"
""",
    )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["workflow", "run", "spec-only", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Layer 1" in result.output
    assert "only" in result.output


def test_run_missing_workflow_reports_not_found_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A name matching nothing on disk keeps the existing not-found message."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "no-home"))

    result = CliRunner().invoke(cli, ["workflow", "run", "does-not-exist-anywhere"])

    assert result.exit_code == 1
    assert "not found" in result.output


# ---------------------------------------------------------------------------
# `workflow list`
# ---------------------------------------------------------------------------


def test_list_renders_dsl_manifest_exactly_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A DSL-form manifest gets exactly one DSL row, never a duplicate error row."""
    _write(tmp_path, "two-phase", _DSL_MANIFEST)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["workflow", "list", "--dir", str(tmp_path / ".bernstein" / "workflows")])

    assert result.exit_code == 0, result.output
    assert "validation error" not in result.output.lower()
    assert result.output.count("two-phase") == 1
    assert "DSL" in result.output


def test_list_broken_manifest_is_not_a_stack_trace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A manifest that fails both parsers still renders (exit 0, one error row)."""
    _write(tmp_path, "broken", _BROKEN_MANIFEST)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["workflow", "list", "--dir", str(tmp_path / ".bernstein" / "workflows")])

    assert result.exit_code == 0, result.output
    assert "broken" in result.output
    assert "error" in result.output.lower()


# ---------------------------------------------------------------------------
# `_one_line_error` helper (unit-level, precise about the actual bug)
# ---------------------------------------------------------------------------


def test_one_line_error_collapses_multiline_pydantic_dump() -> None:
    """The rendered error string never contains a raw newline.

    A raw multi-line ``ValidationError`` str, handed to a Rich table cell,
    is what produced the one-word-per-line wrapping the issue reports.
    """
    with pytest.raises(WorkflowSpecError) as exc_info:
        load_workflow_spec_from_text("description: only\n")

    rendered = _one_line_error(exc_info.value)

    assert "\n" not in rendered
    assert rendered  # still says *something* useful, just on one line


def test_one_line_error_truncates_very_long_messages() -> None:
    """A pathological error message is capped so the table stays scannable."""
    err = WorkflowSpecError("x" * 500)
    rendered = _one_line_error(err)
    assert len(rendered) <= 160
