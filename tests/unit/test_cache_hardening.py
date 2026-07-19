"""Hardening regressions for the cache policy engine (issue #2637).

Three properties are pinned here:

* **Path containment.** A cache key is a key, not a path. Any key that would
  address a file outside the cache directory is refused with a typed error
  before a single byte is written.
* **Claim atomicity.** Creating the arbiter row and releasing a claim both
  happen inside the claim protocol, so no contender observes a half-initialised
  row and no release clobbers a concurrent claim.
* **Crash-consistent eviction.** A transitive revocation lands as one
  all-or-nothing journal transition; an interrupted eviction never leaves the
  served-from graph partially revoked.
"""

from __future__ import annotations

import concurrent.futures
import threading
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from bernstein.cli.main import cli
from bernstein.core.persistence.cache_dedup import (
    CacheKeyArbiter,
    arbiter_backlog_path,
)
from bernstein.core.persistence.cache_eviction import (
    ServedFromEdge,
    ServedFromLedger,
    Tombstone,
    TombstoneStore,
    cache_dir,
    open_ledger,
    open_tombstones,
    recall_report_path,
)
from bernstein.core.persistence.cache_policy import (
    UnsafeCacheKeyError,
    resolve_cached_path,
    validate_cache_key,
)
from bernstein.core.persistence.cache_served_from import served_from_artifact_path
from bernstein.core.tasks.claim import Backlog, backlog_transaction

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Path containment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "",
        ".",
        "..",
        "../escape",
        "../../../../pwned",
        "sub/../../x",
        "a/b",
        "a\\b",
        "/absolute",
        "C:\\windows",
        "-leading-dash",
        ".hidden",
        "key with space",
        "key\nwith-newline",
        "nul\x00byte",
        "x" * 129,
    ],
)
def test_unsafe_cache_key_is_refused(key: str) -> None:
    with pytest.raises(UnsafeCacheKeyError):
        validate_cache_key(key)


@pytest.mark.parametrize(
    "key",
    [
        "a",
        "key_root",
        "key-a",
        "key.child",
        "deadbeef" * 8,
    ],
)
def test_safe_cache_key_is_accepted(key: str) -> None:
    assert validate_cache_key(key) == key


def test_resolve_cached_path_stays_inside_base(tmp_path: Path) -> None:
    resolved = resolve_cached_path(tmp_path / "cache", "recall-key_root.json")
    assert (tmp_path / "cache").resolve() in resolved.parents


def test_resolve_cached_path_refuses_escape(tmp_path: Path) -> None:
    with pytest.raises(UnsafeCacheKeyError):
        resolve_cached_path(tmp_path / "cache", "../../escaped.json")


def test_resolve_cached_path_follows_a_symlinked_base(tmp_path: Path) -> None:
    """A symlinked base is canonicalised and followed, not refused."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)

    resolved = resolve_cached_path(link, "recall-key_root.json")

    assert resolved == real.resolve() / "recall-key_root.json"


def test_resolve_cached_path_refuses_a_name_symlinked_out_of_base(tmp_path: Path) -> None:
    """A report name that is itself a symlink pointing outside is refused."""
    base = tmp_path / "cache"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (base / "recall-key_root.json").symlink_to(outside / "stolen.json")

    with pytest.raises(UnsafeCacheKeyError):
        resolve_cached_path(base, "recall-key_root.json")


def test_recall_report_path_is_contained(tmp_path: Path) -> None:
    report = recall_report_path(tmp_path, "key_root")
    assert cache_dir(tmp_path).resolve() in report.parents


def test_recall_report_path_refuses_traversal_key(tmp_path: Path) -> None:
    with pytest.raises(UnsafeCacheKeyError):
        recall_report_path(tmp_path, "../../../../pwned")


def test_evict_refuses_traversal_key(tmp_path: Path) -> None:
    store = TombstoneStore(tmp_path / "tombstones.jsonl")
    ledger = ServedFromLedger(tmp_path / "served_from.jsonl")
    with pytest.raises(UnsafeCacheKeyError):
        store.evict("../../../../pwned", "bad", ledger=ledger)


def test_served_from_ledger_refuses_traversal_key(tmp_path: Path) -> None:
    ledger = ServedFromLedger(tmp_path / "served_from.jsonl")
    with pytest.raises(UnsafeCacheKeyError):
        ledger.record(ServedFromEdge(cache_key="../../escape", consumer="run_a"))
    assert not (tmp_path / "served_from.jsonl").exists()


def test_served_from_artifact_path_refuses_traversal_key() -> None:
    with pytest.raises(UnsafeCacheKeyError):
        served_from_artifact_path("../../../../etc/passwd")


def test_arbiter_backlog_path_is_contained(tmp_path: Path) -> None:
    path = arbiter_backlog_path(tmp_path, "key_root")
    assert tmp_path.resolve() in path.parents


def test_arbiter_backlog_path_refuses_traversal_key(tmp_path: Path) -> None:
    with pytest.raises(UnsafeCacheKeyError):
        arbiter_backlog_path(tmp_path, "../../../../pwned")


def test_arbiter_refuses_traversal_key(tmp_path: Path) -> None:
    with pytest.raises(UnsafeCacheKeyError):
        CacheKeyArbiter(tmp_path / "arbiter.json", "../../escape")


def test_cli_evict_refuses_traversal_key_and_writes_nothing(tmp_path: Path) -> None:
    workdir = tmp_path / "repo"
    workdir.mkdir()
    runner = CliRunner()

    result = runner.invoke(
        cli,
        ["cache", "evict", "../../../../pwned", "--reason", "bad", "--workdir", str(workdir)],
    )

    assert result.exit_code != 0
    # The pre-hardening builder composed
    # <workdir>/.sdd/caching/policy/recall-../../../../pwne.json, whose
    # components normalise to <workdir>/.sdd/pwne.json - three levels above the
    # policy cache directory it was supposed to stay in. Assert on that exact
    # target, and recursively, so the check cannot pass by looking in the wrong
    # directory.
    assert not (workdir / ".sdd" / "pwne.json").exists()
    assert list(workdir.rglob("*.json")) == []
    # The refusal precedes every filesystem effect, so the run leaves no trace
    # at all: no report, no tombstone journal, no audit chain.
    assert list(workdir.rglob("*")) == []


def test_cli_evict_writes_the_report_inside_the_cache_dir(tmp_path: Path) -> None:
    """Positive control: a safe key still gets its report, and it stays contained."""
    workdir = tmp_path / "repo"
    workdir.mkdir()
    open_ledger(workdir).record(ServedFromEdge(cache_key="key_root", consumer="run_a"))
    runner = CliRunner()

    result = runner.invoke(
        cli,
        ["cache", "evict", "key_root", "--reason", "pr_reverted", "--workdir", str(workdir)],
    )

    assert result.exit_code == 0, result.output
    reports = list(cache_dir(workdir).glob("recall-*.json"))
    assert len(reports) == 1
    assert cache_dir(workdir).resolve() in reports[0].resolve().parents


# ---------------------------------------------------------------------------
# Claim atomicity
# ---------------------------------------------------------------------------


def test_contend_initialises_inside_the_claim_lock(tmp_path: Path) -> None:
    """The arbiter row is created under the claim lock, not before it.

    While another party holds the claim lock the backlog must not appear on
    disk: a row created outside the lock is the window in which a second
    contender can overwrite a claim that has already been granted.
    """
    backlog_path = tmp_path / "arbiter.json"
    arbiter = CacheKeyArbiter(backlog_path, "hotkey")
    started = threading.Event()
    done = threading.Event()

    def _contend() -> None:
        started.set()
        arbiter.contend("worker-1")
        done.set()

    worker = threading.Thread(target=_contend)
    with backlog_transaction(backlog_path):
        worker.start()
        started.wait(timeout=5)
        # Give the contender a chance to run its initialisation half.
        done.wait(timeout=0.5)
        assert not backlog_path.exists(), "backlog initialised outside the claim lock"
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert backlog_path.exists()


def test_release_runs_inside_the_claim_lock(tmp_path: Path) -> None:
    """Releasing a claim waits for the claim lock instead of racing it."""
    backlog_path = tmp_path / "arbiter.json"
    arbiter = CacheKeyArbiter(backlog_path, "hotkey")
    assert arbiter.contend("worker-1").won

    released = threading.Event()

    def _release() -> None:
        arbiter.release()
        released.set()

    worker = threading.Thread(target=_release)
    with backlog_transaction(backlog_path):
        worker.start()
        released.wait(timeout=0.5)
        row = next(e for e in Backlog.load(backlog_path).entries if e.id == "hotkey")
        assert row.claimer == "worker-1", "release mutated the row outside the claim lock"
    worker.join(timeout=10)
    assert not worker.is_alive()
    row = next(e for e in Backlog.load(backlog_path).entries if e.id == "hotkey")
    assert row.claimer is None
    assert row.status == "open"


def test_concurrent_cold_start_yields_exactly_one_winner(tmp_path: Path) -> None:
    """N contenders racing an *uninitialised* backlog still produce one winner.

    Each contender gets its own arbiter instance, so no in-process state can
    serialise the create-then-claim window; only the claim protocol can.
    """
    backlog_path = tmp_path / "arbiter.json"
    workers = 16
    arbiters = [CacheKeyArbiter(backlog_path, "hotkey") for _ in range(workers)]
    barrier = threading.Barrier(workers)

    def _try(index: int) -> bool:
        barrier.wait(timeout=10)
        return arbiters[index].contend(f"w-{index}").won

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_try, range(workers)))

    assert sum(1 for won in results if won) == 1


def test_contend_preserves_outcome_semantics(tmp_path: Path) -> None:
    """Winner/loser positions and the winner reference are unchanged."""
    backlog_path = tmp_path / "arbiter.json"
    arbiter = CacheKeyArbiter(backlog_path, "hotkey")
    outcomes = [arbiter.contend(f"worker-{i}") for i in range(4)]

    assert outcomes[0].won is True
    assert outcomes[0].claim_position == 0
    assert outcomes[0].winner == "worker-0"
    for index, outcome in enumerate(outcomes[1:], start=1):
        assert outcome.won is False
        assert outcome.claim_position == index
        assert outcome.winner == "worker-0"


def test_current_winner_tracks_claim_state(tmp_path: Path) -> None:
    """The winner query is consistent with the claim before and after release."""
    backlog_path = tmp_path / "arbiter.json"
    arbiter = CacheKeyArbiter(backlog_path, "hotkey")
    assert arbiter.current_winner() == ""
    arbiter.contend("worker-1")
    assert arbiter.current_winner() == "worker-1"
    arbiter.release()
    assert arbiter.current_winner() == ""


# ---------------------------------------------------------------------------
# Crash-consistent transitive eviction
# ---------------------------------------------------------------------------


def _seed_chain(workdir: Path) -> ServedFromLedger:
    ledger = open_ledger(workdir)
    ledger.record(ServedFromEdge(cache_key="key_root", consumer="run_a"))
    ledger.record(ServedFromEdge(cache_key="key_root", consumer="key_child"))
    ledger.record(ServedFromEdge(cache_key="key_child", consumer="run_b"))
    ledger.record(ServedFromEdge(cache_key="key_child", consumer="key_grand"))
    ledger.record(ServedFromEdge(cache_key="key_grand", consumer="run_c"))
    return ledger


def test_interrupted_eviction_leaves_no_partial_revocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash part-way through a transitive eviction revokes nothing.

    The served-from graph must never be observed half-revoked: either every
    reachable key carries a tombstone or none of them does.
    """
    ledger = _seed_chain(tmp_path)
    store = open_tombstones(tmp_path)

    calls = {"n": 0}
    original = Tombstone.to_dict

    def _flaky(self: Tombstone) -> dict[str, object]:
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("crash mid-eviction")
        return original(self)

    monkeypatch.setattr(Tombstone, "to_dict", _flaky)

    with pytest.raises(RuntimeError):
        store.evict("key_root", "bad", ledger=ledger, ts=7)

    monkeypatch.undo()
    assert store.all() == {}, "eviction left the served-from graph partially revoked"


def test_interrupted_eviction_preserves_prior_tombstones(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupted eviction does not damage tombstones written earlier."""
    ledger = _seed_chain(tmp_path)
    store = open_tombstones(tmp_path)
    store.evict("key_grand", "earlier", ledger=ledger, ts=1)
    before = store.all()
    assert set(before) == {"key_grand"}

    calls = {"n": 0}
    original = Tombstone.to_dict

    def _flaky(self: Tombstone) -> dict[str, object]:
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("crash mid-eviction")
        return original(self)

    monkeypatch.setattr(Tombstone, "to_dict", _flaky)
    with pytest.raises(RuntimeError):
        store.evict("key_root", "bad", ledger=ledger, ts=2)
    monkeypatch.undo()

    assert store.all().keys() == before.keys()


def test_transitive_eviction_is_all_or_nothing(tmp_path: Path) -> None:
    """A completed eviction revokes the whole reachable set in one transition."""
    ledger = _seed_chain(tmp_path)
    store = open_tombstones(tmp_path)
    recall = store.evict("key_root", "pr_reverted", ledger=ledger, ts=5)

    assert recall.tombstoned == ["key_root", "key_child", "key_grand"]
    assert recall.consumers == ["run_a", "run_b", "run_c"]
    assert set(store.all()) == {"key_root", "key_child", "key_grand"}


def test_concurrent_evictions_preserve_every_tombstone(tmp_path: Path) -> None:
    """Parallel evictions serialise; no revocation is lost to a lost update."""
    ledger = open_ledger(tmp_path)
    keys = [f"key_{i:02d}" for i in range(12)]
    for key in keys:
        ledger.record(ServedFromEdge(cache_key=key, consumer=f"run_{key}"))
    store = open_tombstones(tmp_path)
    barrier = threading.Barrier(len(keys))

    def _evict(key: str) -> None:
        barrier.wait(timeout=10)
        store.evict(key, "bulk", ledger=ledger, ts=3)

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(keys)) as pool:
        list(pool.map(_evict, keys))

    assert set(store.all()) == set(keys)


def test_eviction_verdict_is_deterministic(tmp_path: Path) -> None:
    """Two operators evicting the same key produce identical recall sets."""
    left = tmp_path / "left"
    right = tmp_path / "right"
    recalls = [
        TombstoneStore(root / "tombstones.jsonl").evict(
            "key_root",
            "pr_reverted",
            ledger=_seed_chain(root),
            ts=11,
        )
        for root in (left, right)
    ]
    assert recalls[0].to_dict() == recalls[1].to_dict()
