"""``bernstein templates show`` prints the template text as stored (#7955).

The command printed the file through Rich, which reads ``[models]`` as markup and drops it, and wraps
long lines at the terminal width. ``templates use`` copies the file, so the two disagreed.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.commands.templates_cmd import _templates_dir, templates_show, templates_use

TEMPLATE = "rest-api"


def test_show_prints_the_stored_bytes_of_a_template_with_brackets() -> None:
    tdir = _templates_dir()
    assert tdir is not None
    stored = (tdir / f"{TEMPLATE}.yaml").read_text()
    assert "[models]" in stored, "the fixture must carry a bracketed value"
    assert any(len(line) > 80 for line in stored.splitlines()), "the fixture must carry a long line"

    result = CliRunner().invoke(templates_show, [TEMPLATE], terminal_width=60)

    assert result.exit_code == 0
    assert result.output == stored


def test_show_matches_what_use_writes(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    used = tmp_path / "plan.yaml"
    assert CliRunner().invoke(templates_use, [TEMPLATE, str(used)]).exit_code == 0

    shown = CliRunner().invoke(templates_show, [TEMPLATE], terminal_width=60).output

    assert shown == used.read_text()
