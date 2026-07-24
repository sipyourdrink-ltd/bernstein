"""Tests for per-hop HMAC-chained delegation receipts.

Covers issue #2305 acceptance criteria 3 and 4:

* AC3 - each delegation hop emits an HMAC-chained receipt reconstructable
  offline.
* AC4 - ``delegation verify`` reconstructs the
  principal->orchestrator->sub-agent chain for a run.
"""

from __future__ import annotations

import pytest

from bernstein.core.identity import delegation


@pytest.fixture
def ledger(tmp_path):
    return delegation.DelegationLedger(root=tmp_path, key=b"k" * 32)


class TestReceiptChain:
    def test_first_hop_chains_to_genesis(self, ledger):
        r = ledger.record_hop(
            run_id="run-1",
            issuer="principal:alex",
            subject="orchestrator",
            audience="sub-agent:backend",
            act="task.spawn",
        )
        assert r.prev_hmac == delegation.GENESIS_HMAC
        assert r.hmac
        assert r.hop_index == 0

    def test_each_hop_chains_to_previous(self, ledger):
        r0 = ledger.record_hop(
            run_id="run-1",
            issuer="principal:alex",
            subject="orchestrator",
            audience="orchestrator",
            act="run.authorize",
        )
        r1 = ledger.record_hop(
            run_id="run-1",
            issuer="orchestrator",
            subject="orchestrator",
            audience="sub-agent:backend",
            act="task.spawn",
        )
        assert r1.prev_hmac == r0.hmac
        assert r1.hop_index == 1

    def test_receipts_are_isolated_per_run(self, ledger):
        ledger.record_hop(run_id="run-a", issuer="p", subject="o", audience="s", act="x")
        rb = ledger.record_hop(run_id="run-b", issuer="p", subject="o", audience="s", act="x")
        assert rb.hop_index == 0
        assert rb.prev_hmac == delegation.GENESIS_HMAC


class TestConcurrentRecordHop:
    def test_concurrent_hops_keep_the_chain_intact(self, ledger):
        # Many threads extend the same run's chain at once. Without serialising
        # the tail-read-through-append, two writers recover the same tail and
        # append records embedding the same stale prev_hmac, forking the chain
        # (issue #2640). The chain must stay linear and fully verifiable.
        #
        # The lock is what this test exercises, so the pool is right-sized to
        # keep it reliably green on a busy CI shard: 16 barrier-released writers
        # reproduce the read-modify-write race just as reliably as a larger pool
        # would, without the OS-thread-table pressure that made a 48-thread pool
        # flake under 4-way shard parallelism (``RuntimeError: can't start new
        # thread``, which run_tests.py's single serial retry cannot clear while
        # sibling shards keep the table exhausted). Workers are daemon threads
        # with a bounded barrier wait so a partial spawn can never leave one
        # parked forever, and each spawn retries with backoff until the table
        # frees up, so a transient exhaustion self-heals instead of failing.
        import threading
        import time

        n = 16
        barrier = threading.Barrier(n)
        errors: list[Exception] = []

        def worker(i: int) -> None:
            try:
                barrier.wait(timeout=120)
                ledger.record_hop(
                    run_id="run-race",
                    issuer=f"principal:{i}",
                    subject="orchestrator",
                    audience="sub-agent:backend",
                    act="task.spawn",
                )
            except threading.BrokenBarrierError:
                # The spawn loop gave up (thread table exhausted past the
                # deadline); the run is surfaced as a skip below, not a fork.
                pass
            except Exception as exc:  # surfaced to the assertion below
                errors.append(exc)

        threads: list[threading.Thread] = []
        spawn_deadline = time.monotonic() + 30.0
        while len(threads) < n:
            t = threading.Thread(target=worker, args=(len(threads),), daemon=True)
            try:
                t.start()
            except RuntimeError:
                # OS thread table transiently exhausted (busy shard). Wait for
                # sibling load to drain and retry this slot with a fresh thread;
                # the already-started workers park on the barrier meanwhile.
                if time.monotonic() >= spawn_deadline:
                    barrier.abort()
                    for done in threads:
                        done.join(timeout=10)
                    pytest.skip("OS thread table exhausted; delegation concurrency probe could not spawn")
                time.sleep(0.25)
                continue
            threads.append(t)

        for t in threads:
            t.join(timeout=120)
        assert not any(t.is_alive() for t in threads), "a delegation worker thread did not finish in time"

        assert errors == []
        result = delegation.verify_run_chain(root=ledger.root, run_id="run-race", key=b"k" * 32)
        assert result.valid, result.errors
        assert result.hops == n
        # hop_index is a contiguous 0..n-1 range with no gaps or duplicates.
        assert sorted(h.hop_index for h in result.receipts) == list(range(n))


class TestOfflineReconstruction:
    def test_verify_reconstructs_intact_chain(self, ledger):
        ledger.record_hop(
            run_id="run-1",
            issuer="principal:alex",
            subject="orchestrator",
            audience="orchestrator",
            act="run.authorize",
        )
        ledger.record_hop(
            run_id="run-1",
            issuer="orchestrator",
            subject="orchestrator",
            audience="sub-agent:backend",
            act="task.spawn",
        )
        result = delegation.verify_run_chain(root=ledger.root, run_id="run-1", key=b"k" * 32)
        assert result.valid
        assert result.errors == []
        assert result.hops == 2
        # principal -> orchestrator -> sub-agent reconstructed in order.
        assert [h.issuer for h in result.receipts] == ["principal:alex", "orchestrator"]
        assert result.receipts[-1].audience == "sub-agent:backend"

    def test_verify_detects_tampered_field(self, ledger, tmp_path):
        ledger.record_hop(
            run_id="run-1",
            issuer="principal:alex",
            subject="orchestrator",
            audience="sub-agent:backend",
            act="task.spawn",
        )
        path = ledger.receipt_path("run-1")
        raw = path.read_text(encoding="utf-8")
        path.write_text(raw.replace("principal:alex", "principal:mallory"), encoding="utf-8")
        result = delegation.verify_run_chain(root=ledger.root, run_id="run-1", key=b"k" * 32)
        assert not result.valid
        assert result.errors

    def test_verify_detects_deleted_hop(self, ledger):
        ledger.record_hop(
            run_id="run-1",
            issuer="a",
            subject="o",
            audience="o",
            act="run.authorize",
        )
        ledger.record_hop(
            run_id="run-1",
            issuer="o",
            subject="o",
            audience="s",
            act="task.spawn",
        )
        path = ledger.receipt_path("run-1")
        lines = path.read_text(encoding="utf-8").splitlines()
        # Drop the first hop -> linkage from the survivor breaks.
        path.write_text(lines[1] + "\n", encoding="utf-8")
        result = delegation.verify_run_chain(root=ledger.root, run_id="run-1", key=b"k" * 32)
        assert not result.valid

    def test_verify_missing_run_is_empty_not_error(self, tmp_path):
        result = delegation.verify_run_chain(root=tmp_path, run_id="absent", key=b"k" * 32)
        assert result.hops == 0
        assert not result.valid  # nothing to attest -> not a verified chain

    def test_wrong_key_fails_verification(self, ledger):
        ledger.record_hop(
            run_id="run-1",
            issuer="a",
            subject="o",
            audience="s",
            act="task.spawn",
        )
        result = delegation.verify_run_chain(root=ledger.root, run_id="run-1", key=b"other" * 6 + b"xx")
        assert not result.valid
