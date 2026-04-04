"""Unit tests for bernstein.core.readme_reminder.

Tests cover:
- detect_api_changes: detects new CLI commands, options, and config keys
- remind_message: formats the reminder string
- Integration with guardrails.check_readme_reminder
"""

from __future__ import annotations

from bernstein.core.readme_reminder import APIChange, detect_api_changes, remind_message

# ---------------------------------------------------------------------------
# Helpers — minimal diff builders
# ---------------------------------------------------------------------------

_CLI_FILE = "src/bernstein/cli/run_cmd.py"
_CONFIG_FILE = "src/bernstein/core/home.py"


def _make_diff(file: str, added_lines: list[str]) -> str:
    """Wrap *added_lines* in a minimal unified diff header for *file*."""
    body = "\n".join(f"+{line}" for line in added_lines)
    return f"diff --git a/{file} b/{file}\n--- a/{file}\n+++ b/{file}\n@@ -1 +1 @@\n{body}\n"


# ---------------------------------------------------------------------------
# detect_api_changes — no changes
# ---------------------------------------------------------------------------


def test_no_changes_on_empty_diff() -> None:
    assert detect_api_changes("") == []


def test_no_changes_on_non_api_diff() -> None:
    diff = _make_diff(_CLI_FILE, ["    result = do_thing()", "    return result"])
    assert detect_api_changes(diff) == []


def test_no_changes_on_removed_lines() -> None:
    """Lines starting with '-' are removals, not additions — must be ignored."""
    diff = _make_diff(_CLI_FILE, [])
    # Add a removal line manually
    diff += "-@click.command()\n"
    assert detect_api_changes(diff) == []


def test_no_changes_outside_cli_or_config_path() -> None:
    """A click.command in a non-CLI file must not trigger detection."""
    diff = _make_diff("src/bernstein/core/utils.py", ["@click.command()", "def foo():"])
    assert detect_api_changes(diff) == []


# ---------------------------------------------------------------------------
# detect_api_changes — CLI commands
# ---------------------------------------------------------------------------


def test_detects_new_click_command() -> None:
    diff = _make_diff(_CLI_FILE, ["@click.command()", "def my_cmd():"])
    changes = detect_api_changes(diff)
    assert len(changes) == 1
    assert changes[0].kind == "command"
    assert changes[0].file == _CLI_FILE


def test_detects_app_command() -> None:
    diff = _make_diff(_CLI_FILE, ["@app.command()", "def deploy():"])
    changes = detect_api_changes(diff)
    assert any(c.kind == "command" for c in changes)


def test_extracts_named_command() -> None:
    diff = _make_diff(_CLI_FILE, ['@click.command(name="ship")', "def ship_it():"])
    changes = detect_api_changes(diff)
    assert changes[0].name == "ship"


def test_unnamed_command_has_fallback_name() -> None:
    diff = _make_diff(_CLI_FILE, ["@click.command()", "def deploy():"])
    changes = detect_api_changes(diff)
    assert changes[0].name == "<new command>"


# ---------------------------------------------------------------------------
# detect_api_changes — click options / arguments
# ---------------------------------------------------------------------------


def test_detects_new_click_option() -> None:
    diff = _make_diff(_CLI_FILE, ['@click.option("--timeout", default=30)'])
    changes = detect_api_changes(diff)
    assert len(changes) == 1
    assert changes[0].kind == "option"
    assert changes[0].name == "--timeout"


def test_detects_new_click_argument() -> None:
    diff = _make_diff(_CLI_FILE, ['@click.argument("plan_file")'])
    changes = detect_api_changes(diff)
    assert any(c.kind == "option" for c in changes)


def test_extracts_option_name_with_short_flag() -> None:
    diff = _make_diff(_CLI_FILE, ['@click.option("-v", "--verbose", is_flag=True)'])
    changes = detect_api_changes(diff)
    assert changes[0].name == "-v"


# ---------------------------------------------------------------------------
# detect_api_changes — config keys
# ---------------------------------------------------------------------------


def test_detects_config_key_in_home_py() -> None:
    diff = _make_diff(_CONFIG_FILE, ['"model" = "claude-sonnet-4-6"'])
    changes = detect_api_changes(diff)
    assert any(c.kind == "config_key" and c.name == "model" for c in changes)


def test_no_config_key_in_non_config_file() -> None:
    diff = _make_diff("src/bernstein/core/orchestrator.py", ['"model" = "claude-sonnet"'])
    assert detect_api_changes(diff) == []


def test_config_key_in_bernstein_yaml_addition() -> None:
    diff = _make_diff("bernstein.yaml", ['"timeout" = 60'])
    changes = detect_api_changes(diff)
    assert any(c.kind == "config_key" for c in changes)


# ---------------------------------------------------------------------------
# remind_message
# ---------------------------------------------------------------------------


def test_remind_message_empty_for_no_changes() -> None:
    assert remind_message([]) == ""


def test_remind_message_contains_change_description() -> None:
    changes = [APIChange(kind="command", name="deploy", file=_CLI_FILE)]
    msg = remind_message(changes)
    assert "README" in msg
    assert "deploy" in msg
    assert "command" in msg


def test_remind_message_lists_all_changes() -> None:
    changes = [
        APIChange(kind="command", name="ship", file=_CLI_FILE),
        APIChange(kind="option", name="--dry-run", file=_CLI_FILE),
    ]
    msg = remind_message(changes)
    assert "ship" in msg
    assert "--dry-run" in msg


# ---------------------------------------------------------------------------
# Integration — guardrails.check_readme_reminder
# ---------------------------------------------------------------------------


def test_guardrails_check_returns_allow_for_empty_diff() -> None:
    from bernstein.core.guardrails import check_readme_reminder
    from bernstein.core.policy_engine import DecisionType

    results = check_readme_reminder("")
    assert len(results) == 1
    assert results[0].type == DecisionType.ALLOW


def test_guardrails_check_returns_ask_for_new_command() -> None:
    from bernstein.core.guardrails import check_readme_reminder
    from bernstein.core.policy_engine import DecisionType

    diff = _make_diff(_CLI_FILE, ["@click.command()", "def brand_new():"])
    results = check_readme_reminder(diff)
    assert len(results) == 1
    assert results[0].type == DecisionType.ASK
    assert "README" in results[0].reason


def test_guardrails_check_readme_reminder_in_config() -> None:
    """GuardrailsConfig exposes readme_reminder flag, defaulting to True."""
    from bernstein.core.guardrails import GuardrailsConfig

    cfg = GuardrailsConfig()
    assert cfg.readme_reminder is True

    cfg_off = GuardrailsConfig(readme_reminder=False)
    assert cfg_off.readme_reminder is False
