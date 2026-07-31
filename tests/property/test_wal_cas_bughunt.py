# pyright: reportPrivateUsage=false
"""Bug-hunt suite for the WAL + CAS + crash-recovery subsystem.

The tests in this module are split into two layers:

* **Stateful machine** (``WalRecoveryMachine``) - drives a randomised
  sequence of ``claim``/``confirm_spawn``/``kill_worker``/``restart``
  events through ``WALWriter`` + ``WALRecovery`` and asserts the
  invariants documented in the bug-hunt brief on every step.
* **Targeted regressions** - each one freezes a deterministic
  reproducer for a bug surfaced by exploratory hand-runs of the
  state machine.  They are marked ``xfail`` so the suite is green on
  ``main`` while the operator triages each finding.

All targeted tests run in ≪ 10 s and need no orchestrator wiring -
they exercise the persistence module directly so an engineer can
``pytest tests/property/test_wal_cas_bughunt.py -x`` and immediately
see the failing assertion line.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from hypothesis import HealthCheck, settings
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    rule,
)
from hypothesis.strategies import integers

from bernstein.core.persistence.wal import (
    GENESIS_HASH,
    WALReader,
    WALRecovery,
    WALWriter,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wal_path(sdd: Path, run_id: str) -> Path:
    return sdd / "runtime" / "wal" / f"{run_id}.wal.jsonl"


def _read_lines(path: Path) -> list[str]:
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _write_orphan_claim(sdd: Path, run_id: str, task_id: str) -> WALWriter:
    """Helper: write a single uncommitted ``task_claimed`` entry."""
    sdd.mkdir(exist_ok=True)
    w = WALWriter(run_id=run_id, sdd_dir=sdd)
    w.append("task_claimed", {"task_id": task_id}, {}, "lifecycle", committed=False)
    return w


# ---------------------------------------------------------------------------
# Stateful machine
# ---------------------------------------------------------------------------


class WalRecoveryMachine(RuleBasedStateMachine):
    """Random sequence of WAL events with crash-recovery invariants.

    The machine models a single ``.sdd`` directory shared by a sequence
    of orchestrator runs.  Each step picks one rule:

    * ``claim_task`` - write ``task_claimed`` (committed=False)
    * ``confirm_spawn`` - write ``task_spawn_confirmed`` (committed=True)
    * ``kill_worker`` - abandon the current writer (no graceful close)
    * ``restart_orchestrator`` - start a new run that scans uncommitted
      entries and asserts the recovery invariants on the result.

    Invariants checked after every step:

    1. Every WAL line is JSON-parseable (no half-written tails leaking
       past graceful append boundaries).
    2. Sequence numbers within one WAL file are strictly monotonic.
    3. If ``task_spawn_confirmed`` exists for ``task_id`` in run R,
       then no other (R, task_id) pair is reported by
       ``find_orphaned_claims`` after a closed-recovery cycle.
    4. The hash chain of every WAL file we wrote ourselves still
       passes ``verify_chain()`` - i.e. a clean append history must
       never break the chain.
    """

    def __init__(self) -> None:
        super().__init__()
        # Each instance gets its own scratch dir; pytest's tmp_path can't
        # be plumbed through Hypothesis state machines so we mkdir our own.
        import tempfile as _tempfile

        self._tmp = Path(_tempfile.mkdtemp(prefix="wal-bughunt-"))
        self._sdd = self._tmp / ".sdd"
        self._sdd.mkdir()
        self._run_counter = 0
        self._writer: WALWriter | None = None
        self._current_run_id: str | None = None
        # Map run_id -> set of task_ids claimed (committed=False) but not
        # yet spawned (committed=True).
        self._open_claims: dict[str, set[str]] = {}
        # Map run_id -> set of task_ids successfully spawned.
        self._spawned: dict[str, set[str]] = {}

    @initialize()
    def boot(self) -> None:
        self._start_new_run()

    def _start_new_run(self) -> None:
        self._run_counter += 1
        self._current_run_id = f"run-{self._run_counter:03d}"
        self._writer = WALWriter(run_id=self._current_run_id, sdd_dir=self._sdd)
        self._open_claims.setdefault(self._current_run_id, set())
        self._spawned.setdefault(self._current_run_id, set())

    @rule(task_id=integers(min_value=1, max_value=20))
    def claim_task(self, task_id: int) -> None:
        if self._writer is None:
            return
        tid = f"T-{task_id}"
        if tid in self._open_claims[self._current_run_id]:
            return  # avoid duplicate claims in the model
        self._writer.append(
            "task_claimed",
            {"task_id": tid},
            {},
            "lifecycle",
            committed=False,
        )
        self._open_claims[self._current_run_id].add(tid)

    @rule(task_id=integers(min_value=1, max_value=20))
    def confirm_spawn(self, task_id: int) -> None:
        if self._writer is None:
            return
        tid = f"T-{task_id}"
        if tid not in self._open_claims[self._current_run_id]:
            return
        self._writer.append(
            "task_spawn_confirmed",
            {"task_id": tid},
            {},
            "lifecycle",
            committed=True,
        )
        self._open_claims[self._current_run_id].discard(tid)
        self._spawned[self._current_run_id].add(tid)

    @rule()
    def kill_worker(self) -> None:
        # Simulate kill -9: drop the writer reference without close.
        # Any uncommitted claims should be visible to find_orphaned_claims
        # from a future run.
        self._writer = None

    @rule()
    def restart_orchestrator(self) -> None:
        # Scan from a fresh run_id; close every prior run.
        new_run = f"recovery-{self._run_counter}"
        orphans = WALRecovery.find_orphaned_claims(self._sdd, exclude_run_id=new_run)
        # Invariant 3: every reported orphan must be a (run, task_id)
        # combination we modelled as still-open.
        for run_id, entry in orphans:
            tid = str(entry.inputs.get("task_id", ""))
            assert tid in self._open_claims.get(run_id, set()), (
                f"find_orphaned_claims reported {run_id}:{tid} but the model has it as spawned/never-claimed"
            )
        # Close every run we touched so the next restart doesn't replay.
        for rid in list(self._open_claims):
            if rid == self._current_run_id:
                continue
            with contextlib.suppress(OSError):
                WALRecovery.close_wal(rid, self._sdd, reason="bughunt")
        self._start_new_run()

    @invariant()
    def lines_are_jsonable(self) -> None:
        wal_dir = self._sdd / "runtime" / "wal"
        if not wal_dir.is_dir():
            return
        for wf in wal_dir.glob("*.wal.jsonl"):
            for ln in _read_lines(wf):
                # Each line must be JSON-parseable.  If the writer ever
                # leaves torn data behind under normal append flow this
                # invariant will fail.
                json.loads(ln)

    @invariant()
    def seqs_are_monotonic(self) -> None:
        wal_dir = self._sdd / "runtime" / "wal"
        if not wal_dir.is_dir():
            return
        for wf in wal_dir.glob("*.wal.jsonl"):
            seqs = []
            for ln in _read_lines(wf):
                data = json.loads(ln)
                seqs.append(int(data["seq"]))
            # Strictly increasing - duplicate seqs are a known bug.
            assert seqs == sorted(set(seqs)) and len(seqs) == len(set(seqs)), (
                f"WAL {wf.name} has non-monotonic seqs: {seqs}"
            )

    @invariant()
    def chain_intact_for_clean_runs(self) -> None:
        wal_dir = self._sdd / "runtime" / "wal"
        if not wal_dir.is_dir():
            return
        for wf in wal_dir.glob("*.wal.jsonl"):
            run_id = wf.name.removesuffix(".wal.jsonl")
            r = WALReader(run_id=run_id, sdd_dir=self._sdd)
            ok, errs = r.verify_chain()
            assert ok, f"chain broken in {run_id}: {errs}"

    def teardown(self) -> None:
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)


TestWalRecoveryStateMachine = WalRecoveryMachine.TestCase
TestWalRecoveryStateMachine.settings = settings(  # type: ignore[attr-defined]
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)


# ---------------------------------------------------------------------------
# Finding 1 (HIGH): torn-tail recovery resets prev_hash to GENESIS_HASH.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "bug-hunt finding #1 (HIGH): WALWriter._load_tail falls back to "
        "(count-1, GENESIS_HASH) when the trailing line is malformed "
        "(torn write after SIGKILL). The next append then chains off "
        "GENESIS_HASH instead of the last valid entry's hash, "
        "permanently breaking the audit chain. Fix: keep scanning "
        "backward for the last *valid* JSON line and use its entry_hash."
    ),
)
def test_torn_tail_does_not_corrupt_chain(tmp_path: Path) -> None:
    """A torn trailing line must not reset prev_hash to GENESIS.

    Repro: write two valid entries, simulate a SIGKILL by appending a
    truncated line (no closing brace, no newline), then restart a
    ``WALWriter`` and append one more entry.  The new entry's
    ``prev_hash`` must equal the last valid entry's ``entry_hash`` -
    otherwise ``verify_chain`` reports a permanent break and downstream
    consumers (audit slice, fingerprinting) cannot re-link the chain.
    """
    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    w1 = WALWriter(run_id="r1", sdd_dir=sdd)
    w1.append("a", {}, {}, "x", committed=True)
    e2 = w1.append("a", {}, {}, "x", committed=True)
    last_good_hash = e2.entry_hash

    wal_path = _wal_path(sdd, "r1")
    with wal_path.open("a") as f:
        # Truncated JSON; no newline. Mirrors a kill-9 mid-fwrite.
        f.write('{"seq":2,"prev_hash":"' + last_good_hash + '","timestamp":1.0,')

    # Restart: open a fresh writer.  The torn line should not destroy
    # the chain - recovery should pick up at the last valid entry.
    w2 = WALWriter(run_id="r1", sdd_dir=sdd)
    e3 = w2.append("a", {}, {}, "x", committed=True)

    # The bug: e3.prev_hash is GENESIS_HASH (all zeros).
    assert e3.prev_hash == last_good_hash, (
        f"new entry chained off torn write: prev_hash={e3.prev_hash[:8]}, expected last_good_hash={last_good_hash[:8]}"
    )


# ---------------------------------------------------------------------------
# Finding 2 (HIGH): fsync failure mid-append produces duplicate seqs.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "bug-hunt finding #2 (HIGH): WALWriter.append writes the JSON "
        "line first, then fsyncs, *then* updates self._seq/_prev_hash. "
        "If fsync raises (ENOSPC, EIO, EBADF) the line is already on "
        "disk but in-memory state is unchanged, so the next successful "
        "append re-uses the same seq number. Result: two WAL lines with "
        "seq=N, breaking the monotonic-seq invariant and permanently "
        "corrupting the hash chain. Fix: advance self._seq/_prev_hash "
        "before fsync, or wrap the whole append in a try/except that "
        "reverts the file truncate on failure."
    ),
)
def test_fsync_failure_does_not_create_duplicate_seqs(tmp_path: Path) -> None:
    """An fsync error mid-append must not leave inconsistent state.

    Repro: write one entry while ``os.fsync`` raises ENOSPC; the line
    lands on disk but the writer's bookkeeping never advances.  When
    fsync recovers and a second entry is appended, it gets the same
    seq=0 - a duplicate that ``verify_chain`` flags forever.
    """
    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    w = WALWriter(run_id="r1", sdd_dir=sdd)

    def boom(_fd: int) -> None:
        raise OSError(28, "No space left on device")

    with patch("bernstein.core.persistence.wal.os.fsync", side_effect=boom):
        with pytest.raises(OSError):
            w.append("task_claimed", {"task_id": "T1"}, {}, "x", committed=False)

    # Disk recovers; same writer is used to append again.
    w.append("task_claimed", {"task_id": "T2"}, {}, "x", committed=False)

    seqs = [int(json.loads(ln)["seq"]) for ln in _read_lines(_wal_path(sdd, "r1"))]
    assert len(seqs) == len(set(seqs)), f"duplicate seqs after fsync error: {seqs}"


# ---------------------------------------------------------------------------
# Finding 3 (MED, fixed): symlinks under .sdd/runtime/wal/ are now skipped.
# ---------------------------------------------------------------------------


def test_symlinked_wal_is_not_replayed(tmp_path: Path) -> None:
    """A symlinked WAL pointing outside its project must not be scanned.

    Repro: project A has an uncommitted task_claimed for task T-secret.
    A symlink in project B's wal dir points at project A's WAL file.
    Recovery in B should NOT see T-secret as an orphan.
    """
    sdd_a = tmp_path / "a" / ".sdd"
    sdd_b = tmp_path / "b" / ".sdd"
    sdd_a.mkdir(parents=True)
    sdd_b.mkdir(parents=True)
    _write_orphan_claim(sdd_a, "a-run", "T-secret")

    wal_dir_b = sdd_b / "runtime" / "wal"
    wal_dir_b.mkdir(parents=True)
    src = sdd_a / "runtime" / "wal" / "a-run.wal.jsonl"
    os.symlink(src, wal_dir_b / "leak-run.wal.jsonl")

    found = WALRecovery.find_orphaned_claims(sdd_b, exclude_run_id="b-current")
    leaked_task_ids = [str(e.inputs.get("task_id", "")) for _, e in found]
    assert "T-secret" not in leaked_task_ids, f"recovery followed symlink to a foreign WAL; leaked={leaked_task_ids}"


# ---------------------------------------------------------------------------
# Finding 4 (MED): find_orphaned_claims trusts hash-chain-broken WALs.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "bug-hunt finding #4 (MED): find_orphaned_claims iterates "
        "WALReader.iter_entries() but never calls verify_chain(). A "
        "WAL file whose entry_hash field is forged (or whose prev_hash "
        "linkage is broken) is treated as authoritative - the recovery "
        "force-claims arbitrary task_ids. The hash chain that the WAL "
        "design promises is only enforced by callers who explicitly "
        "ask for verify_chain(); the recovery path doesn't. Fix: "
        "either gate find_orphaned_claims on verify_chain success, or "
        "skip individual entries whose recomputed hash mismatches."
    ),
)
def test_find_orphaned_claims_rejects_broken_chain(tmp_path: Path) -> None:
    """A WAL with a forged entry_hash must not yield orphan claims."""
    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    wal_dir = sdd / "runtime" / "wal"
    wal_dir.mkdir(parents=True)
    forged = {
        "seq": 0,
        "prev_hash": GENESIS_HASH,
        "entry_hash": "abc",  # obviously not SHA-256 of the payload
        "timestamp": 1.0,
        "decision_type": "task_claimed",
        "inputs": {"task_id": "T-injected"},
        "output": {},
        "actor": "attacker",
        "committed": False,
    }
    (wal_dir / "evil-run.wal.jsonl").write_text(json.dumps(forged) + "\n")

    found = WALRecovery.find_orphaned_claims(sdd, exclude_run_id="current")
    leaked = [str(e.inputs.get("task_id", "")) for _, e in found]
    assert "T-injected" not in leaked, f"recovery accepted forged WAL entry; leaked={leaked}"


# ---------------------------------------------------------------------------
# Finding 5 (LOW): close_wal does not fsync the parent directory.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "bug-hunt finding #5 (LOW): WALRecovery.close_wal fsyncs the "
        ".closed marker file but never the parent directory dirent. On "
        "ext4/xfs (and Linux POSIX semantics) a crash immediately after "
        "the marker write can leave the dirent unflushed, so the file "
        "is invisible on next boot - re-triggering the audit-072 "
        "'unbounded re-scan' bug the marker was added to prevent. The "
        "docstring at line 627-629 explicitly promises this property "
        "but the implementation is missing the directory fsync. Fix: "
        "after os.replace(tmp, marker) call fsync on the parent dir fd."
    ),
)
def test_close_wal_fsyncs_parent_dir(tmp_path: Path, monkeypatch) -> None:
    """close_wal must fsync the dirent so the marker survives a crash.

    We assert it indirectly: instrument os.fsync and check that at
    least one fsync targets a directory file descriptor.
    """
    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    _write_orphan_claim(sdd, "r1", "T1")

    fsynced_targets: list[str] = []
    real_fsync = os.fsync

    def tracking_fsync(fd: int) -> None:
        try:
            # Try to identify the path of the fd
            import os as _os

            path = _os.readlink(f"/dev/fd/{fd}")
        except OSError:
            path = "<unknown>"
        fsynced_targets.append(path)
        return real_fsync(fd)

    monkeypatch.setattr("bernstein.core.persistence.wal.os.fsync", tracking_fsync)

    WALRecovery.close_wal("r1", sdd, reason="test")
    # The wal directory itself should be fsynced after the marker is
    # written. Currently nothing fsyncs the dirent.
    wal_dir_str = str((sdd / "runtime" / "wal").resolve())
    assert any(p == wal_dir_str for p in fsynced_targets), f"close_wal did not fsync wal dir; saw {fsynced_targets}"


# ---------------------------------------------------------------------------
# Finding 6 (LOW): UncommittedIndex is built but never consulted by recovery.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "bug-hunt finding #6 (LOW): UncommittedIndex (audit-085) is "
        "documented as 'sidecar index of uncommitted WAL entries across "
        "all runs' to avoid the O(N) full-WAL scan on every boot. "
        "WALWriter.append() updates the index for every committed=False "
        "row, and mark_committed() removes rows. But "
        "WALRecovery.scan_all_uncommitted and find_orphaned_claims "
        "still glob('*.wal.jsonl') and iterate every entry - they "
        "never read the index. The promised performance gain is not "
        "realised, and the index code is effectively dead-on-read. Fix: "
        "make scan_all_uncommitted's fast path read the index first "
        "and fall back to the glob scan only on missing/corrupt index."
    ),
)
def test_uncommitted_index_is_consulted_by_scan(tmp_path: Path, monkeypatch) -> None:
    """Recovery must not re-parse every WAL line if the index is healthy.

    We assert by patching ``WALReader.iter_entries`` and counting calls
    when an up-to-date index exists.  Currently the call count is
    nonzero - the index isn't consulted at all.
    """
    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    # Create 3 runs with committed entries (none uncommitted).
    for i in range(3):
        w = WALWriter(run_id=f"r{i}", sdd_dir=sdd)
        for _ in range(20):
            w.append("noise", {}, {}, "x", committed=True)
    # No uncommitted entries → the index is empty → scan should be
    # near-instant (zero iter_entries calls).

    calls = {"n": 0}
    real_iter = WALReader.iter_entries

    def counting_iter(self, *args, **kwargs):
        calls["n"] += 1
        return real_iter(self, *args, **kwargs)

    monkeypatch.setattr(WALReader, "iter_entries", counting_iter)
    WALRecovery.scan_all_uncommitted(sdd, exclude_run_id="current")
    assert calls["n"] == 0, (
        f"scan_all_uncommitted parsed entries despite empty index "
        f"(calls={calls['n']}); UncommittedIndex fast path is unwired"
    )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-x", "-q"])
