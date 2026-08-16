"""``bernstein cost`` on a project that has never been run (issue #3917).

The property under test is an agreement between two states that mean the same
thing to a reader: a project with no ``.sdd/metrics`` directory at all, and a
project whose ``.sdd/metrics`` exists and is empty. Both say "this project has
no cost data". Only one of them used to be an error, and a read-only report
that exits non-zero because a directory is absent turns a fleet sweep red for
every project that has simply not run yet.

The refusal is deliberately kept for a directory the caller named explicitly:
there, an absent path is far more likely to be a typo than a new project, and
losing that signal would be a worse trade than the bug it fixes.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.commands.cost import cost_cmd


def _project(root: Path, *, metrics: bool) -> Path:
    """A project directory with ``.sdd/metrics`` either absent or present-and-empty."""
    proj = root / ("empty-metrics" if metrics else "never-run")
    (proj / ".sdd").mkdir(parents=True)
    if metrics:
        (proj / ".sdd" / "metrics").mkdir()
    return proj


def _run(proj: Path, *args: str):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=proj.parent) as _:
        # ``cost``'s default --metrics-dir is relative, so the project is the cwd.
        import os

        os.chdir(proj)
        return runner.invoke(cost_cmd, list(args))


def test_a_missing_metrics_dir_reports_the_same_as_an_empty_one(tmp_path: Path) -> None:
    never_run = _run(_project(tmp_path, metrics=False))
    empty = _run(_project(tmp_path, metrics=True))

    assert never_run.exit_code == empty.exit_code == 0, (
        f"never-run rc={never_run.exit_code!r} output={never_run.output!r}; "
        f"empty rc={empty.exit_code!r} output={empty.output!r}"
    )
    assert never_run.output == empty.output


def test_the_missing_and_empty_pair_also_agree_in_json_mode(tmp_path: Path) -> None:
    never_run = _run(_project(tmp_path, metrics=False), "--json")
    empty = _run(_project(tmp_path, metrics=True), "--json")

    assert never_run.exit_code == empty.exit_code == 0
    assert json.loads(never_run.output) == json.loads(empty.output)


def test_an_explicitly_named_metrics_dir_that_does_not_exist_still_says_so(
    tmp_path: Path,
) -> None:
    """A typo must not read as "no data" — that is the signal the fix keeps."""
    proj = _project(tmp_path, metrics=True)
    typo = _run(proj, "--metrics-dir", str(proj / ".sdd" / "no-such-metrics-dir"))

    assert typo.exit_code == 1
    assert "Metrics directory not found" in typo.output


def test_an_explicit_metrics_dir_typo_is_an_error_in_json_mode_too(tmp_path: Path) -> None:
    proj = _project(tmp_path, metrics=True)
    typo = _run(proj, "--json", "--metrics-dir", str(proj / ".sdd" / "no-such-metrics-dir"))

    assert typo.exit_code == 1
    assert "Metrics directory not found" in json.loads(typo.output)["error"]


def test_passing_the_default_metrics_dir_explicitly_still_refuses_when_absent(
    tmp_path: Path,
) -> None:
    """The distinction is the parameter *source*, not the parameter value.

    Someone who types ``--metrics-dir .sdd/metrics`` has named a path, so the
    typo signal applies to them exactly as it does to any other spelling. This
    pins the mechanism: if the check ever degrades into comparing the string
    against the default, this is the test that notices.
    """
    proj = _project(tmp_path, metrics=False)
    explicit = _run(proj, "--metrics-dir", ".sdd/metrics")

    assert explicit.exit_code == 1
    assert "Metrics directory not found" in explicit.output
