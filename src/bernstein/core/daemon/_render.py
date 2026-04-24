"""Shared template-rendering helpers for the daemon installer."""

from __future__ import annotations

from pathlib import Path
from string import Template

_TEMPLATES_DIR = Path(__file__).parent / "templates"

__all__ = ["load_template", "render_template"]


def load_template(name: str) -> Template:
    """Load a template file from the package ``templates/`` directory.

    Args:
        name: File name relative to ``templates/`` (for example,
            ``"systemd-user.service.template"``).

    Returns:
        A ``string.Template`` built from the file's contents.
    """
    text = (_TEMPLATES_DIR / name).read_text(encoding="utf-8")
    return Template(text)


def render_template(name: str, mapping: dict[str, str]) -> str:
    """Render a template file with the provided substitutions.

    Args:
        name: Template file name.
        mapping: Placeholder-to-value map. Missing placeholders raise
            ``KeyError`` (strict rendering).

    Returns:
        The rendered template as a string.
    """
    return load_template(name).substitute(mapping)
