"""The workflow-heredoc gate catches the line that actually broke.

Every fixture here is a line lifted from ``volunteer-verify.yml``: the
unquoted heredoc as it was merged, and the quoted forms that the same file
already used in its other steps.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from check_workflow_heredocs import check, unquoted_heredocs

REPO_ROOT = Path(__file__).resolve().parents[3]

# The line as merged, which made the shell expand the body and left a bare
# ``CONCLUSION`` in the Python source.
BROKEN = "          uv run python << EOF"
# The two forms the same workflow already used elsewhere.
FIXED = "          uv run python << 'PYEOF'"
PUBLISH = "          PYTEST_EXIT_CODE=\"$pytest_exit_code\" python << 'EOF'"


def test_flags_the_heredoc_that_broke_the_check_run() -> None:
    assert unquoted_heredocs(BROKEN) == [(1, "EOF")]


def test_accepts_the_quoted_form_the_workflow_now_uses() -> None:
    assert unquoted_heredocs(FIXED) == []


def test_accepts_a_quoted_heredoc_behind_an_env_assignment() -> None:
    """``publish.yml`` prefixes the interpreter with an assignment."""
    assert unquoted_heredocs(PUBLISH) == []


def test_accepts_a_dash_heredoc_when_quoted() -> None:
    assert unquoted_heredocs("  python3 <<-'EOF'") == []


def test_flags_a_dash_heredoc_when_unquoted() -> None:
    assert unquoted_heredocs("  python3 <<-EOF") == [(1, "EOF")]


def test_ignores_a_heredoc_that_feeds_something_other_than_python() -> None:
    """The gate speaks to Python bodies; ``cat`` and friends are not its business."""
    assert unquoted_heredocs("          cat << EOF") == []


def test_ignores_a_data_heredoc_piped_into_a_script() -> None:
    """``auto-heal.yml`` builds a JSON payload for a script's stdin.

    The body is data, never parsed as Python, and the substitution is the
    whole point of writing it that way.
    """
    line = "          uv run python scripts/auto_heal_v2_run.py log <<JSON"
    assert unquoted_heredocs(line) == []


def test_still_flags_an_unquoted_body_behind_interpreter_flags() -> None:
    assert unquoted_heredocs("          python3 -u << EOF") == [(1, "EOF")]


def test_reports_the_line_number_within_a_file() -> None:
    text = "\n".join(["steps:", "  run: |", BROKEN])
    assert unquoted_heredocs(text) == [(3, "EOF")]


def test_repository_workflows_are_clean(capsys: pytest.CaptureFixture[str]) -> None:
    """The invariant holds on the tree as committed."""
    assert check(REPO_ROOT) == 0, capsys.readouterr().err


def test_check_fails_on_a_workflow_directory_holding_the_broken_line(tmp_path: Path) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "volunteer-verify.yml").write_text(f"steps:\n  run: |\n{BROKEN}\n")

    assert check(tmp_path) == 1


def test_runs_as_a_script(tmp_path: Path) -> None:
    """CI invokes it as ``python3 scripts/check_workflow_heredocs.py``."""
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ok.yml").write_text(f"steps:\n  run: |\n{FIXED}\n")

    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_workflow_heredocs.py"), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
