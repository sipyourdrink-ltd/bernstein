"""Unit tests for ``scripts/bump_version.py`` - the single version-bump path.

A release bump must move ``pyproject.toml``, ``uv.lock``, and the distribution
manifests in lockstep. These tests pin the pure version-rewrite logic and the
guard that the script never hand-writes the OCI ``packages[].version`` field
(the registry schema forbids it; the generator owns that shape).
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Generator
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "bump_version.py"


@pytest.fixture
def bump_module() -> Generator[ModuleType, None, None]:
    """Load scripts/bump_version.py as an importable module."""
    spec = importlib.util.spec_from_file_location("bump_version_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


_PYPROJECT = """\
[project]
name = "bernstein"
version = "3.4.4"
description = "demo"

[tool.example]
version = "keep-me"
"""


def test_set_pyproject_version_rewrites_only_top_level(bump_module: ModuleType) -> None:
    result = bump_module.set_pyproject_version(_PYPROJECT, "3.4.5")
    assert 'version = "3.4.5"' in result
    # The indented table-scoped version key must be left untouched.
    assert 'version = "keep-me"' in result
    assert 'version = "3.4.4"' not in result


def test_set_pyproject_version_raises_without_version_line(bump_module: ModuleType) -> None:
    with pytest.raises(ValueError, match="no top-level"):
        bump_module.set_pyproject_version('[project]\nname = "x"\n', "3.4.5")


@pytest.mark.parametrize("bad", ["v3.4.5", "3.4", "latest", "3.x.0", ""])
def test_validate_version_rejects_non_semver(bump_module: ModuleType, bad: str) -> None:
    with pytest.raises(ValueError, match="semver"):
        bump_module._validate_version(bad)


@pytest.mark.parametrize("good", ["3.4.5", "10.0.0", "1.2.3-rc.1", "0.1.0+build.7"])
def test_validate_version_accepts_semver(bump_module: ModuleType, good: str) -> None:
    assert bump_module._validate_version(good) == good


def test_script_never_hand_writes_oci_package_version(bump_module: ModuleType) -> None:
    """The bump path must delegate manifest edits, never touch the OCI version.

    Hand-writing the OCI ``packages[].version`` field would reintroduce the
    version onto the entry that the registry schema forbids; the generator keeps
    it version-less. Guard statically so a future edit can't sneak it back in:
    the bump path delegates to the generator and never parses JSON manifests
    itself, so it structurally cannot rewrite the OCI package entry.
    """
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "gen_distribution_manifests.py" in source
    # Delegation guard: manifest edits require JSON handling, which lives only
    # in the generator. The bump script must not import or use the json module.
    assert not hasattr(bump_module, "json")
    assert "import json" not in source
    assert "registryType" not in source
