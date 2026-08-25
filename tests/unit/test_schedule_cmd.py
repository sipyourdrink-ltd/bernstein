from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.commands.schedule_cmd import schedule_group


def test_schedule_group_has_routine_subcommand():
    # The schedule command group should have a subcommand named 'routine'
    assert "routine" in schedule_group.commands


def test_schedule_add_valid_cron(tmp_path: Path):
    # Use a temporary .sdd directory
    sdd_dir = tmp_path / ".sdd"
    sdd_dir.mkdir()
    # Change cwd to tmp_path so _sdd_dir resolves correctly
    cwd = Path.cwd()
    try:
        # Change working directory
        import os

        os.chdir(tmp_path)
        runner = CliRunner()
        # Invoke schedule add with a simple cron
        result = runner.invoke(schedule_group, ["add", "--cron", "* * * * *", "--goal", "test goal"])
        # Expect success (exit code 0) and output contains 'Registered schedule'
        assert result.exit_code == 0, f"Exit code {result.exit_code}, output: {result.output}"
        assert "Registered schedule" in result.output
    finally:
        os.chdir(cwd)


def test_schedule_add_invalid_cron(tmp_path: Path):
    sdd_dir = tmp_path / ".sdd"
    sdd_dir.mkdir()
    cwd = Path.cwd()
    try:
        import os

        os.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(schedule_group, ["add", "--cron", "invalid", "--goal", "test"])
        # Should exit with error code 2 (click usage error)
        assert result.exit_code != 0, "Expected non-zero exit for invalid cron"
        assert "invalid cron expression" in result.output.lower()
    finally:
        os.chdir(cwd)
