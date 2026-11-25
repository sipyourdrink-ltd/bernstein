"""Structural tests for import-linter architecture contracts."""

from __future__ import annotations

import ast
import configparser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
IMPORTLINTER_CONFIG = REPO_ROOT / ".importlinter"
ADAPTER_REGISTRY = REPO_ROOT / "src" / "bernstein" / "adapters" / "registry.py"


def _config_values(section: str, option: str) -> set[str]:
    parser = configparser.ConfigParser()
    parser.read(IMPORTLINTER_CONFIG)
    raw = parser.get(section, option)
    return {line.strip() for line in raw.splitlines() if line.strip()}


def _registered_adapter_modules() -> set[str]:
    tree = ast.parse(ADAPTER_REGISTRY.read_text(encoding="utf-8"))
    modules: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_AdapterRegistry":
            # Built-ins are module:class targets so listing names never imports their implementations.
            targets = ast.literal_eval(node.args[0])
            for target in targets.values():
                module, separator, class_name = target.partition(":")
                assert separator and class_name.isidentifier(), f"Invalid adapter target: {target!r}"
                modules.add(module)
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("bernstein.adapters.")
            and node.module != "bernstein.adapters.base"
        ):
            # The generic adapter and the profile-built adapters are registered directly.
            modules.add(node.module)

    return modules


def test_adapter_independence_contract_covers_registered_adapters() -> None:
    """Every built-in registry adapter should be covered by the independence contract."""
    contracted_modules = _config_values("importlinter:contract:adapters-independent", "modules")

    assert contracted_modules == _registered_adapter_modules()
