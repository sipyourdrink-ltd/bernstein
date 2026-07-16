"""Unit tests for ``scripts/ci_weekly_digest.py``.

The aggregation is the trust-critical part of the weekly digest: it must
never count concurrency-superseded (cancelled) runs as real failures, must
separate scheduled from push failures, de-duplicate paginated runs, and
render a byte-identical body for identical input (so the idempotent weekly
upsert does not thrash the issue).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
_SPEC = importlib.util.spec_from_file_location("ci_weekly_digest", _SCRIPTS / "ci_weekly_digest.py")
assert _SPEC is not None and _SPEC.loader is not None
_MOD = importlib.util.module_from_spec(_SPEC)
# Register before exec so dataclass field processing can resolve the module.
sys.modules["ci_weekly_digest"] = _MOD
_SPEC.loader.exec_module(_MOD)

Run = _MOD.Run
parse_runs = _MOD.parse_runs
parse_issue_numbers = _MOD.parse_issue_numbers
build_summary = _MOD.build_summary
render_body = _MOD.render_body
render_alert = _MOD.render_alert
summary_json = _MOD.summary_json
main = _MOD.main


def _run(**kwargs) -> dict:
    base = {
        "id": 1,
        "conclusion": "failure",
        "event": "push",
        "name": "CI",
        "head_sha": "abcdef1234",
        "html_url": "https://example/1",
    }
    base.update(kwargs)
    return base


def _jsonl(objs: list[dict]) -> list[str]:
    return [json.dumps(o) for o in objs]


def _summary(objs: list[dict], skipped: list[int] | None = None, chronic_threshold: int = 2):
    runs = parse_runs(_jsonl(objs))
    return build_summary(
        runs,
        skipped or [],
        week_label="2026-W28",
        since="2026-07-05T00:00:00Z",
        lookback_days="7",
        chronic_threshold=chronic_threshold,
    )


# --- parsing -----------------------------------------------------------------


def test_parse_runs_dedupes_by_id() -> None:
    runs = parse_runs(_jsonl([_run(id=7), _run(id=7, name="CI-dup"), _run(id=8)]))
    assert len(runs) == 2
    assert {r.run_id for r in runs} == {7, 8}


def test_parse_runs_skips_blank_lines() -> None:
    assert parse_runs(["", "   ", json.dumps(_run())]) != []
    assert len(parse_runs(["", "   "])) == 0


def test_parse_issue_numbers_handles_hash_and_separators() -> None:
    assert parse_issue_numbers("#2485, 2458 #2423") == [2485, 2458, 2423]
    assert parse_issue_numbers("") == []
    assert parse_issue_numbers("not-a-number 12") == [12]


# --- the core data-quality fix: cancelled != failure -------------------------


def test_cancelled_runs_excluded_from_real_failures() -> None:
    # Mirrors the real 2026-W28 digest: 1 failure + 6 cancelled.
    objs = [_run(id=0, conclusion="failure")]
    objs += [_run(id=i, conclusion="cancelled") for i in range(1, 7)]
    summary = _summary(objs)
    assert summary.real_failure_count == 1
    assert summary.cancelled_count == 6
    assert summary.has_signal is True


def test_all_cancelled_reports_no_signal() -> None:
    summary = _summary([_run(id=i, conclusion="cancelled") for i in range(3)])
    assert summary.real_failure_count == 0
    assert summary.cancelled_count == 3
    assert summary.has_signal is False
    assert "informational" in summary.recommended_action


def test_timed_out_counts_as_real_failure() -> None:
    summary = _summary([_run(conclusion="timed_out")])
    assert summary.real_failure_count == 1


def test_success_and_skipped_ignored() -> None:
    summary = _summary(
        [
            _run(id=1, conclusion="success"),
            _run(id=2, conclusion="skipped"),
            _run(id=3, conclusion="failure"),
        ]
    )
    assert summary.real_failure_count == 1
    assert summary.cancelled_count == 0


# --- scheduled vs push -------------------------------------------------------


def test_scheduled_and_push_failures_split() -> None:
    summary = _summary(
        [
            _run(id=1, conclusion="failure", event="push", name="CI"),
            _run(id=2, conclusion="failure", event="schedule", name="Branch protection audit"),
        ]
    )
    assert summary.scheduled_failure_count == 1
    rows = summary.failures_by_workflow
    triggers = {(wf, trig) for wf, trig, _ in rows}
    assert ("CI", "push") in triggers
    assert ("Branch protection audit", "schedule") in triggers


def test_scheduled_failure_drives_recommended_action() -> None:
    summary = _summary([_run(id=2, conclusion="failure", event="schedule", name="Nightly")])
    assert "Scheduled" in summary.recommended_action
    assert "`Nightly`" in summary.recommended_action


# --- chronic-red + top offender ---------------------------------------------


def test_chronic_red_detected_at_threshold() -> None:
    objs = [_run(id=i, conclusion="failure", name="Flaky") for i in range(3)]
    objs += [_run(id=99, conclusion="failure", name="Other")]
    summary = _summary(objs, chronic_threshold=2)
    chronic = dict(summary.chronic_red)
    assert chronic == {"Flaky": 3}  # Other has only 1, below threshold
    assert "Chronically red" in summary.recommended_action
    assert "`Flaky`" in summary.recommended_action


def test_top_offender_deterministic_tie_break() -> None:
    # Two workflows tied at 1 failure -> alphabetical wins for stability.
    summary = _summary(
        [
            _run(id=1, conclusion="failure", name="Zeta"),
            _run(id=2, conclusion="failure", name="Alpha"),
        ],
        chronic_threshold=5,
    )
    assert summary.top_offender == ("Alpha", 1)


def test_clean_week_recommended_action() -> None:
    summary = _summary([_run(conclusion="success")])
    assert summary.has_signal is False
    assert "Clean week" in summary.recommended_action


# --- rendering ---------------------------------------------------------------


def test_body_headline_reports_real_not_inflated_count() -> None:
    objs = [_run(id=0, conclusion="failure")] + [_run(id=i, conclusion="cancelled") for i in range(1, 7)]
    body = render_body(_summary(objs))
    assert "Real CI failures on main: **1**" in body
    assert "Superseded/cancelled runs: 6" in body
    # cancelled runs must be in their own informational section, not the headline
    assert "Superseded / cancelled runs (informational)" in body


def test_body_is_deterministic() -> None:
    objs = [
        _run(id=3, conclusion="failure", name="Zeta", event="schedule"),
        _run(id=1, conclusion="failure", name="Alpha"),
        _run(id=2, conclusion="cancelled", name="Beta"),
    ]
    first = render_body(_summary(objs))
    second = render_body(_summary(objs))
    assert first == second


def test_body_has_no_provenance_leak() -> None:
    body = render_body(_summary([_run()]))
    lowered = body.lower()
    assert "borrowed-from" not in lowered
    assert "alarm-fatigue" not in lowered


def test_body_no_failures_message() -> None:
    body = render_body(_summary([_run(conclusion="success")]))
    assert "_No real failures in the window._" in body


def test_alert_only_summarizes_real_signal() -> None:
    summary = _summary([_run(conclusion="failure", name="CI")])
    alert = render_alert(summary)
    assert "2026-W28" in alert
    assert "1 real failure" in alert


# --- summary json ------------------------------------------------------------


def test_summary_json_shape() -> None:
    objs = [_run(id=i, conclusion="failure", name="Flaky") for i in range(2)]
    payload = summary_json(_summary(objs, skipped=[10, 11]))
    assert payload["real_failure_count"] == 2
    assert payload["has_signal"] is True
    assert payload["skipped_count"] == 2
    assert payload["top_offender"] == {"workflow": "Flaky", "failures": 2}
    assert payload["chronic_red"] == [{"workflow": "Flaky", "failures": 2}]


# --- end-to-end main() -------------------------------------------------------


def test_main_writes_body_and_prints_summary(tmp_path, capsys) -> None:
    runs_file = tmp_path / "runs.jsonl"
    runs_file.write_text(
        "\n".join(
            _jsonl(
                [
                    _run(id=1, conclusion="failure", name="CI"),
                    _run(id=2, conclusion="cancelled", name="CI"),
                ]
            )
        ),
        encoding="utf-8",
    )
    body_file = tmp_path / "body.md"
    alert_file = tmp_path / "alert.txt"
    rc = main(
        [
            "--runs-file",
            str(runs_file),
            "--skipped",
            "#2485 2458",
            "--week-label",
            "2026-W28",
            "--since",
            "2026-07-05T00:00:00Z",
            "--body-file",
            str(body_file),
            "--alert-file",
            str(alert_file),
        ]
    )
    assert rc == 0
    body = body_file.read_text(encoding="utf-8")
    assert "Real CI failures on main: **1**" in body
    assert "- #2485" in body
    assert alert_file.exists()  # has_signal -> alert written
    printed = json.loads(capsys.readouterr().out)
    assert printed["has_signal"] is True
    assert printed["real_failure_count"] == 1


def test_main_skips_alert_file_when_no_signal(tmp_path, capsys) -> None:
    runs_file = tmp_path / "runs.jsonl"
    runs_file.write_text("\n".join(_jsonl([_run(conclusion="cancelled")])), encoding="utf-8")
    body_file = tmp_path / "body.md"
    alert_file = tmp_path / "alert.txt"
    rc = main(
        [
            "--runs-file",
            str(runs_file),
            "--week-label",
            "2026-W28",
            "--since",
            "2026-07-05T00:00:00Z",
            "--body-file",
            str(body_file),
            "--alert-file",
            str(alert_file),
        ]
    )
    assert rc == 0
    assert not alert_file.exists()  # no signal -> no alert
    printed = json.loads(capsys.readouterr().out)
    assert printed["has_signal"] is False
