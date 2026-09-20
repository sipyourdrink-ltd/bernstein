"""The conformance score runs every vector module, not a hand-kept subset."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
VECTOR_DIR = ROOT / "tests" / "conformance" / "auditor"


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "auditor_conformance_under_test", ROOT / "scripts" / "auditor_conformance.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_every_vector_module_on_disk_is_scored() -> None:
    """A vector file that exists is run: adding one needs no second edit."""
    module = _load_script()
    on_disk = {path.name for path in VECTOR_DIR.glob("test_*vectors.py")}
    scored = {Path(entry).name for entry in module.vector_files()}
    assert on_disk, "no vector modules found: the glob or the directory moved"
    assert scored == on_disk


def test_the_attribution_and_authority_vectors_are_scored() -> None:
    """The two modules the hand-written list omitted are named explicitly.

    They answer questions the published score reported as unanswered, so a
    regression that drops them again is worth its own failure.
    """
    module = _load_script()
    scored = {Path(entry).name for entry in module.vector_files()}
    assert "test_attribution_vectors.py" in scored
    assert "test_authority_vectors.py" in scored


def test_only_vector_modules_are_scored() -> None:
    """The harness and the scoreboard are not vector modules."""
    module = _load_script()
    scored = {Path(entry).name for entry in module.vector_files()}
    assert "test_harness.py" not in scored
    assert "conftest.py" not in scored
