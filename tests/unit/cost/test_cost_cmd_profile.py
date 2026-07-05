"""CLI tests for per-profile cost attribution (issue #2245).

Covers ``bernstein cost --by profile``, the per-profile savings
section honesty rule, and ``bernstein cost profile-report``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.cost import cost_cmd, cost_profile_report_cmd
from bernstein.core.cost.profile_attribution import (
    EXCLUDED_LABEL,
    MIN_COMPARABLE_TASKS,
    UNATTRIBUTED_LABEL,
    record_profile_transition,
)
from bernstein.core.cost.spend_ledger import CallTags, SpendLedger
from bernstein.core.security.audit import AuditLog
from bernstein.core.security.audit_chain import EVENT_COST_PROFILE_REPORT


def _record(led: SpendLedger, task_id: str, profile: str, *, role: str = "backend", cost: float = 0.10) -> None:
    extra = {"response_profile": profile, "profile_content_sha256": "a" * 64} if profile else {}
    led.record(
        tags=CallTags(task_id=task_id, agent_id="a-1", role=role, extra=extra),
        model="sonnet",
        cost_usd=cost,
        output_tokens=100,
    )


@pytest.fixture()
def sdd(tmp_path: Path) -> Path:
    """A minimal ``.sdd`` tree with metrics and a profiled ledger."""
    sdd = tmp_path / ".sdd"
    metrics = sdd / "metrics"
    metrics.mkdir(parents=True)
    now = time.time()
    rows = [
        {
            "task_id": tid,
            "role": "backend",
            "model": "sonnet",
            "timestamp": now,
            "cost_usd": 0.10,
            "agent_id": "a-1",
            "janitor_passed": True,
        }
        for tid in ("t-terse-1", "t-verbose-1")
    ]
    (metrics / "tasks.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    led = SpendLedger(path=sdd / "cost" / "ledger.jsonl", run_id="r-1")
    _record(led, "t-terse-1", "terse", cost=0.10)
    _record(led, "t-terse-2", "terse", cost=0.12)
    _record(led, "t-verbose-1", "verbose", cost=0.30)
    _record(led, "t-none", "", cost=0.05)
    return sdd


def _invoke_by_profile(sdd: Path) -> dict[str, object]:
    runner = CliRunner()
    result = runner.invoke(
        cost_cmd,
        [
            "--metrics-dir",
            str(sdd / "metrics"),
            "--ledger",
            str(sdd / "cost" / "ledger.jsonl"),
            "--by",
            "profile",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


class TestCostByProfile:
    def test_grouped_by_profile_from_ledger(self, sdd: Path) -> None:
        data = _invoke_by_profile(sdd)
        assert data["grouped_by"] == "profile"
        grouped = data["grouped"]
        assert grouped["terse"]["cost_usd"] == pytest.approx(0.22)
        assert grouped["terse"]["tasks"] == 2
        assert grouped["verbose"]["cost_usd"] == pytest.approx(0.30)
        assert grouped[UNATTRIBUTED_LABEL]["cost_usd"] == pytest.approx(0.05)

    def test_totals_equal_ledger_sum_to_the_cent(self, sdd: Path) -> None:
        """Acceptance 4: grouped totals == per-task ledger entry sum."""
        data = _invoke_by_profile(sdd)
        grouped = data["grouped"]
        assert isinstance(grouped, dict)
        entries = SpendLedger.load_entries(sdd / "cost" / "ledger.jsonl")
        grouped_total = round(sum(float(v["cost_usd"]) for v in grouped.values()), 2)
        assert grouped_total == round(sum(e.cost_usd for e in entries), 2)

    def test_transitioned_task_shown_excluded(self, sdd: Path) -> None:
        record_profile_transition(
            sdd / "cost" / "profile_transitions.jsonl",
            task_id="t-verbose-1",
            agent_id="a-1",
            from_profile="verbose",
            to_profile="terse",
        )
        grouped = _invoke_by_profile(sdd)["grouped"]
        assert isinstance(grouped, dict)
        assert "verbose" not in grouped
        assert grouped[EXCLUDED_LABEL]["cost_usd"] == pytest.approx(0.30)

    def test_table_output_renders(self, sdd: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cost_cmd,
            [
                "--metrics-dir",
                str(sdd / "metrics"),
                "--ledger",
                str(sdd / "cost" / "ledger.jsonl"),
                "--by",
                "profile",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "terse" in result.output
        assert "By Profile" in result.output


class TestHonestyRuleInCli:
    def test_insufficient_comparable_runs_printed(self, sdd: Path) -> None:
        """Two profiles below the N-task bar: no savings claim, the
        insufficiency line is printed instead."""
        runner = CliRunner()
        result = runner.invoke(
            cost_cmd,
            ["--metrics-dir", str(sdd / "metrics"), "--ledger", str(sdd / "cost" / "ledger.jsonl")],
        )
        assert result.exit_code == 0, result.output
        assert "insufficient comparable runs" in result.output

    def _comparable_sdd(self, sdd: Path) -> None:
        led = SpendLedger(path=sdd / "cost" / "ledger.jsonl", run_id="r-2")
        for i in range(MIN_COMPARABLE_TASKS):
            _record(led, f"c-terse-{i}", "terse", cost=0.10)
            _record(led, f"c-verbose-{i}", "verbose", cost=0.30)

    def test_savings_claim_printed_when_comparable(self, sdd: Path) -> None:
        self._comparable_sdd(sdd)
        runner = CliRunner()
        result = runner.invoke(
            cost_cmd,
            ["--metrics-dir", str(sdd / "metrics"), "--ledger", str(sdd / "cost" / "ledger.jsonl")],
        )
        assert result.exit_code == 0, result.output
        assert "terse vs verbose" in result.output
        assert "insufficient comparable runs" not in result.output

    def test_json_carries_comparisons(self, sdd: Path) -> None:
        self._comparable_sdd(sdd)
        runner = CliRunner()
        result = runner.invoke(
            cost_cmd,
            ["--metrics-dir", str(sdd / "metrics"), "--ledger", str(sdd / "cost" / "ledger.jsonl"), "--json"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["insufficient_comparable_runs"] is False
        comps = data["profile_comparisons"]
        assert len(comps) == 1
        assert comps[0]["profile_a"] == "terse"
        assert comps[0]["profile_b"] == "verbose"

    def test_share_includes_profile_line(self, sdd: Path) -> None:
        self._comparable_sdd(sdd)
        runner = CliRunner()
        result = runner.invoke(
            cost_cmd,
            ["--metrics-dir", str(sdd / "metrics"), "--ledger", str(sdd / "cost" / "ledger.jsonl"), "--share"],
        )
        assert result.exit_code == 0, result.output
        assert "terse vs verbose" in result.output


class TestProfileReportCmd:
    def _invoke(self, sdd: Path, key_path: Path, *extra: str) -> tuple[int, str]:
        runner = CliRunner()
        result = runner.invoke(
            cost_profile_report_cmd,
            [
                "--metrics-dir",
                str(sdd / "metrics"),
                "--ledger",
                str(sdd / "cost" / "ledger.jsonl"),
                "--transitions",
                str(sdd / "cost" / "profile_transitions.jsonl"),
                "--audit-dir",
                str(sdd / "audit"),
                "--reports-dir",
                str(sdd / "reports" / "cost_profiles"),
                *extra,
            ],
            env={"BERNSTEIN_AUDIT_KEY_PATH": str(key_path)},
        )
        return result.exit_code, result.output

    def test_json_report_written_and_chained(self, sdd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        key_path = tmp_path / "keys" / "audit.key"
        monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_path))
        code, output = self._invoke(sdd, key_path, "--json")
        assert code == 0, output
        data = json.loads(output)
        sha = data["sha256"]
        artifact = Path(str(data["artifact"]))
        assert artifact.exists()
        assert artifact.name == f"{sha}.json"
        envelope = json.loads(artifact.read_text(encoding="utf-8"))
        assert envelope["sha256"] == sha
        assert envelope["content"]["profiles"]["terse"]["tasks"] == 2
        # Quality joined from metrics task records.
        assert envelope["content"]["profiles"]["terse"]["quality"]["verdict_pass_rate"] == pytest.approx(1.0)

        # The audit chain carries the event and verifies.
        log = AuditLog(audit_dir=sdd / "audit", key_path=key_path)
        events = log.query(event_type=EVENT_COST_PROFILE_REPORT)
        assert len(events) == 1
        assert events[0].details["report_sha256"] == sha
        ok, errors = log.verify()
        assert ok, errors

    def test_rerun_is_byte_identical(self, sdd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Acceptance 1 at the CLI seam: two runs over the same ledger
        produce the same content-addressed artifact."""
        key_path = tmp_path / "keys" / "audit.key"
        monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_path))
        code_1, out_1 = self._invoke(sdd, key_path, "--json")
        assert code_1 == 0, out_1
        first = Path(str(json.loads(out_1)["artifact"]))
        first_bytes = first.read_bytes()
        code_2, out_2 = self._invoke(sdd, key_path, "--json")
        assert code_2 == 0, out_2
        second = Path(str(json.loads(out_2)["artifact"]))
        assert second == first
        assert second.read_bytes() == first_bytes

    def test_human_output_mentions_sha_and_honesty(
        self, sdd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        key_path = tmp_path / "keys" / "audit.key"
        monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_path))
        code, output = self._invoke(sdd, key_path)
        assert code == 0, output
        assert "insufficient comparable runs" in output
        assert "sha256" in output.lower()

    def test_subcommand_reachable_via_cost_group(self, sdd: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cost_cmd, ["profile-report", "--help"])
        assert result.exit_code == 0, result.output
        assert "profile-report" in result.output or "profile" in result.output
