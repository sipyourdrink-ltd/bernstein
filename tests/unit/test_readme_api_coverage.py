"""README API coverage test — detects undocumented public CLI commands.

When a new CLI command or command group is added to the Bernstein CLI, this
test fails with a clear message pointing to the README and the list of commands
that need documentation.

How it works
------------
1. Walk the top-level ``cli`` Click group to collect every registered command
   name.
2. Compare that set against ``DOCUMENTED_COMMANDS`` — the known set of
   commands that appear in the README.
3. If any command name is absent from both lists, the test fails and names
   the undocumented command explicitly.

Updating this test
------------------
When you add a new top-level command:

1. Add it to the README (``## Monitoring and diagnostics`` or an appropriate
   section, with a one-line description and example).
2. Add the command name to ``DOCUMENTED_COMMANDS`` below.

This file is the contract surface — adding to ``DOCUMENTED_COMMANDS`` without
updating the README defeats the purpose.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Known documented commands
# ---------------------------------------------------------------------------
# Add a command here ONLY after you have added it to README.md.
# Names must match the string passed to cli.add_command(..., "<name>") exactly.

DOCUMENTED_COMMANDS: frozenset[str] = frozenset(
    {
        # Core workflow
        "run",
        "stop",
        "demo",
        "cook",
        # Monitoring & diagnostics
        "live",
        "dashboard",
        "ps",
        "cost",
        "doctor",
        "recap",
        "retro",
        "trace",
        "logs",
        # Plan / task management
        "plan",
        "tasks",
        "add-task",
        "cancel",
        "approve",
        "reject",
        "review",
        "pending",
        "list-tasks",
        "sync",
        # Agents
        "agents",
        # Auth
        "auth",
        "login",
        # Advanced / power-user
        "evolve",
        "benchmark",
        "eval",
        "estimate",
        "checkpoint",
        "wrap-up",
        "replay",
        "diff",
        "dep-impact",
        "changelog",
        "fingerprint",
        "merge",
        # Cloud
        "cloud",
        # Infrastructure groups
        "workspace",
        "config",
        "cache",
        "audit",
        "compliance",
        "verify",
        "chaos",
        "manifest",
        "memory",
        "prompts",
        "ci",
        "graph",
        "policy",
        "mcp",
        "github",
        "plugins",
        "quarantine",
        "validate",
        "workflow",
        "gateway",
        "templates",
        # Reports & profiling
        "man-pages",
        "profile",
        "report",
        "run-changelog",
        # Utilities
        "aliases",
        "completions",
        "config-path",
        "dry-run",
        "explain",
        "init-wizard",
        "ideate",
        "install-hooks",
        "help-all",
        "cleanup",
        "history",
        "commit-stats",
        "test",
        "test-adapter",
        "quickstart",
        "watch",
        "listen",
        "self-update",
        "undo",
        "worker",
        "dr",
        "incident",
        "postmortem",
        "slo",
        "triggers",
        # Debugging
        "debug",
        # Hidden aliases (backward compat — documented in README "Command aliases" table)
        "overture",  # alias for init
        "downbeat",  # alias for start
        "score",  # alias for status
    }
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent


def _collect_top_level_commands() -> set[str]:
    """Return all top-level command names registered with the Bernstein CLI."""
    from bernstein.cli.main import cli  # import here to keep module-level clean

    return set(cli.commands.keys())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_all_cli_commands_are_documented() -> None:
    """Every top-level CLI command must appear in DOCUMENTED_COMMANDS.

    If this test fails, a new command was added without updating the
    documentation allowlist.  Steps to fix:

    1. Add the command name to ``DOCUMENTED_COMMANDS`` in this file.
    2. Add a description and usage example to README.md.
    """
    registered = _collect_top_level_commands()
    undocumented = registered - DOCUMENTED_COMMANDS

    if undocumented:
        names = ", ".join(sorted(undocumented))
        pytest.fail(
            f"New CLI command(s) detected that are not in DOCUMENTED_COMMANDS: {names}\n\n"
            "Action required:\n"
            "  1. Add usage docs / examples to README.md for each command above.\n"
            "  2. Add the command name(s) to DOCUMENTED_COMMANDS in\n"
            "     tests/unit/test_readme_api_coverage.py.\n\n"
            "This keeps the public API contract visible and prevents silent drift."
        )


def test_documented_commands_allowlist_has_no_phantoms() -> None:
    """Every name in DOCUMENTED_COMMANDS must correspond to an actual registered command.

    If this test fails, a command was removed or renamed without updating the
    allowlist — clean it up to keep the allowlist accurate.
    """
    registered = _collect_top_level_commands()
    phantoms = DOCUMENTED_COMMANDS - registered

    if phantoms:
        names = ", ".join(sorted(phantoms))
        pytest.fail(
            f"DOCUMENTED_COMMANDS contains names that are not registered commands: {names}\n\n"
            "Remove these phantom entries from DOCUMENTED_COMMANDS in\n"
            "tests/unit/test_readme_api_coverage.py."
        )


def test_readme_mentions_core_commands() -> None:
    """Smoke-check: README.md mentions at least the core workflow commands.

    This guards against accidentally wiping the command reference section
    from the README.
    """
    readme = (_REPO_ROOT / "README.md").read_text()
    core_commands = ["bernstein run", "bernstein init", "bernstein status", "bernstein stop"]
    missing = [cmd for cmd in core_commands if cmd not in readme]
    if missing:
        pytest.fail(
            f"README.md no longer mentions these core commands: {missing}\n"
            "Either the README was edited incorrectly, or the command was renamed."
        )
