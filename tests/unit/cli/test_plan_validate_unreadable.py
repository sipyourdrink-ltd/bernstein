"""``bernstein plan validate`` reports a plan file it cannot read, without a traceback (#7870).

``_check_schema`` read the file before the command's error handler, so an invalid UTF-8 byte raised
``UnicodeDecodeError`` out of the command as a traceback.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands import plan_validate_cmd
from bernstein.cli.commands.plan_validate_cmd import validate_plan

VALID = """name: garden
stages:
  - name: build
    steps:
      - title: Plant the beds
        role: backend
"""


def _validate(path: Path) -> tuple[int, str, BaseException | None]:
    result = CliRunner().invoke(validate_plan, [str(path)])
    return result.exit_code, result.output, result.exception


def test_an_invalid_utf8_byte_is_reported_by_name(tmp_path: Path) -> None:
    plan = tmp_path / "broken.yaml"
    plan.write_bytes(b"name: garden\n# bad byte \xff\nstages: []\n")
    code, out, exc = _validate(plan)
    assert code == 1
    assert isinstance(exc, SystemExit)
    assert "broken.yaml" in out
    assert "UTF-8" in out
    assert "Traceback" not in out


def test_an_io_error_takes_the_same_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = tmp_path / "locked.yaml"
    plan.write_text(VALID)

    def _refuse(self: Path, *args: object, **kwargs: object) -> str:
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(plan_validate_cmd.Path, "read_text", _refuse)
    code, out, exc = _validate(plan)
    assert code == 1
    assert isinstance(exc, SystemExit)
    assert "locked.yaml" in out
    assert "could not be read" in out


def test_a_valid_plan_still_validates(tmp_path: Path) -> None:
    plan = tmp_path / "garden.yaml"
    plan.write_text(VALID)
    code, out, _exc = _validate(plan)
    assert code == 0, out


def test_a_yaml_syntax_error_keeps_its_load_error(tmp_path: Path) -> None:
    plan = tmp_path / "syntax.yaml"
    plan.write_text("stages: [unclosed\n")
    code, out, _exc = _validate(plan)
    assert code == 1
    assert "Plan load error" in out
