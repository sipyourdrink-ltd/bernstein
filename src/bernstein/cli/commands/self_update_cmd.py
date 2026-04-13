"""Self-update command — upgrade or rollback Bernstein via PyPI.

Commands:
  bernstein self-update              Check PyPI and upgrade if newer version exists.
  bernstein self-update --check      Show latest version without installing.
  bernstein self-update --rollback   Revert to the previously installed version.
"""

from __future__ import annotations

import json
import subprocess
import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path

import click
from rich.panel import Panel
from rich.table import Table

from bernstein.cli.helpers import console

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PACKAGE_NAME: str = "bernstein"
_PYPI_URL: str = "https://pypi.org/pypi/bernstein/json"
_GITHUB_RELEASES_URL: str = "https://api.github.com/repos/chernistry/bernstein/releases"
_PREV_VERSION_FILE: Path = Path.home() / ".bernstein" / "previous-version"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_installed_version() -> str:
    """Return the currently installed Bernstein version.

    Returns:
        Version string, or ``"unknown"`` when the package metadata is absent.
    """
    try:
        return _pkg_version(_PACKAGE_NAME)
    except PackageNotFoundError:
        return "unknown"


def _fetch_latest_pypi_version() -> str | None:
    """Query PyPI for the latest released version of Bernstein.

    Returns:
        Latest version string, or ``None`` on network/parse failure.
    """
    import urllib.error
    import urllib.request

    try:
        req = urllib.request.Request(
            _PYPI_URL,
            headers={"Accept": "application/json", "User-Agent": f"bernstein/{_get_installed_version()}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data: dict[str, object] = json.loads(resp.read())
        info = data.get("info")
        if isinstance(info, dict):
            ver = info.get("version")
            if isinstance(ver, str):
                return ver
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        pass
    return None


def _parse_version(ver: str) -> tuple[int, ...]:
    """Parse a PEP-440 version string into a comparable tuple.

    Args:
        ver: Version string, e.g. ``"1.2.3"``.

    Returns:
        Integer tuple, e.g. ``(1, 2, 3)``.  Non-numeric segments become 0.
    """
    parts: list[int] = []
    for segment in ver.split(".")[:4]:
        try:
            parts.append(int(segment))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _fetch_changelog(current: str, latest: str) -> list[str]:
    """Fetch GitHub release notes between *current* and *latest*.

    Args:
        current: Installed version string.
        latest: Target version string.

    Returns:
        List of ``"vX.Y.Z: <body excerpt>"`` strings, newest first.
        Returns an empty list on failure or when no relevant releases exist.
    """
    import urllib.error
    import urllib.request

    try:
        req = urllib.request.Request(
            _GITHUB_RELEASES_URL,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"bernstein/{current}",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            releases: list[dict[str, object]] = json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        return []

    current_tuple = _parse_version(current)
    latest_tuple = _parse_version(latest)

    entries: list[str] = []
    for release in releases:
        tag: str = str(release.get("tag_name", "")).lstrip("v")
        body: str = str(release.get("body", "")).strip()
        if not tag:
            continue
        tag_tuple = _parse_version(tag)
        if current_tuple < tag_tuple <= latest_tuple:
            # Truncate long release notes for readability
            excerpt = body[:300] + ("…" if len(body) > 300 else "")
            entries.append(f"[bold]v{tag}[/bold]\n{excerpt}" if excerpt else f"[bold]v{tag}[/bold]")

    return entries


def _save_previous_version(ver: str) -> None:
    """Write *ver* to the previous-version file for future rollback.

    Args:
        ver: Version string to persist.
    """
    _PREV_VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PREV_VERSION_FILE.write_text(ver.strip())


def _read_previous_version() -> str | None:
    """Read the previously installed version from disk.

    Returns:
        Version string, or ``None`` when no rollback point is stored.
    """
    if not _PREV_VERSION_FILE.exists():
        return None
    text = _PREV_VERSION_FILE.read_text().strip()
    return text if text else None


def _pip_install(spec: str) -> bool:
    """Run ``pip install <spec>`` in a subprocess.

    Args:
        spec: Package spec passed to pip, e.g. ``"bernstein==1.2.3"``.

    Returns:
        ``True`` on success, ``False`` on failure.
    """
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", spec, "--quiet"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        console.print(f"[red]pip error:[/red]\n{result.stderr.strip()}")
        return False
    return True


# ---------------------------------------------------------------------------
# Click command
# ---------------------------------------------------------------------------


@click.command("self-update")
@click.option(
    "--check",
    "check_only",
    is_flag=True,
    default=False,
    help="Show the latest available version without installing.",
)
@click.option(
    "--rollback",
    "rollback",
    is_flag=True,
    default=False,
    help="Revert to the previously installed version.",
)
@click.option(
    "--yes",
    "-y",
    "auto_yes",
    is_flag=True,
    default=False,
    help="Skip confirmation prompt.",
)
def self_update_cmd(check_only: bool, rollback: bool, auto_yes: bool) -> None:
    """Upgrade Bernstein to the latest version from PyPI.

    \b
      bernstein self-update             Upgrade to latest
      bernstein self-update --check     Show latest version (no install)
      bernstein self-update --rollback  Revert to previous version
      bernstein self-update -y          Upgrade without prompt
    """
    if rollback:
        _do_rollback()
        return

    current = _get_installed_version()

    console.print(f"[dim]Checking PyPI for {_PACKAGE_NAME} updates…[/dim]")
    latest = _fetch_latest_pypi_version()

    if latest is None:
        console.print("[yellow]Could not reach PyPI. Check your internet connection and try again.[/yellow]")
        raise SystemExit(1)

    _print_version_table(current, latest)

    if check_only:
        return

    if _parse_version(current) >= _parse_version(latest):
        console.print("[green]You're up to date![/green]")
        return

    # Show changelog diff
    console.print("\n[bold]Changelog[/bold]")
    entries = _fetch_changelog(current, latest)
    if entries:
        for entry in entries:
            console.print(Panel(entry, border_style="dim", expand=False))
    else:
        console.print("[dim]  (No changelog available from GitHub releases)[/dim]")

    console.print()
    if not auto_yes and not click.confirm(f"Upgrade {_PACKAGE_NAME} {current} → {latest}?"):
        console.print("[dim]Update cancelled.[/dim]")
        return

    # Persist the current version for rollback
    if current != "unknown":
        _save_previous_version(current)
        console.print(f"[dim]Previous version saved to {_PREV_VERSION_FILE}[/dim]")

    console.print(f"[cyan]Installing {_PACKAGE_NAME}=={latest}…[/cyan]")
    ok = _pip_install(f"{_PACKAGE_NAME}=={latest}")

    if ok:
        console.print(f"[bold green]Successfully upgraded to {_PACKAGE_NAME} {latest}[/bold green]")
        console.print("[dim]Restart your shell or run `bernstein --version` to confirm.[/dim]")
    else:
        console.print("[bold red]Upgrade failed.[/bold red] See pip output above.")
        raise SystemExit(1)


def _print_version_table(current: str, latest: str) -> None:
    """Render a version comparison table to the console.

    Args:
        current: Installed version string.
        latest: Latest available version string.
    """
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Label", style="dim", no_wrap=True, min_width=14)
    table.add_column("Version")
    table.add_row("Installed", current)
    table.add_row("Latest", latest)
    console.print(table)


def _do_rollback() -> None:
    """Revert to the previously installed version."""
    prev = _read_previous_version()
    if prev is None:
        console.print(
            Panel(
                "[yellow]No previous version found.[/yellow]\n"
                f"[dim]Rollback history is stored in {_PREV_VERSION_FILE}[/dim]",
                border_style="yellow",
                expand=False,
            )
        )
        raise SystemExit(1)

    current = _get_installed_version()
    console.print(f"[dim]Rolling back:[/dim] {current} → {prev}")

    ok = _pip_install(f"{_PACKAGE_NAME}=={prev}")
    if ok:
        # Clear the rollback file after a successful rollback
        _PREV_VERSION_FILE.unlink(missing_ok=True)
        console.print(f"[bold green]Rolled back to {_PACKAGE_NAME} {prev}[/bold green]")
        console.print("[dim]Restart your shell or run `bernstein --version` to confirm.[/dim]")
    else:
        console.print("[bold red]Rollback failed.[/bold red] See pip output above.")
        raise SystemExit(1)
