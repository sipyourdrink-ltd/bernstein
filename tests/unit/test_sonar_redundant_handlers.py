"""Regression tests for redundant exception handlers tracked by Sonar."""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TARGETS = (
    Path("src/bernstein/core/protocols/acp/transport.py"),
    # The adapters this guard was written for now live in key_custody.py;
    # lineage_kms.py is the compatibility re-export.
    Path("src/bernstein/core/security/key_custody.py"),
)


def _handler_name(handler: ast.ExceptHandler) -> str:
    if isinstance(handler.type, ast.Name):
        return handler.type.id
    if isinstance(handler.type, ast.Attribute):
        return handler.type.attr
    return ""


def test_target_files_do_not_catch_only_to_rethrow() -> None:
    """Handlers should either add behavior or be omitted."""
    redundant_handlers: list[tuple[Path, int, str]] = []
    for path in TARGETS:
        tree = ast.parse((REPO_ROOT / path).read_text(encoding="utf-8"), filename=str(path))
        for handler in ast.walk(tree):
            if (
                isinstance(handler, ast.ExceptHandler)
                and len(handler.body) == 1
                and isinstance(handler.body[0], ast.Raise)
                and handler.body[0].exc is None
            ):
                redundant_handlers.append((path, handler.lineno, _handler_name(handler)))

    assert redundant_handlers == []
