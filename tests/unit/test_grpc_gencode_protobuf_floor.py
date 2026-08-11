"""The declared protobuf floor must cover what the checked-in gencode demands.

The generated modules under ``src/bernstein/core/grpc_gen/`` call
``ValidateProtobufRuntimeVersion()`` at import time, which rejects a runtime
whose major differs from the gencode's. A floor that admits an older major
therefore produces an install that resolves cleanly and then raises
``VersionError`` on the first import -- the failure reported in #3594.

These tests pin the two numbers together so regenerating the gencode without
moving the floor fails here rather than at a user's import.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GENCODE_DIR = REPO / "src" / "bernstein" / "core" / "grpc_gen"
PYPROJECT = REPO / "pyproject.toml"

_HEADER = re.compile(r"^# Protobuf Python Version: (\d+)\.(\d+)\.(\d+)\s*$", re.MULTILINE)
_VALIDATE = re.compile(
    r"ValidateProtobufRuntimeVersion\(\s*"
    r"_runtime_version\.Domain\.\w+,\s*"
    r"(\d+),\s*(\d+),\s*(\d+),",
)
_FLOOR = re.compile(r"^protobuf>=(\d+)\.(\d+)(?:\.(\d+))?$")


def _gencode_modules() -> list[Path]:
    modules = sorted(GENCODE_DIR.glob("*_pb2.py"))
    assert modules, f"no generated modules under {GENCODE_DIR}"
    return modules


def _declared_floor() -> tuple[int, int, int]:
    """The ``protobuf>=`` floor declared by the ``grpc`` extra."""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    grpc_extra = data["project"]["optional-dependencies"]["grpc"]
    floors = [_FLOOR.match(item) for item in grpc_extra]
    matched = [m for m in floors if m is not None]
    assert len(matched) == 1, f"expected exactly one 'protobuf>=X.Y' requirement in the grpc extra, got {grpc_extra}"
    major, minor, patch = matched[0].groups()
    return int(major), int(minor), int(patch or 0)


@pytest.mark.parametrize("module", _gencode_modules(), ids=lambda p: p.name)
def test_gencode_header_matches_its_runtime_check(module: Path) -> None:
    """The comment header is only usable as a source of truth if it cannot lie."""
    source = module.read_text(encoding="utf-8")
    header = _HEADER.search(source)
    assert header is not None, f"{module.name}: no 'Protobuf Python Version' header"
    validated = _VALIDATE.search(source)
    assert validated is not None, f"{module.name}: no ValidateProtobufRuntimeVersion call"
    assert header.groups() == validated.groups(), (
        f"{module.name}: header says {'.'.join(header.groups())} but the runtime check "
        f"demands {'.'.join(validated.groups())}"
    )


@pytest.mark.parametrize("module", _gencode_modules(), ids=lambda p: p.name)
def test_declared_floor_admits_only_runtimes_the_gencode_accepts(module: Path) -> None:
    header = _HEADER.search(module.read_text(encoding="utf-8"))
    assert header is not None, f"{module.name}: no 'Protobuf Python Version' header"
    gencode = tuple(int(part) for part in header.groups())
    floor = _declared_floor()

    # Cross-major is a hard VersionError in protobuf's own validator, so the
    # floor must not admit a runtime the gencode will reject on import.
    assert floor[0] == gencode[0], (
        f"{module.name}: gencode major {gencode[0]}, declared floor major {floor[0]}; "
        "a runtime from a different major raises VersionError at import"
    )
    assert floor >= gencode, (
        f"{module.name}: gencode requires protobuf>={'.'.join(map(str, gencode))} "
        f"but the grpc extra declares >={'.'.join(map(str, floor))}"
    )
