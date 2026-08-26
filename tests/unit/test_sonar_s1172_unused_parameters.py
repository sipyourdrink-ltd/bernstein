"""Regression coverage for Sonar python:S1172 unused-parameter findings."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ParameterTarget:
    """One Sonar S1172 target parameter."""

    path: Path
    qualified_name: str
    parameter: str


S1172_TARGETS: tuple[ParameterTarget, ...] = (
    ParameterTarget(Path("src/bernstein/core/knowledge/repo_analyzer.py"), "_modules_without_tests", "analysis"),
    ParameterTarget(
        Path("src/bernstein/core/observability/sidechannel.py"), "NullSideChannel.flush", "deadline_seconds"
    ),
    ParameterTarget(
        Path("src/bernstein/core/protocols/mcp/mcp_client.py"),
        "MCPClientSession.call_tool_streaming",
        "arguments",
    ),
    ParameterTarget(Path("src/bernstein/core/quality/review_pipeline/schema.py"), "_line_for_pointer", "data"),
    ParameterTarget(
        Path("src/bernstein/core/tokens/context_compression.py"),
        "DependencyGraph._resolve_module_paths",
        "source_file",
    ),
)


def _function_named(tree: ast.AST, qualified_name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    stack: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            stack.append(node.name)
            for child in node.body:
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    name = ".".join([*stack, child.name])
                    if name == qualified_name:
                        return child
            stack.pop()
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == qualified_name:
            return node
    raise AssertionError(f"missing function {qualified_name}")


def _parameter_is_absent_or_referenced(target: ParameterTarget) -> bool:
    tree = ast.parse(target.path.read_text(encoding="utf-8"), filename=str(target.path))
    function = _function_named(tree, target.qualified_name)
    parameters = {arg.arg for arg in function.args.args}
    if target.parameter not in parameters:
        return True
    return any(isinstance(node, ast.Name) and node.id == target.parameter for node in ast.walk(function))


def test_sonar_s1172_targets_do_not_leave_unused_parameters() -> None:
    offenders = [
        f"{target.path}:{target.qualified_name}:{target.parameter}"
        for target in S1172_TARGETS
        if not _parameter_is_absent_or_referenced(target)
    ]
    assert offenders == []
