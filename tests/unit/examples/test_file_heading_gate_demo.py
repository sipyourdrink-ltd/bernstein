"""Exercise the same file plugin in required and optional pipeline steps."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from tests.unit.examples import run_demo

if TYPE_CHECKING:
    from pathlib import Path


def test_file_heading_gate_reports_blocking(tmp_path: Path) -> None:
    proc = run_demo("plugins/file-heading-gate", tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == [
        {"scenario": "heading-required", "required": True, "status": "pass", "blocked": False, "overall_pass": True},
        {"scenario": "missing-required", "required": True, "status": "fail", "blocked": True, "overall_pass": False},
        {"scenario": "missing-optional", "required": False, "status": "fail", "blocked": False, "overall_pass": True},
    ]
