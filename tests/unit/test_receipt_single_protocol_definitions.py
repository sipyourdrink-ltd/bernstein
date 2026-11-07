"""Guard: one definition each for verify_receipt, sign_receipt, and canonical_receipt_bytes.

Red-first slice for #5096. The repository currently defines verify_receipt eight times,
sign_receipt five times, and canonical_receipt_bytes five times under src/bernstein. These
tests fail until the receipt protocol is consolidated behind core/receipts/protocol.py.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_BERNSTEIN = _REPO_ROOT / "src" / "bernstein"
_PROTOCOL_FILE = "src/bernstein/core/receipts/protocol.py"

pytestmark = pytest.mark.whole_tree_guard


def _definition_sites(name: str) -> list[tuple[str, int]]:
    """Return (repo-relative path, line) for every top-level ``def name`` under src/bernstein."""
    sites: list[tuple[str, int]] = []
    for path in _SRC_BERNSTEIN.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue  # Skip files with syntax errors; they'll fail other checks
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                sites.append((str(path.relative_to(_REPO_ROOT)), node.lineno))
    return sites


def test_exactly_one_verify_receipt_definition() -> None:
    """verify_receipt must be defined exactly once under src/bernstein."""
    sites = _definition_sites("verify_receipt")
    assert len(sites) == 1, f"expected one verify_receipt definition, found {len(sites)}: {sites}"
    assert sites[0][0] == _PROTOCOL_FILE, f"verify_receipt must live in {_PROTOCOL_FILE}, found {sites[0]}"


def test_exactly_one_sign_receipt_definition() -> None:
    """sign_receipt must be defined exactly once under src/bernstein."""
    sites = _definition_sites("sign_receipt")
    assert len(sites) == 1, f"expected one sign_receipt definition, found {len(sites)}: {sites}"
    assert sites[0][0] == _PROTOCOL_FILE, f"sign_receipt must live in {_PROTOCOL_FILE}, found {sites[0]}"


def test_exactly_one_canonical_receipt_bytes_definition() -> None:
    """canonical_receipt_bytes must be defined exactly once under src/bernstein."""
    sites = _definition_sites("canonical_receipt_bytes")
    assert len(sites) == 1, f"expected one canonical_receipt_bytes definition, found {len(sites)}: {sites}"
    assert sites[0][0] == _PROTOCOL_FILE, f"canonical_receipt_bytes must live in {_PROTOCOL_FILE}, found {sites[0]}"
