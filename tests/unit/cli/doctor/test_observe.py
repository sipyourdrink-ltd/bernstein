"""Tests for ``bernstein doctor observe`` and its per-backend probes.

Covers:

* Soft-fail behaviour for every probe when env-vars are unset.
* Aggregation order and exit-code mapping in ``collect_reports``.
* Persistence + delta-since-last-check via the snapshot cache.
* Click wiring for ``bernstein doctor observe`` (Rich + JSON).
* A crashing probe must not bring down the umbrella.

The HTTP layer is never reached because the suite injects scrubbed env
mappings into each probe.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

import bernstein.cli.commands.doctor.observe as observe_module

# Importing main attaches the observability subcommands (code-scanning /
# observe) to the doctor group via the register helpers. The import must
# come before the doctor_group import so the side-effect runs first.
import bernstein.cli.main as _bernstein_main  # noqa: F401
from bernstein.cli.commands.advanced_cmd import doctor as doctor_group
from bernstein.cli.commands.doctor.backends import (
    BackendReport,
    MetricRow,
    ProbeStatus,
    apply_deltas,
    load_previous,
    probe_code_scanning,
    save_snapshot,
)


@pytest.mark.parametrize(
    ("probe", "env"),
    [
        (probe_code_scanning, {}),
    ],
)
def test_each_probe_soft_fails_with_empty_env(
    probe: Any,
    env: dict[str, str],
) -> None:
    """Every probe must return SKIPPED when its env-vars are missing."""

    report = probe(env=env)
    assert isinstance(report, BackendReport)
    assert report.status == ProbeStatus.SKIPPED
    assert report.metrics == []
    assert report.error is None


def test_probe_code_scanning_soft_fails_without_token() -> None:
    report = probe_code_scanning(env={"GITHUB_REPOSITORY": "owner/repo"})
    assert report.status == ProbeStatus.SKIPPED


def _report_with_metrics(values: dict[str, float]) -> BackendReport:
    return BackendReport(
        backend="dummy",
        status=ProbeStatus.OK,
        detail="ok",
        metrics=[MetricRow(name=k, value=str(v), numeric=v) for k, v in values.items()],
    )


def test_snapshot_round_trip(tmp_path: Path) -> None:
    report = _report_with_metrics({"coverage_pct": 87.5, "bugs": 0})
    save_snapshot(report, workdir=tmp_path)
    loaded = load_previous("dummy", workdir=tmp_path)
    assert loaded == {"coverage_pct": 87.5, "bugs": 0.0}


def test_apply_deltas_marks_new_metrics_when_no_baseline(tmp_path: Path) -> None:
    report = _report_with_metrics({"open_alerts": 3})
    annotated = apply_deltas(report, workdir=tmp_path)
    assert annotated.metrics[0].delta == "new"


def test_apply_deltas_computes_signed_diff(tmp_path: Path) -> None:
    base = _report_with_metrics({"open_alerts": 3})
    save_snapshot(base, workdir=tmp_path)

    fresh = _report_with_metrics({"open_alerts": 7})
    annotated = apply_deltas(fresh, workdir=tmp_path)
    assert annotated.metrics[0].delta.startswith("+4")


def test_skipped_reports_do_not_overwrite_baseline(tmp_path: Path) -> None:
    base = _report_with_metrics({"open_alerts": 9})
    save_snapshot(base, workdir=tmp_path)
    save_snapshot(
        BackendReport(backend="dummy", status=ProbeStatus.SKIPPED),
        workdir=tmp_path,
    )
    loaded = load_previous("dummy", workdir=tmp_path)
    assert loaded == {"open_alerts": 9.0}


def test_collect_reports_preserves_order_and_isolates_failures() -> None:
    """A crashing probe must become an ERROR row without stopping siblings."""

    def good(label: str) -> Any:
        def _probe() -> BackendReport:
            return BackendReport(backend=label, status=ProbeStatus.OK, detail="ok")

        return _probe

    def boom() -> BackendReport:
        # The raw message must never reach the stored report; only the
        # exception type name is persisted so tokens/URLs cannot leak.
        raise RuntimeError("synthetic secret token=abc123")

    reports = observe_module.collect_reports(
        probes=(("good", good("good")), ("boom", boom), ("good2", good("good2"))),
    )
    assert [r.backend for r in reports] == ["good", "boom", "good2"]
    assert reports[1].status == ProbeStatus.ERROR
    assert "RuntimeError" in (reports[1].error or "")
    assert "secret" not in (reports[1].error or "")


def test_observe_command_registered_under_doctor() -> None:
    runner = CliRunner()
    result = runner.invoke(doctor_group, ["observe", "--help"])
    assert result.exit_code == 0, result.output
    assert "observe" in result.output.lower()
    assert "--json" in result.output
    assert "--watch" in result.output


def test_observe_command_renders_table_with_skipped_probes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    for key in (
        "GITHUB_TOKEN",
        "GITHUB_REPOSITORY",
    ):
        monkeypatch.delenv(key, raising=False)

    runner = CliRunner()
    result = runner.invoke(doctor_group, ["observe", "--no-persist"])
    assert result.exit_code == 0, result.output
    for label in ("code-scanning",):
        assert label in result.output


def test_observe_command_emits_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    for key in (
        "GITHUB_TOKEN",
        "GITHUB_REPOSITORY",
    ):
        monkeypatch.delenv(key, raising=False)

    runner = CliRunner()
    result = runner.invoke(doctor_group, ["observe", "--json", "--no-persist"])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.output)
    assert [b["backend"] for b in payload["backends"]] == [
        "code-scanning",
    ]
    assert payload["summary"]["skipped"] == 1
    assert payload["summary"]["error"] == 0
    assert payload["summary"]["fail"] == 0


def test_observe_command_persists_cache_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    fake = BackendReport(
        backend="fake",
        status=ProbeStatus.OK,
        detail="synthetic",
        metrics=[MetricRow(name="m1", value="1", numeric=1.0)],
    )
    monkeypatch.setattr(observe_module, "DEFAULT_PROBES", (("fake", lambda: fake),))

    runner = CliRunner()
    result = runner.invoke(doctor_group, ["observe", "--json"])
    assert result.exit_code == 0, result.output

    cache_file = tmp_path / ".sdd" / "observability" / "fake.json"
    assert cache_file.exists()
    payload = json.loads(cache_file.read_text(encoding="utf-8"))
    assert payload["metrics"] == {"m1": 1.0}


def test_observe_command_no_persist_flag_skips_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    fake = BackendReport(
        backend="fake",
        status=ProbeStatus.OK,
        detail="synthetic",
        metrics=[MetricRow(name="m1", value="1", numeric=1.0)],
    )
    monkeypatch.setattr(observe_module, "DEFAULT_PROBES", (("fake", lambda: fake),))

    runner = CliRunner()
    result = runner.invoke(doctor_group, ["observe", "--json", "--no-persist"])
    assert result.exit_code == 0, result.output
    assert not (tmp_path / ".sdd" / "observability" / "fake.json").exists()


def test_observe_exit_code_is_nonzero_when_any_probe_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    warn = BackendReport(
        backend="warn",
        status=ProbeStatus.WARN,
        detail="something to look at",
        metrics=[MetricRow(name="m1", value="1", numeric=1.0)],
    )
    monkeypatch.setattr(observe_module, "DEFAULT_PROBES", (("warn", lambda: warn),))
    runner = CliRunner()
    result = runner.invoke(doctor_group, ["observe", "--json", "--no-persist"])
    assert result.exit_code == 1, result.output


def test_observe_code_scanning_classifies_high_severity_as_warn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> Any:
            return [
                {"rule": {"security_severity_level": "high"}},
                {"rule": {"security_severity_level": "low"}},
            ]

    def _get(*args: Any, **kwargs: Any) -> Any:
        return _Resp()

    import httpx

    monkeypatch.setattr(httpx, "get", _get)
    report = probe_code_scanning(
        env={
            "GITHUB_TOKEN": "t",
            "GITHUB_REPOSITORY": "owner/repo",
        }
    )
    assert report.status == ProbeStatus.WARN
    counts = {m.name: m.value for m in report.metrics}
    assert counts["high_alerts"] == "1"
    assert counts["low_alerts"] == "1"
