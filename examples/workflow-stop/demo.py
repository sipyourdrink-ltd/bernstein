"""Show which commands ran before a workflow stopped on failure."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from bernstein.core.persistence.workspace import grant_workspace_trust
from bernstein.core.workflows import WorkflowRunner, load_workflow_spec


def main() -> None:
    spec = load_workflow_spec(Path(__file__).with_name("workflow.yaml"))
    with TemporaryDirectory() as directory:
        project = Path(directory)
        grant_workspace_trust(project)
        execution = WorkflowRunner(workdir=project).run(spec)
        summary = {
            "succeeded": execution.succeeded,
            "nodes": [
                {"id": node.node_id, "status": node.status.value, "exit_code": node.exit_code}
                for node in execution.nodes
            ],
            "markers": sorted(path.name for path in project.glob("*-ran.txt")),
        }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
