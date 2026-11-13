"""Pin the trust boundary for same-repository branches carrying fork code."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
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
        assert not re.search(r"\bsecrets\b", repr(workflow.get("env", {}))), path.name
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
                or re.search(r"\$\{\{[^}]*\bsecrets\b", repr(job))
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


def test_queue_release_dispatch_fails_closed_on_carry_or_missing_source():
    jobs = load(WORKFLOWS / "post-ci-dispatcher.yml")["jobs"]
    step = next(step for step in jobs["meta"]["steps"] if step.get("id") == "queue-source")
    assert jobs["meta"]["permissions"] == {"contents": "read", "pull-requests": "read"}
    assert ".head.ref // empty" in step["run"]
    assert '[[ -n "$source_branch" && "$source_branch" != fleet/carry-* ]]' in step["run"]
    assert 'gh api "repos/$REPO/compare/main...$HEAD_SHA"' in step["run"]
    assert '[[ "$status" == behind || "$status" == identical ]]' in step["run"]
    assert "needs.meta.outputs.trusted_queue_source == 'true'" in jobs["auto-release"]["if"]


@pytest.mark.parametrize(
    ("source", "compare", "succeeds"),
    [
        ("fleet/carry-17", "behind", False),
        ("FLEET/CARRY-17", "behind", False),
        ("", "behind", False),
        ("fix/tail", "ahead", False),  # The group can include unmerged carry entries.
        ("fix/tail", "error", False),
        ("fix/tail", "behind", True),
        ("fix/tail", "identical", True),
    ],
)
def test_release_admission_only_after_the_exact_group_lands(tmp_path, source, compare, succeeds):
    jobs = load(WORKFLOWS / "post-ci-dispatcher.yml")["jobs"]
    step = next(step for step in jobs["meta"]["steps"] if step.get("id") == "queue-source")
    gh = tmp_path / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'case "$2" in\n'
        '  */pulls/17) printf "%s\\n" "$SOURCE" ;;\n'
        '  */compare/main...*) [ "$COMPARE" != error ] && printf "%s\\n" "$COMPARE" ;;\n'
        "  *) exit 2 ;;\n"
        "esac\n"
    )
    gh.chmod(0o755)
    sleep = tmp_path / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n")
    sleep.chmod(0o755)
    output = tmp_path / "output"
    output.touch()
    env = {
        "PATH": f"{tmp_path}{os.pathsep}{os.defpath}",
        "HEAD_BRANCH": "gh-readonly-queue/main/pr-17-" + "a" * 40,
        "HEAD_SHA": "b" * 40,
        "REPO": "example/repo",
        "SOURCE": source,
        "COMPARE": compare,
        "GITHUB_OUTPUT": str(output),
    }
    subprocess.run(["bash", "-c", step["run"]], env=env, check=compare != "error")
    assert output.read_text() == ("trusted=true\n" if succeeds else "")


@pytest.mark.parametrize(
    ("ref", "compare", "accepted"),
    [
        ("fleet/carry-17", "behind", False),
        ("refs/heads/FLEET/CARRY-17", "behind", False),
        ("refs/tags/v99.1.0", "ahead", False),
        ("a" * 40, "diverged", False),
        ("main", "identical", True),
        ("refs/tags/v3.24.1", "behind", True),
    ],
)
def test_privileged_checkout_resolves_only_refs_already_on_main(tmp_path, ref, compare, accepted):
    job = load(WORKFLOWS / "verify-trusted-main-ref.yml")["jobs"]["verify"]
    assert job["permissions"] == {"contents": "read"}
    assert job["outputs"]["sha"] == "${{ steps.pin.outputs.sha }}"
    output = tmp_path / "output"
    output.touch()
    gh = tmp_path / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'case "$2" in\n'
        '  */commits/*) printf "%s\\n" "$RESOLVED" ;;\n'
        '  */compare/main...*) printf "%s\\n" "$COMPARE" ;;\n'
        "  *) exit 2 ;;\n"
        "esac\n"
    )
    gh.chmod(0o755)
    sha = "b" * 40
    env = {
        "PATH": os.pathsep.join((str(tmp_path), str(Path(sys.executable).parent), os.defpath)),
        "REPO": "example/repo",
        "TARGET_REF": ref,
        "RESOLVED": sha,
        "COMPARE": compare,
        "GITHUB_OUTPUT": str(output),
    }
    result = subprocess.run(["bash", "-c", job["steps"][0]["run"]], env=env, capture_output=True, text=True)
    assert (result.returncode == 0) is accepted, result.stderr
    assert output.read_text() == (f"sha={sha}\n" if accepted else "")


@pytest.mark.parametrize(
    "name",
    ["cluster-tunnel-e2e", "publish", "publish-docker", "publish-extension", "publish-homebrew", "sbom"],
)
def test_privileged_release_and_tunnel_checkouts_use_verified_shas(name):
    jobs = load(WORKFLOWS / f"{name}.yml")["jobs"]
    for job in jobs.values():
        for step in job.get("steps", ()):
            if not step.get("uses", "").startswith("actions/checkout@"):
                continue
            ref = step.get("with", {}).get("ref", "")
            match = re.fullmatch(r"\$\{\{ needs\.(trusted-(?:rpm-)?source)\.outputs\.sha }}", ref)
            assert match, (name, ref)
            source = match[1]
            needs = job["needs"] if isinstance(job["needs"], list) else [job["needs"]]
            assert source in needs
            assert jobs[source]["uses"] == "./.github/workflows/verify-trusted-main-ref.yml"
