"""A budget halt is reconstructable from the audit chain alone (issue #2918).

``SpendLedger`` already marks each soft / hard cap transition exactly once
and then only calls ``logger.warning``, so "this run stopped because of its
budget" survived in stderr and nowhere else. These tests pin the property
the chain entry is there to provide:

1. A halt driven by the hard cap lands in the chain, exactly once.
2. A second ``record()`` past the cap adds no second entry.
3. The soft cap halt is a distinct, closed band on the same event.
4. Amounts in the signed payload are integer nano-USD, never floats.
5. Editing the recorded cap afterwards fails chain verification.
6. With no chain attached the ledger behaves exactly as it does today.
7. Threads racing past the cap still produce exactly one entry.
8. A chain that refuses the append never breaks the ledger.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from bernstein.core.cost.showback_canonical import nano_usd_from_float
from bernstein.core.cost.spend_ledger import CallTags, SpendLedger
from bernstein.core.security.audit_chain import EVENT_BUDGET_HALT, AuditChainStore

_KEY = b"k" * 32


def _chain(tmp_path: Path) -> AuditChainStore:
    return AuditChainStore(tmp_path / "audit", key=_KEY)


def _halt_events(chain: AuditChainStore) -> list[Any]:
    return chain.query(event_type=EVENT_BUDGET_HALT)


class TestBudgetHaltIsInTheChain:
    def test_hard_cap_halt_is_recorded_in_the_chain_not_only_stderr(self, tmp_path: Path) -> None:
        chain = _chain(tmp_path)
        led = SpendLedger(path=tmp_path / "ledger.jsonl", run_id="r-1", hard_budget_usd=2.0, chain=chain)

        led.record(tags=CallTags(task_id="t-1"), model="opus", cost_usd=2.5)

        events = _halt_events(chain)
        assert len(events) == 1
        details = events[0].details
        assert details["run_id"] == "r-1"
        assert details["band"] == "hard"
        assert details["prev_chain_digest"] is not None
        ok, errors = chain.verify()
        assert ok, errors

    def test_second_record_past_the_cap_adds_no_second_halt_event(self, tmp_path: Path) -> None:
        chain = _chain(tmp_path)
        led = SpendLedger(path=tmp_path / "ledger.jsonl", hard_budget_usd=2.0, chain=chain)

        led.record(tags=CallTags(), model="opus", cost_usd=2.5)
        led.record(tags=CallTags(), model="opus", cost_usd=1.0)

        assert len(_halt_events(chain)) == 1

    def test_soft_cap_halt_is_recorded_with_its_own_band(self, tmp_path: Path) -> None:
        chain = _chain(tmp_path)
        led = SpendLedger(path=tmp_path / "ledger.jsonl", budget_usd=5.0, chain=chain)

        led.record(tags=CallTags(), model="sonnet", cost_usd=5.0)

        events = _halt_events(chain)
        assert len(events) == 1
        assert events[0].details["band"] == "soft"
        assert events[0].details["cap_nano_usd"] == nano_usd_from_float(5.0)

    def test_halt_amounts_are_integer_nano_usd_not_floats(self, tmp_path: Path) -> None:
        chain = _chain(tmp_path)
        led = SpendLedger(path=tmp_path / "ledger.jsonl", hard_budget_usd=0.25, chain=chain)

        led.record(tags=CallTags(), model="opus", cost_usd=0.30)

        details = _halt_events(chain)[0].details
        assert isinstance(details["spent_nano_usd"], int)
        assert not isinstance(details["spent_nano_usd"], bool)
        assert isinstance(details["cap_nano_usd"], int)
        assert details["spent_nano_usd"] == nano_usd_from_float(0.30)
        assert details["cap_nano_usd"] == nano_usd_from_float(0.25)


class TestBudgetHaltCannotBeEditedAfterwards:
    def test_edited_halt_cap_fails_chain_verification(self, tmp_path: Path) -> None:
        """Load-bearing: the recorded cap is only worth something if a later
        edit of it is detectable."""
        audit_dir = tmp_path / "audit"
        chain = AuditChainStore(audit_dir, key=_KEY)
        led = SpendLedger(path=tmp_path / "ledger.jsonl", hard_budget_usd=2.0, chain=chain)
        led.record(tags=CallTags(), model="opus", cost_usd=2.5)
        assert AuditChainStore(audit_dir, key=_KEY).verify()[0] is True

        log_file = next(audit_dir.glob("*.jsonl"))
        lines = log_file.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            entry = json.loads(line)
            if entry.get("event_type") != EVENT_BUDGET_HALT:
                continue
            entry["details"]["cap_nano_usd"] = entry["details"]["cap_nano_usd"] * 10
            lines[i] = json.dumps(entry, sort_keys=True)
            break
        else:  # pragma: no cover - the halt event must be on disk
            raise AssertionError("no budget halt event written")
        log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        assert AuditChainStore(audit_dir, key=_KEY).verify()[0] is False


class TestLedgerWithoutAChain:
    def test_ledger_without_a_chain_records_no_events_and_still_halts(self, tmp_path: Path) -> None:
        """Regression: unattached ledgers keep today's soft-halt behaviour."""
        led = SpendLedger(path=tmp_path / "ledger.jsonl", budget_usd=5.0, hard_budget_usd=6.0)

        status = led.record(tags=CallTags(), model="opus", cost_usd=6.0)

        assert status.soft_halt is True
        assert status.hard_halt is True
        assert led.admits() is False
        assert list((tmp_path / "audit").glob("*.jsonl")) == []


class TestBudgetHaltUnderConcurrency:
    def test_concurrent_records_past_the_hard_cap_emit_one_halt_event(self, tmp_path: Path) -> None:
        chain = _chain(tmp_path)
        led = SpendLedger(path=tmp_path / "ledger.jsonl", hard_budget_usd=1.0, chain=chain)
        start = threading.Barrier(8)

        def spend() -> None:
            start.wait()
            led.record(tags=CallTags(), model="opus", cost_usd=1.0)

        threads = [threading.Thread(target=spend) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(_halt_events(chain)) == 1
        ok, errors = chain.verify()
        assert ok, errors


class TestChainFailureNeverBreaksTheLedger:
    def test_chain_append_failure_does_not_break_the_ledger(self, tmp_path: Path) -> None:
        class _BrokenChain:
            def log_with_prev_digest(self, **_kwargs: object) -> object:
                raise OSError("chain unavailable")

        led = SpendLedger(
            path=tmp_path / "ledger.jsonl",
            hard_budget_usd=1.0,
            chain=_BrokenChain(),  # type: ignore[arg-type]
        )

        status = led.record(tags=CallTags(), model="opus", cost_usd=2.0)

        assert status.hard_halt is True
        assert led.entries_written == 1
