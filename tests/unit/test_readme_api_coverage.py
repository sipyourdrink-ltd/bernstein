"""CLI Reference and README coverage gate (#3468).

Detects undocumented public CLI commands by checking registered commands
against ``docs/reference/cli-reference.md`` and an explicit exemption set
(``UNDOCUMENTED_EXEMPTIONS``). Also validates core README structure.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent


def _documented_commands_from_docs() -> set[str]:
    """Extract top-level command names documented in docs/reference/cli-reference.md."""
    reference = _REPO_ROOT / "docs" / "reference" / "cli-reference.md"
    if not reference.exists():
        return set()

    text = reference.read_text(encoding="utf-8")
    headings = re.findall(r"^#+\s+`bernstein\s+([a-zA-Z0-9_-]+)", text, re.M)
    tables = re.findall(r"\|\s*`bernstein\s+([a-zA-Z0-9_-]+)", text, re.M)
    return set(headings + tables)


# ---------------------------------------------------------------------------
# Registered top-level commands exempt from cli-reference.md
# ---------------------------------------------------------------------------
# Every entry MUST carry a non-empty reason string explaining why it is exempt.
# When a command is documented in docs/reference/cli-reference.md, remove it
# from this set (test_exemptions_are_not_already_documented enforces this).

UNDOCUMENTED_EXEMPTIONS: dict[str, str] = {
    "abandonments": "Subcommand group for task abandonment tracking (#2550)",
    "adapters": "Adapter lifecycle and listing group (#2550)",
    "agents-md": "AAIF AGENTS.md generator (#1087)",
    "analyze": "Static code analysis utilities (#2550)",
    "artifact": "Artifact management single alias (#2553)",
    "artifacts": "Task artifact management group (#2553)",
    "backlog": "Task backlog group (#2358)",
    "bench": "Reproducibility-gated evaluation harness (#2932)",
    "best-of-n": "Best-of-N candidate sampler (#2550)",
    "blast-radius": "Change blast radius analyzer (#3139)",
    "bom": "Bill of materials export group (#2550)",
    "bundle": "Debug bundle export helper (#2550)",
    "cluster": "Cluster orchestration group (#2550)",
    "compare": "Contract drift comparison tool (#2550)",
    "conn": "Connection document management group (#2550)",
    "context": "Chain-anchored worker context capsules (#2545)",
    "cost-envelopes": "Cost envelope management group (#2550)",
    "criterion-profile": "Criterion profile management group (#2550)",
    "ctx": "Context capsule alias (#2545)",
    "datasource": "Datasource connection management group (#2550)",
    "decisions": "Governance decision tracking group (#2309)",
    "desktop-register": "Desktop application registration helper (#2550)",
    "endpoints": "Self-hosted OpenAI endpoint certifier (#2889)",
    "events": "Audit event log group (#2550)",
    "export": "Report and data export group (#2550)",
    "git": "Git worktree and repository helper group (#2550)",
    "gui": "Web UI launcher (v2.0.0)",
    "handoff": "Agent session handoff group (#2550)",
    "hook-gate": "In-process worker hook verification gate (#2360)",
    "integrations": "Third-party integrations list group (#2550)",
    "intent": "Intent recognition group (#2550)",
    "knowledge": "Knowledge base management group (#2550)",
    "limits": "Rate and resource limit inspection group (#2550)",
    "listen": "Optional voice extra speech-to-text listener (#3145)",
    "migrate": "Database and schema migration group (#2550)",
    "mission": "Mission statement and goal tracking group (#2550)",
    "payment-mandate": "Signed payment mandates group (#2612)",
    "pipeline": "Workflow pipeline group (#2550)",
    "pool": "Named sandbox pool management (#2547)",
    "quality": "Quality metric inspection group (#2550)",
    "readme-l10n": "Translated README drift gate (#3425)",
    "recipes": "Recipe execution group (#2550)",
    "resume": "Session resume helper (#2550)",
    "routine": "Routine task schedule group (#3140)",
    "run-lookup": "Run ID lookup utility (#2550)",
    "sandbox": "Playwright UI sandbox testing (#2550)",
    "secrets": "Secret store management group (#2550)",
    "security": "Role-adapter policy security group (#2550)",
    "serve": "Background task server daemon (#2550)",
    "simulate": "Simulation and benchmark group (#3143)",
    "sla": "Per-goal SLA contract receipts (#2549)",
    "spec": "Specification renderer group (#2550)",
    "spiffe": "SPIFFE workload identity group (#2363)",
    "supervisor": "Process supervisor group (#2550)",
    "sync": "Task synchronization helper (#2358)",
    "team": "Agent team coordination group (#2550)",
    "telemetry": "Telemetry collection group (#2550)",
    "trackers": "Issue tracker integration group (#2550)",
    "trend-scan": "Metric trend scanner (#2550)",
    "var": "Fleet configuration variable group (#2550)",
    "wheelhouse": "Wheelhouse package cache group (#2550)",
    "worktrees": "Git worktree management group (#2550)",
}

# Backwards compatibility alias for code or tools expecting DOCUMENTED_COMMANDS
DOCUMENTED_COMMANDS: frozenset[str] = frozenset(_documented_commands_from_docs() | set(UNDOCUMENTED_EXEMPTIONS.keys()))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collect_top_level_commands() -> set[str]:
    """Return all top-level command names registered with the Bernstein CLI."""
    from bernstein.cli.main import cli

    return set(cli.commands.keys())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_all_cli_commands_are_documented_in_reference() -> None:
    """Every top-level CLI command must appear in docs/reference/cli-reference.md or UNDOCUMENTED_EXEMPTIONS.

    If this test fails, a new command was added without documenting it. Steps to fix:

    1. Document the command in docs/reference/cli-reference.md.
    2. Or, if the command is experimental or pending documentation, add an entry
       with a non-empty reason to UNDOCUMENTED_EXEMPTIONS in this file.
    """
    registered = _collect_top_level_commands()
    documented = _documented_commands_from_docs()
    exempted = set(UNDOCUMENTED_EXEMPTIONS.keys())

    missing = registered - (documented | exempted)

    if missing:
        names = ", ".join(sorted(missing))
        pytest.fail(
            f"New CLI command(s) detected that are neither documented in docs/reference/cli-reference.md "
            f"nor listed in UNDOCUMENTED_EXEMPTIONS: {names}\n\n"
            "Action required:\n"
            "  1. Document the command(s) in docs/reference/cli-reference.md.\n"
            "  2. Or, if the command is experimental/internal, add an explicit exemption with a reason to\n"
            "     UNDOCUMENTED_EXEMPTIONS in tests/unit/test_readme_api_coverage.py.\n\n"
            "This keeps the CLI reference grounded against disk documentation."
        )


def test_exemptions_are_not_already_documented() -> None:
    """Commands in UNDOCUMENTED_EXEMPTIONS must not already be documented in docs/reference/cli-reference.md.

    This ensures UNDOCUMENTED_EXEMPTIONS shrinks as commands are documented.
    """
    documented = _documented_commands_from_docs()
    redundant = set(UNDOCUMENTED_EXEMPTIONS.keys()) & documented

    if redundant:
        names = ", ".join(sorted(redundant))
        pytest.fail(
            f"Command(s) in UNDOCUMENTED_EXEMPTIONS are now documented in docs/reference/cli-reference.md: {names}\n\n"
            "Action required:\n"
            "  Remove these entries from UNDOCUMENTED_EXEMPTIONS in\n"
            "  tests/unit/test_readme_api_coverage.py."
        )


def test_exemptions_have_no_phantoms() -> None:
    """Every name in UNDOCUMENTED_EXEMPTIONS must correspond to an actual registered command."""
    registered = _collect_top_level_commands()
    phantoms = set(UNDOCUMENTED_EXEMPTIONS.keys()) - registered

    if phantoms:
        names = ", ".join(sorted(phantoms))
        pytest.fail(
            f"UNDOCUMENTED_EXEMPTIONS contains names that are not registered commands: {names}\n\n"
            "Remove these phantom entries from UNDOCUMENTED_EXEMPTIONS in\n"
            "tests/unit/test_readme_api_coverage.py."
        )


def test_exemptions_have_nonempty_reasons() -> None:
    """Every entry in UNDOCUMENTED_EXEMPTIONS must carry a non-empty reason string."""
    empty_reasons = [cmd for cmd, reason in UNDOCUMENTED_EXEMPTIONS.items() if not reason or not reason.strip()]
    if empty_reasons:
        names = ", ".join(sorted(empty_reasons))
        pytest.fail(
            f"UNDOCUMENTED_EXEMPTIONS contains entries without a non-empty reason: {names}\n\n"
            "Provide a non-empty reason string for each entry in UNDOCUMENTED_EXEMPTIONS."
        )


def test_readme_mentions_core_commands() -> None:
    """Smoke-check: README.md mentions at least the core workflow commands."""
    readme = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
    core_commands = ["bernstein run", "bernstein init", "bernstein stop"]
    missing = [cmd for cmd in core_commands if cmd not in readme]
    if missing:
        pytest.fail(
            f"README.md no longer mentions these core commands: {missing}\n"
            "Either the README was edited incorrectly, or the command was renamed."
        )


def test_readme_has_three_line_install_block() -> None:
    """README.md must contain the canonical 3-line install block."""
    readme = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
    required_lines = (
        "pipx install bernstein",
        "bernstein init",
        'bernstein -g "fix the failing test in tests/test_foo.py"',
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
    """README.md must list Bernstein's load-bearing capability rows."""
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
