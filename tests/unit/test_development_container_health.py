"""Health contracts for the development environment and the one-shot fixture seeder."""

import json
from pathlib import Path

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
    assert "/usr/local/bin/volunteer-rig-assert" in (rig / "seed-gitea.sh").read_text().splitlines()
