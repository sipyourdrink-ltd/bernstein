"""Plugin trust checking and risk scoring for Bernstein plugins."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

if TYPE_CHECKING:
    from pathlib import Path

RISK_LEVEL = Literal["trusted", "verified", "community", "unknown"]
_PLUGIN_PY = "plugin.py"
_README_NAMES = ("README.md", "README.rst", "README.txt")
_PYPROJECT_TOML = "pyproject.toml"
_MANIFEST_FIELDS = frozenset({"name", "version", "author"})


@dataclass(frozen=True)
class PluginTrust:
    """Trust assessment for a Bernstein plugin.

    Attributes:
        plugin_name: Plugin display name, extracted from metadata or directory name.
        risk_level: One of ``"trusted"``, ``"verified"``, ``"community"``, or ``"unknown"``.
        signed: Whether the plugin has a valid cryptographic signature.
        source_verified: Whether the source code includes verified provenance.
        has_readme: Whether a README file is present.
        has_tests: Whether a ``tests/`` directory or ``test_*.py`` files exist.
        trust_score: Integer 0-100 computed from trust signals.
    """

    plugin_name: str
    risk_level: RISK_LEVEL
    signed: bool
    source_verified: bool
    has_readme: bool
    has_tests: bool
    trust_score: int


def _discover_readme(plugin_dir: Path) -> Path | None:
    """Return the first README file found in *plugin_dir*."""
    for name in _README_NAMES:
        candidate = plugin_dir / name
        if candidate.is_file():
            return candidate
    return None


def _has_tests(plugin_dir: Path) -> bool:
    """Check whether *plugin_dir* contains test files."""
    tests_dir = plugin_dir / "tests"
    if tests_dir.is_dir():
        return True
    return bool(list(plugin_dir.rglob("test_*.py")))


def _compute_signature_fingerprint(plugin_dir: Path) -> str | None:
    """Check for a ``.signature`` file and return its hex digest if present."""
    sig_path = plugin_dir / ".signature"
    if sig_path.is_file():
        return hashlib.sha256(sig_path.read_bytes()).hexdigest()
    return None


def _has_pyproject_metadata(plugin_dir: Path) -> bool:
    """Check whether ``pyproject.toml`` has at minimum ``name``, ``version`` and ``author``.

    Uses a text-based heuristic to avoid ``tomllib`` type-stub issues in strict
    Pyright mode.
    """
    pyproject = plugin_dir / _PYPROJECT_TOML
    if not pyproject.is_file():
        return False

    content = pyproject.read_text(encoding="utf-8")

    # Search for required fields in ``[project]`` or ``[tool.bernstein]`` sections.
    sections = ("[project]", "[tool.bernstein]")
    for marker in sections:
        idx = content.find(marker)
        if idx < 0:
            continue
        rest = content[idx + len(marker) :]
        end = rest.find("\n[")
        section_text = rest[:end] if end >= 0 else rest
        if all(field in section_text for field in _MANIFEST_FIELDS):
            return True
    return False


def _derive_risk_level(
    *,
    signed: bool,
    source_verified: bool,
    has_readme: bool,
    has_tests: bool,
    has_pyproject: bool,
) -> RISK_LEVEL:
    """Assign a risk level based on available trust signals."""
    if signed and source_verified and has_tests:
        return "trusted"
    if (signed or source_verified) and has_readme:
        return "verified"
    if has_readme or has_tests or has_pyproject:
        return "community"
    return "unknown"


def _compute_trust_score(
    *,
    signed: bool,
    source_verified: bool,
    has_readme: bool,
    has_tests: bool,
    has_pyproject: bool,
) -> int:
    """Return a 0-100 trust score from boolean signals."""
    score = 0
    if signed:
        score += 30
    if source_verified:
        score += 25
    if has_readme:
        score += 15
    if has_tests:
        score += 20
    if has_pyproject:
        score += 10
    return min(score, 100)


def check_plugin_trust(plugin_path: Path) -> PluginTrust:
    """Inspect *plugin_path* and return a :class:`PluginTrust` assessment.

    *plugin_path* may point to:
    - A plugin directory (containing ``plugin.py`` or ``__init__.py``)
    - A single ``plugin.py`` file

    Args:
        plugin_path: Path to the plugin directory or file to inspect.

    Returns:
        A frozen ``PluginTrust`` dataclass with trust signals populated.

    Raises:
        FileNotFoundError: If *plugin_path* does not exist.
    """
    if not plugin_path.exists():
        raise FileNotFoundError(f"Plugin path not found: {plugin_path}")

    if plugin_path.is_file():
        plugin_dir = plugin_path.parent
        display_name = plugin_path.stem
    else:
        plugin_dir = plugin_path
        display_name = plugin_path.name

    signed = _compute_signature_fingerprint(plugin_dir) is not None
    source_verified = _has_pyproject_metadata(plugin_dir)
    has_readme = _discover_readme(plugin_dir) is not None
    has_tests = _has_tests(plugin_dir)

    risk_level = _derive_risk_level(
        signed=signed,
        source_verified=source_verified,
        has_readme=has_readme,
        has_tests=has_tests,
        has_pyproject=source_verified,
    )
    trust_score = _compute_trust_score(
        signed=signed,
        source_verified=source_verified,
        has_readme=has_readme,
        has_tests=has_tests,
        has_pyproject=source_verified,
    )

    return PluginTrust(
        plugin_name=display_name,
        risk_level=risk_level,
        signed=signed,
        source_verified=source_verified,
        has_readme=has_readme,
        has_tests=has_tests,
        trust_score=trust_score,
    )


_YN_GREEN = "\u2713"  # green check mark
_YN_RED = "\u2717"  # red cross mark


def _yes_no(value: bool) -> str:
    """Return a Rich-styled yes/no indicator."""
    if value:
        return f"[green]{_YN_GREEN}[/green]"
    return f"[red]{_YN_RED}[/red]"


def format_trust_warning(trust: PluginTrust) -> str:
    """Return a Rich-formatted warning string for *trust*.

    Uses ANSI escape sequences produced by Rich :class:`Panel` so the result
    is ready to print to a TTY.

    Args:
        trust: The ``PluginTrust`` assessment to format.

    Returns:
        A string containing the Rich-formatted panel (includes escape codes).
    """
    console = Console(force_terminal=True, width=60)

    color_map: dict[RISK_LEVEL, str] = {
        "trusted": "green",
        "verified": "blue",
        "community": "yellow",
        "unknown": "red",
    }
    border_color = color_map.get(trust.risk_level, "white")

    lines: list[str] = [
        f"[bold]Plugin:[/bold] {trust.plugin_name}",
        f"[bold]Risk level:[/bold] [{border_color}]{trust.risk_level}[/{border_color}]",
        f"[bold]Trust score:[/bold] {trust.trust_score}/100",
        "",
        "[bold]Signals:[/bold]",
        f"  Cryptographic signature: {_yes_no(trust.signed)}",
        f"  Source verified:         {_yes_no(trust.source_verified)}",
        f"  README present:          {_yes_no(trust.has_readme)}",
        f"  Tests present:           {_yes_no(trust.has_tests)}",
    ]

    if trust.risk_level in ("unknown", "community"):
        lines.append("")
        lines.append(
            "[bold red]WARNING:[/bold red] This plugin has limited trust signals. Review the source before installing."
        )

    text = Text.from_markup("\n".join(lines))
    panel = Panel(
        text,
        title="[bold yellow]Plugin Trust Assessment[/bold yellow]",
        border_style=border_color,
    )

    with console.capture() as capture:
        console.print(panel)
    return capture.get()
