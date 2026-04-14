"""Auto-generate the Module map section of AGENTS.md from the codebase.

Usage:
    uv run python scripts/gen_agents_md.py --check   # exit 1 if stale
    uv run python scripts/gen_agents_md.py --update  # rewrite module map in place
    uv run python scripts/gen_agents_md.py           # print generated section to stdout

The script scans src/bernstein/ with ast to extract one-line module
docstrings, groups them by package, then replaces the section of AGENTS.md
between the "## Module map" heading and the next "---" separator.
All other content (naming conventions, test patterns, gotchas, etc.) is
preserved verbatim.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_INIT_PY = "__init__.py"

# ---------------------------------------------------------------------------
# Package metadata — human-curated package descriptions (not auto-derived)
# ---------------------------------------------------------------------------

PACKAGE_META: dict[str, str] = {
    "core": "orchestration engine",
    "adapters": "CLI agent adapters",
    "agents": "agent catalog & discovery",
    "cli": "Click CLI",
    "evolution": "self-evolution engine",
    "eval": "evaluation harness",
    "plugins": "plugin system (pluggy)",
    "tui": "Textual TUI",
    "github_app": "GitHub App integration",
    "mcp": "MCP server",
    "benchmark": "SWE-bench",
}

# Packages whose __init__.py should NOT appear as a row (no meaningful content)
SKIP_INIT = {
    "core",
    "adapters",
    "agents",
    "cli",
    "evolution",
    "eval",
    "plugins",
    "tui",
    "github_app",
    "mcp",
    "benchmark",
}

# Files to skip entirely (test artefacts, generated, pure re-exports with no description)
SKIP_FILES = {"__pycache__", "__main__.py"}

# Ordering override for core/ (rest appear alphabetically after these)
CORE_PINNED_ORDER = [
    "models.py",
    "server.py",
    "orchestrator.py",
    "tick_pipeline.py",
    "task_lifecycle.py",
    "agent_lifecycle.py",
    "spawner.py",
    "router.py",
    "janitor.py",
    "context.py",
]

# Multi-file row overrides: when several files share one conceptual row
MULTI_FILE_ROWS: dict[str, list[str]] = {
    "store.py": ["store.py", "store_redis.py", "store_postgres.py"],
}
SKIP_IN_MULTI: set[str] = {"store_redis.py", "store_postgres.py"}

# Non-package directories to document (relative to repo root)
NON_PACKAGE_DIRS: list[tuple[str, str]] = [
    ("templates/roles/", "Jinja2 role prompts (manager, backend, qa, security, devops, etc.)"),
    ("templates/prompts/", "Prompt templates (judge.md, etc.) — bundled into wheel"),
    (".sdd/", "All runtime state (never commit `.sdd/runtime/`)"),
    (".sdd/backlog/open/", "YAML task specs waiting to be picked up"),
    (".sdd/backlog/claimed/", "Tasks currently being worked"),
    (".sdd/backlog/closed/", "Completed/cancelled tasks"),
    (".sdd/runtime/", "PIDs, logs, session state, signal files"),
    (".sdd/metrics/", "JSONL metric records"),
    (".sdd/traces/", "JSONL agent traces"),
    (".sdd/agents/catalog.json", "Registered agent catalog"),
    ("tests/unit/", "Fast unit tests (no network)"),
    ("tests/integration/", "Integration tests (require running server)"),
    ("scripts/run_tests.py", "Per-file isolated test runner"),
]

# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent
SRC_ROOT = REPO_ROOT / "src" / "bernstein"
AGENTS_MD = REPO_ROOT / "AGENTS.md"


def _first_docstring_line(path: Path) -> str:
    """Return the first non-empty line of the module docstring, or ''."""
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (SyntaxError, UnicodeDecodeError):
        return ""
    doc = ast.get_docstring(tree)
    if not doc:
        return ""
    for line in doc.splitlines():
        stripped = line.strip()
        if stripped:
            # Strip trailing period for table cleanliness
            return stripped.rstrip(".")
    return ""


def _module_display(path: Path, package_dir: Path) -> str:
    """Return the display name for a module file (e.g. 'models.py' or 'routes/')."""
    rel = path.relative_to(package_dir)
    parts = rel.parts
    if len(parts) == 1:
        return parts[0]
    # sub-package: show as 'routes/'
    return parts[0] + "/"


def _get_ordered_files(pkg_dir: Path, package: str) -> list[Path]:
    """Return Python files in the package, with core files pinned first."""
    if package == "core":
        pinned = [f for f in CORE_PINNED_ORDER if (pkg_dir / f).exists()]
        rest = sorted(
            f.name
            for f in pkg_dir.iterdir()
            if f.is_file()
            and f.suffix == ".py"
            and f.name not in pinned
            and f.name not in SKIP_FILES
            and (package != "core" or f.name != _INIT_PY)
        )
        return [pkg_dir / name for name in (pinned + rest)]
    return sorted(f for f in pkg_dir.iterdir() if f.is_file() and f.suffix == ".py" and f.name not in SKIP_FILES)


def _collect_subpackage_rows(pkg_dir: Path) -> list[tuple[str, str]]:
    """Collect rows for sub-packages (directories) in a package."""
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for subdir in sorted(pkg_dir.iterdir()):
        if not subdir.is_dir() or subdir.name.startswith("_") or subdir.name in seen:
            continue
        seen.add(subdir.name)
        init = subdir / _INIT_PY
        desc = _first_docstring_line(init) if init.exists() else f"{subdir.name}/ sub-package"
        py_names = sorted(f.stem for f in subdir.glob("*.py") if not f.name.startswith("_"))
        if py_names and not desc:
            desc = f"Sub-package: {', '.join(py_names)}"
        elif py_names and desc:
            desc += f" ({', '.join(py_names)}.py)"
        rows.append((f"`{subdir.name}/`", desc))
    return rows


def _collect_package(package: str) -> list[tuple[str, str]]:
    """Collect (display_name, description) rows for one top-level package."""
    pkg_dir = SRC_ROOT / package
    if not pkg_dir.is_dir():
        return []

    rows: list[tuple[str, str]] = []
    all_files = _get_ordered_files(pkg_dir, package)

    for py_file in all_files:
        fname = py_file.name
        if fname == _INIT_PY and package in SKIP_INIT:
            continue
        if fname in SKIP_IN_MULTI:
            continue
        if fname in MULTI_FILE_ROWS:
            display = " / ".join(f"`{f}`" for f in MULTI_FILE_ROWS[fname])
            rows.append((display, _first_docstring_line(py_file)))
            continue
        rows.append((f"`{fname}`", _first_docstring_line(py_file)))

    rows.extend(_collect_subpackage_rows(pkg_dir))
    return rows


def _render_table(rows: list[tuple[str, str]]) -> str:
    """Render a two-column markdown table."""
    if not rows:
        return ""
    col1_w = max(len(r[0]) for r in rows)
    col1_w = max(col1_w, 4)  # minimum "File"
    lines = [
        f"| {'File':<{col1_w}} | Purpose |",
        f"|{'-' * (col1_w + 2)}|---------|",
    ]
    for name, purpose in rows:
        lines.append(f"| {name:<{col1_w}} | {purpose} |")
    return "\n".join(lines)


def _render_non_package_table(rows: list[tuple[str, str]]) -> str:
    col1_w = max(len(r[0]) for r in rows)
    col1_w = max(col1_w, 4)
    lines = [
        f"| {'Path':<{col1_w}} | Purpose |",
        f"|{'-' * (col1_w + 2)}|---------|",
    ]
    for path, purpose in rows:
        lines.append(f"| `{path}`{' ' * (col1_w - len(path))} | {purpose} |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Module map generation
# ---------------------------------------------------------------------------


def generate_module_map() -> str:
    """Return the full '## Module map' section as a string."""
    sections: list[str] = [
        "## Module map\n",
        "<!-- AUTO-GENERATED: run `uv run python scripts/gen_agents_md.py --update` to refresh -->\n",
    ]

    package_order = list(PACKAGE_META.keys())

    for pkg in package_order:
        pkg_dir = SRC_ROOT / pkg
        if not pkg_dir.is_dir():
            continue
        meta = PACKAGE_META[pkg]
        rows = _collect_package(pkg)
        if not rows:
            continue
        sections.append(f"\n### `src/bernstein/{pkg}/` — {meta}\n")
        sections.append("\n" + _render_table(rows) + "\n")

    sections.append("\n### Key non-package directories\n")
    sections.append("\n" + _render_non_package_table(NON_PACKAGE_DIRS) + "\n")

    return "".join(sections)


# ---------------------------------------------------------------------------
# AGENTS.md update
# ---------------------------------------------------------------------------

_MODULE_MAP_HEADING = "## Module map"
_SECTION_SEP = "\n---\n"


def _split_agents_md(text: str) -> tuple[str, str, str] | None:
    """Split AGENTS.md into (before_module_map, module_map_body, after_module_map).

    Returns None if the module map section cannot be found.
    """
    start = text.find(_MODULE_MAP_HEADING)
    if start == -1:
        return None

    # Find the "---" separator that ends the module map section
    sep_pos = text.find(_SECTION_SEP, start + len(_MODULE_MAP_HEADING))
    if sep_pos == -1:
        # Module map goes to end of file
        return text[:start], text[start:], ""

    before = text[:start]
    body = text[start:sep_pos]
    after = text[sep_pos:]
    return before, body, after


def update_agents_md(dry_run: bool = False) -> bool:
    """Rewrite the module map section of AGENTS.md.

    Returns True if the file was (or would be) changed.
    """
    old_text = AGENTS_MD.read_text(encoding="utf-8")
    parts = _split_agents_md(old_text)
    if parts is None:
        print("ERROR: Could not find '## Module map' section in AGENTS.md", file=sys.stderr)
        return False

    before, _old_body, after = parts
    new_body = generate_module_map()
    new_text = before + new_body + after

    changed = new_text != old_text
    if changed and not dry_run:
        AGENTS_MD.write_text(new_text, encoding="utf-8")
        print("AGENTS.md updated.")
    elif not changed:
        print("AGENTS.md is already up to date.")
    return changed


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    args = sys.argv[1:]

    if "--update" in args:
        update_agents_md(dry_run=False)
        return 0

    if "--check" in args:
        changed = update_agents_md(dry_run=True)
        if changed:
            print(
                "AGENTS.md module map is stale. Run:\n  uv run python scripts/gen_agents_md.py --update",
                file=sys.stderr,
            )
            return 1
        return 0

    # Default: print generated section to stdout
    print(generate_module_map())
    return 0


if __name__ == "__main__":
    sys.exit(main())
