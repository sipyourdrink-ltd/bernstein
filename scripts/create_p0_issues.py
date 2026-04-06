#!/usr/bin/env python3
"""Create GitHub issues for all P0 backlog tickets."""

import glob
import os
import re
import subprocess
import sys
import time

BACKLOG_DIR = "/Users/sasha/IdeaProjects/personal_projects/bernstein/.sdd/backlog/open"
REPO = "chernistry/bernstein"

# Map filename prefix to GitHub label(s)
PREFIX_TO_LABELS: dict[str, list[str]] = {
    "orch": ["orchestrator"],
    "agent": ["adapter"],
    "cli": ["cli"],
    "cost": ["cost"],
    "sec": ["core"],  # security modules live in core
    "task": ["tasks"],
    "mcp": ["mcp"],
    "tui": ["tui"],
    "web": ["web-dashboard"],
    "cfg": ["config"],
    "hook": ["hooks"],
    "doc": ["docs"],
    "test": ["testing"],
    "ent": ["core"],  # enterprise in core
    "claude": ["claude-code"],
    "road": ["roadmap"],
}

# Map prefix to relevant source files for implementation suggestions
PREFIX_TO_FILES: dict[str, list[str]] = {
    "orch": ["src/bernstein/core/orchestrator.py"],
    "agent": ["src/bernstein/core/spawner.py", "src/bernstein/adapters/"],
    "cli": ["src/bernstein/cli/"],
    "cost": ["src/bernstein/core/cost_tracker.py"],
    "sec": ["src/bernstein/core/"],
    "task": ["src/bernstein/core/task_store.py"],
    "mcp": ["src/bernstein/mcp/"],
    "tui": ["src/bernstein/tui/"],
    "web": ["src/bernstein/web/"],
    "cfg": ["src/bernstein/core/config.py"],
    "hook": ["src/bernstein/core/hooks.py"],
    "doc": ["docs/"],
    "test": ["tests/"],
    "ent": ["src/bernstein/core/"],
    "claude": ["src/bernstein/adapters/claude.py"],
    "road": [],
}


def parse_yaml_ticket(filepath: str) -> dict[str, str] | None:
    """Parse a backlog YAML ticket file. Returns None if not P0."""
    with open(filepath) as f:
        content = f.read()

    # Check priority
    m = re.search(r"^priority:\s*(\d+)", content, re.MULTILINE)
    if not m or m.group(1) != "0":
        return None

    result: dict[str, str] = {"filepath": filepath, "filename": os.path.basename(filepath)}

    # Parse front-matter fields
    for field in ("id", "title", "type", "scope", "complexity", "role"):
        m = re.search(rf'^{field}:\s*"?([^"\n]+)"?', content, re.MULTILINE)
        result[field] = m.group(1).strip() if m else ""

    # Parse tags
    m = re.search(r"^tags:\s*\[(.+?)\]", content, re.MULTILINE)
    result["tags"] = m.group(1).strip() if m else ""

    # Parse description (everything after "## Description")
    m = re.search(r"## Description\s*\n\s*\n(.+)", content, re.DOTALL)
    result["description"] = m.group(1).strip() if m else result.get("title", "")

    # Get prefix
    basename = os.path.basename(filepath)
    prefix_m = re.match(r"([a-z]+)-\d+", basename)
    result["prefix"] = prefix_m.group(1) if prefix_m else ""

    return result


def generate_suggestions(ticket: dict[str, str]) -> list[str]:
    """Generate implementation suggestions based on the ticket."""
    prefix = ticket["prefix"]
    desc = ticket["description"].lower()
    ticket["title"].lower()
    suggestions: list[str] = []

    # Generic suggestions based on category
    category_suggestions: dict[str, list[str]] = {
        "orch": [
            "Review `src/bernstein/core/orchestrator.py` for the main orchestration loop",
            "Check `src/bernstein/core/spawner.py` for agent lifecycle management",
            "Add tests in `tests/unit/` covering the new behavior",
        ],
        "agent": [
            "Review `src/bernstein/core/spawner.py` for agent lifecycle management",
            "Check `src/bernstein/adapters/` for adapter implementations",
            "Add tests in `tests/unit/` covering the new behavior",
        ],
        "cli": [
            "Review `src/bernstein/cli/` for existing command implementations",
            "Follow the pattern of existing CLI commands using Click",
            "Add tests in `tests/unit/` covering the new command",
        ],
        "cost": [
            "Review `src/bernstein/core/cost_tracker.py` for cost tracking logic",
            "Check `.sdd/metrics/` for how cost data is persisted",
            "Add tests in `tests/unit/` covering cost calculations",
        ],
        "sec": [
            "Review `src/bernstein/core/` for security-related modules",
            "Consider defense-in-depth: validate at multiple layers",
            "Add tests covering both positive and negative security cases",
        ],
        "task": [
            "Review `src/bernstein/core/task_store.py` for task management logic",
            "Check the task server API routes in `src/bernstein/core/routes/`",
            "Add tests in `tests/unit/` covering task state transitions",
        ],
        "mcp": [
            "Review `src/bernstein/mcp/` for MCP protocol implementation",
            "Check MCP specification for protocol compliance",
            "Add tests in `tests/unit/` covering MCP interactions",
        ],
        "tui": [
            "Review `src/bernstein/tui/` for terminal UI components",
            "Check Textual or Rich library usage patterns",
            "Add tests in `tests/unit/` covering TUI rendering",
        ],
        "web": [
            "Review `src/bernstein/web/` for web dashboard implementation",
            "Check static assets and templates for the dashboard",
            "Add tests covering API endpoints and rendering",
        ],
        "cfg": [
            "Review `src/bernstein/core/config.py` for configuration handling",
            "Check `bernstein.yaml` schema and validation logic",
            "Add tests in `tests/unit/` covering config edge cases",
        ],
        "hook": [
            "Review `src/bernstein/core/hooks.py` for hook system implementation",
            "Check how hooks integrate with the orchestrator lifecycle",
            "Add tests in `tests/unit/` covering hook execution",
        ],
        "doc": [
            "Review `docs/` for existing documentation structure",
            "Follow the existing documentation style and format",
            "Cross-reference with source code for accuracy",
        ],
        "test": [
            "Review `tests/` for existing test patterns and conventions",
            "Use `uv run python scripts/run_tests.py -x` for isolated test execution",
            "Follow pytest best practices for fixtures and parametrization",
        ],
        "ent": [
            "Review `src/bernstein/core/` for enterprise module integration points",
            "Consider multi-tenant isolation requirements",
            "Add tests covering enterprise-specific scenarios",
        ],
        "claude": [
            "Review `src/bernstein/adapters/claude.py` for Claude Code adapter",
            "Check Claude Code CLI documentation for new capabilities",
            "Add tests in `tests/unit/` covering Claude-specific behavior",
        ],
        "road": [
            "Break this feature into smaller implementation tasks",
            "Identify dependencies on existing modules",
            "Create a design document before implementation",
        ],
    }

    suggestions = category_suggestions.get(prefix, [
        "Review relevant source files for integration points",
        "Add comprehensive tests for the new functionality",
        "Update documentation to reflect the changes",
    ])

    # Add description-specific suggestions
    if "error" in desc or "failure" in desc or "fix" in desc:
        suggestions.append("Add error handling tests and edge case coverage")
    if "validation" in desc or "validate" in desc or "schema" in desc:
        suggestions.append("Include both valid and invalid input test cases")
    if "api" in desc or "endpoint" in desc or "route" in desc:
        suggestions.append("Add API integration tests with mock server")
    if "performance" in desc or "speed" in desc or "latency" in desc:
        suggestions.append("Add benchmark tests to measure performance impact")
    if "security" in desc or "auth" in desc or "permission" in desc:
        suggestions.append("Conduct threat modeling for the new attack surface")

    # Limit to 5 suggestions
    return suggestions[:5]


def build_issue_body(ticket: dict[str, str]) -> str:
    """Build the GitHub issue body."""
    prefix = ticket["prefix"]
    suggestions = generate_suggestions(ticket)
    relevant_files = PREFIX_TO_FILES.get(prefix, [])

    suggestions_md = "\n".join(f"- {s}" for s in suggestions)
    files_md = "\n".join(f"- `{f}`" for f in relevant_files) if relevant_files else "- Determine based on feature scope"

    body = f"""## Description

{ticket['description']}

## Metadata

| Field | Value |
|-------|-------|
| Priority | P0 |
| Scope | {ticket.get('scope', 'N/A')} |
| Complexity | {ticket.get('complexity', 'N/A')} |
| Role | {ticket.get('role', 'N/A')} |
| Tags | {ticket.get('tags', 'N/A')} |

## Implementation Suggestions

{suggestions_md}

## Relevant Files

{files_md}

---
*Backlog: `{ticket['filename']}`*"""

    return body


def create_issue(ticket: dict[str, str]) -> tuple[bool, str]:
    """Create a GitHub issue for the ticket. Returns (success, message)."""
    prefix = ticket["prefix"]
    labels = ["P0", *PREFIX_TO_LABELS.get(prefix, [])]
    labels_str = ",".join(labels)

    title = ticket["title"]
    body = build_issue_body(ticket)

    # Use subprocess with stdin to avoid shell escaping issues
    cmd = [
        "gh", "issue", "create",
        "--repo", REPO,
        "--title", title,
        "--label", labels_str,
        "--body", body,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            url = result.stdout.strip()
            return True, url
        else:
            return False, result.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "Timed out"
    except Exception as e:
        return False, str(e)


def main() -> None:
    """Main entry point."""
    # Glob all YAML files
    pattern = os.path.join(BACKLOG_DIR, "*.yaml")
    all_files = sorted(glob.glob(pattern))
    print(f"Found {len(all_files)} total backlog files")

    # Filter to P0 only
    p0_tickets: list[dict[str, str]] = []
    for filepath in all_files:
        ticket = parse_yaml_ticket(filepath)
        if ticket is not None:
            p0_tickets.append(ticket)

    print(f"Found {len(p0_tickets)} P0 tickets")
    print()

    created = 0
    failed = 0
    errors: list[str] = []

    for i, ticket in enumerate(p0_tickets, 1):
        prefix = ticket["prefix"]
        title = ticket["title"]
        print(f"[{i}/{len(p0_tickets)}] Creating: [{prefix}] {title}...", end=" ", flush=True)

        success, msg = create_issue(ticket)
        if success:
            created += 1
            print(f"OK -> {msg}")
        else:
            failed += 1
            error_msg = f"FAILED: {ticket['filename']}: {msg}"
            errors.append(error_msg)
            print(f"FAILED: {msg}")

        # Rate limit safety
        if i < len(p0_tickets):
            time.sleep(1)

    print()
    print("=" * 60)
    print(f"P0 ISSUES: {created}/{len(p0_tickets)} created successfully")
    if errors:
        print(f"\nFailed ({failed}):")
        for e in errors:
            print(f"  - {e}")
    print("=" * 60)

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
