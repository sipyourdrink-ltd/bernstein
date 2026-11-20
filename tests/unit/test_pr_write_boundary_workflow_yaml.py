"""PR code and write authority belong to different workflows and runners."""

from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github/workflows"
PRODUCERS = ("pr-observability-summary.yml", "pr-policy.yml")


def load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


@pytest.mark.parametrize("name", PRODUCERS)
def test_pr_jobs_have_only_read_permissions_and_no_write_secret(name: str) -> None:
    workflow = load(name)
    for job in workflow["jobs"].values():
        permissions = job.get("permissions", workflow["permissions"])
        assert isinstance(permissions, dict) and set(permissions.values()) <= {"read"}
        assert "secrets." not in str(job)
        for step in job["steps"]:
            assert not step.get("uses", "").startswith(("./", "$/"))


@pytest.mark.parametrize("name", PRODUCERS)
def test_every_pr_checkout_discards_credentials(name: str) -> None:
    checkouts = [
        step
        for job in load(name)["jobs"].values()
        for step in job["steps"]
        if step.get("uses", "").startswith("actions/checkout@")
    ]
    assert checkouts
    for checkout in checkouts:
        assert checkout["with"].get("persist-credentials") is False
        assert "token" not in checkout["with"]


def test_write_jobs_use_only_default_branch_code_and_untrusted_data_inputs() -> None:
    workflow = load("pr-publish.yml")
    events = workflow.get("on", workflow.get(True))
    assert set(events) == {"workflow_run"}
    assert set(events["workflow_run"]["workflows"]) == {"PR observability summary", "PR policy"}
    assert workflow["permissions"] == {}
    jobs = workflow["jobs"]
    assert set(jobs) == {"prepare", "comment", "autosync"}
    assert set(jobs["prepare"]["permissions"].values()) == {"read"}
    assert jobs["comment"]["permissions"] == {"pull-requests": "write"}
    assert jobs["autosync"]["permissions"] == {"contents": "read"}
    for job_name, job in jobs.items():
        checked_out = False
        for step in job["steps"]:
            uses = step.get("uses", "")
            if uses.startswith("actions/checkout@"):
                assert step["with"] == {"ref": "${{ github.sha }}", "path": "trusted", "persist-credentials": False}
                checked_out = True
            elif uses.startswith(("./", "$/")):
                assert checked_out and uses.startswith("./trusted/")
            elif uses:
                assert job_name == "comment" and uses.startswith("marocchino/sticky-pull-request-comment@")
            if "run" in step:
                assert "${{" not in step["run"]
                if job_name == "comment":
                    if step.get("id") == "target":
                        assert 'gh api "repos/$GITHUB_REPOSITORY/pulls/$NUMBER"' in step["run"]
                        assert step["env"]["EXPECTED_SHA"] == "${{ needs.prepare.outputs.sha }}"
                    else:
                        assert step["run"].strip().splitlines() == [
                            'test -n "$REPORT"',
                            """printf '%s' "$REPORT" | base64 --decode > "$RUNNER_TEMP/observability.md" """.strip(),
                        ]
                else:
                    script = "prepare_pr_publication" if job_name == "prepare" else "publish_autosync"
                    assert checked_out and step["run"] == f"python3 -I trusted/scripts/{script}.py"
    comment = jobs["comment"]["steps"][-1]
    assert jobs["comment"]["needs"] == jobs["autosync"]["needs"] == "prepare"
    assert comment["if"] == "steps.target.outputs.current == 'true'"
    assert comment["with"]["number"] == "${{ needs.prepare.outputs.number }}"
    assert comment["with"]["path"] == "${{ runner.temp }}/observability.md"
    assert "secrets." not in str(jobs["prepare"]) + str(jobs["comment"])
    assert str(jobs["autosync"]).count("secrets.") == 1
