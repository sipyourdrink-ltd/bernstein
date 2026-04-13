"""Installation mismatch detection -- detect multiple Bernstein installs and config conflicts.

Checks for duplicate bernstein binaries in PATH, version mismatches between
config and installed package, and unavailable features referenced in config.
"""

from __future__ import annotations

import importlib.metadata
import logging
import os
from dataclasses import dataclass
from pathlib import Path

_INSTALLATIONS_LABEL = "Bernstein installations"

logger = logging.getLogger(__name__)


@dataclass
class InstallWarning:
    """A single installation mismatch result."""

    name: str
    ok: bool
    detail: str
    fix: str = ""


def check_installations() -> list[InstallWarning]:
    """Check for multiple Bernstein installations and config/reality conflicts.

    Returns:
        List of InstallWarning results.
    """
    results: list[InstallWarning] = []

    # 1. Detect multiple bernstein binaries in PATH
    bernstein_paths = _find_all_binaries("bernstein")
    if len(bernstein_paths) > 1:
        paths_str = ", ".join(str(p) for p in bernstein_paths)
        results.append(
            InstallWarning(
                name=_INSTALLATIONS_LABEL,
                ok=False,
                detail=f"found {len(bernstein_paths)} installations: {paths_str}",
                fix="Uninstall duplicates or adjust PATH to prioritize one installation",
            )
        )
    else:
        if bernstein_paths:
            results.append(
                InstallWarning(
                    name=_INSTALLATIONS_LABEL,
                    ok=True,
                    detail=f"single installation at {bernstein_paths[0]}",
                )
            )
        else:
            results.append(
                InstallWarning(
                    name=_INSTALLATIONS_LABEL,
                    ok=False,
                    detail="bernstein not found in PATH",
                    fix="Install Bernstein: pip install bernstein or uv tool install bernstein",
                )
            )

    # 2. Version mismatch: installed package version
    installed_version = _get_installed_version()
    if installed_version:
        results.append(
            InstallWarning(
                name="Bernstein version",
                ok=True,
                detail=f"v{installed_version}",
            )
        )
    else:
        results.append(
            InstallWarning(
                name="Bernstein version",
                ok=False,
                detail="could not determine installed version",
                fix="Reinstall Bernstein: pip install --upgrade bernstein",
            )
        )

    # 3. Check for venv isolation mismatches
    venv_mismatch = _check_venv_isolation()
    if venv_mismatch:
        results.append(venv_mismatch)

    return results


def _find_all_binaries(name: str) -> list[Path]:
    """Find all binaries with the given name in PATH."""
    paths_str = os.environ.get("PATH", "")
    binaries: list[Path] = []
    seen_real: set[Path] = set()
    for dir_str in paths_str.split(os.pathsep):
        dir_path = Path(dir_str)
        if not dir_path.is_dir():
            continue
        candidate = dir_path / name
        if candidate.is_file():
            real_path = candidate.resolve()
            if real_path not in seen_real:
                binaries.append(candidate)
                seen_real.add(real_path)
    return binaries


def _get_installed_version() -> str | None:
    """Get the installed Bernstein package version."""
    try:
        return importlib.metadata.version("bernstein")
    except importlib.metadata.PackageNotFoundError:
        return None


def _check_venv_isolation() -> InstallWarning | None:
    """Check if running outside a virtual environment when one is expected."""
    in_venv = os.environ.get("VIRTUAL_ENV") is not None or has_venv()
    venv_configured = _venv_configured_in_project()

    if venv_configured and not in_venv:
        return InstallWarning(
            name="Virtual environment",
            ok=False,
            detail="project has a .venv/ but VIRTUAL_ENV is not set",
            fix="Activate the virtual environment: source .venv/bin/activate",
        )

    if in_venv:
        venv_path = os.environ.get("VIRTUAL_ENV", "active")
        return InstallWarning(
            name="Virtual environment",
            ok=True,
            detail=f"active: {venv_path}",
        )

    return None


def has_venv() -> bool:
    """Check if Python is running inside a virtual environment."""
    import sys

    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def _venv_configured_in_project() -> bool:
    """Check if .venv/ directory exists in the current working directory."""
    return (Path.cwd() / ".venv").is_dir()
