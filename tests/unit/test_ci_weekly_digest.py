"""Unit tests for ``scripts/ci_weekly_digest.py``.

The aggregation is the trust-critical part of the weekly digest: it must
never count concurrency-superseded (cancelled) runs as real failures, must
separate scheduled from push failures, de-duplicate paginated runs, and
render a byte-identical body for identical input (so the idempotent weekly
upsert does not thrash the issue).
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
_SPEC = importlib.util.spec_from_file_location("ci_weekly_digest", _SCRIPTS / "ci_weekly_digest.py")
assert _SPEC is not None and _SPEC.loader is not None
_MOD = importlib.util.module_from_spec(_SPEC)
# Register before exec so dataclass field processing can resolve the module.
sys.modules["ci_weekly_digest"] = _MOD
_SPEC.loader.exec_module(_MOD)

Run = _MOD.Run
QualityReport = _MOD.QualityReport
QualityRunMeta = _MOD.QualityRunMeta
QUALITY_ARTIFACT_NAMES = _MOD.QUALITY_ARTIFACT_NAMES
fetch_quality_report = _MOD.fetch_quality_report
parse_quality_artifact_files = _MOD.parse_quality_artifact_files
render_quality_section = _MOD.render_quality_section
select_quality_artifacts = _MOD.select_quality_artifacts
parse_runs = _MOD.parse_runs
parse_issue_numbers = _MOD.parse_issue_numbers
build_summary = _MOD.build_summary
render_body = _MOD.render_body
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
        ]
    )
    assert rc == 0
    body = body_file.read_text(encoding="utf-8")
    assert "Real CI failures on main: **1**" in body
    assert "- #2485" in body
    printed = json.loads(capsys.readouterr().out)
    assert printed["has_signal"] is True
    assert printed["real_failure_count"] == 1


def test_main_reports_no_signal_for_a_clean_or_cancelled_only_week(tmp_path, capsys) -> None:
    runs_file = tmp_path / "runs.jsonl"
    runs_file.write_text("\n".join(_jsonl([_run(conclusion="cancelled")])), encoding="utf-8")
    body_file = tmp_path / "body.md"
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
        ]
    )
    assert rc == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["has_signal"] is False


# --- weekly code quality summary (K-2..K-5, K-7) ----------------------------


def _sarif_bytes(tool: str, rules: list[str], *, path: str = "src/a.py", msg: str = "m") -> bytes:
    loc = [{"physicalLocation": {"artifactLocation": {"uri": path}, "region": {"startLine": 9}}}]
    res = [{"ruleId": rid, "message": {"text": msg}, "locations": loc} for rid in rules]
    return json.dumps({"version": "2.1.0", "runs": [{"tool": {"driver": {"name": tool}}, "results": res}]}).encode()


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _base_files() -> dict[str, dict[str, bytes]]:
    return {
        "vulture-sarif": {"vulture.sarif": _sarif_bytes("vulture", ["vulture/dead-code"])},
        "refurb-sarif": {"refurb.sarif": _sarif_bytes("refurb", ["FURB123", "FURB184"])},
        "perflint-sarif": {
            "perflint.sarif": _sarif_bytes("perflint", []),
            "perflint.txt": (
                b"************* Module pkg.mod\n"
                b"Command line:1: [W0012(unknown-option-value), ] Unknown option value for '--enable', got 'W8203'\n"
                b"src/pkg/mod.py:12: [W8201(loop-invariant-statement), fn] Consider moving outside loop.\n"
            ),
        },
    }


def _mock_gh_bytes(requested: list[str] | None = None):
    specs = [
        (14757695286, "trivy-iac-sarif"),
        (14757557538, "semgrep-sarif"),
        (14757317720, "perflint-sarif"),
        (14757142903, "vulture-sarif"),
        (14756778848, "refurb-sarif"),
    ]
    all_five = [{"id": aid, "name": n, "expired": False} for aid, n in specs]
    base = _base_files()
    zips = {aid: _zip_bytes(base[n]) for aid, n in specs if n in base}
    run_obj = {
        "id": 45027559316,
        "event": "schedule",
        "status": "completed",
        "created_at": "2026-11-22T13:27:13Z",
        "head_sha": "22d40089c762952f52a220696fb978f79365bbf0",
        "html_url": "https://github.com/sipyourdrink-ltd/bernstein/actions/runs/45027559316",
    }

    def _fetch(ep: str) -> bytes:
        if "event=schedule" in ep:
            return json.dumps({"workflow_runs": [run_obj]}).encode()
        if "event=workflow_dispatch" in ep:
            return b'{"workflow_runs": []}'
        if ep.endswith("/artifacts?per_page=30"):
            return json.dumps({"artifacts": all_five}).encode()
        if requested is not None:
            requested.append(ep)
        return zips[int(ep.rstrip("/zip").rsplit("/", 1)[-1])]

    return _fetch


def test_quality_fetch_requests_exact_three_allowlisted_artifacts_only() -> None:
    assert QUALITY_ARTIFACT_NAMES == ("vulture-sarif", "refurb-sarif", "perflint-sarif")
    requested: list[str] = []
    report = fetch_quality_report(
        "sipyourdrink-ltd/bernstein",
        now=datetime(2026, 11, 22, 19, 8, tzinfo=UTC),
        api_get_bytes=_mock_gh_bytes(requested),
    )
    assert report.status == "ok"
    assert requested == [
        f"repos/sipyourdrink-ltd/bernstein/actions/artifacts/{aid}/zip"
        for aid in (14757142903, 14756778848, 14757317720)
    ]


@pytest.mark.parametrize(
    "bad",
    [
        {"vulture-sarif": {"vulture.sarif": _sarif_bytes("semgrep", ["vulture/dead-code"])}},
        {"refurb-sarif": {"refurb.sarif": _sarif_bytes("refurb", ["FURB123", "B101"])}},
        {
            "perflint-sarif": {
                "perflint.sarif": _sarif_bytes("perflint", []),
                "perflint.txt": b"src/a.py:9: [W9001(other), fn] nope\n",
            }
        },
    ],
    ids=["foreign-tool", "bad-sarif-rule", "bad-perflint-rule"],
)
def test_quality_fail_closed_omits_section_with_neutral_line(bad: dict[str, dict[str, bytes]]) -> None:
    meta = QualityRunMeta(45027559316, "schedule", "2026-11-22T13:27:13Z", "22d40089c7", "https://example/run")
    report = parse_quality_artifact_files(_base_files() | bad, meta)
    section = render_quality_section(report)
    assert report.status == "rejected" and "\n" not in section and "omitted" in section
    assert "FURB" not in section and "W8201" not in section


def test_quality_render_omits_paths_lines_messages_and_snippets() -> None:
    meta = QualityRunMeta(45027559316, "schedule", "2026-11-22T13:27:13Z", "22d40089c7", "https://example/run")
    files = {
        "vulture-sarif": {
            "vulture.sarif": _sarif_bytes("vulture", ["vulture/dead-code"], path="src/sec/key.py", msg="SECRET_1")
        },
        "refurb-sarif": {"refurb.sarif": _sarif_bytes("refurb", ["FURB123"], path="src/sec/tok.py", msg="SECRET_2")},
        "perflint-sarif": {
            "perflint.sarif": _sarif_bytes("perflint", []),
            "perflint.txt": b"src/sec/hot.py:98765: [W8201(fake-slug-leak), secret_fn] SECRET_3\n",
        },
    }
    full = render_quality_section(parse_quality_artifact_files(files, meta))
    assert all(
        s not in full
        for s in ("src/sec", "key.py", "tok.py", "hot.py", "98765", "SECRET_", "fake-slug-leak", "secret_fn")
    )
    assert "vulture/dead-code" in full and "FURB123" in full and "W8201" in full


def test_quality_staleness_guard_via_cli(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(_MOD, "_gh_api_bytes", _mock_gh_bytes())
    runs_file, stale_out, fresh_out = tmp_path / "runs.jsonl", tmp_path / "stale.md", tmp_path / "fresh.md"
    runs_file.write_text(json.dumps(_run(conclusion="success")), encoding="utf-8")
    base_args = [
        "--runs-file",
        str(runs_file),
        "--week-label",
        "2026-W47",
        "--since",
        "2026-11-15T12:00:00Z",
        "--quality-repo",
        "sipyourdrink-ltd/bernstein",
    ]
    assert main([*base_args, "--now", "2026-11-30T13:27:14Z", "--body-file", str(stale_out)]) == 0
    assert (
        "older than 8 days" in stale_out.read_text(encoding="utf-8")
        and json.loads(capsys.readouterr().out)["quality_status"] == "stale"
    )
    assert main([*base_args, "--now", "2026-11-30T13:27:13Z", "--body-file", str(fresh_out)]) == 0
    fresh_text = fresh_out.read_text(encoding="utf-8")
    assert "## Weekly code quality summary (advisory)" in fresh_text and "(2026-11-22, `22d4008`)" in fresh_text
    assert json.loads(capsys.readouterr().out)["quality_status"] == "ok"

    monkeypatch.setattr(_MOD, "_gh_api_bytes", lambda _ep: (_ for _ in ()).throw(OSError("offline")))
    assert fetch_quality_report("sipyourdrink-ltd/bernstein").status == "unavailable"
