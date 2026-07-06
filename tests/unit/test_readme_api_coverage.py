"""README API coverage test - detects undocumented public CLI commands.

When a new CLI command or command group is added to the Bernstein CLI, this
test fails with a clear message pointing to the README and the list of commands
that need documentation.

How it works
------------
1. Walk the top-level ``cli`` Click group to collect every registered command
   name.
2. Compare that set against ``DOCUMENTED_COMMANDS`` - the known set of
   commands that appear in the README.
3. If any command name is absent from both lists, the test fails and names
   the undocumented command explicitly.

Updating this test
------------------
When you add a new top-level command:

1. Add it to the README (``## Monitoring and diagnostics`` or an appropriate
   section, with a one-line description and example).
2. Add the command name to ``DOCUMENTED_COMMANDS`` below.

This file is the contract surface - adding to ``DOCUMENTED_COMMANDS`` without
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
        "backlog",
        # Agents
        "agents",
        # Skills (oai-004)
        "skills",
        # Skill usage provenance (issue #2301)
        "skill",
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
        # Web UI (v2.0.0)
        "gui",
        # Reports & profiling
        "export",
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
        "debug-bundle",
        # Core session
        "init",
        "start",
        "status",
        # Operator experience
        "pr",
        "from-ticket",
        "ticket",
        "remote",
        "hooks",
        "chat",
        "tunnel",
        "approve-tool",
        "reject-tool",
        "daemon",
        # release/1.9 features
        "acp",
        "autofix",
        "connect",
        "creds",
        "fleet",
        "notify",
        "preview",
        "review-responder",
        # May 2026 feature batch
        "cluster",
        "compaction",
        "handoff",
        "lineage",
        "credential",
        "migrate",
        "routine",
        "wheelhouse",
        # AAIF AGENTS.md generator (closes #1087)
        "agents-md",
        # Project bootstrapping from a single goal prompt
        "scaffold",
        # Local AST -> WIKI.md renderer
        "wiki",
        # Install-rev fingerprint operator helpers
        "identity",
        # Delegation-receipt verification (principal->orchestrator->sub-agent)
        "delegation",
        # Per-role adapter allow/deny-list inspection (role-adapter-policy group)
        "security",
        # Bughunt 2026-05-13 release wave
        "adapters",
        "analyze",
        # Recorded run-session inspection + fork (#1222)
        "session",
        # Bot-added: drift autofix (regen_contract_drift.py)
        "compare",
        "decisions",
        "recipes",
        "resume",
        "worktrees",
        # Bot-added: drift autofix (regen_contract_drift.py)
        "abandonments",
        "best-of-n",
        "blast-radius",
        "criterion-profile",
        "simulate",
        # Bot-added: drift autofix (regen_contract_drift.py)
        "telemetry",
        # Bot-added: drift autofix (regen_contract_drift.py)
        "quality",
        # Bot-added: drift autofix (regen_contract_drift.py)
        "cost-envelopes",
        # Bot-added: drift autofix (regen_contract_drift.py)
        "bom",
        "consensus",
        "knowledge",
        # Bot-added: drift autofix (regen_contract_drift.py)
        "integrations",
        # Bot-added: drift autofix (regen_contract_drift.py)
        "git",
        # Bot-added: drift autofix (regen_contract_drift.py)
        "bundle",
        # Playwright-based self-testing for UI/web agent runs
        "sandbox",
        # Bot-added: drift autofix (regen_contract_drift.py)
        "issue-to-pr",
        "pipeline",
        "secrets",
        "trackers",
        "trend-scan",
        # Bot-added: drift autofix (regen_contract_drift.py)
        "spec",
        # Bot-added: drift autofix (regen_contract_drift.py)
        "run-lookup",
        # Bot-added: drift autofix (regen_contract_drift.py)
        "interop",
        # Bot-added: drift autofix (regen_contract_drift.py)
        "desktop-register",
        # Bot-added: drift autofix (regen_contract_drift.py)
        "supervisor",
        # Bot-added: drift autofix (regen_contract_drift.py)
        "schedule",
        # Bot-added: drift autofix (regen_contract_drift.py)
        "team",
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
    allowlist - clean it up to keep the allowlist accurate.
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
    readme = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
    core_commands = ["bernstein run", "bernstein init", "bernstein stop"]
    missing = [cmd for cmd in core_commands if cmd not in readme]
    if missing:
        pytest.fail(
            f"README.md no longer mentions these core commands: {missing}\n"
            "Either the README was edited incorrectly, or the command was renamed."
        )


# ---------------------------------------------------------------------------
# Top-section structure (closes #1112)
# ---------------------------------------------------------------------------
# These guard the first-impression DX: the README's first screen must show an
# install block, a demo image, and a comparison table. If any disappears the
# tests fail loud so the rewrite from #1112 cannot silently regress.


def test_readme_has_three_line_install_block() -> None:
    """README.md must contain the canonical 3-line install block."""
    readme = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
    required_lines = (
        "pipx install bernstein",
        "bernstein init",
        'bernstein run -g "fix the failing test in tests/test_foo.py"',
    )
    missing = [line for line in required_lines if line not in readme]
    if missing:
        pytest.fail(
            "README.md is missing the 3-line install block (closes #1112).\n"
            f"Missing lines: {missing}\n"
            "The block must appear at the top of the README so first-time "
            "visitors can copy/paste without scrolling. See #1112 for context."
        )


def test_readme_top_section_lists_core_capabilities() -> None:
    """README.md must list Bernstein's load-bearing capability rows.

    The top-of-file capabilities block is the first technical context a
    visitor sees and gates how the rest of the README reads.
    """
    readme = (_REPO_ROOT / "README.md").read_text(encoding="utf-8").lower()
    required_substrings = (
        "hmac-chained audit",
        "signed agent cards",
        "air-gap",
        "mcp server",
    )
    missing = [s for s in required_substrings if s not in readme]
    if missing:
        pytest.fail(
            f"README.md top section is missing required capability rows: {missing}",
        )
