"""Regression tests for issue #2785.

Three related validation gaps in the ``bernstein --dry-run`` path:

1. ``--dry-run`` skipped seed validation, so a seed the real run rejects
   (e.g. an unsupported ``cli:`` value) printed "No open tasks found in
   backlog" and exited 0 instead of raising the same parse error.
2. A backlog file that is not the expected frontmatter/markdown format
   (e.g. plain YAML) was silently skipped with no warning.
3. The preflight cost estimator resolved the model by swallowing seed
   parse errors and falling back to sonnet, so it could print an estimate
   "at sonnet pricing" for a seed the run would reject or run under a
   different default model.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console

from bernstein.cli import helpers
from bernstein.cli.run_preflight import _resolve_model_and_cli, validate_seed_or_exit


def _write_seed(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Gap 1: --dry-run must validate the seed the same way run does.
# ---------------------------------------------------------------------------


def test_validate_seed_or_exit_rejects_unsupported_cli(tmp_path: Path) -> None:
    """A seed with an unsupported ``cli:`` value raises the same error as run."""
    seed = _write_seed(tmp_path / "bernstein.yaml", 'goal: "ship it"\ncli: definitely-not-a-real-adapter\n')
    with pytest.raises(SystemExit) as exc_info:
        validate_seed_or_exit(str(seed))
    assert exc_info.value.code != 0


def test_validate_seed_or_exit_accepts_valid_seed(tmp_path: Path) -> None:
    """A valid seed parses and is returned for downstream reuse."""
    seed = _write_seed(tmp_path / "bernstein.yaml", 'goal: "ship it"\ncli: claude\nmodel: opus\n')
    result = validate_seed_or_exit(str(seed))
    assert result is not None
    assert result.model == "opus"


def test_validate_seed_or_exit_none_when_no_seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No seed file present is a no-op (inline goal / empty backlog modes)."""
    monkeypatch.chdir(tmp_path)
    assert validate_seed_or_exit(None) is None


def test_dry_run_flag_validates_bad_seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``bernstein --dry-run`` on a rejected seed exits non-zero, not 'No open tasks'."""
    from click.testing import CliRunner

    import bernstein.cli.main as main_mod

    _write_seed(tmp_path / "bernstein.yaml", 'goal: "ship it"\ncli: definitely-not-a-real-adapter\n')

    # Keep the heavy startup path out of the unit test.
    monkeypatch.setattr(main_mod, "_background_startup", lambda _workdir: {"agents": [], "task_count": 0})
    import bernstein.cli.splash_screen as splash_mod

    monkeypatch.setattr(splash_mod, "splash", lambda *a, **k: False)

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as fs:
        _write_seed(Path(fs) / "bernstein.yaml", 'goal: "ship it"\ncli: definitely-not-a-real-adapter\n')
        result = runner.invoke(main_mod.cli, ["--dry-run"])

    assert result.exit_code != 0
    assert "No open tasks found" not in result.output


# ---------------------------------------------------------------------------
# Gap 2: an unparseable backlog file must emit a visible warning.
# ---------------------------------------------------------------------------


def test_dry_run_table_warns_on_unparseable_backlog_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A plain-YAML file (no frontmatter, no ``# `` title) is warned about, not skipped."""
    open_dir = tmp_path / ".sdd" / "backlog" / "open"
    open_dir.mkdir(parents=True)
    # Valid frontmatter ticket so the table still renders.
    (open_dir / "001-ok.md").write_text(
        "---\ntitle: Valid ticket\nrole: backend\npriority: 2\nscope: small\ncomplexity: low\n---\n# Valid ticket\n",
        encoding="utf-8",
    )
    # Plain YAML with no frontmatter delimiters and no markdown title.
    (open_dir / "002-plain.yaml").write_text("title: Plain yaml\nrole: backend\npriority: 2\n", encoding="utf-8")

    buf = io.StringIO()
    fake_console = Console(file=buf, width=200, force_terminal=False, record=False)
    monkeypatch.setattr(helpers, "console", fake_console)

    helpers.print_dry_run_table(tmp_path)

    output = buf.getvalue()
    assert "002-plain.yaml" in output, output
    assert "could not be parsed" in output.lower() or "warning" in output.lower(), output


# ---------------------------------------------------------------------------
# Gap 3: the estimate must reflect the effective default model, not sonnet.
# ---------------------------------------------------------------------------


def test_resolve_model_and_cli_honors_passed_validated_seed(tmp_path: Path) -> None:
    """When a validated seed is supplied, its default model wins over sonnet."""
    from bernstein.core.seed import parse_seed

    seed_path = _write_seed(tmp_path / "bernstein.yaml", 'goal: "ship it"\ncli: claude\nmodel: opus\n')
    seed = parse_seed(seed_path)
    model, cli, _role = _resolve_model_and_cli(None, None, seed=seed)
    assert model == "opus"
    assert cli == "claude"


def test_run_validates_seed_before_printing_estimate(tmp_path: Path) -> None:
    """The run path rejects a bad seed before printing any cost estimate."""
    from click.testing import CliRunner

    from bernstein.cli.run_cmd import run

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as fs:
        _write_seed(Path(fs) / "bernstein.yaml", 'goal: "ship it"\ncli: definitely-not-a-real-adapter\n')
        result = runner.invoke(run, ["--auto-approve"])

    assert result.exit_code != 0
    # The seed is rejected before the preflight estimate, so no "pricing"
    # line for the sonnet fallback is printed.
    assert "pricing" not in result.output.lower()


def test_resolve_model_and_cli_passed_seed_does_not_reparse(tmp_path: Path) -> None:
    """A supplied seed is used directly, so a missing file does not fall back to sonnet."""
    from bernstein.core.seed import parse_seed

    seed_path = _write_seed(tmp_path / "bernstein.yaml", 'goal: "ship it"\nmodel: haiku\n')
    seed = parse_seed(seed_path)
    seed_path.unlink()  # file gone; only the in-memory seed remains
    model, _cli, _role = _resolve_model_and_cli(str(seed_path), None, seed=seed)
    assert model == "haiku"
