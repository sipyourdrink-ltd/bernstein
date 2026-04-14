#!/usr/bin/env python3
"""Convert all tickets in .sdd/backlog/open/ to Bernstein Ticket Format v1.

Reads existing markdown tickets (old format with **Priority:** etc.) and
rewrites them with YAML frontmatter + structured markdown body.
"""

from __future__ import annotations

import re
from pathlib import Path

BACKLOG = Path(".sdd/backlog/open")

# Map ticket ID prefix to default tags
TAG_MAP: dict[str, list[str]] = {
    "W5": ["ecosystem"],
    "W6": ["enterprise", "compliance"],
    "W7": ["dx", "developer-experience"],
    "W8": ["testing", "quality"],
    "W9": ["observability", "monitoring"],
    "W10": ["ai-engineering"],
    "R0": ["adoption", "dx"],
    "R1": ["adoption", "dx"],
    "R2": ["cost", "efficiency"],
    "R3": ["cost", "efficiency"],
    "R4": ["quality", "trust"],
    "R5": ["enterprise", "compliance"],
    "R6": ["ecosystem", "moat"],
    "R7": ["ecosystem", "integrations"],
    "R8": ["thought-leadership", "marketing"],
    "R9": ["moonshot", "future"],
    "R10": ["moonshot", "saas"],
    "D0": ["ecosystem", "adapters"],
    "F1": ["agent-intelligence"],
    "F2": ["platform"],
    "F3": ["enterprise"],
    "F4": ["ecosystem"],
    "F5": ["research"],
    "F6": ["dx"],
    "F7": ["safety"],
    "F8": ["cost"],
    "F9": ["community"],
}

# Map ticket ID prefix to default type
TYPE_MAP: dict[str, str] = {
    "W8": "test",
    "W9": "feature",
    "R3": "feature",
    "R4": "test",
    "R5": "feature",
    "R8": "docs",
}

# Complexity heuristic from scope
COMPLEXITY_FROM_SCOPE: dict[str, str] = {
    "small": "low",
    "medium": "medium",
    "large": "high",
}

# Estimated minutes from scope
MINUTES_FROM_SCOPE: dict[str, int] = {
    "small": 20,
    "medium": 45,
    "large": 90,
}


def _extract_field(text: str, field: str) -> str:
    """Extract **Field:** value from markdown."""
    m = re.search(rf"\*\*{field}:\*\*\s*(.+)", text)
    return m.group(1).strip() if m else ""


def _extract_body_sections(text: str) -> dict[str, str]:
    """Extract markdown sections (## Heading → content)."""
    sections: dict[str, str] = {}
    current = ""
    lines: list[str] = []
    for line in text.split("\n"):
        if line.startswith("## "):
            if current:
                sections[current] = "\n".join(lines).strip()
            current = line[3:].strip()
            lines = []
        elif current:
            lines.append(line)
    if current:
        sections[current] = "\n".join(lines).strip()
    return sections


def _get_tags(ticket_id: str) -> list[str]:
    """Get default tags from ticket ID prefix."""
    for prefix, tags in TAG_MAP.items():
        if ticket_id.startswith(prefix):
            return tags
    return []


def _get_type(ticket_id: str, role: str) -> str:
    """Get ticket type from ID prefix or role."""
    for prefix, t in TYPE_MAP.items():
        if ticket_id.startswith(prefix):
            return t
    if role == "qa":
        return "test"
    if role == "security":
        return "security"
    if role == "docs":
        return "docs"
    return "feature"


def _guess_affected_paths(body: str) -> list[str]:
    """Extract file paths mentioned in the body."""
    paths: list[str] = []
    for m in re.finditer(r"`((?:src|tests|docs|scripts)/[a-zA-Z0-9_/.-]+\.(?:py|md|html|yml|yaml|toml))`", body):
        paths.append(m.group(1))
    # Also find bare paths
    for m in re.finditer(r"(?:^|\s)((?:src|tests)/bernstein/[a-zA-Z0-9_/.-]+\.py)", body):
        if m.group(1) not in paths:
            paths.append(m.group(1))
    return paths[:10]  # Cap at 10


def _guess_janitor_signals(_ticket_id: str, affected: list[str], body: str) -> list[dict[str, str]]:
    """Generate janitor signals from affected paths and body."""
    signals: list[dict[str, str]] = []
    # New files mentioned with "(new)"
    for m in re.finditer(r"`([^`]+)`\s*\(new\)", body):
        signals.append({"type": "path_exists", "value": m.group(1)})
    # Test commands mentioned
    for m in re.finditer(r"(?:uv run pytest|pytest)\s+([^\s]+)", body):
        signals.append({"type": "test_passes", "value": f"uv run pytest {m.group(1)} -x -q"})
    # If no signals, use first affected path
    if not signals and affected:
        for p in affected[:2]:
            if "(new)" in p or "new" in body.lower():
                signals.append({"type": "path_exists", "value": p.replace(" (new)", "")})
    return signals[:5]


def _parse_priority(priority_str: str | None) -> int:
    """Parse priority string into an integer, defaulting to 2."""
    if not priority_str:
        return 2
    try:
        m = re.match(r"(\d+)", priority_str)
        return int(m.group(1)) if m else 2
    except (ValueError, AttributeError):
        return 2


def _normalize_scope(scope: str | None) -> str:
    """Normalize scope to one of small/medium/large."""
    if not scope:
        return "medium"
    normalized = scope.lower().split()[0]
    return normalized if normalized in ("small", "medium", "large") else "medium"


def _determine_model(complexity: str, priority: int, role: str) -> str:
    """Determine model hint based on task attributes."""
    if role == "security":
        return "opus"
    if complexity.lower() == "high" or priority <= 1:
        return "sonnet"
    return "auto"


def convert_ticket(path: Path) -> None:
    """Convert a single ticket file to v1 format."""
    text = path.read_text(encoding="utf-8")

    # Skip if already has YAML frontmatter
    if text.startswith("---\n"):
        return

    # Extract title from first heading
    title_match = re.match(r"#\s+\S+\s*[—–-]\s*(.+)", text)
    title = title_match.group(1).strip() if title_match else path.stem

    # Extract ID from filename or heading
    id_match = re.match(r"#\s+(\S+)", text)
    ticket_id = id_match.group(1) if id_match else path.stem.split("-")[0]

    # Extract metadata fields
    priority_str = _extract_field(text, "Priority")
    scope = _normalize_scope(_extract_field(text, "Scope") or "medium")
    complexity = _extract_field(text, "Complexity") or COMPLEXITY_FROM_SCOPE.get(scope, "medium")
    role = (_extract_field(text, "Role") or "backend").lower().split()[0]
    progress = _extract_field(text, "Progress")
    priority = _parse_priority(priority_str)

    # Extract sections
    sections = _extract_body_sections(text)

    # Build body parts
    summary = sections.get("Problem", sections.get("Summary", ""))
    if not summary:
        # Use first paragraph after metadata
        body_start = text.find("\n\n", text.find("**"))
        if body_start > 0:
            next_section = text.find("\n##", body_start)
            if next_section > 0:
                summary = text[body_start:next_section].strip()
            else:
                summary = text[body_start : body_start + 500].strip()

    dod = sections.get("Acceptance Criteria", sections.get("Objective & Definition of Done", ""))
    steps = sections.get("Implementation", sections.get("Steps", ""))
    whats_done = sections.get("What's Done", "")
    remaining = sections.get("Remaining Work", "")
    risks = sections.get("Risks & Edge Cases", sections.get("Risks", ""))
    best_approach = sections.get("Best Approach", "")
    tests = sections.get("Tests & Verification", "")
    agent_notes = sections.get("Agent Notes", "")

    # Guess affected paths
    affected = _guess_affected_paths(text)

    # Build janitor signals
    janitor = _guess_janitor_signals(ticket_id, affected, text)

    # Determine tags, type
    tags = _get_tags(ticket_id)
    ticket_type = _get_type(ticket_id, role)

    # Determine status
    status = "in_progress" if progress else "open"

    # Determine model hint
    model = _determine_model(complexity, priority, role)

    # Estimated minutes
    est_min = MINUTES_FROM_SCOPE.get(scope, 45)

    # Determine if review required
    require_review = role in ("security", "architect") or priority <= 1
    require_human = priority == 0  # P0 = critical, needs human

    # Build YAML frontmatter
    deps_str = "[]"
    blocks_str = "[]"
    tags_str = str(tags).replace("'", '"')
    context_str = "[]"

    # Affected paths — inline [] if empty, block list if not
    if affected:
        affected_lines = ["affected_paths:"] + [f'  - "{p}"' for p in affected]
        affected_block = "\n".join(affected_lines)
    else:
        affected_block = "affected_paths: []"

    # Janitor signals — inline [] if empty, block list if not
    if janitor:
        janitor_lines = ["janitor_signals:"]
        for s in janitor:
            janitor_lines.append(f"  - type: {s['type']}")
            janitor_lines.append(f'    value: "{s["value"]}"')
        janitor_block = "\n".join(janitor_lines)
    else:
        janitor_block = "janitor_signals: []"

    frontmatter = f'''---
id: "{ticket_id}"
title: "{title.replace('"', "'")}"
status: {status}
type: {ticket_type}
priority: {priority}
scope: {scope}
complexity: {complexity.lower()}
role: {role}
model: {model}
effort: normal
estimated_minutes: {est_min}
depends_on: {deps_str}
blocks: {blocks_str}
tags: {tags_str}
{janitor_block}
context_files: {context_str}
{affected_block}
max_tokens: null
require_review: {"true" if require_review else "false"}
require_human_approval: {"true" if require_human else "false"}
---'''

    # Build markdown body
    body_parts = [frontmatter, ""]

    # Summary
    body_parts.append("## Summary")
    body_parts.append("")
    body_parts.append(summary if summary else f"Implement {title}.")
    body_parts.append("")

    # What's Done (if partial)
    if whats_done:
        body_parts.append("## What's Done")
        body_parts.append("")
        body_parts.append(whats_done)
        body_parts.append("")

    # Remaining Work (if partial)
    if remaining:
        body_parts.append("## Remaining Work")
        body_parts.append("")
        body_parts.append(remaining)
        body_parts.append("")

    # DoD
    body_parts.append("## Objective & Definition of Done")
    body_parts.append("")
    if dod:
        body_parts.append(dod)
    else:
        body_parts.append(f"- [ ] {title} implemented and working")
        body_parts.append("- [ ] Unit tests pass")
        body_parts.append("- [ ] Ruff lint clean")
    body_parts.append("")

    # Steps
    body_parts.append("## Steps")
    body_parts.append("")
    if steps:
        body_parts.append(steps)
    else:
        body_parts.append("1. Read relevant source files")
        body_parts.append("2. Implement the feature")
        body_parts.append("3. Add/update unit tests")
        body_parts.append("4. Run `uv run ruff check src/` and `uv run pytest tests/unit/ -x -q`")
    body_parts.append("")

    # Affected Paths
    if affected:
        body_parts.append("## Affected Paths")
        body_parts.append("")
        for p in affected:
            body_parts.append(f"- `{p}`")
        body_parts.append("")

    # Tests
    body_parts.append("## Tests & Verification")
    body_parts.append("")
    if tests:
        body_parts.append(tests)
    else:
        body_parts.append("- `uv run ruff check src/bernstein/`")
        body_parts.append("- `uv run pytest tests/unit/ -x -q`")
    body_parts.append("")

    # Risks
    if risks:
        body_parts.append("## Risks & Edge Cases")
        body_parts.append("")
        body_parts.append(risks)
        body_parts.append("")

    # Best Approach
    if best_approach:
        body_parts.append("## Best Approach")
        body_parts.append("")
        body_parts.append(best_approach)
        body_parts.append("")

    # Agent Notes
    body_parts.append("## Agent Notes")
    body_parts.append("")
    if agent_notes:
        body_parts.append(agent_notes)
    else:
        body_parts.append("<!-- Reserved for implementing agent -->")
    body_parts.append("")

    # Write
    path.write_text("\n".join(body_parts), encoding="utf-8")


def main() -> None:
    tickets = sorted(BACKLOG.glob("*.md"))
    converted = 0
    skipped = 0
    for t in tickets:
        text = t.read_text(encoding="utf-8")
        if text.startswith("---\n"):
            skipped += 1
            continue
        convert_ticket(t)
        converted += 1
    print(f"Converted {converted} tickets, skipped {skipped} (already v1)")


if __name__ == "__main__":
    main()
