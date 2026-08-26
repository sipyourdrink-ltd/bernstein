from bernstein.cli.commands.daemon_cmd import DEFAULT_COMMAND


def test_default_command_includes_schedule_run():
    """Ensure the default daemon command starts the schedule supervisor."""
    assert "schedule run" in DEFAULT_COMMAND, "DEFAULT_COMMAND should contain 'schedule run'"
