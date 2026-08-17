"""The legacy AGENTS.md generator steps aside when `agents-md sync` owns the file (#4019).

`scripts/gen_agents_md.py --check` reported the committed AGENTS.md as stale on a
clean tree while `bernstein agents-md verify` reported it in sync, and the remedy
it printed removed 74 rows and reddened the docs-drift gate. `scripts/` is where a
contributor goes to find out what to run before pushing, so running the `--check`
variants there is the careful behaviour - and it was the behaviour being punished.

The invariant: **a check that cannot be the authority on an artifact must not
issue a verdict on it.** `--force` still reaches the legacy behaviour for anyone
who wants the detailed map, because retiring the script is a separate decision.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "gen_agents_md.py"


def load_script() -> ModuleType:
    """Import the script by path - `scripts/` is not a package."""
    spec = importlib.util.spec_from_file_location("gen_agents_md_under_test", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_script(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )


class TestItStepsAsideRatherThanMisleading:
    def test_check_on_a_clean_tree_exits_zero_with_the_note(self) -> None:
        # The reproduction from the issue, inverted: this used to exit 1 saying
        # "module map is stale" and hand over a command that breaks the gate.
        proc = run_script("--check")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "managed by `bernstein agents-md sync`" in proc.stdout
        assert "module map is stale" not in proc.stdout + proc.stderr

    def test_the_note_names_both_ways_forward(self) -> None:
        # A refusal that does not say what to run instead is a dead end on the
        # one command somebody ran to find out what to run.
        out = run_script("--check").stdout
        assert "bernstein agents-md sync" in out
        assert "--force" in out

    def test_the_bare_stdout_form_steps_aside_too(self) -> None:
        # The guard sits ahead of the flag dispatch, so the no-argument form is
        # covered as well. Pinned because the module docstring documents all
        # three spellings: a doc that named only two would be the same defect
        # this change fixes, one layer in.
        proc = run_script()
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "managed by `bernstein agents-md sync`" in proc.stdout
        assert "## Module map" not in proc.stdout, "the smaller legacy map must not reach stdout unforced"

    def test_update_without_force_writes_nothing(self) -> None:
        # The destructive half. --check being safe would be no comfort if the
        # command the old message printed still removed the rows.
        before = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        proc = run_script("--update")
        assert proc.returncode == 0
        assert (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8") == before


class TestForceStillReachesTheLegacyBehaviour:
    def test_force_bypasses_the_supersession_check(self) -> None:
        # Retiring the script is a separate decision (its tests still pin the
        # legacy contract), so the old path has to stay reachable on purpose.
        proc = run_script("--check", "--force")
        assert proc.returncode == 1
        assert "module map is stale" in proc.stderr

    def test_the_forced_remedy_names_force_so_it_does_not_loop(self) -> None:
        # The old message said `--update` with no --force. Following it now
        # would hit the supersession guard and do nothing, leaving a reader
        # running a command that reports stale and then declines to fix it.
        assert "--update --force" in run_script("--check", "--force").stderr


class TestUndeterminableIsNotTreatedAsSuperseded:
    """The failure mode worth guarding: this script must still work standalone."""

    def test_an_unimportable_bernstein_means_cannot_tell_not_superseded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Silently going quiet in a checkout with no installed package would
        # trade one wrong verdict for another - a generator that reports
        # nothing is not more honest than one that reports wrongly.
        module = load_script()
        monkeypatch.setitem(sys.modules, "bernstein.core.knowledge.agents_md_bridge", None)
        assert module.agents_md_is_synced() is None

    def test_a_missing_agents_md_is_cannot_tell(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module = load_script()
        monkeypatch.setattr(module, "AGENTS_MD", REPO_ROOT / "does-not-exist.md")
        assert module.agents_md_is_synced() is None

    def test_on_this_tree_it_reports_synced(self) -> None:
        # The positive control. Without it, every assertion above would pass
        # just as well if the helper always returned None - which is exactly
        # the "reports success without checking" shape this fixes.
        assert load_script().agents_md_is_synced() is True
