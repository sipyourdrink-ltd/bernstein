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
_bundled_templates_dir = _PACKAGE_DIR / "_default_templates"
if not _bundled_templates_dir.is_dir():
    # Dev mode: src/bernstein/../../templates → <repo>/templates
    _bundled_templates_dir = _PACKAGE_DIR.parent.parent / "templates"

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
