"""Output style customization -- load per-project agent output format preferences.

Reads style definitions from ``.bernstein/output-styles/`` (Markdown files
with YAML frontmatter) and produces a combined style prompt appended to the
agent system instructions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


@dataclass
class OutputStyle:
    """A single output style definition loaded from a markdown file."""

    name: str
    description: str = ""
    keep_coding_instructions: bool = True
    suppress_progress: bool = False
    terse_mode: bool = False

    def render_prompt(self) -> str:
        """Return the style prompt fragment to inject into agent system prompts."""
        parts: list[str] = [f"Output style: {self.name}"]
        if self.description:
            parts.append(self.description)
        if not self.keep_coding_instructions:
            parts.append("Do NOT include coding instructions in output.")
        if self.suppress_progress:
            parts.append("Suppress incremental progress indicators.")
        if self.terse_mode:
            parts.append("Use terse/concise output format.")
        return " ".join(parts)


@dataclass
class StyleConfig:
    """Container for all loaded output styles."""

    active_style: OutputStyle | None = None
    available: list[OutputStyle] = field(default_factory=lambda: [])

    def get_prompt(self) -> str:
        """Return the combined style prompt, or empty string if no active style."""
        if self.active_style is None:
            return ""
        return self.active_style.render_prompt()


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

_DEFAULT_FILES = ["compact.md", "terse.md", "detailed.md"]


def _parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Split YAML frontmatter from markdown body.

    Args:
        content: Raw file content.

    Returns:
        Tuple of (parsed_yaml_dict, body_string).
    """
    stripped = content.strip()
    if not stripped.startswith("---"):
        return {}, stripped

    parts = stripped.split("---", 2)
    if len(parts) < 3:
        return {}, stripped

    fm_text = parts[1].strip()
    body = parts[2].strip()

    if yaml is None:
        logger.warning("PyYAML not installed; cannot parse output style frontmatter")
        return {}, body

    try:
        raw = yaml.safe_load(fm_text)
    except Exception:
        logger.warning("Invalid YAML frontmatter in output style")
        raw = {}

    data: dict[str, Any] = cast("dict[str, Any]", raw) if isinstance(raw, dict) else {}
    return data, body


def load_style(path: Path) -> OutputStyle | None:
    """Load a single output style from a markdown file.

    Args:
        path: Path to the .md file.

    Returns:
        OutputStyle instance, or None if the file cannot be read.
    """
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None

    fm, _body = _parse_frontmatter(content)
    name = str(fm.get("name", path.stem))
    if not name.strip():
        return None

    return OutputStyle(
        name=name.strip(),
        description=str(fm.get("description", "")),
        keep_coding_instructions=bool(fm.get("keep_coding_instructions", True)),
        suppress_progress=bool(fm.get("suppress_progress", False)),
        terse_mode=bool(fm.get("terse_mode", False)),
    )


def _collect_styles(styles_dir: Path) -> list[OutputStyle]:
    """Load styles from default files first, then any extras, deduped by name."""
    seen: set[str] = set()
    result: list[OutputStyle] = []

    for default_name in _DEFAULT_FILES:
        path = styles_dir / default_name
        if path.is_file():
            style = load_style(path)
            if style is not None:
                result.append(style)
                seen.add(style.name.lower())

    for path in sorted(styles_dir.glob("*.md")):
        style = load_style(path)
        if style is not None and style.name.lower() not in seen:
            result.append(style)
            seen.add(style.name.lower())

    return result


def _read_preferred_style_name(project_dir: Path) -> str | None:
    """Read the preferred output style name from bernstein.yaml, if present."""
    if yaml is None:
        return None
    yaml_path = project_dir / "bernstein.yaml"
    if not yaml_path.exists():
        return None
    try:
        raw_yaml = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        data: dict[str, Any] = cast("dict[str, Any]", raw_yaml) if isinstance(raw_yaml, dict) else {}
        if "output_style" in data:
            return str(data["output_style"]).lower()
    except Exception:
        pass  # Config read failed
    return None


def _find_style_by_name(available: list[OutputStyle], name: str) -> OutputStyle | None:
    """Find a style by case-insensitive name match."""
    for s in available:
        if s.name.lower() == name:
            return s
    return None


def _resolve_active_style(
    project_dir: Path,
    available: list[OutputStyle],
) -> OutputStyle | None:
    """Determine the active style: explicit bernstein.yaml pref, or first available."""
    if not available:
        return None

    preferred = _read_preferred_style_name(project_dir)
    if preferred is not None:
        match = _find_style_by_name(available, preferred)
        if match is not None:
            return match

    return available[0]


def load_output_styles(project_dir: Path) -> StyleConfig:
    """Load all output styles from .bernstein/output-styles/.

    Args:
        project_dir: Project root directory.

    Returns:
        StyleConfig with available styles and active style (if any).
    """
    styles_dir = project_dir / ".bernstein" / "output-styles"
    config = StyleConfig()

    if not styles_dir.is_dir():
        return config

    config.available = _collect_styles(styles_dir)
    config.active_style = _resolve_active_style(project_dir, config.available)

    return config
