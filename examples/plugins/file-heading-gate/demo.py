"""Run a file-heading plugin as a required and an optional gate."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from bernstein.core.quality.gate_pipeline import GatePipelineStep
from bernstein.core.quality.gate_runner import GateRunner
from bernstein.core.quality.quality_gates import QualityGatesConfig
from bernstein.core.tasks.models import Task


async def main() -> None:
    with TemporaryDirectory() as directory:
        project = Path(directory)
        gates = project / ".bernstein" / "gates"
        gates.mkdir(parents=True)
        shutil.copy(Path(__file__).with_name("file_heading_gate.py"), gates)

        rows = []
        for scenario, content, required in (
            ("heading-required", "# Status Report\n", True),
            ("missing-required", "# Notes\n", True),
            ("missing-optional", "# Notes\n", False),
        ):
            (project / "doc.md").write_text(content, encoding="utf-8")
            task = Task(
                id=scenario, title="Check the report heading", description="", role="qa", owned_files=["doc.md"]
            )
            step = GatePipelineStep(name="file_heading", required=required)
            config = QualityGatesConfig(pipeline=[step], cache_enabled=False)
            report = await GateRunner(config, project).run_all(task, project)
            (result,) = report.results
            rows.append(
                {
                    "scenario": scenario,
                    "required": result.required,
                    "status": result.status,
                    "blocked": result.blocked,
                    "overall_pass": report.overall_pass,
                }
            )

    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
