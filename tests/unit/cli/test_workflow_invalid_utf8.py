"""A workflow file with an invalid UTF-8 byte is reported, not raised (#7869).

``_detect_kind`` caught only ``OSError`` and ``yaml.YAMLError``, and ``load_workflow_spec`` let
``UnicodeDecodeError`` through. ``workflow validate`` and ``workflow run --dry-run`` printed a
traceback, and ``workflow list --dir`` stopped at the bad file and hid the valid one beside it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.workflow_cmd import workflow_group
from bernstein.core.workflows import load_workflow_spec

BROKEN = b"name: example\n# bad byte \xff\n"
BUNDLED = Path(__file__).resolve().parents[3] / "templates" / "workflows"


@pytest.fixture
def workflows(tmp_path: Path) -> Path:
    (tmp_path / "broken.yaml").write_bytes(BROKEN)
    valid = sorted(BUNDLED.glob("*.yaml"))[0]
    shutil.copy(valid, tmp_path / "example.yaml")
    return tmp_path


def _run(*args: str) -> tuple[int, str, BaseException | None]:
    # `list` builds its own Rich console, which reads its width from COLUMNS.
    result = CliRunner().invoke(workflow_group, list(args), terminal_width=300, env={"COLUMNS": "300"})
    return result.exit_code, result.output, result.exception


def test_validate_names_the_file_and_the_encoding_problem(workflows: Path) -> None:
    code, out, exc = _run("validate", str(workflows / "broken.yaml"))
    assert code == 1
    assert isinstance(exc, SystemExit)
    assert "broken.yaml" in out
    assert "UTF-8" in out


def test_dry_run_names_the_file_and_the_encoding_problem(workflows: Path) -> None:
    code, out, exc = _run("run", str(workflows / "broken.yaml"), "--dry-run", "-g", "example")
    assert code == 1
    assert isinstance(exc, SystemExit)
    assert "UTF-8" in out


def test_list_reports_the_broken_file_and_still_lists_the_valid_one(workflows: Path) -> None:
    code, out, exc = _run("list", "--dir", str(workflows))
    assert code == 0, exc
    assert "broken" in out
    assert "UTF-8" in out
    # The valid manifest's row shows its own `name`.
    assert load_workflow_spec(workflows / "example.yaml").name in out


def test_a_valid_manifest_still_validates(workflows: Path) -> None:
    code, out, _exc = _run("validate", str(workflows / "example.yaml"))
    assert code == 0, out
