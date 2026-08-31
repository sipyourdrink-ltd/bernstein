"""``bernstein pipeline`` must not claim work it did not do.

Every surface of ``pipeline run`` promised a sweep it does not perform: the
summary said "Run a single sweep", the docstring said each invocation "walks
every configured tracker once", and ``--dry-run`` offered to print the config
"without dispatching" - while both paths print the same thing and neither
dispatches. ``build_pipeline_from_yaml``, the function that would do the work,
has no caller anywhere in the tree.

An operator following the command's own advice to schedule it via systemd or
cron got a printout every tick, no work, and nothing saying why.

Separately, a config typo escaped as a ``TrackerPipelineError`` traceback even
though the parser had already produced the exact sentence needed - "pipeline
stage missing required key: role" - so a mistake in the operator's file read as
a crash in Bernstein.
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


def test_run_says_nothing_was_dispatched(tmp_path: Path) -> None:
    """The one sentence an operator needs, and the one that was missing."""
    code, output = _run(tmp_path, _VALID)
    assert code == 0
    assert "No dispatch" in output
    assert "nothing was swept" in output


def test_run_does_not_claim_to_have_swept(tmp_path: Path) -> None:
    """A negative control: no wording that implies work happened.

    Asserting the disclaimer is present is not enough - the failure being guarded is a
    command that reads as though it did something, and that can come back through any
    reassuring phrase printed beside the table.
    """
    _, output = _run(tmp_path, _VALID)
    lowered = output.lower()
    for claim in ("swept 1", "dispatched", "sweep complete", "walked"):
        assert claim not in lowered, f"output implies work was done: {claim!r}"


def test_a_config_typo_is_an_error_message_not_a_traceback(tmp_path: Path) -> None:
    """The parser's own sentence reaches the operator."""
    code, output = _run(tmp_path, _MISSING_KEY)
    assert code != 0
    assert "missing required key" in output
    assert "claim_status" in output
    # The give-away that an exception escaped rather than being reported.
    assert "Traceback" not in output


def test_dry_run_and_a_plain_run_agree(tmp_path: Path) -> None:
    """They print the same thing, because neither dispatches.

    Pinned rather than left implicit: the flag's help now says so, and if dispatch is ever
    wired this test is where the difference has to be reintroduced deliberately.
    """
    _, plain = _run(tmp_path, _VALID)
    _, dry = _run(tmp_path, _VALID, "--dry-run")
    assert plain == dry
    assert "No dispatch" in dry


def test_a_yaml_syntax_error_is_an_error_message_not_a_traceback(tmp_path: Path) -> None:
    """The parser error is the operator's typo, not a crash in bernstein.

    Wrapping only ``TrackerPipelineError`` left this half uncovered: a missing
    key was reported cleanly while a bad indent - the mistake people actually
    make - still surfaced as a ``ParserError`` with an empty CLI output.
    """
    code, output = _run(tmp_path, _BAD_YAML)
    assert code != 0
    assert "Error:" in output
    assert "Traceback" not in output
