"""CLI + supervisor-wiring tests for per-goal SLA contracts (#2549)."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.commands.sla_cmd import sla_group
from bernstein.core.orchestration.schedule_supervisor import ScheduleSupervisor
from bernstein.core.planning.schedule_store import ScheduleStore


def _run(runner: CliRunner, workdir: Path, *args: str) -> object:
    return runner.invoke(sla_group, list(args), catch_exceptions=False)


def test_cli_add_list_show_report_roundtrip(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as cwd:
        (Path(cwd) / ".sdd").mkdir()
        add = _run(
            runner,
            Path(cwd),
            "add",
            "--subject-type",
            "schedule",
            "--subject",
            "sched_nightly",
            "--fire-frequency",
            "3600",
            "--json",
        )
        assert add.exit_code == 0, add.output
        contract = json.loads(add.output)
        cid = contract["id"]
        assert contract["contract_hash"]

        listed = _run(runner, Path(cwd), "list", "--json")
        assert cid in listed.output

        shown = _run(runner, Path(cwd), "show", cid, "--json")
        assert json.loads(shown.output)["id"] == cid

        report = _run(runner, Path(cwd), "report", cid, "--json")
        assert report.exit_code == 0
        assert json.loads(report.output)["error_budget"]["total_events"] == 0


def test_cli_verify_roundtrips_a_real_receipt(tmp_path: Path) -> None:
    """A receipt written by the monitor verifies via the CLI verb (exit 0)."""
    from bernstein.core.orchestration.sla_monitor import build_monitor_from_sdd
    from bernstein.core.orchestration.sla_receipt import receipt_path
    from bernstein.core.planning.sla_store import SLAStore, build_contract
    from bernstein.core.security.audit import load_or_create_audit_key
    from bernstein.core.security.audit_chain import AuditChainStore

    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    store = SLAStore(sdd)
    store.add(build_contract(subject_type="schedule", subject_id="s", fire_frequency_s=60))
    # A frequency contract with a single old fire breaches "stopped firing".
    chain = AuditChainStore(sdd / "audit", key=load_or_create_audit_key())

    def _evidence(_contract: object, now: int) -> dict[str, object]:
        return {"fire_frequency": [{"fire_time": now - 100000, "entry_hash": "sha256:f1"}]}

    monitor = build_monitor_from_sdd(sdd, chain=chain)
    monitor._evidence_provider = _evidence  # type: ignore[attr-defined]
    receipts = monitor.evaluate(1_000_000)
    assert len(receipts) == 1
    path = receipt_path(sdd, receipts[0].receipt_id)

    runner = CliRunner()
    result = runner.invoke(sla_group, ["verify", str(path)], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert "OK" in result.output


def test_supervisor_tick_evaluates_sla_monitor_without_dispatch(tmp_path: Path) -> None:
    """The supervisor tick calls the injected SLA monitor and never dispatches for it."""
    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    store = ScheduleStore(sdd)

    dispatched: list[object] = []
    calls: list[int] = []

    class _Monitor:
        def evaluate(self, now: int) -> list[object]:
            calls.append(now)
            return []

    supervisor = ScheduleSupervisor(store, dispatched.append, audit_writer=None, sla_monitor=_Monitor())
    supervisor.tick(now=1_000_000)

    assert calls == [1_000_000]
    assert not dispatched  # no schedules registered; SLA eval must not dispatch


def test_supervisor_tick_is_unchanged_without_a_monitor(tmp_path: Path) -> None:
    """A supervisor with no monitor keeps the pre-#2549 behaviour."""
    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    store = ScheduleStore(sdd)
    supervisor = ScheduleSupervisor(store, lambda _e: None, audit_writer=None)
    assert supervisor.tick(now=1_000_000) == []
