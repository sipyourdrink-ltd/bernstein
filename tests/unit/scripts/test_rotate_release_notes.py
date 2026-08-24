"""Unit tests for ``scripts/rotate_release_notes.py`` - the fragments rotation (#4474).

Every PR used to append its release-notes entry to the single
``docs/release-notes/unreleased.md`` file, so any two open PRs conflicted on
that one anchor in the merge queue. These tests pin the fragment-file
alternative: one entry, one file under ``docs/release-notes/fragments/``,
concatenated in deterministic (filename) order by the release rotation and
deleted once folded into the versioned page.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from collections.abc import Generator
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "rotate_release_notes.py"


@pytest.fixture
def rotate_module() -> Generator[ModuleType, None, None]:
    """Load scripts/rotate_release_notes.py as an importable module."""
    spec = importlib.util.spec_from_file_location("rotate_release_notes_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


_ENTRY_A = (
    "## Nightly dependency audit is green again\n\n"
    "The nightly full-closure audit runs `pip-audit --strict` over the dev closure "
    "and had failed since 2026-08-22 on `pip` 26.1.2 (PYSEC-2026-3721). `pip` is "
    "bumped to 26.2.1 in the lockfile.\n"
)
_ENTRY_B = (
    "## Two independent release-notes entries no longer share a file\n\n"
    "Every PR appended one line to `unreleased.md`, so any two open PRs conflicted "
    "on that file in the merge queue. An entry is a fragment file under "
    "`docs/release-notes/fragments/` now (#4474).\n"
)


# --- collect_fragments ---


class TestCollectFragments:
    def test_returns_empty_list_for_missing_directory(self, rotate_module: ModuleType, tmp_path: Path) -> None:
        assert rotate_module.collect_fragments(tmp_path / "does-not-exist") == []

    def test_returns_empty_list_for_empty_directory(self, rotate_module: ModuleType, tmp_path: Path) -> None:
        fragments_dir = tmp_path / "fragments"
        fragments_dir.mkdir()
        assert rotate_module.collect_fragments(fragments_dir) == []

    def test_orders_by_filename_not_write_order(self, rotate_module: ModuleType, tmp_path: Path) -> None:
        """Two PRs land in either order; the combined output must not depend on which."""
        fragments_dir = tmp_path / "fragments"
        fragments_dir.mkdir()
        # Write "second" (by name) before "first" to prove the sort key is the
        # filename, not filesystem write order.
        (fragments_dir / "0002-second.md").write_text(_ENTRY_B, encoding="utf-8")
        (fragments_dir / "0001-first.md").write_text(_ENTRY_A, encoding="utf-8")

        names = [p.name for p in rotate_module.collect_fragments(fragments_dir)]
        assert names == ["0001-first.md", "0002-second.md"]

    def test_ignores_non_markdown_files(self, rotate_module: ModuleType, tmp_path: Path) -> None:
        fragments_dir = tmp_path / "fragments"
        fragments_dir.mkdir()
        (fragments_dir / ".gitkeep").write_text("", encoding="utf-8")
        (fragments_dir / "4474-fragments.md").write_text(_ENTRY_B, encoding="utf-8")

        names = [p.name for p in rotate_module.collect_fragments(fragments_dir)]
        assert names == ["4474-fragments.md"]

    def test_ignores_subdirectories(self, rotate_module: ModuleType, tmp_path: Path) -> None:
        fragments_dir = tmp_path / "fragments"
        fragments_dir.mkdir()
        (fragments_dir / "nested").mkdir()
        (fragments_dir / "4474-fragments.md").write_text(_ENTRY_B, encoding="utf-8")

        names = [p.name for p in rotate_module.collect_fragments(fragments_dir)]
        assert names == ["4474-fragments.md"]


# --- render_fragments (golden test against the current hand-append flow) ---


class TestRenderFragments:
    def test_empty_directory_renders_empty_string(self, rotate_module: ModuleType, tmp_path: Path) -> None:
        fragments_dir = tmp_path / "fragments"
        fragments_dir.mkdir()
        assert rotate_module.render_fragments(fragments_dir) == ""

    def test_single_fragment_renders_its_stripped_body(self, rotate_module: ModuleType, tmp_path: Path) -> None:
        fragments_dir = tmp_path / "fragments"
        fragments_dir.mkdir()
        (fragments_dir / "4474-fragments.md").write_text(f"\n\n{_ENTRY_A}\n\n", encoding="utf-8")

        assert rotate_module.render_fragments(fragments_dir) == _ENTRY_A.strip()

    def test_matches_the_current_hand_append_flow(self, rotate_module: ModuleType, tmp_path: Path) -> None:
        """Golden test: concatenated fragments == entries hand-appended to unreleased.md in order.

        Every existing versioned page (e.g. v3.17.2.md) separates its
        ``## `` sections with exactly one blank line. That is also what a
        contributor produces today by hand-appending a new section at the
        end of unreleased.md. The fragment rotation must reproduce it
        byte-for-byte, not merely "close enough".
        """
        fragments_dir = tmp_path / "fragments"
        fragments_dir.mkdir()
        (fragments_dir / "0001-first.md").write_text(_ENTRY_A, encoding="utf-8")
        (fragments_dir / "0002-second.md").write_text(_ENTRY_B, encoding="utf-8")

        rendered = rotate_module.render_fragments(fragments_dir)

        hand_appended = _ENTRY_A.strip() + "\n\n" + _ENTRY_B.strip()
        assert rendered == hand_appended


# --- rotate_into (deletes consumed fragments in the same call) ---


class TestRotateInto:
    def test_no_fragments_is_a_noop(self, rotate_module: ModuleType, tmp_path: Path) -> None:
        fragments_dir = tmp_path / "fragments"
        fragments_dir.mkdir()
        version_page = tmp_path / "v3.18.0.md"
        version_page.write_text("# v3.18.0\n\nExisting content.\n", encoding="utf-8")

        result = rotate_module.rotate_into(version_page, fragments_dir)

        assert result.consumed == []
        assert version_page.read_text(encoding="utf-8") == "# v3.18.0\n\nExisting content.\n"

    def test_appends_rendered_section_to_existing_page(self, rotate_module: ModuleType, tmp_path: Path) -> None:
        fragments_dir = tmp_path / "fragments"
        fragments_dir.mkdir()
        (fragments_dir / "0001-first.md").write_text(_ENTRY_A, encoding="utf-8")
        version_page = tmp_path / "v3.18.0.md"
        version_page.write_text("# v3.18.0\n\nA patch release.\n", encoding="utf-8")

        rotate_module.rotate_into(version_page, fragments_dir)

        assert version_page.read_text(encoding="utf-8") == (
            "# v3.18.0\n\nA patch release.\n\n" + _ENTRY_A.strip() + "\n"
        )

    def test_writes_into_a_page_that_does_not_exist_yet(self, rotate_module: ModuleType, tmp_path: Path) -> None:
        fragments_dir = tmp_path / "fragments"
        fragments_dir.mkdir()
        (fragments_dir / "0001-first.md").write_text(_ENTRY_A, encoding="utf-8")
        version_page = tmp_path / "v3.18.0.md"

        rotate_module.rotate_into(version_page, fragments_dir)

        assert version_page.read_text(encoding="utf-8") == _ENTRY_A.strip() + "\n"

    def test_deletes_consumed_fragments(self, rotate_module: ModuleType, tmp_path: Path) -> None:
        fragments_dir = tmp_path / "fragments"
        fragments_dir.mkdir()
        first = fragments_dir / "0001-first.md"
        second = fragments_dir / "0002-second.md"
        first.write_text(_ENTRY_A, encoding="utf-8")
        second.write_text(_ENTRY_B, encoding="utf-8")
        version_page = tmp_path / "v3.18.0.md"
        version_page.write_text("# v3.18.0\n\nA patch release.\n", encoding="utf-8")

        result = rotate_module.rotate_into(version_page, fragments_dir)

        assert not first.exists()
        assert not second.exists()
        assert rotate_module.collect_fragments(fragments_dir) == []
        assert {p.name for p in result.consumed} == {"0001-first.md", "0002-second.md"}

    def test_returns_the_rendered_section(self, rotate_module: ModuleType, tmp_path: Path) -> None:
        fragments_dir = tmp_path / "fragments"
        fragments_dir.mkdir()
        (fragments_dir / "0001-first.md").write_text(_ENTRY_A, encoding="utf-8")
        version_page = tmp_path / "v3.18.0.md"
        version_page.write_text("# v3.18.0\n", encoding="utf-8")

        result = rotate_module.rotate_into(version_page, fragments_dir)

        assert result.rendered == _ENTRY_A.strip()


# --- notes_gate_ok (dual-accept during the fragments transition) ---


class TestNotesGateOk:
    def test_passes_on_unreleased_edit(self, rotate_module: ModuleType) -> None:
        changed = ["src/bernstein/core/foo.py", "docs/release-notes/unreleased.md"]
        assert rotate_module.notes_gate_ok(changed) is True

    def test_passes_on_new_fragment(self, rotate_module: ModuleType) -> None:
        changed = ["src/bernstein/core/foo.py", "docs/release-notes/fragments/4474-fragments.md"]
        assert rotate_module.notes_gate_ok(changed) is True

    def test_fails_with_neither(self, rotate_module: ModuleType) -> None:
        changed = ["src/bernstein/core/foo.py", "tests/unit/test_foo.py"]
        assert rotate_module.notes_gate_ok(changed) is False

    def test_fails_on_empty_changeset(self, rotate_module: ModuleType) -> None:
        assert rotate_module.notes_gate_ok([]) is False

    def test_a_versioned_page_edit_alone_does_not_satisfy_the_gate(self, rotate_module: ModuleType) -> None:
        """Editing a tagged release page is not the same as documenting a new change."""
        changed = ["docs/release-notes/v3.17.2.md"]
        assert rotate_module.notes_gate_ok(changed) is False

    def test_a_non_markdown_file_under_fragments_does_not_satisfy_the_gate(self, rotate_module: ModuleType) -> None:
        changed = ["docs/release-notes/fragments/.gitkeep"]
        assert rotate_module.notes_gate_ok(changed) is False

    def test_passes_with_both(self, rotate_module: ModuleType) -> None:
        changed = ["docs/release-notes/unreleased.md", "docs/release-notes/fragments/4474-fragments.md"]
        assert rotate_module.notes_gate_ok(changed) is True

    def test_windows_style_separators_are_normalized(self, rotate_module: ModuleType) -> None:
        changed = ["docs\\release-notes\\fragments\\4474-fragments.md"]
        assert rotate_module.notes_gate_ok(changed) is True


# --- CLI ---


class TestMain:
    def test_rotate_subcommand_exits_zero(
        self, rotate_module: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fragments_dir = tmp_path / "fragments"
        fragments_dir.mkdir()
        (fragments_dir / "0001-first.md").write_text(_ENTRY_A, encoding="utf-8")
        version_page = tmp_path / "v3.18.0.md"
        version_page.write_text("# v3.18.0\n", encoding="utf-8")

        rc = rotate_module.main(["rotate", str(version_page), "--fragments-dir", str(fragments_dir)])

        assert rc == 0
        assert not (fragments_dir / "0001-first.md").exists()
        out = capsys.readouterr().out
        assert "0001-first.md" in out

    def test_check_gate_subcommand_exits_zero_on_pass(self, rotate_module: ModuleType) -> None:
        rc = rotate_module.main(["check-gate", "docs/release-notes/unreleased.md"])
        assert rc == 0

    def test_check_gate_subcommand_exits_nonzero_on_fail(self, rotate_module: ModuleType) -> None:
        rc = rotate_module.main(["check-gate", "src/bernstein/core/foo.py"])
        assert rc != 0


# --- Merge-queue conflict freedom (acceptance criterion, exercised with real git) ---


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


class TestTwoFragmentPRsMergeWithNoConflict:
    def test_two_branches_each_adding_a_fragment_merge_cleanly(self, tmp_path: Path) -> None:
        """Two PRs that each add their own fragment file never touch the same line.

        Reproduces the merge-queue shape from #4474 with real git: branch
        one adds fragment A, branch two (cut from the same base, unaware of
        branch one) adds fragment B, and both merge into main with no
        conflict -- unlike two PRs that each appended a line to the same
        ``unreleased.md``.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        _git("init", "-q", "-b", "main", cwd=repo)
        _git("config", "user.email", "test@example.invalid", cwd=repo)
        _git("config", "user.name", "Test", cwd=repo)

        fragments = repo / "docs" / "release-notes" / "fragments"
        fragments.mkdir(parents=True)
        (fragments / ".gitkeep").write_text("", encoding="utf-8")
        _git("add", "-A", cwd=repo)
        _git("commit", "-q", "-m", "chore: seed fragments dir", cwd=repo)
        base_sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()

        _git("checkout", "-q", "-b", "pr-a", cwd=repo)
        (fragments / "4474-a.md").write_text(_ENTRY_A, encoding="utf-8")
        _git("add", "-A", cwd=repo)
        _git("commit", "-q", "-m", "docs: add fragment a", cwd=repo)

        _git("checkout", "-q", "-b", "pr-b", base_sha, cwd=repo)
        (fragments / "4474-b.md").write_text(_ENTRY_B, encoding="utf-8")
        _git("add", "-A", cwd=repo)
        _git("commit", "-q", "-m", "docs: add fragment b", cwd=repo)

        _git("checkout", "-q", "main", cwd=repo)
        _git("merge", "-q", "--no-ff", "pr-a", "-m", "merge pr-a", cwd=repo)
        # If this raised CalledProcessError, the merge conflicted -- the
        # exact failure mode fragments are meant to remove.
        _git("merge", "-q", "--no-ff", "pr-b", "-m", "merge pr-b", cwd=repo)

        assert (fragments / "4474-a.md").exists()
        assert (fragments / "4474-b.md").exists()
