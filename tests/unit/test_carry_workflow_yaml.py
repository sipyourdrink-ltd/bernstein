"""Pin the trust boundary for same-repository branches carrying fork code."""

import re
from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github/workflows"
CARRY_EVENTS = {
    "pull_request",
    "pull_request_target",
    "pull_request_review",
    "merge_group",
    "workflow_call",
    "workflow_dispatch",
}
GUARD = (
    "!startsWith(github.head_ref, 'fleet/carry-') && "
    "!startsWith(github.event.pull_request.head.ref, 'fleet/carry-') && "
    "!startsWith(github.ref, 'refs/heads/fleet/carry-')"
)


def load(path):
    return yaml.load(path.read_text(), Loader=yaml.BaseLoader)


def test_carry_jobs_have_read_only_permissions_or_skip_at_job_admission():
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        workflow = load(path)
        events = workflow["on"]
        if isinstance(events, str):
            events = [events]
        if not CARRY_EVENTS.intersection(events):
            continue
        assert not re.search(
            r"\bsecrets\b", repr(workflow.get("env", {})).replace("secrets.GITHUB_TOKEN", "github.token")
        ), path.name
        for name, job in workflow["jobs"].items():
            permissions = job.get("permissions", workflow.get("permissions"))
            assert permissions is not None, (path.name, name, "implicit permissions")
            read_only = permissions == "read-all" or (
                isinstance(permissions, dict) and set(permissions.values()) <= {"read", "none"}
            )
            privileged = (
                not read_only
                or "uses" in job
                or "environment" in job
                or "self-hosted" in repr(job.get("runs-on"))
                or re.search(r"\$\{\{[^}]*\bsecrets\b", repr(job).replace("secrets.GITHUB_TOKEN", "github.token"))
            )
            if privileged:
                condition = " ".join(job.get("if", "").split())
                assert condition == GUARD or (condition.startswith(GUARD + " && (") and condition.endswith(")")), (
                    path.name,
                    name,
                    "carry job can run with privileges",
                )
                if "merge_group" in events:
                    # Queue refs hide the PR head; these jobs are confined to a main push/dispatch.
                    assert "github.ref == 'refs/heads/main'" in condition, (path.name, name)


def test_push_workflows_do_not_target_carry_branches():
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        events = load(path)["on"]
        if isinstance(events, str):
            events = [events]
        if "push" not in events:
            continue
        assert isinstance(events, dict) and isinstance(events["push"], dict), path.name
        push = events["push"]
        assert push.get("branches") == ["main"] or (
            "tags" in push and "branches" not in push and "branches-ignore" not in push
        ), path.name
