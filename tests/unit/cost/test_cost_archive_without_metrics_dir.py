"""``bernstein cost`` on a project whose metrics were cleaned but whose archive survived
(issue #3923).

``.sdd/archive/tasks.jsonl`` is a *sibling* of ``.sdd/metrics``, not a child of it, so
the two rot on different schedules: ``.sdd/metrics`` is cleaned by hand, by a container
rebuild, or by anything that treats it as a cache, and the archive is the part meant to
outlive that. The early "nothing here" return added for #3917 sat in front of the archive
read, so surviving the cleanup was exactly what hid the data -- reported as
``No metrics data found.`` at exit 0, which is a confident answer about a source that was
never consulted.

The property under test is that the *two spellings of the same project* agree: whether or
not an empty ``.sdd/metrics`` happens to exist, a project with archived tasks reports
them. An empty directory is not information and must not change an answer.

The neighbours pinned by ``test_cost_missing_metrics_dir.py`` are unchanged and are
re-asserted here from the archive's side, because this fix moves the code that produces
them: a project with no data anywhere still reports "no data" at exit 0, and an
explicitly named directory that does not exist is still an error.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.commands.cost import cost_cmd

ARCHIVED_TASK = {
    "task_id": "archived-1",
    "role": "backend",
    "model": "claude-haiku-4",
    "tokens_prompt": 400,
    "tokens_completion": 200,
    "cost_usd": 0.02,
    "duration_seconds": 15.0,
    "agent_id": "agent-archived",
}


def _project(root: Path, name: str, *, metrics: bool, archived: bool) -> Path:
    """A project whose ``.sdd/metrics`` and ``.sdd/archive`` are independently present."""
    proj = root / name
    sdd = proj / ".sdd"
    sdd.mkdir(parents=True)
    if metrics:
        (sdd / "metrics").mkdir()
    if archived:
        (sdd / "archive").mkdir()
        record = dict(ARCHIVED_TASK, timestamp=time.time())
        (sdd / "archive" / "tasks.jsonl").write_text(json.dumps(record) + "\n")
    return proj


def _run(proj: Path, *args: str):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=proj.parent):
        # ``cost``'s default --metrics-dir is relative, so the project is the cwd.
        os.chdir(proj)
        return runner.invoke(cost_cmd, list(args))


def test_archived_tasks_are_reported_when_the_metrics_dir_is_absent(tmp_path: Path) -> None:
    """The bug itself: data in the archive, no ``.sdd/metrics``, reported as no data."""
    result = _run(_project(tmp_path, "cleaned", metrics=False, archived=True))

    assert result.exit_code == 0, f"rc={result.exit_code!r} output={result.output!r}"
    assert "No metrics data found" not in result.output, (
        "the archive holds a task and the report says there is nothing here"
    )
    assert ARCHIVED_TASK["model"] in result.output


def test_archived_tasks_are_reported_in_json_mode_too(tmp_path: Path) -> None:
    result = _run(_project(tmp_path, "cleaned-json", metrics=False, archived=True), "--json")

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["rows"], f"--json reported no rows for an archived task: {payload!r}"
    assert any(row.get("model") == ARCHIVED_TASK["model"] for row in payload["rows"])


def test_an_empty_metrics_dir_does_not_change_the_answer(tmp_path: Path) -> None:
    """The acceptance criterion: same project, two spellings, identical output.

    This is the test that fails if the archive is ever read from only one of the two
    paths again. An empty directory carries no information and must not be the
    difference between reporting a task and denying it exists.
    """
    without = _run(_project(tmp_path, "no-metrics-dir", metrics=False, archived=True))
    with_empty = _run(_project(tmp_path, "empty-metrics-dir", metrics=True, archived=True))

    assert without.exit_code == with_empty.exit_code == 0
    assert without.output == with_empty.output


def test_the_two_spellings_also_agree_in_json_mode(tmp_path: Path) -> None:
    without = _run(_project(tmp_path, "no-dir-json", metrics=False, archived=True), "--json")
    with_empty = _run(_project(tmp_path, "empty-dir-json", metrics=True, archived=True), "--json")

    assert without.exit_code == with_empty.exit_code == 0
    assert json.loads(without.output) == json.loads(with_empty.output)


def test_a_project_with_no_data_anywhere_still_reports_no_data(tmp_path: Path) -> None:
    """#3917's outcome, re-asserted from the archive's side: still quiet, still exit 0."""
    result = _run(_project(tmp_path, "never-run", metrics=False, archived=False))

    assert result.exit_code == 0, f"rc={result.exit_code!r} output={result.output!r}"
    assert "No metrics data found" in result.output


def test_an_explicitly_named_missing_dir_still_errors_even_with_an_archive(
    tmp_path: Path,
) -> None:
    """A typo is still a typo. Falling through must not swallow the named-path signal.

    The archive is present here on purpose: if the fall-through were placed before the
    ``named`` check rather than after it, this project would find the archived task and
    print a confident report about a directory the caller misspelled.
    """
    proj = _project(tmp_path, "typo", metrics=True, archived=True)
    result = _run(proj, "--metrics-dir", str(proj / ".sdd" / "no-such-metrics-dir"))

    assert result.exit_code == 1
    assert "Metrics directory not found" in result.output
