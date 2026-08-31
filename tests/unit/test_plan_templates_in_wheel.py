"""Wheel-packaging regression: plan templates must ship and resolve (#4877).

``bernstein templates list`` and ``bernstein templates use`` were dead on every
path. Two candidate directories were derived by walking up from ``__file__``;
with the module at ``src/bernstein/cli/commands/templates_cmd.py`` they resolved
to ``src/plans/templates`` and ``src/bernstein/plans/templates``, while the
files sat at the repo root under ``plans/templates/``. Neither existed, so the
command printed "Templates directory not found" in a checkout, and the wheel
did not carry the YAMLs at all.

The test that matters here is the one that runs against the INSTALLED layout.
A source-checkout test is what let this regress unnoticed: the files were
present somewhere in the tree, so any assertion phrased against the repo root
passed while the shipped artifact carried nothing.

So this builds a real wheel, unpacks it, and imports ``bernstein`` FROM the
unpacked tree with the source checkout off ``sys.path`` — the resolver then
answers about the wheel, the way it does on a user's machine.
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

#: The five families the command advertises; a wheel missing any is a dead command.
EXPECTED_TEMPLATES = frozenset({"cli-tool", "fullstack", "library", "refactor", "rest-api"})

#: Where the wheel puts them, per the force-include convention every family uses.
PACKAGED_RELPATH = "bernstein/_default_templates/plans"

pytestmark = pytest.mark.skipif(
    not (REPO_ROOT / "pyproject.toml").is_file(),
    reason="wheel packaging guards only run inside a bernstein source checkout",
)


@pytest.fixture(scope="module")
def unpacked_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the wheel once and unpack it; the result is the installed layout."""
    out = tmp_path_factory.mktemp("wheel")
    # `uv` is what CI builds with; `python -m build` is the fallback so the guard still
    # runs on a checkout that has the PEP 517 frontend but not uv. Skipping on neither is
    # deliberate — this test exists precisely because the shipped artifact went unchecked,
    # so it must not become a no-op that reports green.
    uv = shutil.which("uv")
    commands = (
        [[uv, "build", "--wheel", "--out-dir", str(out)]]
        if uv is not None
        else [[sys.executable, "-m", "build", "--wheel", "--outdir", str(out)]]
    )
    failures: list[str] = []
    for command in commands:
        built = subprocess.run(command, capture_output=True, text=True, cwd=REPO_ROOT, check=False)
        if built.returncode == 0:
            break
        failures.append(f"{command[1]}: {built.stderr.strip()[-400:]}")
    else:
        pytest.skip("no usable wheel builder (need uv or `python -m build`): " + " | ".join(failures))
    wheels = sorted(out.glob("*.whl"))
    assert wheels, "the build reported success but produced no wheel"
    unpacked = out / "unpacked"
    with zipfile.ZipFile(wheels[-1]) as archive:
        archive.extractall(unpacked)
    return unpacked


def test_wheel_carries_every_plan_template(unpacked_wheel: Path) -> None:
    """The YAMLs are inside the built wheel, not merely inside the checkout."""
    shipped = unpacked_wheel / PACKAGED_RELPATH
    assert shipped.is_dir(), (
        f"the wheel has no {PACKAGED_RELPATH}; `bernstein templates list` will "
        "print 'Templates directory not found' on every install"
    )
    assert {p.stem for p in shipped.glob("*.yaml")} == EXPECTED_TEMPLATES


def test_resolver_finds_the_templates_in_the_wheel_layout(unpacked_wheel: Path) -> None:
    """``_templates_dir()`` answers from the WHEEL, with no source checkout in reach.

    The import runs in a subprocess whose ``sys.path`` starts at the unpacked
    wheel, so ``bernstein`` resolves there rather than to ``src/``. This is the
    half a checkout-based test cannot see: it is the arrangement in which both
    old candidate paths were wrong.
    """
    probe = (
        "from bernstein.cli.commands.templates_cmd import _templates_dir\n"
        "d = _templates_dir()\n"
        "assert d is not None, 'resolver found no templates directory in the wheel'\n"
        "names = sorted(p.stem for p in d.glob('*.yaml'))\n"
        "print(','.join(names))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=unpacked_wheel,
        env={"PYTHONPATH": str(unpacked_wheel), "PATH": "/usr/bin:/bin"},
        check=False,
    )
    assert result.returncode == 0, f"resolving inside the wheel failed:\n{result.stderr}"
    assert set(result.stdout.strip().split(",")) == EXPECTED_TEMPLATES


def test_force_include_declares_the_plan_templates() -> None:
    """The packaging declaration and the runtime location must name the same place.

    Asserted separately from the built wheel so a drifting declaration names
    itself, rather than surfacing as a missing directory two tests up.
    """
    data: Any = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    for key in ("tool", "hatch", "build", "targets", "wheel", "force-include"):
        data = data.get(key, {})
    assert data.get("templates/plans") == PACKAGED_RELPATH


def test_templates_live_under_the_shared_templates_root() -> None:
    """The source of truth is ``templates/plans/``, like every other family.

    Root ``plans/`` is the user's own working directory - ``bernstein run
    plans/hello.yaml`` - which is what made a shipped asset living there
    ambiguous in the first place.
    """
    assert {p.stem for p in (REPO_ROOT / "templates" / "plans").glob("*.yaml")} == EXPECTED_TEMPLATES
    assert not (REPO_ROOT / "plans" / "templates").exists()
