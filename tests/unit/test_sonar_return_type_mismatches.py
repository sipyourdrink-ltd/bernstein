"""Regression tests for Sonar python:S5886 return-type mismatch findings."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _uncast_replace_values(relative_path: str) -> list[str]:
    tree = ast.parse((PROJECT_ROOT / relative_path).read_text(encoding="utf-8"))
    findings: list[str] = []

    def _is_replace_call(value: ast.expr | None) -> bool:
        if not isinstance(value, ast.Call):
            return False
        func = value.func
        return (isinstance(func, ast.Name) and func.id == "replace") or (
            isinstance(func, ast.Attribute) and func.attr == "replace"
        )

    def _is_casted_replace(value: ast.expr | None) -> bool:
        if not isinstance(value, ast.Call):
            return False
        func = value.func
        is_cast = isinstance(func, ast.Name) and func.id == "cast"
        return is_cast and len(value.args) >= 2 and _is_replace_call(value.args[1])

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        # The typed wrapper is the one allowed bare dataclasses.replace call.
        if node.name == "_typed_replace":
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Return) and _is_replace_call(child.value) and not _is_casted_replace(child.value):
                findings.append(f"{relative_path}:{node.name}:{child.lineno}")
            if (
                isinstance(child, ast.AnnAssign)
                and _is_replace_call(child.value)
                and not _is_casted_replace(child.value)
            ):
                findings.append(f"{relative_path}:{node.name}:{child.lineno}")
            if isinstance(child, ast.Assign) and _is_replace_call(child.value) and not _is_casted_replace(child.value):
                findings.append(f"{relative_path}:{node.name}:{child.lineno}")

    return findings


def test_s5886_cluster_avoids_uncast_replace_values() -> None:
    """Typed functions should not expose raw dataclasses.replace values."""
    paths = [
        "src/bernstein/core/orchestration/consensus_relay.py",
        "src/bernstein/core/orchestration/run_actor.py",
        "src/bernstein/core/cost/retry_budget.py",
        "src/bernstein/core/routing/criterion_profile.py",
        "src/bernstein/core/routing/mode_profile.py",
    ]

    findings = [finding for path in paths for finding in _uncast_replace_values(path)]

    assert findings == []


def test_direct_replace_scanner_detects_attribute_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The scanner should catch module-qualified replace calls too."""
    sample = tmp_path / "sample.py"
    sample.write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import dataclasses",
                "",
                "def update(value: object) -> object:",
                "    return dataclasses.replace(value)",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("tests.unit.test_sonar_return_type_mismatches.PROJECT_ROOT", tmp_path)
    findings = _uncast_replace_values("sample.py")

    assert findings == ["sample.py:update:6"]
