"""Bernstein — Declarative agent orchestration for engineering teams."""

from __future__ import annotations

from pathlib import Path

try:
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("bernstein")
except Exception:  # pragma: no cover — editable installs / bare checkout
    __version__ = "0.0.0"

_PACKAGE_DIR = Path(__file__).resolve().parent

# Bundled default templates — present inside the wheel after pip install.
# In dev/editable mode, fall back to <repo>/templates/ at the project root.
#
# The wheel ships the full tree under src/bernstein/_default_templates/ via
# hatch force-include (templates/prompts, templates/bernstein.yaml). In a
# source checkout only ascii_logo.md lives there directly, so the presence
# of that single file is not enough — probe for a real template subtree
# (prompts/) before deciding we're inside a wheel install. If not, fall
# back to <repo>/templates/ which contains the full dev copy.
_bundled_templates_dir = _PACKAGE_DIR / "_default_templates"
_dev_templates_dir = _PACKAGE_DIR.parent.parent / "templates"
if not (_bundled_templates_dir / "prompts").is_dir() and _dev_templates_dir.is_dir():
    _bundled_templates_dir = _dev_templates_dir

# Public access via uppercase constant
_BUNDLED_TEMPLATES_DIR = _bundled_templates_dir


def get_templates_dir(workdir: Path) -> Path:
    """Return the templates directory for a project, with bundled fallback.

    Checks ``workdir / "templates"`` first; falls back to the package's
    bundled defaults so that ``bernstein`` works right after ``pip install``
    without requiring ``bernstein init`` first.
    """
    local = workdir / "templates"
    if local.is_dir():
        return local
    return _BUNDLED_TEMPLATES_DIR
