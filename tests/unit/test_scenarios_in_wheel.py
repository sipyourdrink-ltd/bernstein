"""Wheel-packaging regression: packaged scenarios must ship and resolve.

``RoutineBridge.from_paths`` located the packaged library by walking five
parents up from its own file, which only lands on ``templates/scenarios`` in a
source checkout. ``pyproject.toml`` carried no force-include for it either, so on
a pip install ``bernstein scenario list`` printed "No scenarios found." and
``scenario run docs-sync`` failed with "Unknown scenario".

The wheel-layout test builds a real wheel, unpacks it, and resolves the library
from the unpacked tree with the source checkout off ``sys.path``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGED_RELPATH = "bernstein/_default_templates/scenarios"

pytestmark = pytest.mark.skipif(
    not (REPO_ROOT / "pyproject.toml").is_file(),
    reason="wheel packaging guards only run inside a bernstein source checkout",
)


def _source_scenarios() -> set[str]:
    return {p.name for p in (REPO_ROOT / "templates" / "scenarios").glob("*.yaml")}


def test_force_include_declares_the_scenarios() -> None:
    data: Any = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    for key in ("tool", "hatch", "build", "targets", "wheel", "force-include"):
        data = data.get(key, {})
    assert data.get("templates/scenarios") == PACKAGED_RELPATH


def test_packaged_scenarios_dir_resolves_in_a_checkout() -> None:
    from bernstein.core.planning.scenario_library import load_scenario_library, packaged_scenarios_dir

    root = packaged_scenarios_dir()
    assert root.is_dir()
    assert len(load_scenario_library(root).scenarios) == len(_source_scenarios()) > 0


@pytest.fixture(scope="module")
def unpacked_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("wheel")
    uv = shutil.which("uv")
    command = (
        [uv, "build", "--wheel", "--out-dir", str(out)]
        if uv is not None
        else [sys.executable, "-m", "build", "--wheel", "--outdir", str(out)]
    )
    built = subprocess.run(command, capture_output=True, text=True, cwd=REPO_ROOT, check=False)
    if built.returncode != 0:
        pytest.skip(f"no usable wheel builder: {built.stderr.strip()[-300:]}")
    wheels = sorted(out.glob("*.whl"))
    assert wheels, "the build reported success but produced no wheel"
    unpacked = out / "unpacked"
    with zipfile.ZipFile(wheels[-1]) as archive:
        archive.extractall(unpacked)
    return unpacked


def test_wheel_carries_every_scenario(unpacked_wheel: Path) -> None:
    shipped = unpacked_wheel / PACKAGED_RELPATH
    assert shipped.is_dir(), f"the wheel has no {PACKAGED_RELPATH}"
    assert {p.name for p in shipped.glob("*.yaml")} == _source_scenarios()


def test_scenario_list_sees_the_packaged_library_from_the_wheel(unpacked_wheel: Path, tmp_path: Path) -> None:
    probe = (
        "from bernstein.core.planning.routine_bridge import RoutineBridge\n"
        "from pathlib import Path\n"
        "b = RoutineBridge.from_paths(Path('ws'), Path('state'))\n"
        "print(','.join(sorted(r.scenario_id for r in b.provisioner.list_scenarios())))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={"PYTHONPATH": str(unpacked_wheel), "PATH": "/usr/bin:/bin"},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    found = [s for s in result.stdout.strip().split(",") if s]
    assert len(found) == len(_source_scenarios())
    assert not (tmp_path / "state").exists()
