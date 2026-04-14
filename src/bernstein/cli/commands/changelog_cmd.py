"""changelog command — auto-generate a changelog from conventional commits."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import click
from rich.panel import Panel
from rich.text import Text

from bernstein.cli.helpers import console

# ---------------------------------------------------------------------------
# Conventional commit parser
# ---------------------------------------------------------------------------

_CONV_RE = re.compile(
    r"^(?P<type>feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)"
    r"(?:\((?P<scope>[^)]+)\))?"
    r"(?P<breaking>!)?"
    r":\s*(?P<desc>.+)$",
    re.IGNORECASE,
)

_TYPE_LABELS: dict[str, str] = {
    "feat": "Features",
    "fix": "Bug Fixes",
    "perf": "Performance",
    "refactor": "Refactoring",
    "docs": "Documentation",
    "build": "Build System",
    "ci": "CI/CD",
    "chore": "Chores",
    "test": "Tests",
    "style": "Style",
    "revert": "Reverts",
}

# Types included in a standard changelog (omit noise)
_NOTABLE_TYPES = {"feat", "fix", "perf", "refactor", "revert"}


@dataclass
class CommitEntry:
    """A single parsed commit."""

    sha: str
    subject: str
    author: str
    date: str
    commit_type: str
    scope: str | None
    description: str
    is_breaking: bool
    body: str = ""


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _run_git(args: list[str], cwd: Path) -> str:
    """Run a git command and return stdout, or empty string on error."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return ""


def _latest_tag(cwd: Path, pattern: str = "v*") -> str | None:
    """Return the most recent semver tag, or None if no tags exist."""
    out = _run_git(
        ["describe", "--tags", "--abbrev=0", "--match", pattern],
        cwd,
    )
    return out.strip() or None


def _commits_since(cwd: Path, since_ref: str | None, until_ref: str) -> list[CommitEntry]:
    """Fetch commits in [since_ref..until_ref] (or all if since_ref is None).

    Uses a delimiter-safe format: fields separated by ASCII unit-separator (\\x1f),
    records separated by ASCII record-separator (\\x1e).
    """
    fmt = "%H\x1f%s\x1f%an\x1f%ad\x1f%b\x1e"
    rev_range = f"{since_ref}..{until_ref}" if since_ref else until_ref

    raw = _run_git(
        ["log", rev_range, f"--pretty=format:{fmt}", "--date=short", "--no-merges"],
        cwd,
    )
    entries: list[CommitEntry] = []
    for record in raw.split("\x1e"):
        record = record.strip()
        if not record:
            continue
        parts = record.split("\x1f", 4)
        if len(parts) < 4:
            continue
        sha, subject, author, date = parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3].strip()
        body = parts[4].strip() if len(parts) > 4 else ""

        m = _CONV_RE.match(subject)
        if m:
            commit_type = m.group("type").lower()
            scope = m.group("scope")
            description = m.group("desc").strip()
            is_breaking = bool(m.group("breaking")) or "BREAKING CHANGE" in body
        else:
            commit_type = "chore"
            scope = None
            description = subject
            is_breaking = "BREAKING CHANGE" in body

        entries.append(
            CommitEntry(
                sha=sha,
                subject=subject,
                author=author,
                date=date,
                commit_type=commit_type,
                scope=scope,
                description=description,
                is_breaking=is_breaking,
                body=body,
            )
        )
    return entries


# ---------------------------------------------------------------------------
# Changelog formatters
# ---------------------------------------------------------------------------


_COMMIT_TYPE_ORDER = ["feat", "fix", "perf", "refactor", "docs", "build", "ci", "chore", "test", "style", "revert"]


def _group_entries(entries: list[CommitEntry]) -> tuple[dict[str, list[CommitEntry]], list[CommitEntry]]:
    """Group entries by commit type and collect breaking changes."""
    from collections import defaultdict

    grouped: dict[str, list[CommitEntry]] = defaultdict(list)
    breaking: list[CommitEntry] = []
    for e in entries:
        if e.is_breaking:
            breaking.append(e)
        grouped[e.commit_type].append(e)
    return grouped, breaking


def _format_keepachangelog(
    entries: list[CommitEntry],
    version: str,
    since_ref: str | None,
    _until_ref: str,
    repo_url: str | None,
) -> str:
    """Render entries in Keep a Changelog format."""
    grouped, breaking = _group_entries(entries)

    lines: list[str] = ["# Changelog", ""]

    date = entries[0].date if entries else ""
    compare_url = ""
    if repo_url and since_ref:
        compare_url = f" ({repo_url}/compare/{since_ref}...{version})"
    elif repo_url:
        compare_url = f" ({repo_url}/commits/{version})"

    lines.append(f"## [{version}] - {date}{compare_url}")
    lines.append("")

    if breaking:
        lines.append("### Breaking Changes")
        lines.append("")
        for e in breaking:
            scope_prefix = f"**{e.scope}:** " if e.scope else ""
            lines.append(f"- {scope_prefix}{e.description} ([`{e.sha[:8]}`])")
        lines.append("")

    for ctype in _COMMIT_TYPE_ORDER:
        type_entries = grouped.get(ctype, [])
        if not type_entries:
            continue
        label = _TYPE_LABELS.get(ctype, ctype.capitalize())
        lines.append(f"### {label}")
        lines.append("")
        for e in type_entries:
            scope_prefix = f"**{e.scope}:** " if e.scope else ""
            lines.append(f"- {scope_prefix}{e.description} ([`{e.sha[:8]}`])")
        lines.append("")

    return "\n".join(lines)


def _format_simple(
    entries: list[CommitEntry],
    version: str,
    _since_ref: str | None,
    _until_ref: str,
) -> str:
    """Render a simple plain-text changelog."""
    from collections import defaultdict

    grouped: dict[str, list[CommitEntry]] = defaultdict(list)
    for e in entries:
        grouped[e.commit_type].append(e)

    lines: list[str] = [f"## {version}"]
    lines.append("")

    for ctype in ["feat", "fix", "perf", "refactor", "docs", "build", "ci", "chore", "test", "style", "revert"]:
        type_entries = grouped.get(ctype, [])
        if not type_entries:
            continue
        label = _TYPE_LABELS.get(ctype, ctype.capitalize())
        lines.append(f"### {label}")
        for e in type_entries:
            scope_prefix = f"{e.scope}: " if e.scope else ""
            breaking_flag = " [BREAKING]" if e.is_breaking else ""
            lines.append(f"  - {scope_prefix}{e.description}{breaking_flag}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Rich console renderer
# ---------------------------------------------------------------------------


_CTYPE_COLORS: dict[str, str] = {"feat": "green", "fix": "yellow"}


def _write_changelog_file(output_path: str, text: str, entry_count: int, version_label: str) -> None:
    """Write changelog text to a file, prepending if an existing changelog header is found."""
    out = Path(output_path)
    if out.exists():
        existing = out.read_text()
        if existing.startswith("# Changelog"):
            title_end = existing.find("\n")
            new_content = existing[: title_end + 1] + "\n" + text.split("\n", 1)[1] + existing[title_end + 1 :]
            out.write_text(new_content)
        else:
            out.write_text(text)
    else:
        out.write_text(text)
    console.print(f"[green]Changelog written to[/green] [bold]{output_path}[/bold]")
    console.print(f"[dim]{entry_count} commit(s), version {version_label}[/dim]")


def _render_to_console(entries: list[CommitEntry], version: str, since_ref: str | None) -> None:
    """Print changelog to console with Rich formatting."""
    grouped, breaking = _group_entries(entries)

    range_label = f"{since_ref}..HEAD" if since_ref else "all commits"
    console.print()
    console.print(
        Panel(
            f"[bold]Changelog[/bold]  [dim]{version}[/dim]  [dim]({range_label})[/dim]",
            border_style="blue",
            expand=False,
        )
    )

    if breaking:
        console.print("\n[bold red]Breaking Changes[/bold red]")
        for e in breaking:
            scope_prefix = f"[cyan]{e.scope}:[/cyan] " if e.scope else ""
            console.print(f"  [red]![/red] {scope_prefix}{e.description}  [dim]{e.sha[:8]}[/dim]")

    for ctype in _COMMIT_TYPE_ORDER:
        type_entries = grouped.get(ctype, [])
        if not type_entries:
            continue
        label = _TYPE_LABELS.get(ctype, ctype.capitalize())
        color = _CTYPE_COLORS.get(ctype, "blue")
        console.print(f"\n[bold {color}]{label}[/bold {color}]")
        for e in type_entries:
            scope_prefix = f"[cyan]{e.scope}:[/cyan] " if e.scope else ""
            breaking_flag = Text(" [BREAKING]", style="bold red") if e.is_breaking else Text("")
            console.print(f"  • {scope_prefix}{e.description}  [dim]{e.sha[:8]}[/dim]{breaking_flag}")

    console.print()
    console.print(f"[dim]{len(entries)} commit(s) total[/dim]")
    console.print()


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


@click.command("changelog")
@click.option(
    "--since",
    "since_ref",
    default=None,
    metavar="REF",
    help="Start from this tag/commit (default: latest semver tag).",
)
@click.option(
    "--until",
    "until_ref",
    default="HEAD",
    show_default=True,
    metavar="REF",
    help="End at this ref.",
)
@click.option(
    "--version",
    "version_label",
    default=None,
    metavar="VER",
    help="Version label for this changelog section (default: next semver from commits).",
)
@click.option(
    "--all",
    "include_all",
    is_flag=True,
    default=False,
    help="Include all commits (ignore --since, start from repo beginning).",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["keepachangelog", "simple", "console"]),
    default="console",
    show_default=True,
    help="Output format.",
)
@click.option(
    "--output",
    "-o",
    "output_path",
    default=None,
    type=click.Path(dir_okay=False),
    help="Write output to this file (implies --format keepachangelog if not set).",
)
@click.option(
    "--repo-url",
    default=None,
    metavar="URL",
    help="Repository URL for comparison links (keepachangelog format).",
)
@click.option(
    "--workdir",
    default=".",
    show_default=True,
    type=click.Path(),
    help="Project root.",
)
def changelog_cmd(
    since_ref: str | None,
    until_ref: str,
    version_label: str | None,
    include_all: bool,
    fmt: str,
    output_path: str | None,
    repo_url: str | None,
    workdir: str,
) -> None:
    """Auto-generate a changelog from conventional commits.

    By default, shows commits since the latest semver tag. Use --since to
    specify a different starting point, or --all for the full history.

    \b
    Examples:
      bernstein changelog                         # since latest tag, console
      bernstein changelog --since v1.0.0          # since specific tag
      bernstein changelog --all                   # full history
      bernstein changelog -o CHANGELOG.md         # write to file
      bernstein changelog --format keepachangelog  # Markdown output to stdout
    """
    cwd = Path(workdir).resolve()

    # ------------------------------------------------------------------
    # 1. Resolve since_ref
    # ------------------------------------------------------------------
    effective_since: str | None
    if include_all:
        effective_since = None
    elif since_ref is not None:
        effective_since = since_ref
    else:
        effective_since = _latest_tag(cwd)
        if effective_since:
            console.print(f"[dim]Using latest tag: {effective_since}[/dim]")
        else:
            console.print("[dim]No tags found — showing all commits.[/dim]")

    # ------------------------------------------------------------------
    # 2. Fetch commits
    # ------------------------------------------------------------------
    entries = _commits_since(cwd, effective_since, until_ref)
    if not entries:
        console.print("[yellow]No commits found in the specified range.[/yellow]")
        return

    # ------------------------------------------------------------------
    # 3. Resolve version label
    # ------------------------------------------------------------------
    if version_label is None:
        from bernstein.core.git_basic import version_from_commits

        version_label = "v" + version_from_commits(cwd)

    # ------------------------------------------------------------------
    # 4. Determine format
    # ------------------------------------------------------------------
    effective_fmt = fmt
    if output_path and fmt == "console":
        effective_fmt = "keepachangelog"

    # ------------------------------------------------------------------
    # 5. Render
    # ------------------------------------------------------------------
    if effective_fmt == "console":
        _render_to_console(entries, version_label, effective_since)
        return

    if effective_fmt == "keepachangelog":
        text = _format_keepachangelog(entries, version_label, effective_since, until_ref, repo_url)
    else:  # simple
        text = _format_simple(entries, version_label, effective_since, until_ref)

    if output_path:
        _write_changelog_file(output_path, text, len(entries), version_label)
    else:
        click.echo(text)
