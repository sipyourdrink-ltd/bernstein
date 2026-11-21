"""Pin landed merge-group admission before privileged release dispatch."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github/workflows"


def load(path):
    return yaml.load(path.read_text(), Loader=yaml.BaseLoader)


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
