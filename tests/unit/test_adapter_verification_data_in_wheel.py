"""Wheel-packaging regression: adapter admission evidence must ship.

``bernstein adapters verify --seal`` derives its verdict from two trees that
live outside ``src/`` in the source layout: the pinned contract YAMLs under
``tests/contract/contracts/`` and the golden transcripts under
``tests/golden/``. Both were absent from the wheel, so on a pip install every
adapter resolved to a skip-shaped refusal (``no_contract`` /
``no_transcript``) and the seal could never go green off a dev checkout
(issue #3547).

They now reach an installed host through
``[tool.hatch.build.targets.wheel.force-include]``. These tests pin the two
halves of that arrangement together: the wheel really declares the trees, and
the loaders really look where the wheel puts them. Either half drifting on its
own reproduces the original silent refusal.
"""

from __future__ import annotations

import tomllib
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

import bernstein
from bernstein.adapters import admission
from bernstein.adapters._contract import (  # pyright: ignore[reportPrivateUsage]
    _DEV_CONTRACTS_DIR,
    _PACKAGED_CONTRACTS_DIR,
    CONTRACTS_DIR,
)
from bernstein.adapters.admission import golden_transcripts_dir

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Root the force-include destinations are expressed relative to.
WHEEL_PACKAGE_ROOT = PurePosixPath("bernstein")

#: On-disk root the loaders resolve their packaged copies against.
PACKAGE_ROOT = Path(bernstein.__file__).resolve().parent

pytestmark = pytest.mark.skipif(
    not (REPO_ROOT / "pyproject.toml").is_file(),
    reason="wheel packaging guards only run inside a bernstein source checkout",
)


def _force_include_map(target: str = "wheel") -> dict[str, str]:
    """Return ``[tool.hatch.build.targets.<target>.force-include]`` as a mapping."""
    data: Any = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    for key in ("tool", "hatch", "build", "targets", target, "force-include"):
        data = data.get(key, {})
    assert isinstance(data, dict), f"pyproject.toml has no {target} force-include table"
    return data


@pytest.mark.parametrize(
    ("source", "packaged_dir", "pattern"),
    [
        ("tests/contract/contracts", _PACKAGED_CONTRACTS_DIR, "*.yaml"),
        ("tests/golden", admission._GOLDEN_DIR_PACKAGED, "*_adapter.yaml"),  # pyright: ignore[reportPrivateUsage]
    ],
    ids=["contracts", "golden-transcripts"],
)
def test_admission_evidence_is_force_included_into_the_wheel(
    source: str,
    packaged_dir: Path,
    pattern: str,
) -> None:
    """The wheel ships each evidence tree where its loader looks for it.

    Drops the force-include entry, renames the destination, or moves the
    loader's packaged path, and this fails - the three have to agree or a pip
    install refuses every adapter again.
    """
    source_dir = REPO_ROOT / source
    assert list(source_dir.glob(pattern)), f"no {pattern} files under {source} to package"

    destination = _force_include_map().get(source)
    assert destination is not None, (
        f"{source} is not force-included into the wheel; `bernstein adapters verify --seal` "
        "cannot reach it off a pip install. See [tool.hatch.build.targets.wheel.force-include]."
    )

    relative = PurePosixPath(destination).relative_to(WHEEL_PACKAGE_ROOT)
    expected = PurePosixPath(packaged_dir.relative_to(PACKAGE_ROOT).as_posix())
    assert relative == expected, (
        f"{source} ships to {destination!r} but the loader reads {packaged_dir}; "
        "the wheel destination and the packaged-copy path must match."
    )


@pytest.mark.parametrize(
    "source",
    ["tests/contract/contracts", "tests/golden"],
    ids=["contracts", "golden-transcripts"],
)
def test_admission_evidence_survives_into_the_sdist(source: str) -> None:
    """Each wheel force-include source is re-included into the sdist verbatim.

    ``uv build`` (and any pip install from the sdist) builds the wheel FROM
    the sdist, and hatchling hard-fails on a missing force-include source.
    These trees live under ``tests/``, which the global
    ``[tool.hatch.build]`` exclude drops from the sdist - so without a
    matching sdist force-include entry every sdist-based build of the wheel
    breaks (this is exactly how CI's "Package size check" job builds).
    """
    sdist_destination = _force_include_map("sdist").get(source)
    assert sdist_destination == source, (
        f"{source} is force-included into the wheel but does not survive into the sdist; "
        "building a wheel from the sdist then fails with 'Forced include not found'. "
        "Re-include it at its checkout path under "
        "[tool.hatch.build.targets.sdist.force-include]."
    )


def test_contracts_dir_resolves_to_a_populated_directory() -> None:
    """The resolved contracts directory is one of the two known layouts."""
    assert CONTRACTS_DIR in (_DEV_CONTRACTS_DIR, _PACKAGED_CONTRACTS_DIR)
    assert CONTRACTS_DIR.is_dir(), f"no contracts directory resolved at {CONTRACTS_DIR}"
    assert list(CONTRACTS_DIR.glob("*.yaml")), f"no contract YAMLs under {CONTRACTS_DIR}"


def test_golden_transcripts_dir_resolves_to_a_populated_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resolved transcript directory holds replayable transcripts."""
    monkeypatch.delenv("BERNSTEIN_ADAPTER_GOLDEN_DIR", raising=False)
    resolved = golden_transcripts_dir()
    assert resolved is not None, "no golden-transcript directory resolved"
    assert list(resolved.glob("*_adapter.yaml")), f"no golden transcripts under {resolved}"


def test_golden_transcripts_dir_falls_back_to_the_packaged_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With no checkout on disk the wheel-bundled copy is what answers."""
    monkeypatch.delenv("BERNSTEIN_ADAPTER_GOLDEN_DIR", raising=False)
    packaged = tmp_path / "adapter_golden"
    packaged.mkdir()
    monkeypatch.setattr(admission, "_GOLDEN_DIR_DEFAULT", tmp_path / "absent")
    monkeypatch.setattr(admission, "_GOLDEN_DIR_PACKAGED", packaged)

    assert golden_transcripts_dir() == packaged
