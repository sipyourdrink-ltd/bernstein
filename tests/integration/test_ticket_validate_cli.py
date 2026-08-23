"""Integration tests for the ``bernstein ticket validate`` Click command."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.ticket_cmd import ticket_group

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "sdd_tickets"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _contains_wrapped(output: str, needle: str) -> bool:
    return needle in output or needle in output.replace("\n", "")


def test_cli_exits_zero_for_valid_minimal_fixture(runner: CliRunner) -> None:
    result = runner.invoke(
        ticket_group,
        ["validate", str(FIXTURES / "valid" / "minimal.md")],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "[OK]" in result.output or "[WARN]" in result.output
    assert _contains_wrapped(result.output, "minimal.md")


def test_cli_exits_one_for_invalid_fixture(runner: CliRunner) -> None:
    result = runner.invoke(
        ticket_group,
        ["validate", str(FIXTURES / "invalid" / "bad_status.md")],
        catch_exceptions=False,
    )
    assert result.exit_code == 1
    assert _contains_wrapped(result.output, "bad_status.md")


def test_cli_glob_expands_multiple_files(runner: CliRunner, tmp_path: Path) -> None:
    # Compose a small mixed directory and pass a glob.
    for name in ("minimal.md", "rich.md"):
        (tmp_path / name).write_text((FIXTURES / "valid" / name).read_text())
    result = runner.invoke(
        ticket_group,
        ["validate", str(tmp_path / "*.md")],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert _contains_wrapped(result.output, "minimal.md")
    assert _contains_wrapped(result.output, "rich.md")


def test_cli_glob_mixed_pass_fail_exits_one(runner: CliRunner, tmp_path: Path) -> None:
    (tmp_path / "ok.md").write_text((FIXTURES / "valid" / "minimal.md").read_text())
    (tmp_path / "fail.md").write_text((FIXTURES / "invalid" / "bad_status.md").read_text())
    result = runner.invoke(
        ticket_group,
        ["validate", str(tmp_path / "*.md")],
        catch_exceptions=False,
    )
    assert result.exit_code == 1
    assert _contains_wrapped(result.output, "ok.md")
    assert _contains_wrapped(result.output, "fail.md")


def test_cli_strict_promotes_warnings_to_errors(runner: CliRunner) -> None:
    # Minimal fixture passes by default but fails under --strict because
    # recommended keys are missing.
    result = runner.invoke(
        ticket_group,
        ["validate", "--strict", str(FIXTURES / "valid" / "minimal.md")],
        catch_exceptions=False,
    )
    assert result.exit_code == 1


def test_cli_strict_on_rich_fixture_still_passes(runner: CliRunner) -> None:
    result = runner.invoke(
        ticket_group,
        ["validate", "--strict", str(FIXTURES / "valid" / "rich.md")],
        catch_exceptions=False,
    )
    assert result.exit_code == 0


def test_cli_format_json_emits_machine_readable_payload(runner: CliRunner) -> None:
    result = runner.invoke(
        ticket_group,
        [
            "validate",
            "--format",
            "json",
            str(FIXTURES / "valid" / "minimal.md"),
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema"] == "v1"
    assert payload["summary"]["total"] == 1
    assert payload["reports"][0]["status"] in {"ok", "warn"}


def test_cli_format_json_for_failing_file_exits_one(runner: CliRunner) -> None:
    result = runner.invoke(
        ticket_group,
        [
            "validate",
            "--format",
            "json",
            str(FIXTURES / "invalid" / "bad_status.md"),
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["reports"][0]["status"] == "fail"
    assert payload["summary"]["fail"] == 1


def test_cli_schema_not_found_exits_two(runner: CliRunner) -> None:
    result = runner.invoke(
        ticket_group,
        ["validate", "--schema", "v777", str(FIXTURES / "valid" / "minimal.md")],
        catch_exceptions=False,
    )
    assert result.exit_code == 2


def test_cli_no_args_errors_out(runner: CliRunner) -> None:
    result = runner.invoke(ticket_group, ["validate"], catch_exceptions=False)
    # Click reports missing argument with exit code 2.
    assert result.exit_code != 0


def test_cli_missing_path_reports_as_failure(runner: CliRunner, tmp_path: Path) -> None:
    nope = tmp_path / "does-not-exist.md"
    result = runner.invoke(
        ticket_group,
        ["validate", str(nope)],
        catch_exceptions=False,
    )
    assert result.exit_code == 1
    # The report renders through Rich, which hard-wraps at the terminal width
    # and can split the filename mid-token
    # (``does-not-ex\nist.md``). Whether the CLI names the offending file is
    # the behaviour under test; where the renderer broke the line is not, and
    # asserting on the wrapped form makes the test fail on nothing but a long
    # tmpdir path or a narrow terminal.
    unwrapped = result.output.replace("\n", "")
    assert "does-not-exist" in unwrapped
