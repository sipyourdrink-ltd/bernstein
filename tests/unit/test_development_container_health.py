"""Health contracts for the development environment and the one-shot fixture seeder."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_devcontainer_checks_its_installed_tool() -> None:
    devcontainer = ROOT / ".devcontainer"
    config = json.loads((devcontainer / "devcontainer.json").read_text())
    lines = (devcontainer / config["build"]["dockerfile"]).read_text().splitlines()
    (health,) = (line for line in lines if line.startswith("HEALTHCHECK "))
    assert json.loads(health.split(" CMD ", 1)[1]) == ["/usr/local/bin/uv", "--version"]
    assert config["postCreateCommand"].startswith("uv ")


def test_one_shot_seeder_uses_exit_status_and_waits_for_gitea() -> None:
    rig = ROOT / "docker" / "volunteer-rig"
    services = yaml.safe_load((rig / "compose.yaml").read_text())["services"]
    assert "HEALTHCHECK NONE" in (rig / "Dockerfile").read_text().splitlines()
    assert services["seeder"]["restart"] == "no"
    assert services["seeder"]["depends_on"]["gitea"]["condition"] == "service_healthy"
    assert services["gitea"]["healthcheck"]["test"]


@pytest.mark.parametrize(("issue_response", "exit_code"), [("[{},{}]", 0), ("[]", 1)])
def test_seed_entrypoint_propagates_the_fixture_assertion(tmp_path: Path, issue_response: str, exit_code: int) -> None:
    if not shutil.which("bash") or not shutil.which("jq"):
        pytest.skip("the volunteer rig uses bash and jq")
    rig = ROOT / "docker" / "volunteer-rig"
    commands = tmp_path / "bin"
    commands.mkdir()
    for script, name in [("seed-gitea.sh", "volunteer-rig-seed"), ("assert.sh", "volunteer-rig-assert")]:
        subprocess.run(["bash", "-n", str(rig / script)], check=True)
        target = commands / name
        shutil.copyfile(rig / script, target)
        target.chmod(0o755)
    # No server or repository is involved. Only the image's transport commands are replaced.
    (commands / "git").write_text('#!/bin/sh\nif [ "$1" = clone ]; then mkdir fixture-project; fi\n')
    (commands / "curl").write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *"/tokens"*"-u bernstein:volunteer-rig-pass"*) echo \'{"sha1":"fixture"}\' ;;\n'
        '  *"/user/repos "*|*"/health") exit 0 ;;\n'
        '  *"/labels "*) echo \'{"id":17}\' ;;\n'
        '  *"/issues "*\'"labels": [17]\'*) exit 0 ;;\n'
        '  *"/issues?labels="*) echo "$FIXTURE_ISSUES" ;;\n'
        '  *"/raw/.bernstein/volunteer.json"*) cat "$FIXTURES_DIR/volunteer.json" ;;\n'
        "  *) exit 2 ;;\nesac\n"
    )
    for command in ("curl", "git"):
        (commands / command).chmod(0o755)
    result = subprocess.run(
        [str(commands / "volunteer-rig-seed")],
        env={
            **os.environ,
            "PATH": str(commands) + os.pathsep + os.environ["PATH"],
            "TMPDIR": str(tmp_path),
            "FIXTURES_DIR": str(rig / "fixtures"),
            "FIXTURE_ISSUES": issue_response,
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert "Querying issues with volunteer-ok label" in result.stdout, result.stderr
    assert result.returncode == exit_code, result.stderr
    assert ("All asserts passed." in result.stdout) == (exit_code == 0)
