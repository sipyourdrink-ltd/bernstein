"""``bernstein config-path`` reports the seed file the resolver selects (#7958).

With ``BERNSTEIN_SEED_PATH`` naming ``garden.yaml``, the seed resolver selected ``garden.yaml`` and
``config-path --json`` still reported ``bernstein.yaml``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.config_path_cmd import config_path_cmd
from bernstein.core.config.seed import resolve_seed_path


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "bernstein.yaml").write_text("goal: default\n")
    (tmp_path / "garden.yaml").write_text("goal: garden\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BERNSTEIN_SEED_PATH", raising=False)
    return tmp_path


def _json() -> dict[str, str | None]:
    result = CliRunner().invoke(config_path_cmd, ["--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_the_override_names_the_config_file(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_SEED_PATH", str(workspace / "garden.yaml"))
    out = _json()
    assert out["config_file"] == str((workspace / "garden.yaml").resolve())
    assert out["config_file"] == str(resolve_seed_path(workspace))
    assert out["cwd"] == str(Path.cwd())


def test_without_the_override_the_default_is_reported(workspace: Path) -> None:
    out = _json()
    assert out["config_file"] == str((workspace / "bernstein.yaml").resolve())
    assert out["config_file"] == str(resolve_seed_path(workspace))
    assert out["sdd_config"] is None


def test_the_table_names_the_override_too(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_SEED_PATH", str(workspace / "garden.yaml"))
    result = CliRunner().invoke(config_path_cmd, [], terminal_width=300)
    assert "garden.yaml" in result.output
