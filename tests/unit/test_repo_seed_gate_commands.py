"""The repo's own run seed must invoke Python tooling through uv.

This project is uv-managed: the virtualenv lives at ``.venv`` and is never
activated, so a bare ``ruff`` is not on PATH for the shell the quality gates
spawn. A gate configured with a bare binary fails with "ruff: not found" on
every run, which reads as a lint failure rather than a misconfiguration.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SEED = REPO_ROOT / "bernstein.yaml"

# Tools that live in the project venv rather than on the system PATH.
_VENV_TOOLS = ("ruff", "pytest", "mypy", "pyright")


def _gate_commands() -> dict[str, str]:
    config = yaml.safe_load(SEED.read_text(encoding="utf-8"))
    gates = config.get("quality_gates") or {}
    return {key: value for key, value in gates.items() if key.endswith("_command")}


def test_seed_declares_gate_commands() -> None:
    """Guard the guard: the assertions below are vacuous without commands."""
    assert _gate_commands(), "bernstein.yaml declares no *_command gate entries"


@pytest.mark.parametrize("tool", _VENV_TOOLS)
def test_venv_tool_is_never_invoked_bare(tool: str) -> None:
    for key, command in _gate_commands().items():
        # Match the tool only as the command word, so "uv run ruff" passes
        # while "ruff check ." fails.
        if re.match(rf"^{re.escape(tool)}\b", command.strip()):
            pytest.fail(
                f"bernstein.yaml quality_gates.{key} runs {tool!r} directly "
                f"({command!r}). The venv is not on PATH for the gate shell; "
                f"use 'uv run {tool} ...' so the gate reports real violations "
                f"instead of '{tool}: not found'."
            )
