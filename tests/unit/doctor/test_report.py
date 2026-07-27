"""Unit tests for the doctor report renderer and aggregator."""

from __future__ import annotations

import asyncio
from io import StringIO
from typing import Any

import pytest
from rich.console import Console

from bernstein.cli.doctor import report as report_mod
from bernstein.cli.doctor.report import (
    STATUS_GLYPHS,
    DoctorResult,
    exit_code_for,
    render_report,
    run_all,
    summarize,
)


def _make(status: str = "ok", category: str = "installation") -> DoctorResult:
    return DoctorResult(
        name=f"sample:{status}",
        category=category,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        detail=f"detail-{status}",
    )


def test_summarize_zero_when_no_results() -> None:
    assert summarize([]) == {"ok": 0, "warn": 0, "fail": 0, "skip": 0}


def test_summarize_counts_each_status() -> None:
    results = [_make("ok"), _make("ok"), _make("fail"), _make("warn"), _make("skip")]
    assert summarize(results) == {"ok": 2, "warn": 1, "fail": 1, "skip": 1}


def test_exit_code_zero_when_no_fail() -> None:
    assert exit_code_for([_make("ok"), _make("warn"), _make("skip")]) == 0


def test_exit_code_one_when_any_fail() -> None:
    assert exit_code_for([_make("ok"), _make("fail")]) == 1


def test_render_report_includes_all_status_glyphs() -> None:
    results = [_make("ok"), _make("warn"), _make("fail"), _make("skip")]
    text = render_report(results)
    for status in ("OK", "WARN", "FAIL", "SKIP"):
        assert status in text


def test_render_report_footer_has_counts() -> None:
    results = [_make("ok"), _make("ok"), _make("warn")]
    text = render_report(results)
    assert "2 OK" in text
    assert "1 WARN" in text


def test_render_report_show_skip_false_hides_skip_rows() -> None:
    buf = StringIO()
    console = Console(file=buf, width=120, color_system=None, record=True)
    render_report([_make("skip"), _make("ok")], console=console, show_skip=False)
    rendered = console.export_text()
    assert "sample:skip" not in rendered
    assert "sample:ok" in rendered


def test_status_glyphs_cover_every_status() -> None:
    for status in ("ok", "warn", "fail", "skip"):
        assert status in STATUS_GLYPHS


def test_run_all_merges_categories(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_adapter(_: object = None) -> list[DoctorResult]:
        return [_make("ok", category="adapter")]

    async def fake_network(_: object = None) -> list[DoctorResult]:
        return [_make("skip", category="network")]

    monkeypatch.setattr(report_mod, "run_adapter_checks", fake_adapter, raising=False)
    monkeypatch.setattr(report_mod, "run_network_checks", fake_network, raising=False)

    # Patch lazy imports inside run_all.
    import bernstein.cli.doctor.adapter_checks as adapter_mod
    import bernstein.cli.doctor.network_checks as network_mod

    monkeypatch.setattr(adapter_mod, "run_adapter_checks", fake_adapter)
    monkeypatch.setattr(network_mod, "run_network_checks", fake_network)

    # Avoid actually probing installation checks; replace check_installations.
    from bernstein.cli import install_check

    monkeypatch.setattr(install_check, "check_installations", lambda: [])

    # Force environment probe to produce a known marker.
    import bernstein.cli.doctor.environment_checks as env_mod

    monkeypatch.setattr(env_mod, "run_environment_checks", lambda: [_make("ok", category="environment")])

    results = asyncio.run(run_all())
    categories = {r.category for r in results}
    assert categories == {"adapter", "network", "environment"}


def test_run_all_handles_empty_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    import bernstein.cli.doctor.adapter_checks as adapter_mod
    import bernstein.cli.doctor.environment_checks as env_mod
    import bernstein.cli.doctor.network_checks as network_mod

    async def empty(_: Any = None, **__: Any) -> list[DoctorResult]:
        return []

    monkeypatch.setattr(adapter_mod, "run_adapter_checks", empty)
    monkeypatch.setattr(network_mod, "run_network_checks", empty)
    monkeypatch.setattr(env_mod, "run_environment_checks", lambda: [])

    # The audit-lock probe (#3064) is a fifth source. It always emits exactly
    # one row, so it is pinned to a sentinel here: the property under test is
    # that ``run_all`` merges its sources and adds nothing of its own.
    import bernstein.cli.doctor.audit_lock_checks as audit_lock_mod

    sentinel = _make("skip", category="environment")
    monkeypatch.setattr(audit_lock_mod, "check_audit_lock_filesystem", lambda *_a, **_k: sentinel)

    from bernstein.cli import install_check

    monkeypatch.setattr(install_check, "check_installations", lambda: [])

    results = asyncio.run(run_all())
    assert results == [sentinel]


def test_render_report_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    # Snapshot test for the Rich report (stable layout, no terminal width drift).
    results = [
        DoctorResult(name="install:bernstein", category="installation", status="ok", detail="v1.0"),
        DoctorResult(
            name="adapter:claude",
            category="adapter",
            status="fail",
            detail="Binary `claude` not in PATH",
            remediation="install claude",
        ),
        DoctorResult(name="network:anthropic", category="network", status="skip", detail="BERNSTEIN_OFFLINE=1"),
        DoctorResult(
            name="env:github-actions",
            category="environment",
            status="ok",
            detail="GitHub Actions detected",
        ),
    ]
    text = render_report(results)
    # Key tokens that must appear in the rendered table:
    for token in (
        "Bernstein Doctor",
        "install:bernstein",
        "adapter:claude",
        "FAIL",
        "BERNSTEIN_OFFLINE=1",
        "GitHub Actions detected",
    ):
        assert token in text, f"missing token: {token}"


def test_doctor_result_extra_default_empty_tuple() -> None:
    result = DoctorResult(name="x", category="installation", status="ok", detail="")
    assert result.extra == ()
