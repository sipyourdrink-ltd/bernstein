"""Unit tests for the tag-filtered resource-lease primitive (#5128).

Concurrency fixtures follow the GC-lock tests in
``tests/unit/test_worktrees_cmd.py``: a real lock file on disk, real threads,
and a real killed subprocess -- never a mocked lock. Resource/tag fixtures
follow ``tests/unit/sandbox/test_pool_placement.py``.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from bernstein.core.sandbox.resource_lease import (
    LeaseConflictError,
    LeaseStore,
    NoFreeResourceError,
    NoMatchingResourceError,
    ResourceDeclaration,
    ResourceRegistry,
    claim,
    named_lock,
)


@pytest.fixture
def store(tmp_path: Path) -> LeaseStore:
    """A lease store rooted in an isolated tmp tree."""
    return LeaseStore(tmp_path / "state")


@pytest.fixture
def registry() -> ResourceRegistry:
    """Three resources with overlapping, many-to-many tags."""
    return ResourceRegistry(
        [
            ResourceDeclaration("gpu-0", {"gpu", "cuda", "linux"}),
            ResourceDeclaration("gpu-1", {"gpu", "cuda", "linux"}),
            ResourceDeclaration("mac-0", {"macos", "arm64"}),
        ]
    )


# ---------------------------------------------------------------------------
# Atomic claim
# ---------------------------------------------------------------------------


def test_two_claimants_for_one_resource_yield_exactly_one_lease(store: LeaseStore) -> None:
    """One resource, two concurrent claimants: exactly one lease is handed out."""
    registry = ResourceRegistry([ResourceDeclaration("gpu-0", {"gpu"})])

    granted: list[str] = []
    refused: list[BaseException] = []
    unexpected: list[BaseException] = []
    barrier = threading.Barrier(2, timeout=5)

    def contend() -> None:
        try:
            barrier.wait()
            lease = claim(registry, {"gpu"}, store=store)
            granted.append(lease.lease_id)
        except NoFreeResourceError as exc:
            refused.append(exc)
        except BaseException as exc:  # pragma: no cover - surfaced by the assert
            unexpected.append(exc)

    threads = [threading.Thread(target=contend) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not unexpected
    assert len(granted) == 1
    assert len(refused) == 1


def test_no_match_and_none_free_are_distinct_errors(store: LeaseStore, registry: ResourceRegistry) -> None:
    """A filter nothing declares raises NoMatch; a fully-held match raises NoneFree."""
    with pytest.raises(NoMatchingResourceError):
        claim(registry, {"fpga"}, store=store)

    held = [claim(registry, {"gpu"}, store=store), claim(registry, {"gpu"}, store=store)]
    try:
        with pytest.raises(NoFreeResourceError):
            claim(registry, {"gpu"}, store=store)
    finally:
        for lease in held:
            lease.release()

    assert not issubclass(NoMatchingResourceError, NoFreeResourceError)
    assert not issubclass(NoFreeResourceError, NoMatchingResourceError)


def test_claim_skips_held_resources_and_takes_a_free_sibling(store: LeaseStore, registry: ResourceRegistry) -> None:
    """With gpu-0 held, a second claim for the same filter lands on gpu-1."""
    first = claim(registry, {"cuda"}, store=store)
    second = claim(registry, {"cuda"}, store=store)
    try:
        assert {first.resource_id, second.resource_id} == {"gpu-0", "gpu-1"}
    finally:
        first.release()
        second.release()


# ---------------------------------------------------------------------------
# TTL, owner, keepalive
# ---------------------------------------------------------------------------


def test_killed_holders_lease_expires_by_ttl(
    store: LeaseStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A SIGKILLed holder leaves its lease file behind; the TTL alone reclaims it."""
    from bernstein.core.sandbox import resource_lease

    script = tmp_path / "holder.py"
    script.write_text(
        "import sys, time\n"
        "from bernstein.core.sandbox.resource_lease import LeaseStore\n"
        "store = LeaseStore(sys.argv[1])\n"
        "store.acquire('gpu-0', ttl_s=1.0)\n"
        "print('held', flush=True)\n"
        "time.sleep(600)\n",
        encoding="utf-8",
    )
    env = {**os.environ, "UV_NO_SYNC": "1"}
    proc = subprocess.Popen(
        [sys.executable, str(script), str(store.root)],
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "held"
    finally:
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)

    lock_path = store.path_for("gpu-0")
    assert lock_path.exists(), "a killed holder must not release its lease"

    # Pin liveness to True so the *only* reason the lease can be reclaimed is
    # its TTL, not the dead pid.
    monkeypatch.setattr(resource_lease, "_holder_process_alive", lambda meta: True)

    with pytest.raises(LeaseConflictError):
        store.acquire("gpu-0")

    time.sleep(1.1)
    reclaimed = store.acquire("gpu-0")
    try:
        assert reclaimed.resource_id == "gpu-0"
    finally:
        reclaimed.release()


def test_keepalive_extends_the_ttl(store: LeaseStore) -> None:
    """A keepalive pushes the recorded expiry out and keeps the lease unreclaimable."""
    lease = store.acquire("gpu-0", ttl_s=1.0)
    try:
        first_expiry = lease.expires_at
        extended = lease.keepalive(ttl_s=120.0)
        assert extended > first_expiry
        assert lease.expires_at == extended

        recorded = json.loads(store.path_for("gpu-0").read_text(encoding="utf-8"))
        assert recorded["expires_at"] == pytest.approx(extended)

        time.sleep(1.1)
        with pytest.raises(LeaseConflictError):
            store.acquire("gpu-0")
    finally:
        lease.release()


def test_keepalive_on_a_reclaimed_lease_refuses(store: LeaseStore) -> None:
    """Once another claimant owns the resource, the old holder cannot extend it."""
    lease = store.acquire("gpu-0", ttl_s=0.05)
    time.sleep(0.1)
    stealer = store.acquire("gpu-0")
    try:
        with pytest.raises(LeaseConflictError):
            lease.keepalive(ttl_s=60.0)
    finally:
        stealer.release()
    # The rightful owner's release must not delete the stealer's lease either.
    lease.release()


def test_lease_records_owner_from_session_identity(store: LeaseStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """The owner field is sourced from the install identity, not invented."""
    from bernstein.core.sandbox import resource_lease

    monkeypatch.setattr(resource_lease, "_session_identity", lambda: "install-rev-abc")
    lease = store.acquire("gpu-0")
    try:
        assert lease.owner == "install-rev-abc"
        recorded = json.loads(store.path_for("gpu-0").read_text(encoding="utf-8"))
        assert recorded["owner"] == "install-rev-abc"
        assert recorded["pid"] == os.getpid()
    finally:
        lease.release()


# ---------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------


def test_context_manager_releases_on_exception(store: LeaseStore, registry: ResourceRegistry) -> None:
    """The lease file is unlinked even when the body raises."""

    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom):
        with claim(registry, {"macos"}, store=store) as lease:
            assert store.path_for(lease.resource_id).exists()
            raise Boom

    assert not store.path_for("mac-0").exists()
    # Next claim for the same filter succeeds.
    with claim(registry, {"macos"}, store=store) as again:
        assert again.resource_id == "mac-0"


def test_process_exit_releases_leases_the_process_still_holds(store: LeaseStore, tmp_path: Path) -> None:
    """A holder that exits without releasing still leaves no lease behind."""
    script = tmp_path / "forgetful.py"
    script.write_text(
        "import sys\n"
        "from bernstein.core.sandbox.resource_lease import LeaseStore\n"
        "store = LeaseStore(sys.argv[1])\n"
        "store.acquire('gpu-0', ttl_s=600.0)\n"
        "assert store.path_for('gpu-0').exists()\n",
        encoding="utf-8",
    )
    env = {**os.environ, "UV_NO_SYNC": "1"}
    result = subprocess.run(
        [sys.executable, str(script), str(store.root)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not store.path_for("gpu-0").exists()


def test_release_never_raises_when_the_lease_file_is_already_gone(store: LeaseStore) -> None:
    """Release logs and returns on a missing file rather than raising."""
    lease = store.acquire("gpu-0")
    store.path_for("gpu-0").unlink()
    lease.release()
    lease.release()


# ---------------------------------------------------------------------------
# Named locks
# ---------------------------------------------------------------------------


def test_named_lock_serialises_unregistered_resources(store: LeaseStore) -> None:
    """An arbitrary name that no registry declares still serialises two holders."""
    started = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def hold() -> None:
        try:
            with named_lock(store, "catalogue-rebuild"):
                started.set()
                release.wait(timeout=5)
        except BaseException as exc:  # pragma: no cover - surfaced by the assert
            errors.append(exc)

    holder = threading.Thread(target=hold)
    holder.start()
    started.wait(timeout=5)
    try:
        with pytest.raises(LeaseConflictError):
            with named_lock(store, "catalogue-rebuild"):
                pass
    finally:
        release.set()
        holder.join(timeout=5)

    assert not errors
    assert not store.path_for("catalogue-rebuild").exists()


# ---------------------------------------------------------------------------
# Staleness is provable, never inferred from an empty or half-written file
# ---------------------------------------------------------------------------


def test_fresh_empty_lease_file_is_a_holder_mid_write_not_stale(store: LeaseStore) -> None:
    """A lease created between ``O_EXCL`` and the payload write is never stolen."""
    path = store.path_for("gpu-0")
    path.parent.mkdir(parents=True)
    path.write_text("")  # holder created the file, has not written the payload yet

    with pytest.raises(LeaseConflictError):
        store.acquire("gpu-0")

    assert path.exists()
    assert path.read_text() == ""


def test_old_empty_lease_file_from_a_crashed_holder_is_reclaimed(store: LeaseStore) -> None:
    """An empty file that outlives any plausible write is a crashed holder, not a live one."""
    path = store.path_for("gpu-0")
    path.parent.mkdir(parents=True)
    path.write_text("")
    long_ago = time.time() - 3600
    os.utime(path, (long_ago, long_ago))

    lease = store.acquire("gpu-0")

    assert lease.pid == os.getpid()
    assert json.loads(path.read_text())["lease_id"] == lease.lease_id


def test_old_malformed_lease_file_is_reclaimed_but_a_fresh_one_is_not(store: LeaseStore) -> None:
    path = store.path_for("gpu-0")
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    with pytest.raises(LeaseConflictError):
        store.acquire("gpu-0")

    long_ago = time.time() - 3600
    os.utime(path, (long_ago, long_ago))
    assert store.acquire("gpu-0").resource_id == "gpu-0"


def _seed_expired_lease(store: LeaseStore, name: str) -> None:
    path = store.path_for(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "resource_id": name,
                "lease_id": "dead",
                "owner": "gone",
                "pid": os.getpid(),
                "host": "",
                "acquired_at": 0.0,
                "expires_at": 1.0,
            }
        )
    )


def test_racing_reclaimers_of_one_expired_lease_yield_exactly_one_holder(store: LeaseStore) -> None:
    """Several claimants find the same expired lease; only one may take it over.

    A steal that is ``unlink`` then retry lets a second claimant delete the
    first one's freshly created lease and hold the resource as well.
    """
    claimants = 8
    for round_no in range(40):
        name = f"gpu-{round_no}"
        _seed_expired_lease(store, name)
        barrier = threading.Barrier(claimants)
        won: list[object] = []
        lost: list[BaseException] = []

        def contend(
            name: str = name,
            barrier: threading.Barrier = barrier,
            won: list[object] = won,
            lost: list[BaseException] = lost,
        ) -> None:
            barrier.wait(timeout=5)
            try:
                won.append(store.acquire(name))
            except LeaseConflictError as exc:
                lost.append(exc)

        threads = [threading.Thread(target=contend) for _ in range(claimants)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(won) == 1, f"round {round_no}: {len(won)} holders for {name}"
        assert len(lost) == claimants - 1


_COUNTER_WORKER = """
import sys, time
from pathlib import Path
from bernstein.core.sandbox.resource_lease import LeaseConflictError, LeaseStore, named_lock

root, counter, rounds = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
store = LeaseStore(root)
done = 0
while done < rounds:
    try:
        with named_lock(store, "counter"):
            value = int(counter.read_text())
            time.sleep(0.001)  # widen the read-modify-write window
            counter.write_text(str(value + 1))
        done += 1
    except LeaseConflictError:
        time.sleep(0.001)
"""


def test_named_lock_gives_processes_mutual_exclusion(store: LeaseStore, tmp_path: Path) -> None:
    """Separate processes incrementing a shared counter under one lock lose no update."""
    counter = tmp_path / "counter.txt"
    counter.write_text("0")
    procs = [
        subprocess.Popen([sys.executable, "-c", _COUNTER_WORKER, str(store.root), str(counter), "15"]) for _ in range(4)
    ]
    for proc in procs:
        assert proc.wait(timeout=60) == 0

    assert int(counter.read_text()) == 4 * 15
    assert not store.path_for("counter").exists()
