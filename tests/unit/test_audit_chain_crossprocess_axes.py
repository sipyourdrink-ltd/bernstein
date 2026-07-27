"""Cross-process audit-chain appends, varied along four axes (issue #3064).

``tests/unit/test_audit_chain_crossprocess.py`` already races four processes
against one audit directory and asserts the chain verifies. What it does not do
is vary anything: one spelling of one directory, one day, no retention running,
no exception unwinding out of the append path. Every defect found in this area
so far was found by measurement rather than by the suite, and a single-vector
test cannot tell whether a change to the append path preserved the property in
the shapes that actually break it.

The four axes here are the ones where the append section carries weight beyond
"two writers, one file":

``path spelling``
    ``_chain_append_lock`` keys its in-process guard on ``(st_dev, st_ino)``
    rather than on a path string, precisely so that a symlinked or dot-segmented
    spelling of one directory maps to one guard. The flock is taken on
    ``<dir>/.chain.lock``, which is one inode under every spelling. Workers here
    are handed *different* spellings of the same directory.

``day rollover crossed mid-append``
    The lock file is a single stable ``.chain.lock`` per directory rather than
    one per day, so a writer that has rolled to the next day still contends with
    one still on the previous day. Workers cross a UTC midnight partway through
    their appends, all at the same chain position, so the rollover is deterministic
    rather than a function of how long the test happened to take.

``archive concurrent with appends``
    ``AuditLog.archive`` compresses and unlinks a segment inside the append
    section, because the pair is not separable. A writer racing it must not
    recover a head from a segment that is halfway through being replaced.

``exception unwinding out of the append path``
    The section's ``finally`` releases the flock and decrements the re-entrancy
    depth. If either leaked, the next append in the same process would either
    block on itself forever or silently skip the flock and stop excluding other
    processes.

Design notes
------------

Workers are real interpreters started with :mod:`subprocess`, not threads and
not ``multiprocessing`` children: the defects this covers are ones a shared
address space hides, and ``fork`` is unavailable on one of the platforms the
suite runs on. They synchronise on a *filesystem* barrier taken after their
imports and after ``AuditLog`` construction, so they genuinely race the append
rather than serialising on interpreter startup.

That barrier polls a file. It is test scaffolding and nothing else: the chain
lock itself must stay a blocking ``flock`` with no deadline, because a polling
or deadline-based append lock was measured to drop appends and is rejected.
"""

from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bernstein.core.security.audit import (
    AuditLog,
    RetentionPolicy,
    _audit_dir_key,
    _chain_append_lock,
    _inside_append_section,
    fcntl,
)

_KEY = b"crossprocess-axes-key-0123456789"
_SRC = str(Path(__file__).resolve().parents[2] / "src")

requires_flock = pytest.mark.skipif(
    fcntl is None,
    reason=(
        "the cross-process append lock is a documented no-op without fcntl "
        "(Windows); only the in-process locks order appends there, so a "
        "cross-process linearity assertion would be asserting something the "
        "platform does not provide"
    ),
)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

_WORKER = '''
"""One audit-chain writer, driven by a JSON config file."""

import json
import sys
import time
from pathlib import Path

cfg = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
sys.path.insert(0, cfg["src"])

from datetime import UTC, datetime, timedelta  # noqa: E402

import bernstein.core.security.audit as audit_mod  # noqa: E402
from bernstein.core.security.audit import AuditLog, RetentionPolicy  # noqa: E402

audit_dir = Path(cfg["audit_dir"])
barrier = Path(cfg["barrier"])
key = cfg["key"].encode("utf-8")
worker = cfg["worker"]
events = cfg["events"]
mode = cfg["mode"]

if cfg.get("break_lock"):
    # Reproduces the platform where the append section cannot take an OS lock.
    audit_mod.fcntl = None

if mode == "rollover":
    _BASE = datetime(2026, 3, 14, 23, 59, 50, tzinfo=UTC)
    _SWITCH_AT = cfg["switch_at"]

    class _RolloverClock(datetime):
        """A clock every worker agrees on, driven by chain length not wall time.

        ``now`` is read inside the append section, so the number of records on
        disk is stable while it is being counted and rises monotonically. Every
        worker therefore crosses the same midnight at the same chain position,
        which makes the rollover reproducible instead of a function of how long
        the test took.
        """

        @classmethod
        def now(cls, tz=None):
            written = 0
            for path in audit_dir.glob("*.jsonl"):
                written += sum(1 for line in path.read_bytes().split(b"\\n") if line)
            offset = timedelta(seconds=20) if written >= _SWITCH_AT else timedelta()
            return _BASE + offset

    audit_mod.datetime = _RolloverClock

log = AuditLog(audit_dir, key=key)

# Filesystem barrier, taken after imports and after construction so the
# workers race the append rather than the interpreter start.
(barrier / ("ready-" + worker)).write_text("1", encoding="utf-8")
deadline = time.monotonic() + 60.0
while not (barrier / "go").exists():
    if time.monotonic() > deadline:
        raise SystemExit("barrier timeout waiting for go")
    time.sleep(0.002)

if mode in {"append", "rollover"}:
    for i in range(events):
        log.log("concurrent.event", worker, "res", worker + "-" + str(i))

elif mode == "archive":
    result = log.archive(RetentionPolicy(retention_days=cfg["retention_days"]))
    print(json.dumps({"archived": result.archived}))

elif mode == "raise_then_append":
    # The section must unwind cleanly: the flock is released and the
    # re-entrancy depth returns to zero, so the appends below neither block on
    # a leaked descriptor nor silently skip the lock.
    try:
        with log.append_transaction():
            log.resync_head()
            raise RuntimeError("deliberate unwind")
    except RuntimeError:
        pass
    for i in range(events):
        log.log("concurrent.event", worker, "res", worker + "-" + str(i))

elif mode == "hold_lock":
    from bernstein.core.security.audit import _chain_append_lock

    with _chain_append_lock(audit_dir):
        (barrier / "held").write_text("1", encoding="utf-8")
        # Do not leave the section until the prober has announced that it is
        # about to take it. Under a working lock the prober is then blocked and
        # the outcome is a function of the lock rather than of who the
        # scheduler ran first.
        deadline = time.monotonic() + 60.0
        while not (barrier / "probing").exists():
            if time.monotonic() > deadline:
                raise SystemExit("barrier timeout waiting for probing")
            time.sleep(0.002)
        time.sleep(cfg["hold_seconds"])
        # Written as the last act *inside* the section. Writing it after the
        # block would leave a window between the flock dropping and the mark
        # landing, and the prober would report a violation that never happened.
        (barrier / "holder-done").write_text("1", encoding="utf-8")

elif mode == "probe_lock":
    from bernstein.core.security.audit import _chain_append_lock

    deadline = time.monotonic() + 60.0
    while not (barrier / "held").exists():
        if time.monotonic() > deadline:
            raise SystemExit("barrier timeout waiting for held")
        time.sleep(0.002)
    (barrier / "probing").write_text("1", encoding="utf-8")
    with _chain_append_lock(audit_dir):
        print(json.dumps({"holder_had_finished": (barrier / "holder-done").exists()}))

else:
    raise SystemExit("unknown mode " + mode)
'''


def _write_worker(tmp_path: Path) -> Path:
    path = tmp_path / "audit_worker.py"
    path.write_text(_WORKER, encoding="utf-8")
    return path


def _spawn(
    worker_script: Path,
    tmp_path: Path,
    *,
    name: str,
    audit_dir: Path | str,
    barrier: Path,
    mode: str,
    events: int = 0,
    **extra: object,
) -> subprocess.Popen[str]:
    cfg = {
        "src": _SRC,
        "audit_dir": str(audit_dir),
        "barrier": str(barrier),
        "key": _KEY.decode("utf-8"),
        "worker": name,
        "events": events,
        "mode": mode,
        **extra,
    }
    cfg_path = tmp_path / f"cfg-{name}.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, str(worker_script), str(cfg_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def _release_barrier(barrier: Path, names: list[str], *, timeout: float = 90.0) -> None:
    """Wait until every worker has signalled ready, then let them all go."""
    deadline = time.monotonic() + timeout
    while True:
        if all((barrier / f"ready-{name}").exists() for name in names):
            break
        if time.monotonic() > deadline:
            missing = [n for n in names if not (barrier / f"ready-{n}").exists()]
            pytest.fail(f"workers never reached the barrier: {missing}")
        time.sleep(0.005)
    (barrier / "go").write_text("1", encoding="utf-8")


def _reap(procs: list[subprocess.Popen[str]], *, timeout: float = 120.0) -> list[str]:
    outs: list[str] = []
    for proc in procs:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            pytest.fail("a worker did not finish; the append section most likely blocked on itself")
        assert proc.returncode == 0, f"worker failed rc={proc.returncode}\nstdout={out}\nstderr={err}"
        outs.append(out)
    return outs


def _live_lines(audit_dir: Path) -> int:
    return sum(len([ln for ln in path.read_bytes().split(b"\n") if ln]) for path in audit_dir.glob("*.jsonl"))


def _archived_lines(audit_dir: Path) -> int:
    total = 0
    for path in sorted(audit_dir.glob("archive/*.jsonl.gz")):
        total += len([ln for ln in gzip.decompress(path.read_bytes()).split(b"\n") if ln])
    return total


def _run_writers(
    tmp_path: Path,
    *,
    spellings: list[Path | str],
    events: int,
    mode: str = "append",
    **extra: object,
) -> None:
    """Race one writer per spelling and reap them all."""
    worker_script = _write_worker(tmp_path)
    barrier = tmp_path / "barrier"
    barrier.mkdir(exist_ok=True)
    names = [f"w{i}" for i in range(len(spellings))]
    procs = [
        _spawn(
            worker_script,
            tmp_path,
            name=name,
            audit_dir=spelling,
            barrier=barrier,
            mode=mode,
            events=events,
            **extra,
        )
        for name, spelling in zip(names, spellings, strict=True)
    ]
    _release_barrier(barrier, names)
    _reap(procs)


# ---------------------------------------------------------------------------
# Axis: the harness itself has teeth
# ---------------------------------------------------------------------------


@requires_flock
def test_two_processes_never_occupy_the_append_section_at_once(tmp_path: Path) -> None:
    """A second process cannot enter the section while the first holds it.

    Timing-free, unlike the N-writers tests below: the holder does not leave the
    section until the prober has announced it is about to take it, and marks the
    section left as its last act inside. Under a working lock the prober is
    blocked at that point, so it can only ever observe the mark, whatever the
    scheduler does. ``hold_seconds`` is therefore zero here; it exists for the
    no-lock twin, where the holder has to still be inside when the prober looks.
    """
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    barrier = tmp_path / "barrier"
    barrier.mkdir()
    worker_script = _write_worker(tmp_path)

    holder = _spawn(
        worker_script,
        tmp_path,
        name="holder",
        audit_dir=audit_dir,
        barrier=barrier,
        mode="hold_lock",
        hold_seconds=0.0,
    )
    prober = _spawn(
        worker_script,
        tmp_path,
        name="prober",
        audit_dir=audit_dir,
        barrier=barrier,
        mode="probe_lock",
    )
    _release_barrier(barrier, ["holder", "prober"])
    _, prober_out = _reap([holder, prober])
    assert json.loads(prober_out)["holder_had_finished"] is True, (
        "a second process entered the append section while the first still held it"
    )


@requires_flock
def test_the_probe_reports_a_violation_when_the_lock_cannot_lock(tmp_path: Path) -> None:
    """The same probe against a section that takes no OS lock reports the breach.

    This is the teeth. A mutual-exclusion assertion that has never been seen to
    fail proves nothing about the lock; running the identical harness against
    the ``fcntl is None`` path - the one the code already documents as a no-op -
    shows the assertion is load bearing.

    The holder stays inside for five seconds after the prober announces itself.
    The prober's announce-then-acquire-then-stat is a handful of syscalls, so
    the margin is not a tuning knob to be trimmed: it is the gap between a
    couple of milliseconds of work and a deschedule long enough to make a loaded
    runner report a lock it does not have.
    """
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    barrier = tmp_path / "barrier"
    barrier.mkdir()
    worker_script = _write_worker(tmp_path)

    holder = _spawn(
        worker_script,
        tmp_path,
        name="holder",
        audit_dir=audit_dir,
        barrier=barrier,
        mode="hold_lock",
        hold_seconds=5.0,
        break_lock=True,
    )
    prober = _spawn(
        worker_script,
        tmp_path,
        name="prober",
        audit_dir=audit_dir,
        barrier=barrier,
        mode="probe_lock",
        break_lock=True,
    )
    _release_barrier(barrier, ["holder", "prober"])
    _, prober_out = _reap([holder, prober])
    assert json.loads(prober_out)["holder_had_finished"] is False, (
        "expected the no-lock variant to let a second process in; if this passes, "
        "the probe is measuring something other than the lock"
    )


# ---------------------------------------------------------------------------
# Axis: path spelling
# ---------------------------------------------------------------------------


def _spelling_variants(audit_dir: Path, tmp_path: Path) -> list[Path | str]:
    """Return distinct path strings that all name ``audit_dir``.

    The dot-segment forms work everywhere. The symlink is added only where the
    platform lets an unprivileged process create one, so the test degrades to
    fewer spellings rather than failing on a Windows runner without developer
    mode.
    """
    variants: list[Path | str] = [
        str(audit_dir),
        str(audit_dir.parent / "." / audit_dir.name),
        str(audit_dir / "." / ".." / audit_dir.name),
    ]
    link = tmp_path / "audit-link"
    try:
        link.symlink_to(audit_dir, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
        return variants
    variants.append(str(link))
    return variants


def test_one_audit_dir_under_several_spellings_is_one_identity(tmp_path: Path) -> None:
    """``_audit_dir_key`` folds every spelling onto one key.

    The in-process guard is keyed on this. Two spellings that hashed apart would
    hand one thread two guards for one directory, and the re-entrancy bookkeeping
    would then re-take a flock it already holds.
    """
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    keys = {_audit_dir_key(Path(spelling)) for spelling in _spelling_variants(audit_dir, tmp_path)}
    assert len(keys) == 1, f"spellings of one directory produced several identities: {keys}"


@requires_flock
def test_writers_using_different_spellings_share_one_chain(tmp_path: Path) -> None:
    """Concurrent writers that name the directory differently still serialise."""
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    spellings = _spelling_variants(audit_dir, tmp_path)
    events = 15

    _run_writers(tmp_path, spellings=spellings, events=events)

    valid, errors = AuditLog(audit_dir, key=_KEY).verify()
    assert valid, f"chain forked across path spellings: {errors[:5]}"
    assert _live_lines(audit_dir) == len(spellings) * events
    # One lock file, not one per spelling.
    assert [p.name for p in audit_dir.glob(".chain.lock*")] == [".chain.lock"]


# ---------------------------------------------------------------------------
# Axis: day rollover crossed mid-append
# ---------------------------------------------------------------------------


@requires_flock
def test_appends_that_cross_a_day_boundary_keep_one_chain(tmp_path: Path) -> None:
    """Writers straddling UTC midnight contend on the same lock and stay linear.

    The lock file is deliberately one stable ``.chain.lock`` per directory
    rather than one per day. A per-day lock would let a writer that has already
    rolled over append at the same time as one that has not, and the second
    segment's first record would chain onto a head that moved underneath it.
    """
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    workers = 4
    events = 10
    total = workers * events

    _run_writers(
        tmp_path,
        spellings=[audit_dir] * workers,
        events=events,
        mode="rollover",
        switch_at=total // 2,
    )

    segments = sorted(p.name for p in audit_dir.glob("*.jsonl"))
    assert segments == ["2026-03-14.jsonl", "2026-03-15.jsonl"], (
        f"expected the run to straddle the rollover, got {segments}"
    )
    for name in segments:
        assert (audit_dir / name).stat().st_size > 0

    valid, errors = AuditLog(audit_dir, key=_KEY).verify()
    assert valid, f"chain broke across the day rollover: {errors[:5]}"
    assert _live_lines(audit_dir) == total


# ---------------------------------------------------------------------------
# Axis: archive concurrent with appends
# ---------------------------------------------------------------------------


def _seed_old_segment(audit_dir: Path, *, days_ago: int, count: int) -> str:
    """Write ``count`` real chain records dated ``days_ago`` and return the stem."""
    import bernstein.core.security.audit as audit_mod

    stamp = datetime.now(tz=UTC) - timedelta(days=days_ago)

    class _FrozenClock(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return stamp

    real = audit_mod.datetime
    audit_mod.datetime = _FrozenClock  # type: ignore[misc]
    try:
        log = AuditLog(audit_dir, key=_KEY)
        for i in range(count):
            log.log("seed.event", "seeder", "res", f"seed-{i}")
    finally:
        audit_mod.datetime = real  # type: ignore[misc]
    return stamp.strftime("%Y-%m-%d")


@requires_flock
def test_archive_running_against_live_appends_loses_no_record(tmp_path: Path) -> None:
    """Retention compressing an old segment does not race the writers appending.

    ``archive`` compresses and unlinks inside the append section because an
    append that landed between the copy and the unlink would exist only in the
    file about to be removed. The writers here construct their ``AuditLog``
    while the old segment is still live, so at least one of them recovers its
    chain tail from a segment that retention is about to replace.
    """
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    seeded = 6
    stem = _seed_old_segment(audit_dir, days_ago=40, count=seeded)

    workers = 3
    events = 12
    worker_script = _write_worker(tmp_path)
    barrier = tmp_path / "barrier"
    barrier.mkdir()

    names = [f"w{i}" for i in range(workers)]
    procs = [
        _spawn(
            worker_script,
            tmp_path,
            name=name,
            audit_dir=audit_dir,
            barrier=barrier,
            mode="append",
            events=events,
        )
        for name in names
    ]
    names.append("archiver")
    procs.append(
        _spawn(
            worker_script,
            tmp_path,
            name="archiver",
            audit_dir=audit_dir,
            barrier=barrier,
            mode="archive",
            retention_days=7,
        )
    )
    _release_barrier(barrier, names)
    outs = _reap(procs)

    assert json.loads(outs[-1])["archived"] == [f"{stem}.jsonl"]
    assert not (audit_dir / f"{stem}.jsonl").exists()

    valid, errors = AuditLog(audit_dir, key=_KEY).verify()
    assert valid, f"chain broke while retention ran against live appends: {errors[:5]}"
    assert _archived_lines(audit_dir) + _live_lines(audit_dir) == seeded + workers * events


# ---------------------------------------------------------------------------
# Axis: an exception unwinding out of the append path
# ---------------------------------------------------------------------------


def test_an_exception_leaves_no_append_section_open(tmp_path: Path) -> None:
    """The re-entrancy depth returns to zero when the body raises.

    A leaked depth would make the *next* append in this thread believe it is
    nested, skip the flock, and stop excluding other processes: a lock that
    reports success and locks nothing.
    """
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()

    with pytest.raises(RuntimeError, match="deliberate"), _chain_append_lock(audit_dir):
        assert _inside_append_section(audit_dir) is True
        raise RuntimeError("deliberate unwind")

    assert _inside_append_section(audit_dir) is False


@requires_flock
def test_a_writer_that_raised_still_excludes_another_process(tmp_path: Path) -> None:
    """After an unwind, appends from two processes still produce one chain.

    Covers the two ways the ``finally`` could leak: a descriptor still holding
    the flock (the worker would block on itself and the reap would time out) and
    a depth that never returned to zero (the worker would keep appending without
    the flock and fork the chain against the second writer).
    """
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    events = 15
    worker_script = _write_worker(tmp_path)
    barrier = tmp_path / "barrier"
    barrier.mkdir()

    names = ["raiser", "plain"]
    procs = [
        _spawn(
            worker_script,
            tmp_path,
            name="raiser",
            audit_dir=audit_dir,
            barrier=barrier,
            mode="raise_then_append",
            events=events,
        ),
        _spawn(
            worker_script,
            tmp_path,
            name="plain",
            audit_dir=audit_dir,
            barrier=barrier,
            mode="append",
            events=events,
        ),
    ]
    _release_barrier(barrier, names)
    _reap(procs)

    valid, errors = AuditLog(audit_dir, key=_KEY).verify()
    assert valid, f"chain broke after an exception unwound out of the append path: {errors[:5]}"
    assert _live_lines(audit_dir) == 2 * events


# ---------------------------------------------------------------------------
# The constant the archive axis leans on
# ---------------------------------------------------------------------------


def test_retention_policy_default_is_not_what_the_archive_axis_relies_on() -> None:
    """Guard the constant the archive axis picks its dates against.

    ``retention_days=7`` in the archive test is only meaningful while the
    default window is wider than a few days; if the default shrank to zero the
    axis would archive today's segment too and stop testing the race it names.
    """
    assert RetentionPolicy().retention_days >= 7
