"""End-to-end dogfood test for the ``bernstein agents-md`` CLI pipeline.

Exercises the full canonical-IR → render → write → verify → diff → re-sync
loop on a synthetic Bernstein-shaped fixture repo. Each test case invokes
the click commands via ``CliRunner`` so the CLI surface is covered as well
as the underlying generator/bridge functions - the closest the unit suite
can get to "actually shipping it".

The fixture repo (created in ``tmp_path``) carries:

* ``pyproject.toml`` with a ``[project] name`` and a ``[project.scripts]``
  entry so the architecture and build-test sections fire.
* ``src/bernstein/{core,cli,adapters}/`` with one ``__init__.py`` plus a
  module file each so the module-map collects rows.
* A ``README.md`` whose first paragraph drives the overview section.
* A fake ``templates/roles/`` tree so the agent-roles section materialises.
* A real git repository (``git init`` + one commit) so the git-workflow
  section produces a default-branch row.

The five render targets must all (a) produce at least one file, (b) include
the AUTO-GENERATED marker, (c) verify cleanly immediately after sync, and
(d) recover from drift when sync is re-run.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.agents_md_cmd import agents_md_cmd
from bernstein.core.knowledge.agents_md_bridge import (
    ALL_TARGETS,
    render,
    render_all,
)
from bernstein.core.knowledge.agents_md_generator import generate

if TYPE_CHECKING:
    from collections.abc import Iterator

# ---------------------------------------------------------------------------
# Fixture repo construction
# ---------------------------------------------------------------------------


_PYPROJECT = """\
[project]
name = "bernstein-fixture"
version = "0.0.0"
description = "Fixture project for agents-md dogfood integration tests."
requires-python = ">=3.12"

[project.scripts]
bernstein-fixture = "bernstein.cli.main:cli"

[tool.uv]
managed = true

[tool.ruff]
line-length = 88

[tool.pyright]
strict = ["src/"]
"""


_README = """\
# bernstein-fixture

An integration test fixture for the `bernstein agents-md` pipeline.

This README exists so the generator's overview section can pull a real
first paragraph rather than degrading to an N-line slice.

## Usage

```bash
uv sync
uv run pytest
```
"""


# Module sources keyed by relative path. Each carries a Google-style
# docstring so the module-map table renders meaningful Purpose cells.
_MODULES: dict[str, str] = {
    "src/bernstein/__init__.py": '"""Bernstein fixture package - root."""\n',
    "src/bernstein/core/__init__.py": '"""Orchestration engine - core sub-package init."""\n',
    "src/bernstein/core/models.py": '"""Core data models for tasks, agents, and cells."""\n',
    "src/bernstein/core/orchestrator.py": ('"""Orchestrator loop: watch tasks, spawn agents, verify completion."""\n'),
    "src/bernstein/cli/__init__.py": '"""Click CLI - commands sub-package init."""\n',
    "src/bernstein/cli/main.py": '"""CLI entry point for the bernstein-fixture binary."""\ndef cli() -> None:\n    pass\n',
    "src/bernstein/adapters/__init__.py": '"""CLI agent adapters - registry and base class."""\n',
    "src/bernstein/adapters/base.py": '"""Base adapter for CLI coding agents."""\n',
}


_ROLES = ("manager", "backend", "qa")


@pytest.fixture()
def fixture_repo(tmp_path: Path) -> Iterator[Path]:
    """Build a Bernstein-shape fixture repo and ``yield`` its root.

    The directory tree is materialised on a per-test basis so cases that
    mutate files (drift simulations) cannot bleed into siblings.
    """
    repo = tmp_path / "fixture-repo"
    repo.mkdir()

    (repo / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    (repo / "README.md").write_text(_README, encoding="utf-8")
    (repo / "uv.lock").write_text("# fixture\n", encoding="utf-8")  # triggers uv setup branch

    for relpath, source in _MODULES.items():
        target = repo / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")

    roles_root = repo / "templates" / "roles"
    for role in _ROLES:
        role_dir = roles_root / role
        role_dir.mkdir(parents=True, exist_ok=True)
        (role_dir / "system_prompt.md").write_text(f"# {role}\nSystem prompt for the {role} role.\n", encoding="utf-8")

    _git_init(repo)
    yield repo


def _git_init(repo: Path) -> None:
    """Initialise a deterministic git repo so the git-workflow section fires.

    Falls back gracefully if git is missing (CI runners always have git, so
    this is mostly defensive against ad-hoc local runs).
    """
    if shutil.which("git") is None:  # pragma: no cover - CI has git
        pytest.skip("git not available in this environment")

    env = {
        "GIT_AUTHOR_NAME": "fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.com",
        "GIT_COMMITTER_NAME": "fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.com",
    }
    subprocess.run(
        ["git", "init", "-b", "main", "--quiet"],
        cwd=repo,
        check=True,
        env=env | {"PATH": _safe_path()},
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True, env=env | {"PATH": _safe_path()})
    subprocess.run(
        ["git", "commit", "-m", "fixture: initial", "--quiet"],
        cwd=repo,
        check=True,
        env=env | {"PATH": _safe_path()},
    )


def _safe_path() -> str:
    """Return a PATH that exposes the system git binary inside subprocess env.

    ``check=True`` requires git to be findable; the surrounding env dict
    provides only author/committer identities, so PATH must be re-attached.
    """
    import os

    return os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_AUTO_GEN_TOKEN = "AUTO-GENERATED by `bernstein agents-md sync`"


def _invoke(args: list[str], workdir: Path) -> tuple[int, str]:
    """Run the click ``agents-md`` group with ``--workdir`` injected.

    ``--workdir`` is a per-subcommand flag (not on the group), so the
    helper appends it after the subcommand name. ``catch_exceptions`` is
    ``False`` so test failures surface raw tracebacks rather than the
    runner's neutered "1" exit code.

    Returns ``(exit_code, combined_stdout_stderr)`` - Click 8.3 dropped
    ``mix_stderr``, so we fold ``stderr_bytes`` (when present) into the
    same string the legacy contract used to expose.
    """
    runner = CliRunner()
    cmd = [*args, "--workdir", str(workdir)]
    result = runner.invoke(agents_md_cmd, cmd, catch_exceptions=False)
    combined = result.output
    err = getattr(result, "stderr", None)
    if isinstance(err, str) and err and err not in combined:
        combined = combined + err
    return result.exit_code, combined


# ---------------------------------------------------------------------------
# 1. generate end-to-end - every renderer produces at least one file
# ---------------------------------------------------------------------------


class TestGeneratePipeline:
    """The ``generate`` step plus all five target renders fire on a real repo."""

    def test_generate_returns_non_empty_section_list(self, fixture_repo: Path) -> None:
        """Sanity: the generator surfaces multiple sections from the fixture."""
        sections = generate(fixture_repo)
        assert sections, "generator must return at least one section for a populated repo"
        keys = {sec.key for sec in sections}
        # Overview, module-map, build-test, setup, architecture, git-workflow,
        # roles - every section the fixture is shaped to produce.
        assert {"overview", "module-map", "build-test", "setup", "architecture", "git-workflow", "roles"} <= keys

    def test_render_all_emits_at_least_one_file_per_target(self, fixture_repo: Path) -> None:
        sections = generate(fixture_repo)
        outputs = render_all(sections, repo_name="bernstein-fixture")
        assert set(outputs.keys()) == set(ALL_TARGETS)
        for target, output in outputs.items():
            assert output.files, f"target {target!r} produced an empty file map"

    def test_canonical_h1_uses_repo_name(self, fixture_repo: Path) -> None:
        sections = generate(fixture_repo)
        out = render(sections, "canonical", repo_name="bernstein-fixture")
        assert out.files["AGENTS.md"].startswith("# bernstein-fixture - AGENTS.md\n")


# ---------------------------------------------------------------------------
# 2. AUTO-GENERATED marker is present in every renderer output
# ---------------------------------------------------------------------------


class TestAutoGeneratedMarker:
    """Bug-driven coverage: every emitted file announces its provenance.

    The Cursor target previously omitted the marker entirely (regression
    surfaced by the dogfood). This class locks the invariant in place so a
    future renderer can't quietly drop it.
    """

    @pytest.mark.parametrize("target", list(ALL_TARGETS))
    def test_marker_present_in_every_target(self, fixture_repo: Path, target: str) -> None:
        sections = generate(fixture_repo)
        out = render(sections, target, repo_name="bernstein-fixture")  # type: ignore[arg-type]
        for relpath, content in out.files.items():
            assert _AUTO_GEN_TOKEN in content, f"target {target!r} file {relpath!r} missing AUTO-GENERATED marker"


# ---------------------------------------------------------------------------
# 3. sync writes every target and verify passes immediately afterwards
# ---------------------------------------------------------------------------


class TestSyncWriteVerifyLoop:
    def test_sync_writes_all_fourteen_canonical_files(self, fixture_repo: Path) -> None:
        """The 5 targets together produce a fixed-shape file set the operator
        can reason about: 2 + N(cursor) + 1 + 2 + 1 = 6+N. Canonical is 2
        (``AGENTS.md`` plus the ``docs/sdd/module-map.md`` reference page,
        #4142) rather than 1. For our fixture ``N==8`` (7 auto-derived
        sections + the project-wide ``documentation-duty`` section produce
        MDC files), so 14 files total.
        """
        exit_code, output = _invoke(["sync"], fixture_repo)
        assert exit_code == 0, output
        assert "Synced 14 file(s) across 5 target(s)" in output

    def test_verify_passes_immediately_after_sync(self, fixture_repo: Path) -> None:
        sync_code, _ = _invoke(["sync"], fixture_repo)
        assert sync_code == 0
        verify_code, verify_out = _invoke(["verify"], fixture_repo)
        assert verify_code == 0, verify_out
        assert "OK" in verify_out and "in sync" in verify_out

    def test_every_synced_file_carries_marker(self, fixture_repo: Path) -> None:
        _invoke(["sync"], fixture_repo)
        for sub in (
            "AGENTS.md",
            "CLAUDE.md",
            "CONVENTIONS.md",
            ".aider.conf.yml",
            ".goosehints",
            "docs/sdd/module-map.md",
        ):
            content = (fixture_repo / sub).read_text(encoding="utf-8")
            assert _AUTO_GEN_TOKEN in content, f"{sub} written without provenance marker"
        # Every Cursor MDC also gets the marker (post-fix invariant).
        for mdc in (fixture_repo / ".cursor" / "rules").glob("*.mdc"):
            assert _AUTO_GEN_TOKEN in mdc.read_text(encoding="utf-8")

    def test_two_consecutive_syncs_are_byte_identical(self, fixture_repo: Path) -> None:
        """#4142's acceptance criterion, at the CLI level: sync is idempotent.

        Every file the first ``sync`` wrote must come out byte-identical
        from a second ``sync`` with nothing changed in between - the
        property ``verify`` passing immediately afterwards already implies,
        made explicit here by comparing the bytes directly rather than
        through the drift-detector's own normalisation.
        """
        _invoke(["sync"], fixture_repo)
        before = {p: p.read_bytes() for p in fixture_repo.rglob("*") if p.is_file()}
        _invoke(["sync"], fixture_repo)
        after = {p: p.read_bytes() for p in fixture_repo.rglob("*") if p.is_file()}
        assert before == after

    def test_agents_md_module_map_section_stays_under_line_budget(self, fixture_repo: Path) -> None:
        """#4142's line-budget acceptance criterion, at the CLI level."""
        _invoke(["sync"], fixture_repo)
        agents_md = (fixture_repo / "AGENTS.md").read_text(encoding="utf-8")
        assert agents_md.count("\n") <= 160


# ---------------------------------------------------------------------------
# 4. drift detection: hand-edit a generated file → verify exits non-zero
# ---------------------------------------------------------------------------


class TestDriftDetection:
    def test_verify_detects_canonical_drift(self, fixture_repo: Path) -> None:
        _invoke(["sync"], fixture_repo)
        agents_md = fixture_repo / "AGENTS.md"
        agents_md.write_text(agents_md.read_text(encoding="utf-8") + "\n<!-- hand edit -->\n")
        exit_code, output = _invoke(["verify"], fixture_repo)
        assert exit_code == 1
        assert "DRIFT" in output and "AGENTS.md" in output

    def test_verify_detects_missing_file(self, fixture_repo: Path) -> None:
        _invoke(["sync"], fixture_repo)
        (fixture_repo / ".goosehints").unlink()
        exit_code, output = _invoke(["verify"], fixture_repo)
        assert exit_code == 1
        assert "MISSING" in output and ".goosehints" in output


# ---------------------------------------------------------------------------
# 5. round-trip: drift → sync → verify passes again
# ---------------------------------------------------------------------------


class TestRoundTripRecovery:
    def test_sync_after_drift_recovers_clean_verify(self, fixture_repo: Path) -> None:
        """The full operator loop: edit drifts, run sync, verify passes."""
        _invoke(["sync"], fixture_repo)

        # Drift simulation: tamper with the canonical file *and* a per-target
        # one to prove sync's idempotency across targets.
        (fixture_repo / "AGENTS.md").write_text("# bogus\n", encoding="utf-8")
        (fixture_repo / "CLAUDE.md").write_text("# bogus\n", encoding="utf-8")

        verify_code_before, _ = _invoke(["verify"], fixture_repo)
        assert verify_code_before == 1, "drift must be detected before re-sync"

        sync_code, _ = _invoke(["sync"], fixture_repo)
        assert sync_code == 0

        verify_code_after, verify_after = _invoke(["verify"], fixture_repo)
        assert verify_code_after == 0, verify_after


# ---------------------------------------------------------------------------
# 6. diff is informational only - exit code 0 even when content drifts
# ---------------------------------------------------------------------------


class TestDiffSubcommand:
    def test_diff_after_sync_is_silent(self, fixture_repo: Path) -> None:
        _invoke(["sync"], fixture_repo)
        exit_code, output = _invoke(["diff"], fixture_repo)
        assert exit_code == 0
        assert "No drift" in output

    def test_diff_after_drift_emits_unified_diff_but_returns_zero(self, fixture_repo: Path) -> None:
        _invoke(["sync"], fixture_repo)
        (fixture_repo / "AGENTS.md").write_text("# tampered\n", encoding="utf-8")
        exit_code, output = _invoke(["diff", "--target", "canonical"], fixture_repo)
        assert exit_code == 0  # diff is informational
        assert "AGENTS.md" in output
        assert "@@" in output  # unified diff hunk header


# ---------------------------------------------------------------------------
# 7. dry-run honesty - sync --dry-run reports the right count
# ---------------------------------------------------------------------------


class TestDryRunCounting:
    """Regression coverage: ``sync --dry-run`` previously reported 0 files
    because the writer returned its 0-byte count instead of the planned set
    size. The fix surfaces the planned count separately.
    """

    def test_sync_dry_run_reports_planned_total(self, fixture_repo: Path) -> None:
        exit_code, output = _invoke(["sync", "--dry-run"], fixture_repo)
        assert exit_code == 0
        assert "[dry-run] 14 file(s) across 5 target(s) would be synced" in output

    def test_sync_dry_run_does_not_touch_disk(self, fixture_repo: Path) -> None:
        _invoke(["sync", "--dry-run"], fixture_repo)
        assert not (fixture_repo / "AGENTS.md").exists()
        assert not (fixture_repo / "CLAUDE.md").exists()
        assert not (fixture_repo / ".cursor").exists()


# ---------------------------------------------------------------------------
# 8. repo-name inference - defaults to project name in pyproject.toml
# ---------------------------------------------------------------------------


class TestRepoNameInference:
    """The CLI used to default the H1 to the workdir basename, which is
    misleading inside auto-generated worktrees. Project name from
    ``pyproject.toml`` should win.
    """

    def test_pyproject_name_drives_h1(self, fixture_repo: Path) -> None:
        _invoke(["sync"], fixture_repo)
        agents_md = (fixture_repo / "AGENTS.md").read_text(encoding="utf-8")
        # The fixture's pyproject says ``name = "bernstein-fixture"``.
        assert agents_md.startswith("# bernstein-fixture - AGENTS.md\n")

    def test_explicit_repo_name_overrides_pyproject(self, fixture_repo: Path) -> None:
        _invoke(["sync", "--repo-name", "MyOverride"], fixture_repo)
        agents_md = (fixture_repo / "AGENTS.md").read_text(encoding="utf-8")
        assert agents_md.startswith("# MyOverride - AGENTS.md\n")


# ---------------------------------------------------------------------------
# 9. selective verify - single-target verify gates only that target
# ---------------------------------------------------------------------------


class TestSelectiveVerify:
    def test_verify_target_canonical_ignores_cursor_drift(self, fixture_repo: Path) -> None:
        _invoke(["sync"], fixture_repo)
        # Drift the cursor MDC but not the canonical AGENTS.md.
        mdc = fixture_repo / ".cursor" / "rules" / "overview.mdc"
        mdc.write_text(mdc.read_text(encoding="utf-8") + "\n<!-- tampered -->\n")
        exit_code, _ = _invoke(["verify", "--target", "canonical"], fixture_repo)
        assert exit_code == 0  # cursor drift doesn't fail canonical verify

    def test_verify_target_cursor_catches_cursor_drift(self, fixture_repo: Path) -> None:
        _invoke(["sync"], fixture_repo)
        mdc = fixture_repo / ".cursor" / "rules" / "overview.mdc"
        mdc.write_text(mdc.read_text(encoding="utf-8") + "\n<!-- tampered -->\n")
        exit_code, output = _invoke(["verify", "--target", "cursor"], fixture_repo)
        assert exit_code == 1
        assert "DRIFT" in output and "overview.mdc" in output
