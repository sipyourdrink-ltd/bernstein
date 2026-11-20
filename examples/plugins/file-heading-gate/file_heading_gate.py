"""A local quality gate that looks for an exact heading line in doc.md."""

from __future__ import annotations

from typing import TYPE_CHECKING

from bernstein.core.quality.gate_pipeline import GateResult
from bernstein.core.quality.gate_plugins import GatePlugin

if TYPE_CHECKING:
    from pathlib import Path


class FileHeadingGate(GatePlugin):
    @property
    def name(self) -> str:
        return "file_heading"

    def run(self, changed_files: list[str], run_dir: Path, task_title: str, task_description: str) -> GateResult:
        target = run_dir / "doc.md"
        heading = "# Status Report"
        found = target.is_file() and heading in target.read_text(encoding="utf-8").splitlines()
        return GateResult(
            name=self.name,
            status="pass" if found else "fail",
            required=self.required,
            blocked=False,  # Let the pipeline step decide whether a failure blocks.
            cached=False,
            duration_ms=0,
            details=f"{target.name} {'contains' if found else 'is missing'} {heading!r}",
        )
