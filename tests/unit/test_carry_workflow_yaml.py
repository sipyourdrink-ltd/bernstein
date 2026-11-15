"""Pin trusted-main checkout resolution for privileged release callers."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github/workflows"


def load(path):
    return yaml.load(path.read_text(), Loader=yaml.BaseLoader)


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
