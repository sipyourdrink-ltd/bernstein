"""CLI Reference and README coverage gate (#3468).

Detects undocumented public CLI commands by checking registered commands
against ``docs/reference/cli-reference.md`` and an explicit exemption set
(``UNDOCUMENTED_EXEMPTIONS``). Also validates core README structure.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent
CLI_REFERENCE = _REPO_ROOT / "docs" / "reference" / "cli-reference.md"

# One backticked ``bernstein <command> ...`` span, e.g. ``` `bernstein run` ``` or
# ``` `bernstein cost policy verify DECISION_HASH` ```.
_COMMAND_SPAN = r"`bernstein\s+[A-Za-z0-9_-][^`\n]*`"

# A heading documents the command(s) its title *ends* with. Three shapes occur
# in docs/reference/cli-reference.md and all three are documentation:
#     #### `bernstein run`
#     ## SPIFFE workload identity: `bernstein spiffe`
#     #### `bernstein voice` / `bernstein listen`
# Anchoring on the end of the line is what keeps this from matching a command
# named in passing halfway through a title.
_DOC_HEADING = re.compile(
    # The prefix is lazily optional (``??``) so the run is anchored as early as
    # the line allows. A greedy-optional prefix would swallow the first half of
    # ``#### `bernstein voice` / `bernstein listen` `` and lose the alias.
    rf"^#+[ \t]+(?:[^\n]*?[ \t])??({_COMMAND_SPAN}(?:[ \t]*/[ \t]*{_COMMAND_SPAN})*)[ \t]*$",
    re.M,
)

# A table row documents the command(s) its first cell *starts* with. Anchoring
# on the cell start is load-bearing: description cells mention commands in prose
# ("see `bernstein adapters list`") and those mentions are not documentation.
_DOC_TABLE_ROW = re.compile(
    rf"^\|[ \t]*({_COMMAND_SPAN}(?:[ \t]*/[ \t]*{_COMMAND_SPAN})*)",
    re.M,
)

_COMMAND_NAME = re.compile(r"`bernstein\s+([a-zA-Z0-9_-]+)")


def _parse_documented_commands(text: str) -> set[str]:
    """Return the top-level command names ``text`` documents.

    Split out from the file read so the parsing rules can be tested against
    fixture markdown rather than only against the reference file on disk.
    """
    names: set[str] = set()
    for pattern in (_DOC_HEADING, _DOC_TABLE_ROW):
        for match in pattern.finditer(text):
            names.update(_COMMAND_NAME.findall(match.group(1)))
    return names


def _documented_commands_from_docs() -> set[str]:
    """Extract top-level command names documented in docs/reference/cli-reference.md.

    A missing reference file is a hard failure, not an empty set: silently
    treating "no documentation on disk" as "nothing is documented" would leave
    the exemption tests below passing vacuously.
    """
    if not CLI_REFERENCE.is_file():
        pytest.fail(
            f"{CLI_REFERENCE} is missing. This gate derives the documented command set from it; "
            "restore the file rather than letting the gate degrade to an empty set."
        )
    return _parse_documented_commands(CLI_REFERENCE.read_text(encoding="utf-8"))


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
    "bom": "Bill of materials export group (#2550)",
    "bundle": "Debug bundle export helper (#2550)",
    "cluster": "Cluster orchestration group (#2550)",
    "compare": "Contract drift comparison tool (#2550)",
    "conn": "Connection document management group (#2550)",
    "context": "Chain-anchored worker context capsules (#2545)",
    "criterion-profile": "Criterion profile management group (#2550)",
    "ctx": "Context capsule alias (#2545)",
    "dashboard": "Deprecated legacy dashboard command, redirects to gui serve (#4395)",
    "datasource": "Datasource connection management group (#2550)",
    "decisions": "Governance decision tracking group (#2309)",
    "desktop-register": "Desktop application registration helper (#2550)",
    "endpoints": "Self-hosted OpenAI endpoint certifier (#2889)",
    "events": "Audit event log group (#2550)",
    "export": "Report and data export group (#2550)",
    "git": "Git worktree and repository helper group (#2550)",
    "handoff": "Agent session handoff group (#2550)",
    "integrations": "Third-party integrations list group (#2550)",
    "intent": "Intent recognition group (#2550)",
    "knowledge": "Knowledge base management group (#2550)",
    "migrate": "Database and schema migration group (#2550)",
    "mission": "Mission statement and goal tracking group (#2550)",
    "payment-mandate": "Signed payment mandates group (#2612)",
    "pipeline": "Workflow pipeline group (#2550)",
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
    "supervisor": "Process supervisor group (#2550)",
    "team": "Agent team coordination group (#2550)",
    "telemetry": "Telemetry collection group (#2550)",
    "trackers": "Issue tracker integration group (#2550)",
    "trend-scan": "Metric trend scanner (#2550)",
    "var": "Fleet configuration variable group (#2550)",
    "wheelhouse": "Wheelhouse package cache group (#2550)",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collect_top_level_commands() -> set[str]:
    """Return all top-level command names registered with the Bernstein CLI."""
    from bernstein.cli.main import cli

    return set(cli.commands.keys())


# ---------------------------------------------------------------------------
# The extractor itself
# ---------------------------------------------------------------------------
# Everything below hangs off _parse_documented_commands: a command it fails to
# see is reported as undocumented and picks up an exemption it does not need,
# and a command it sees where no documentation exists walks through the gate.
# Both directions are pinned here so a regex edit cannot quietly move them.


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        pytest.param("#### `bernstein run`\n", {"run"}, id="plain-heading"),
        pytest.param(
            "## SPIFFE workload identity: `bernstein spiffe`\n",
            {"spiffe"},
            id="heading-with-prose-prefix",
        ),
        pytest.param(
            "#### `bernstein voice` / `bernstein listen`\n",
            {"voice", "listen"},
            id="heading-alias-pair",
        ),
        pytest.param(
            "#### `bernstein cost policy verify DECISION_HASH`\n",
            {"cost"},
            id="heading-with-subcommand-path",
        ),
        pytest.param(
            "| `bernstein doctor` | Run diagnostics. | `cli/doctor.py` |\n",
            {"doctor"},
            id="table-row",
        ),
        pytest.param(
            "| `bernstein voice` / `bernstein listen` | Voice control. | `cli/voice_cmd.py` |\n",
            {"voice", "listen"},
            id="table-row-alias-pair",
        ),
    ],
)
def test_parser_reads_every_documentation_shape(markdown: str, expected: set[str]) -> None:
    """Each shape the reference actually uses to document a command is recognised."""
    assert _parse_documented_commands(markdown) == expected


@pytest.mark.parametrize(
    ("markdown", "why"),
    [
        pytest.param(
            "| `--cli NAME` | Any adapter from `bernstein adapters list`. |\n",
            "a command named inside a description cell is a cross-reference, not documentation",
            id="prose-mention-in-table-cell",
        ),
        pytest.param(
            "Run `bernstein telemetry export` to dump the buffer.\n",
            "a command named in body prose is not documentation",
            id="prose-mention-in-paragraph",
        ),
        pytest.param(
            "## `bernstein pipeline` is covered in the workflow guide\n",
            "a command named mid-heading is a pointer, not a section documenting it",
            id="mid-heading-mention",
        ),
    ],
)
def test_parser_rejects_mere_mentions(markdown: str, why: str) -> None:
    """A passing mention must not count as documentation -- that would open the gate."""
    assert _parse_documented_commands(markdown) == set(), why


def test_missing_reference_file_fails_instead_of_returning_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing cli-reference.md must fail loudly, not degrade to "nothing is documented".

    Returning an empty set there would leave test_exemptions_are_not_already_documented
    passing vacuously, so the exemption set would stop shrinking without any signal.
    """
    monkeypatch.setattr(
        sys.modules[__name__],
        "CLI_REFERENCE",
        _REPO_ROOT / "docs" / "reference" / "no-such-cli-reference.md",
        raising=True,
    )
    with pytest.raises(pytest.fail.Exception, match="no-such-cli-reference.md is missing"):
        _documented_commands_from_docs()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_all_cli_commands_are_documented() -> None:
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
