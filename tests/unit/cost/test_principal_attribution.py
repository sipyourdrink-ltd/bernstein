"""Per-principal / per-grant spend attribution (issue #4985).

The spend ledger and the grant chain were two records with no join: a
ledger row named the task and the agent, never the principal that
incurred the cost nor the grant that authorized it. These tests pin the
join and, more importantly, pin the honesty rule around it -- a row that
does not carry the full attribution tuple is reported as *unattributed*
and is never folded into a principal's total.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.cost.cost_tracker import CostTracker
from bernstein.core.cost.principal_attribution import (
    UNATTRIBUTED,
    PrincipalAttribution,
    PrincipalBudgetError,
    PrincipalEnvelope,
    attribution_from_grant,
    attribution_report,
    to_micro_usd,
)
from bernstein.core.cost.spend_ledger import CallTags, LedgerEntry, SpendLedger, aggregate_entries

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

ATTRIBUTED = PrincipalAttribution(
    principal_id="agent:reviewer",
    grant_id="g-1",
    authorizing_identity="manager:install-a",
)


def _ledger(tmp_path: Path) -> SpendLedger:
    return SpendLedger(path=tmp_path / "cost" / "ledger.jsonl", run_id="r-1")


def _read_rows(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# 1. the ledger row carries the attribution tuple
# ---------------------------------------------------------------------------


class TestLedgerCarriesAttribution:
    def test_ledger_row_carries_principal_and_grant(self, tmp_path: Path) -> None:
        led = _ledger(tmp_path)
        led.record(
            tags=CallTags(
                task_id="t-1",
                agent_id="a-1",
                principal_id=ATTRIBUTED.principal_id,
                grant_id=ATTRIBUTED.grant_id,
                authorizing_identity=ATTRIBUTED.authorizing_identity,
            ),
            model="sonnet",
            cost_usd=0.25,
        )
        rows = _read_rows(led.path)
        assert rows[0]["principal_id"] == "agent:reviewer"
        assert rows[0]["grant_id"] == "g-1"
        assert rows[0]["authorizing_identity"] == "manager:install-a"

        loaded = SpendLedger.load_entries(led.path)
        assert loaded[0].attribution() == ATTRIBUTED

    def test_attribution_from_grant_receipt_names_the_issuer(self, tmp_path: Path) -> None:
        from bernstein.core.identity.grants import GrantLedger, GrantSigner
        from bernstein.core.security.agent_card_keystore import AgentCardKeystore

        private_pem, public_pem = AgentCardKeystore(tmp_path / "keys").load_or_generate()
        ledger = GrantLedger(
            root=tmp_path / "audit",
            key=b"k" * 32,
            signer=GrantSigner(private_pem, public_pem, issuer="manager:install-a"),
        )
        receipt = ledger.issue_grant(
            run_id="r-1",
            task_id="t-1",
            secret_name="ANTHROPIC_API_KEY",
            audience="api.anthropic.com",
        )
        attribution = attribution_from_grant(receipt, principal_id="agent:reviewer")
        assert attribution.principal_id == "agent:reviewer"
        assert attribution.grant_id == receipt.grant_id
        assert attribution.authorizing_identity == "manager:install-a"
        assert attribution.attributed is True


# ---------------------------------------------------------------------------
# 2. grouping reconciles exactly with the run total
# ---------------------------------------------------------------------------


class TestReconciliation:
    def test_grouping_by_principal_reconciles_exactly_with_run_total(self, tmp_path: Path) -> None:
        led = _ledger(tmp_path)
        # Costs chosen so a float accumulation drifts off the run total.
        costs = [0.1, 0.2, 0.3, 0.7, 0.000001, 0.000002]
        principals = ["agent:a", "agent:b", "agent:a", "agent:c", "agent:b", "agent:a"]
        for i, (cost, principal) in enumerate(zip(costs, principals, strict=True)):
            led.record(
                tags=CallTags(
                    task_id=f"t-{i}",
                    principal_id=principal,
                    grant_id=f"g-{principal}",
                    authorizing_identity="manager:install-a",
                ),
                model="sonnet",
                cost_usd=cost,
            )
        entries = SpendLedger.load_entries(led.path)
        report = attribution_report(entries, dimension="principal")

        run_total_micro = sum(to_micro_usd(e.cost_usd) for e in entries)
        assert report.total_micro_usd == run_total_micro
        assert sum(row.micro_usd for row in report.rows) == run_total_micro
        assert report.reconciles is True
        assert report.unattributed_micro_usd == 0

    def test_grouping_by_grant_and_by_identity_reconcile_with_the_same_total(self, tmp_path: Path) -> None:
        led = _ledger(tmp_path)
        for i in range(5):
            led.record(
                tags=CallTags(
                    task_id=f"t-{i}",
                    principal_id=f"agent:{i % 2}",
                    grant_id=f"g-{i % 3}",
                    authorizing_identity="manager:install-a",
                ),
                model="sonnet",
                cost_usd=0.11,
            )
        entries = SpendLedger.load_entries(led.path)
        totals = {
            dim: attribution_report(entries, dimension=dim).total_micro_usd
            for dim in ("principal", "grant", "authorizing_identity")
        }
        assert len(set(totals.values())) == 1
        assert totals["principal"] == 550_000


# ---------------------------------------------------------------------------
# 3. an ingested event without attribution is never assigned to a principal
# ---------------------------------------------------------------------------


class TestUnattributedIsNeverAssigned:
    def test_ingested_event_without_attribution_is_reported_unattributed(self, tmp_path: Path) -> None:
        led = _ledger(tmp_path)
        led.record(
            tags=CallTags(
                task_id="t-1",
                principal_id="agent:a",
                grant_id="g-1",
                authorizing_identity="manager:install-a",
            ),
            model="sonnet",
            cost_usd=1.0,
        )
        # Activity ingested from a runtime we did not schedule: no tuple.
        led.record(
            tags=CallTags(task_id="t-ingested", extra={"activity_source": "adapter"}),
            model="sonnet",
            cost_usd=4.0,
        )
        report = attribution_report(SpendLedger.load_entries(led.path), dimension="principal")

        keys = {row.key: row for row in report.rows}
        assert keys["agent:a"].micro_usd == 1_000_000
        assert keys[UNATTRIBUTED].micro_usd == 4_000_000
        assert report.unattributed_micro_usd == 4_000_000
        assert report.unattributed_calls == 1
        assert report.total_micro_usd == 5_000_000

    def test_half_tuple_is_unattributed_and_counted_as_partial(self, tmp_path: Path) -> None:
        led = _ledger(tmp_path)
        # A principal with no grant cannot say by whose authority it spent.
        led.record(tags=CallTags(task_id="t-1", principal_id="agent:a"), model="sonnet", cost_usd=2.0)
        report = attribution_report(SpendLedger.load_entries(led.path), dimension="principal")

        keys = {row.key for row in report.rows}
        assert keys == {UNATTRIBUTED}
        assert report.partial_calls == 1
        assert report.partial_micro_usd == 2_000_000


# ---------------------------------------------------------------------------
# 4. historical rows are reported, not backfilled
# ---------------------------------------------------------------------------


class TestHistoricalRows:
    def test_historical_row_without_attribution_is_reported_not_backfilled(self, tmp_path: Path) -> None:
        path = tmp_path / "cost" / "ledger.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        # A row written before the attribution columns existed.
        legacy = {
            "ts": 1_700_000_000.0,
            "ts_iso": "2023-11-14T22:13:20+00:00",
            "run_id": "r-0",
            "task_id": "t-old",
            "agent_id": "a-old",
            "role": "backend",
            "feature_label": "",
            "model": "sonnet",
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "cost_usd": 3.0,
            "quota_envelope": "subscription",
            "tags": {"task_id": "t-old", "agent_id": "a-old", "role": "backend"},
        }
        path.write_text(json.dumps(legacy) + "\n", encoding="utf-8")

        led = SpendLedger(path=path, run_id="r-1")
        led.record(
            tags=CallTags(
                task_id="t-new",
                principal_id="agent:a",
                grant_id="g-1",
                authorizing_identity="manager:install-a",
            ),
            model="sonnet",
            cost_usd=1.0,
        )
        entries = SpendLedger.load_entries(path)
        assert entries[0].attribution() == PrincipalAttribution()

        report = attribution_report(entries, dimension="principal")
        keys = {row.key: row for row in report.rows}
        # The only principal on record must not absorb the historical spend.
        assert keys["agent:a"].micro_usd == 1_000_000
        assert keys[UNATTRIBUTED].micro_usd == 3_000_000
        assert report.unattributed_calls == 1
        assert report.partial_calls == 0


# ---------------------------------------------------------------------------
# 5. a per-principal envelope refuses at its ceiling
# ---------------------------------------------------------------------------


class TestPerPrincipalEnvelope:
    def test_per_principal_envelope_refuses_at_its_ceiling(self, tmp_path: Path) -> None:
        tracker = CostTracker(
            run_id="r-1",
            principal_envelopes={"agent:a": PrincipalEnvelope(principal_id="agent:a", hard_budget_usd=1.0)},
        )
        tracker.record(
            "a-1",
            "t-1",
            "sonnet",
            0,
            0,
            cost_usd=0.75,
            principal_id="agent:a",
            grant_id="g-1",
            authorizing_identity="manager:install-a",
        )
        with pytest.raises(PrincipalBudgetError) as excinfo:
            tracker.record(
                "a-1",
                "t-2",
                "sonnet",
                0,
                0,
                cost_usd=0.50,
                principal_id="agent:a",
                grant_id="g-1",
                authorizing_identity="manager:install-a",
            )
        # The refused call must not have moved any total.
        assert tracker.spent_by_principal()["agent:a"] == pytest.approx(0.75)
        assert excinfo.value.receipt.principal_id == "agent:a"

    def test_refusal_receipt_names_the_principal_and_its_grant(self, tmp_path: Path) -> None:
        tracker = CostTracker(
            run_id="r-1",
            principal_envelopes={"agent:a": PrincipalEnvelope(principal_id="agent:a", hard_budget_usd=0.10)},
        )
        with pytest.raises(PrincipalBudgetError) as excinfo:
            tracker.record(
                "a-1",
                "t-1",
                "sonnet",
                0,
                0,
                cost_usd=0.50,
                principal_id="agent:a",
                grant_id="g-7",
                authorizing_identity="manager:install-a",
            )
        receipt = excinfo.value.receipt.to_dict()
        assert receipt["principal_id"] == "agent:a"
        assert receipt["grant_id"] == "g-7"
        assert receipt["authorizing_identity"] == "manager:install-a"
        assert receipt["cap_usd"] == pytest.approx(0.10)
        assert receipt["attempted_usd"] == pytest.approx(0.50)
        assert "agent:a" in str(excinfo.value)

    def test_envelope_of_another_principal_does_not_refuse(self, tmp_path: Path) -> None:
        tracker = CostTracker(
            run_id="r-1",
            principal_envelopes={"agent:a": PrincipalEnvelope(principal_id="agent:a", hard_budget_usd=0.10)},
        )
        tracker.record(
            "a-1",
            "t-1",
            "sonnet",
            0,
            0,
            cost_usd=5.0,
            principal_id="agent:b",
            grant_id="g-1",
            authorizing_identity="manager:install-a",
        )
        assert tracker.spent_by_principal()["agent:b"] == pytest.approx(5.0)

    def test_unattributed_spend_is_bucketed_apart_from_every_principal(self, tmp_path: Path) -> None:
        tracker = CostTracker(run_id="r-1")
        tracker.record("a-1", "t-1", "sonnet", 0, 0, cost_usd=1.0)
        tracker.record(
            "a-2",
            "t-2",
            "sonnet",
            0,
            0,
            cost_usd=2.0,
            principal_id="agent:a",
            grant_id="g-1",
            authorizing_identity="manager:install-a",
        )
        totals = tracker.spent_by_principal()
        assert totals[UNATTRIBUTED] == pytest.approx(1.0)
        assert totals["agent:a"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# 6. the tracker threads attribution into the ledger row
# ---------------------------------------------------------------------------


class TestTrackerThreadsAttribution:
    def test_cost_tracker_threads_attribution_into_the_ledger_row(self, tmp_path: Path) -> None:
        led = _ledger(tmp_path)
        tracker = CostTracker(run_id="r-1", spend_ledger=led)
        tracker.record(
            "a-1",
            "t-1",
            "sonnet",
            10,
            5,
            cost_usd=0.5,
            principal_id="agent:a",
            grant_id="g-1",
            authorizing_identity="manager:install-a",
        )
        rows = _read_rows(led.path)
        assert rows[0]["principal_id"] == "agent:a"
        assert rows[0]["grant_id"] == "g-1"
        assert rows[0]["authorizing_identity"] == "manager:install-a"


# ---------------------------------------------------------------------------
# 7. aggregation + CLI surface
# ---------------------------------------------------------------------------


class TestAggregationSurface:
    def test_aggregate_entries_supports_principal_and_grant_dimensions(self) -> None:
        entries = [
            LedgerEntry(
                ts=1.0,
                ts_iso="",
                run_id="r",
                task_id="t",
                agent_id="a",
                role="",
                feature_label="",
                model="sonnet",
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=0,
                cache_write_tokens=0,
                cost_usd=1.0,
                principal_id="agent:a",
                grant_id="g-1",
                authorizing_identity="manager:install-a",
            ),
            LedgerEntry(
                ts=2.0,
                ts_iso="",
                run_id="r",
                task_id="t",
                agent_id="a",
                role="",
                feature_label="",
                model="sonnet",
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=0,
                cache_write_tokens=0,
                cost_usd=2.0,
            ),
        ]
        by_principal = aggregate_entries(entries, "principal")
        assert by_principal["agent:a"]["cost_usd"] == pytest.approx(1.0)
        assert by_principal[UNATTRIBUTED]["cost_usd"] == pytest.approx(2.0)
        assert aggregate_entries(entries, "grant")["g-1"]["calls"] == 1
        assert aggregate_entries(entries, "authorizing_identity")[UNATTRIBUTED]["calls"] == 1

    def test_cost_cli_by_principal_reports_the_unattributed_bucket(self, tmp_path: Path) -> None:
        import time

        from bernstein.cli.cost import cost_cmd
        from click.testing import CliRunner

        sdd = tmp_path / ".sdd"
        metrics = sdd / "metrics"
        metrics.mkdir(parents=True)
        (metrics / "tasks.jsonl").write_text(
            json.dumps(
                {
                    "task_id": "t-1",
                    "model": "claude-sonnet-4",
                    "timestamp": time.time(),
                    "tokens_prompt": 100,
                    "tokens_completion": 50,
                    "cost_usd": 0.05,
                    "agent_id": "a-1",
                }
            )
        )
        led = SpendLedger(path=sdd / "cost" / "ledger.jsonl", run_id="r-1")
        led.record(
            tags=CallTags(
                task_id="t-1",
                principal_id="agent:a",
                grant_id="g-1",
                authorizing_identity="manager:install-a",
            ),
            model="sonnet",
            cost_usd=0.10,
        )
        led.record(tags=CallTags(task_id="t-2"), model="sonnet", cost_usd=0.40)

        result = CliRunner().invoke(
            cost_cmd,
            [
                "--metrics-dir",
                str(metrics),
                "--ledger",
                str(sdd / "cost" / "ledger.jsonl"),
                "--by",
                "principal",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        grouped = json.loads(result.output)["grouped"]
        assert grouped["agent:a"]["cost_usd"] == pytest.approx(0.10)
        assert grouped[UNATTRIBUTED]["cost_usd"] == pytest.approx(0.40)
