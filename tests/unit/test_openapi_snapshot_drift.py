"""Guard: the committed OpenAPI snapshot tracks the live FastAPI app.

``docs/reference/openapi.json`` is a build input, not a report: it renders the
published REST reference and seeds ``scripts/generate_sdk.py``. Refreshing it
was a documented manual step that nothing executed, so it rotted quietly --
the snapshot described 216 paths while the app served 459, and every client
generated from it was missing more than half the surface.

These tests fail the moment a route is added, removed, or renamed without
regenerating the snapshot, and they name the offending paths so the fix is one
command:

    uv run python scripts/generate_openapi.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SNAPSHOT = _REPO_ROOT / "docs" / "reference" / "openapi.json"
_GENERATOR = _REPO_ROOT / "scripts" / "generate_openapi.py"
_SDK_GENERATOR = _REPO_ROOT / "scripts" / "generate_sdk.py"

_REGENERATE = "uv run python scripts/generate_openapi.py"


def _load(path: Path) -> ModuleType:
    """Load a loose ``scripts/*.py`` file as an importable module."""
    spec = importlib.util.spec_from_file_location(f"{path.stem}_under_test", path)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


@pytest.fixture(scope="module")
def generator() -> ModuleType:
    return _load(_GENERATOR)


@pytest.fixture(scope="module")
def sdk_generator() -> ModuleType:
    return _load(_SDK_GENERATOR)


def _live_spec() -> dict[str, Any]:
    """Build the OpenAPI document straight from the app definition."""
    from bernstein.core.server import create_app

    return create_app().openapi()


def _committed_spec() -> dict[str, Any]:
    spec: dict[str, Any] = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))
    return spec


def _format(kind: str, missing: set[str], stale: set[str]) -> str:
    lines = []
    if missing:
        lines.append(f"{len(missing)} live {kind}(s) absent from the snapshot:")
        lines += [f"  + {name}" for name in sorted(missing)]
    if stale:
        lines.append(f"{len(stale)} snapshot {kind}(s) the app no longer defines:")
        lines += [f"  - {name}" for name in sorted(stale)]
    lines.append(f"Regenerate with: {_REGENERATE}")
    return "\n".join(lines)


def test_generator_targets_the_committed_snapshot(generator: ModuleType) -> None:
    """The regenerate command must overwrite the file the guard reads.

    Both tests below tell the operator to run ``scripts/generate_openapi.py``.
    If that script writes somewhere else, the advice is a dead end and the
    snapshot stays stale no matter how often it is followed.
    """
    assert generator.SPEC_PATH == _SNAPSHOT, (
        f"'{_REGENERATE}' writes {generator.SPEC_PATH}, but the published reference "
        f"and scripts/generate_sdk.py read {_SNAPSHOT}"
    )


def test_sdk_generator_reads_the_committed_snapshot(sdk_generator: ModuleType) -> None:
    """The SDK generator's default spec is the file this guard keeps fresh."""
    assert sdk_generator.DEFAULT_SPEC_PATH == _SNAPSHOT, (
        f"scripts/generate_sdk.py defaults to {sdk_generator.DEFAULT_SPEC_PATH}, "
        f"which is not the committed snapshot at {_SNAPSHOT}"
    )


def test_snapshot_paths_match_live_app() -> None:
    """Every documented path is served, and every served path is documented."""
    live = set(_live_spec()["paths"])
    committed = set(_committed_spec()["paths"])

    assert live == committed, "docs/reference/openapi.json is stale.\n" + _format(
        "path", live - committed, committed - live
    )


def test_snapshot_schema_names_match_live_app() -> None:
    """Request/response models stay in step with the snapshot's components."""
    live = set(_live_spec().get("components", {}).get("schemas", {}))
    committed = set(_committed_spec().get("components", {}).get("schemas", {}))

    assert live == committed, "docs/reference/openapi.json component schemas are stale.\n" + _format(
        "schema", live - committed, committed - live
    )
