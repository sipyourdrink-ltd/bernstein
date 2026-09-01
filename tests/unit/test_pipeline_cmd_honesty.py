"""``bernstein pipeline`` must not claim work it did not do.

Honesty properties:
- ``--dry-run`` prints the resolved configuration and performs zero tracker dispatch.
- A plain sweep over configuration with zero trackers reports zero trackers and zero handoffs.
- A sweep that contacted zero trackers is distinguishable from a dry-run or a command that never ran.
- Configuration typos and YAML syntax errors produce clean error messages rather than tracebacks.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.commands.pipeline_cmd import pipeline_group

_VALID = """
orchestration:
  tracker_pipeline:
    pipeline_stages:
      - role: engineer
        claim_status: claimed
        success_status: done
        failure_status: failed
"""

_MISSING_KEY = """
orchestration:
  tracker_pipeline:
    pipeline_stages:
      - role: engineer
"""

# A bad indent, which is the commoner of the two operator mistakes.
_BAD_YAML = """
orchestration:
  tracker_pipeline:
    pipeline_stages:
      - role: engineer
       claim_status: claimed
"""


def _run(tmp_path: Path, config: str, *args: str) -> tuple[int, str]:
    (tmp_path / "bernstein.yaml").write_text(config, encoding="utf-8")
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as _cwd:
        (Path(_cwd) / "bernstein.yaml").write_text(config, encoding="utf-8")
        result = runner.invoke(pipeline_group, ["run", *args])
    return result.exit_code, result.output


def test_dry_run_prints_resolved_config_and_does_not_sweep(tmp_path: Path) -> None:
    """--dry-run resolves and validates config without sweeping."""
    code, output = _run(tmp_path, _VALID, "--dry-run")
    assert code == 0
    assert "Tracker pipeline (resolved)" in output
    assert "engineer" in output
    assert "claimed" in output
    assert "Sweep complete" not in output


def test_plain_run_sweeps_and_reports_honest_summary(tmp_path: Path) -> None:
    """A plain run performs a sweep and reports what actually happened."""
    code, output = _run(tmp_path, _VALID)
    assert code == 0
    assert "Sweep complete: 0 trackers configured; 0 handoffs." in output
    assert "No dispatch" not in output


def test_dry_run_and_a_plain_run_are_distinguishable(tmp_path: Path) -> None:
    """Dry-run and plain run produce distinct outputs because only plain run sweeps."""
    _, plain = _run(tmp_path, _VALID)
    _, dry = _run(tmp_path, _VALID, "--dry-run")
    assert plain != dry
    assert "Tracker pipeline (resolved)" in dry
    assert "Sweep complete" in plain


def test_a_config_typo_is_an_error_message_not_a_traceback(tmp_path: Path) -> None:
    """The parser's own sentence reaches the operator."""
    code, output = _run(tmp_path, _MISSING_KEY)
    assert code != 0
    assert "missing required key" in output
    assert "claim_status" in output
    assert "Traceback" not in output


def test_a_yaml_syntax_error_is_an_error_message_not_a_traceback(tmp_path: Path) -> None:
    """The parser error is the operator's typo, not a crash in bernstein."""
    code, output = _run(tmp_path, _BAD_YAML)
    assert code != 0
    assert "Error:" in output
    assert "Traceback" not in output
