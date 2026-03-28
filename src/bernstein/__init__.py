"""Bernstein — Multi-agent orchestration for CLI coding agents."""
from __future__ import annotations

from pathlib import Path

__version__ = "0.1.0"

_PACKAGE_DIR = Path(__file__).resolve().parent

# Bundled default templates — present inside the wheel after pip install.
# In dev/editable mode, fall back to <repo>/templates/ at the project root.
_BUNDLED_TEMPLATES_DIR = _PACKAGE_DIR / "_default_templates"
if not _BUNDLED_TEMPLATES_DIR.is_dir():
    # Dev mode: src/bernstein/../../templates → <repo>/templates
    _BUNDLED_TEMPLATES_DIR = _PACKAGE_DIR.parent.parent / "templates"


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
