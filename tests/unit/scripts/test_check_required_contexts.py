"""Unit tests for ``scripts/check_required_contexts.py``.

The script exists to separate two states that produce an identical zero in the
PR failure histogram: every required context passed, and a required context
never ran. These tests pin that classification and the fact that the required
context list is read from the in-tree canary rather than restated here.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_required_contexts.py"


@pytest.fixture(scope="module")
def module() -> ModuleType:
    """Load ``scripts/check_required_contexts.py`` as an importable module."""
    spec = importlib.util.spec_from_file_location("check_required_contexts_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


def _run(name: str, status: str = "completed", conclusion: str | None = "success") -> dict[str, object]:
    """Build a check-run payload in the shape the API returns."""
    return {"name": name, "status": status, "conclusion": conclusion}


def test_required_contexts_come_from_the_canary(module: ModuleType) -> None:
    """The required list is read from the workflow the audit already trusts."""
    contexts = module.read_required_contexts()
    assert contexts, "the canary must define at least one required context"
    assert "CI gate" in contexts


def test_canary_without_the_key_is_rejected(module: ModuleType, tmp_path: Path) -> None:
    """A canary that lost the key fails loudly instead of returning nothing."""
    canary = tmp_path / "canary.yml"
    canary.write_text("jobs:\n  verify:\n    steps:\n      - run: echo hi\n", encoding="utf-8")
    with pytest.raises(ValueError, match="does not define"):
        module.read_required_contexts(canary)


def test_absent_context_is_missing_not_passing(module: ModuleType) -> None:
    """A required context with no check-run is reported as never having run."""
    report = module.classify(["CI gate", "review-bot-ack"], [_run("review-bot-ack")], sha="abc123")
    assert [state.name for state in report.missing] == ["CI gate"]
    assert not report.ok
    assert "never ran" in module.summary_line(report)
    assert "abc123" in module.summary_line(report)


def test_all_present_and_passing_is_ok(module: ModuleType) -> None:
    """Everything present and green reads as such."""
    report = module.classify(["CI gate", "review-bot-ack"], [_run("CI gate"), _run("review-bot-ack")], sha="abc123")
    assert report.ok
    assert module.summary_line(report) == "all required contexts present and completed"


def test_failing_context_is_present_not_missing(module: ModuleType) -> None:
    """A red required check is present; presence is what this script judges."""
    report = module.classify(["CI gate"], [_run("CI gate", conclusion="failure")], sha="abc123")
    assert report.ok, "a failing context is still present, so this guard stays quiet"
    assert report.states[0].state == module.FAILING
    assert "1 failing" in module.summary_line(report)


def test_pending_context_is_present(module: ModuleType) -> None:
    """A queued or running check is present, not absent."""
    report = module.classify(["CI gate"], [_run("CI gate", status="in_progress", conclusion=None)], sha="abc")
    assert report.states[0].state == module.PENDING
    assert report.ok
    assert "still running" in module.summary_line(report)


def test_cancelled_context_is_failing(module: ModuleType) -> None:
    """A cancelled run is a completed non-pass, not an absence."""
    report = module.classify(["review-bot-ack"], [_run("review-bot-ack", conclusion="cancelled")], sha="abc")
    assert report.states[0].state == module.FAILING


def test_skipped_context_has_its_own_state(module: ModuleType) -> None:
    """A skipped required job is neither a pass nor an absence."""
    report = module.classify(["CI gate"], [_run("CI gate", conclusion="skipped")], sha="abc")
    assert report.states[0].state == module.SKIPPED
    assert report.ok


def test_latest_run_of_a_repeated_name_wins(module: ModuleType) -> None:
    """A re-run replaces the earlier attempt's verdict."""
    runs = [_run("CI gate", conclusion="failure"), _run("CI gate", conclusion="success")]
    report = module.classify(["CI gate"], runs, sha="abc")
    assert report.states[0].state == module.PASSING


def test_unrelated_check_runs_do_not_satisfy_a_context(module: ModuleType) -> None:
    """A commit full of green checks can still be missing the required one."""
    runs = [_run(f"Test (ubuntu-latest, Python 3.13, shard {index})") for index in range(1, 5)]
    report = module.classify(["CI gate"], runs, sha="abc")
    assert [state.name for state in report.missing] == ["CI gate"]


def test_check_runs_file_accepts_both_shapes(module: ModuleType, tmp_path: Path) -> None:
    """The offline input reads the raw API object and a plain list alike."""
    api_shape = tmp_path / "api.json"
    api_shape.write_text(json.dumps({"check_runs": [_run("CI gate")]}), encoding="utf-8")
    list_shape = tmp_path / "list.json"
    list_shape.write_text(json.dumps([_run("CI gate")]), encoding="utf-8")

    assert module._load_check_runs_file(str(api_shape)) == [_run("CI gate")]
    assert module._load_check_runs_file(str(list_shape)) == [_run("CI gate")]


def test_cli_reports_missing_context_and_exits_non_zero(
    module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """End to end: an absent required context exits non-zero and says why."""
    runs = tmp_path / "runs.json"
    runs.write_text(json.dumps({"check_runs": [_run("review-bot-ack")]}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["check_required_contexts.py", "--sha", "deadbeef", "--check-runs", str(runs)])

    exit_code = module.main()
    captured = capsys.readouterr().out

    assert exit_code == 1
    assert "CI gate: missing" in captured
    assert "::error title=Required context never ran::" in captured
    assert "deadbeef" in captured


def test_cli_is_quiet_when_every_context_is_present(
    module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A commit carrying every required context exits zero."""
    present = [_run(name) for name in module.read_required_contexts()]
    runs = tmp_path / "runs.json"
    runs.write_text(json.dumps({"check_runs": present}), encoding="utf-8")
    monkeypatch.setattr(
        sys, "argv", ["check_required_contexts.py", "--sha", "deadbeef", "--check-runs", str(runs), "--json"]
    )

    exit_code = module.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["missing"] == []
    assert payload["sha"] == "deadbeef"
