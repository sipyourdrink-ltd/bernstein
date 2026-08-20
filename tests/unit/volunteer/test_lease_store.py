"""A lease is a promise that exactly one worker is holding a task, so these tests
fake the clock and nothing else.

Expiry is the whole subject here, and expiry is arithmetic on a wall clock.  The
store takes its clock by injection, so every test below advances time by an exact
number of seconds rather than sleeping: "expired" is a fact the test states, not a
race it hopes for.  The JSONL log is a real file under ``tmp_path`` and the store
under test is the real one -- no mocked writes, because two of the properties that
matter (reload fidelity, and a reassignment that is not repeated after a restart)
are *only* visible through bytes that actually landed on disk.

Each test is named for the property it protects rather than the method it calls.
The one that carries the acceptance criterion is
``test_an_expired_lease_is_reassigned_exactly_once_under_concurrent_claims``: two
donors racing for the same abandoned task must produce one holder and one refusal.
That holds only if reaping happens *inline*, as the first statement under the lock
of the claim itself.  A background reaper -- a timer thread that sweeps expired
leases on its own schedule -- looks correct in a single-threaded test and fails
this one, because both claimants would still see the stale lease and both would be
turned away (or, worse, both admitted).  The test is written so that arrangement
cannot pass.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.volunteer.lease_store import (
    LEASE_STORE_SCHEMA_VERSION,
    Lease,
    LeaseRefusal,
    LeaseRefusalReason,
    LeaseStore,
    ReassignedLease,
    load_or_create_worker_key,
)

if TYPE_CHECKING:
    from pathlib import Path

#: A fixed "now" so every expiry below is exact.  The value is a plausible wall
#: clock reading rather than 0, because the store stores wall time (a lease has to
#: survive a restart, which ``time.monotonic`` does not).
T0 = 1_700_000_000.0

#: The lease window every test claims with, unless it is testing the window.
TTL = 60

TASK_ID = "T-4036"


# ---------------------------------------------------------------------------
# Fake clock and store/worker builders
# ---------------------------------------------------------------------------


class _FakeClock:
    """A mutable ``time.time`` stand-in.

    ``LeaseStore`` reads the clock on every operation, so a test advances this
    between calls to place an operation exactly on either side of an expiry
    boundary.  Mutable rather than a sequence of canned values: the store is free
    to read the clock more than once per call, and a test should not have to know
    how often.
    """

    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _store(tmp_path: Path, clock: _FakeClock) -> LeaseStore:
    """A store over a real JSONL file in the test's own directory."""
    return LeaseStore(tmp_path / "leases.jsonl", clock=clock)


async def _worker(store: LeaseStore) -> str:
    """Enroll a fresh keypair and return the worker id the store assigned it."""
    return await store.enroll(Ed25519PrivateKey.generate().public_key())


def _records(tmp_path: Path) -> list[dict[str, object]]:
    """Every JSONL record written so far, in file order."""
    path = tmp_path / "leases.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# claim: one holder at a time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_claim_on_an_unleased_task_succeeds(tmp_path: Path) -> None:
    clock = _FakeClock()
    store = _store(tmp_path, clock)
    worker = await _worker(store)

    lease = await store.claim(TASK_ID, worker, TTL)

    assert isinstance(lease, Lease)
    assert lease.task_id == TASK_ID
    assert lease.worker_id == worker
    assert lease.generation == 1
    assert lease.claimed_at == T0
    assert lease.expires_at == T0 + TTL
    assert lease.submission is None
    assert store.lease_for(TASK_ID) == lease


@pytest.mark.asyncio
async def test_a_second_claim_on_an_already_leased_unexpired_task_is_refused(tmp_path: Path) -> None:
    # The point of the whole module: while a lease is live, a second donor is
    # turned away rather than handed duplicate work.  The refusal arrives as a
    # value, not an exception -- this package's stated discipline.
    clock = _FakeClock()
    store = _store(tmp_path, clock)
    holder = await _worker(store)
    latecomer = await _worker(store)

    granted = await store.claim(TASK_ID, holder, TTL)
    assert isinstance(granted, Lease)

    clock.advance(TTL - 1)  # still inside the window, by one second
    refusal = await store.claim(TASK_ID, latecomer, TTL)

    assert isinstance(refusal, LeaseRefusal)
    assert refusal.reason is LeaseRefusalReason.ALREADY_LEASED
    assert store.lease_for(TASK_ID) == granted


@pytest.mark.asyncio
async def test_a_claim_on_a_task_whose_lease_expired_succeeds_for_a_new_worker(tmp_path: Path) -> None:
    # A donor whose laptop closed mid-task must not hold the work forever.  Once
    # the window passes, the next claimant gets it at the next generation -- the
    # generation counter is what lets a late heartbeat from the old holder be
    # recognised as stale rather than merely unauthorised.
    clock = _FakeClock()
    store = _store(tmp_path, clock)
    gone = await _worker(store)
    successor = await _worker(store)

    first = await store.claim(TASK_ID, gone, TTL)
    assert isinstance(first, Lease)

    clock.advance(TTL + 1)
    second = await store.claim(TASK_ID, successor, TTL)

    assert isinstance(second, Lease)
    assert second.worker_id == successor
    assert second.generation == first.generation + 1
    assert second.expires_at == clock.now + TTL


@pytest.mark.asyncio
async def test_an_expired_lease_is_reassigned_exactly_once_under_concurrent_claims(tmp_path: Path) -> None:
    # The acceptance criterion, and the reason reaping is inline rather than a
    # background sweep.  Two donors reach for the same abandoned task in the same
    # event-loop turn.  Exactly one may come away holding it.
    #
    # With inline reaping the lock serialises them: the first claim reaps the dead
    # lease and takes generation 2, the second sees a live lease held by someone
    # else and is refused.  With a background reaper, neither claimant reaps, both
    # see the stale lease, and the "exactly one Lease" assertion below fails --
    # which is the point of writing it this way.
    clock = _FakeClock()
    store = _store(tmp_path, clock)
    gone = await _worker(store)
    racer_a = await _worker(store)
    racer_b = await _worker(store)

    first = await store.claim(TASK_ID, gone, TTL)
    assert isinstance(first, Lease)
    clock.advance(TTL + 1)

    results = await asyncio.gather(
        store.claim(TASK_ID, racer_a, TTL),
        store.claim(TASK_ID, racer_b, TTL),
    )

    granted = [r for r in results if isinstance(r, Lease)]
    refused = [r for r in results if isinstance(r, LeaseRefusal)]
    assert len(granted) == 1, f"expected exactly one winner, got {results}"
    assert len(refused) == 1
    # One reassignment, not two: the loser must not bump the generation again.
    assert granted[0].generation == 2
    assert refused[0].reason is LeaseRefusalReason.ALREADY_LEASED
    assert store.lease_for(TASK_ID) == granted[0]
    assert granted[0].worker_id in {racer_a, racer_b}


@pytest.mark.asyncio
async def test_a_re_claim_by_the_holder_extends_the_lease_and_keeps_the_generation(tmp_path: Path) -> None:
    # A worker that retries its own claim (a restarted run resuming its own task)
    # is not a competitor.  Re-granting is idempotent: the window moves, the
    # generation does not, so nobody downstream reads this as a reassignment.
    clock = _FakeClock()
    store = _store(tmp_path, clock)
    holder = await _worker(store)

    first = await store.claim(TASK_ID, holder, TTL)
    assert isinstance(first, Lease)

    clock.advance(10)
    again = await store.claim(TASK_ID, holder, TTL)

    assert isinstance(again, Lease)
    assert again.generation == first.generation
    assert again.worker_id == holder
    assert again.expires_at == T0 + 10 + TTL
    assert again.expires_at > first.expires_at


@pytest.mark.asyncio
async def test_a_claim_on_a_submitted_task_is_refused_as_already_submitted(tmp_path: Path) -> None:
    # A submission is terminal.  Submitted work is not abandoned work, so no
    # amount of elapsed time makes the task claimable again -- otherwise a slow
    # review window would hand the same finished task to a second donor.
    clock = _FakeClock()
    store = _store(tmp_path, clock)
    holder = await _worker(store)
    other = await _worker(store)

    assert isinstance(await store.claim(TASK_ID, holder, TTL), Lease)
    assert isinstance(await store.submit(TASK_ID, holder, "sha256:abc", "s3://bundles/abc"), Lease)

    clock.advance(TTL * 100)
    refusal = await store.claim(TASK_ID, other, TTL)

    assert isinstance(refusal, LeaseRefusal)
    assert refusal.reason is LeaseRefusalReason.ALREADY_SUBMITTED


@pytest.mark.asyncio
async def test_a_claim_by_a_worker_that_was_never_enrolled_is_refused(tmp_path: Path) -> None:
    # Enrollment is what binds a worker id to a public key.  An unenrolled id is
    # an id nothing can be attributed to, so it is refused before it can take a
    # lease -- an unattributable holder is worse than an unheld task.
    clock = _FakeClock()
    store = _store(tmp_path, clock)

    refusal = await store.claim(TASK_ID, "wk-never-enrolled", TTL)

    assert isinstance(refusal, LeaseRefusal)
    assert refusal.reason is LeaseRefusalReason.UNKNOWN_WORKER
    assert store.lease_for(TASK_ID) is None


# ---------------------------------------------------------------------------
# heartbeat: extend the window, change nothing else
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_extends_the_expiry_and_does_not_reset_other_lease_fields(tmp_path: Path) -> None:
    # A heartbeat is a liveness signal, not a re-claim.  If it reset claimed_at
    # the task's age would be unknowable; if it bumped the generation every
    # heartbeat would look like a reassignment to anyone watching the log.  Only
    # the two time fields that describe liveness are allowed to move.
    clock = _FakeClock()
    store = _store(tmp_path, clock)
    holder = await _worker(store)

    lease = await store.claim(TASK_ID, holder, TTL)
    assert isinstance(lease, Lease)

    clock.advance(30)
    beat = await store.heartbeat(TASK_ID, holder)

    assert isinstance(beat, Lease)
    assert beat.task_id == lease.task_id
    assert beat.worker_id == lease.worker_id
    assert beat.claimed_at == lease.claimed_at
    assert beat.generation == lease.generation
    assert beat.submission is lease.submission is None
    # The window is re-derived from the ttl the original claim supplied, which is
    # why the lease carries it: heartbeat has no ttl argument of its own.
    assert beat.ttl_seconds == TTL
    assert beat.heartbeat_at == T0 + 30
    assert beat.expires_at == T0 + 30 + TTL


@pytest.mark.asyncio
async def test_heartbeat_from_a_worker_that_does_not_hold_the_lease_is_refused(tmp_path: Path) -> None:
    # Otherwise any donor could keep any task alive -- including one it has no
    # work in flight for, which is a denial of service dressed as liveness.
    clock = _FakeClock()
    store = _store(tmp_path, clock)
    holder = await _worker(store)
    stranger = await _worker(store)

    granted = await store.claim(TASK_ID, holder, TTL)
    assert isinstance(granted, Lease)

    refusal = await store.heartbeat(TASK_ID, stranger)

    assert isinstance(refusal, LeaseRefusal)
    assert refusal.reason is LeaseRefusalReason.NOT_LEASE_HOLDER
    assert store.lease_for(TASK_ID) == granted


# ---------------------------------------------------------------------------
# release and submit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_frees_the_task_for_a_new_claim_immediately(tmp_path: Path) -> None:
    # A donor that gives up voluntarily should not cost the pool a full TTL of
    # dead time.  Release drops the lease now; the clock does not move here at
    # all, and the next claim still succeeds.
    clock = _FakeClock()
    store = _store(tmp_path, clock)
    holder = await _worker(store)
    successor = await _worker(store)

    assert isinstance(await store.claim(TASK_ID, holder, TTL), Lease)
    assert await store.release(TASK_ID, holder) is None
    assert store.lease_for(TASK_ID) is None

    regranted = await store.claim(TASK_ID, successor, TTL)

    assert isinstance(regranted, Lease)
    assert regranted.worker_id == successor
    assert regranted.expires_at == T0 + TTL


@pytest.mark.asyncio
async def test_a_second_submission_for_an_already_submitted_lease_is_refused(tmp_path: Path) -> None:
    # Submission is the terminal state and must be write-once: a second submit
    # would silently replace the digest the first one attested, which is exactly
    # the swap a verification gate exists to prevent.
    clock = _FakeClock()
    store = _store(tmp_path, clock)
    holder = await _worker(store)

    assert isinstance(await store.claim(TASK_ID, holder, TTL), Lease)
    first = await store.submit(TASK_ID, holder, "sha256:first", "s3://bundles/first")
    assert isinstance(first, Lease)
    assert first.submission is not None
    assert first.submission.bundle_digest == "sha256:first"

    second = await store.submit(TASK_ID, holder, "sha256:second", "s3://bundles/second")

    assert isinstance(second, LeaseRefusal)
    assert second.reason is LeaseRefusalReason.ALREADY_SUBMITTED
    held = store.lease_for(TASK_ID)
    assert held is not None
    assert held.submission is not None
    assert held.submission.bundle_digest == "sha256:first"


@pytest.mark.asyncio
async def test_submit_without_an_active_or_recently_held_lease_is_refused(tmp_path: Path) -> None:
    # A bundle for a task nobody ever leased has no provenance the store can
    # vouch for.  NO_LEASE is distinct from NOT_LEASE_HOLDER on purpose: the
    # caller is not a thief, there is simply nothing here.
    clock = _FakeClock()
    store = _store(tmp_path, clock)
    worker = await _worker(store)

    refusal = await store.submit(TASK_ID, worker, "sha256:orphan", "s3://bundles/orphan")

    assert isinstance(refusal, LeaseRefusal)
    assert refusal.reason is LeaseRefusalReason.NO_LEASE
    assert store.lease_for(TASK_ID) is None


# ---------------------------------------------------------------------------
# enroll
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enroll_is_idempotent_for_the_same_public_key(tmp_path: Path) -> None:
    # The worker id is derived from the key, so re-enrolling is a no-op by
    # construction.  Asserting on the log as well as the return value is what
    # catches the version that returns the right id but appends a duplicate
    # record every time a worker restarts.
    clock = _FakeClock()
    store = _store(tmp_path, clock)
    pubkey = Ed25519PrivateKey.generate().public_key()

    first = await store.enroll(pubkey)
    second = await store.enroll(pubkey)

    assert first == second
    assert store.is_enrolled(first)
    worker_records = [r for r in _records(tmp_path) if r.get("kind") == "worker"]
    assert len(worker_records) == 1
    assert worker_records[0]["worker_id"] == first
    assert worker_records[0]["schema_version"] == LEASE_STORE_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Durability: the log is the state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_state_survives_a_reload_from_the_jsonl_log(tmp_path: Path) -> None:
    # Nothing is mocked here on purpose.  The second store is a real LeaseStore
    # replaying the bytes the first one actually wrote, which is the only way to
    # find out whether a mutation was appended at all -- an in-memory-only
    # heartbeat or submit passes every assertion up to this one.
    clock = _FakeClock()
    store = _store(tmp_path, clock)
    holder = await _worker(store)

    assert isinstance(await store.claim(TASK_ID, holder, TTL), Lease)
    clock.advance(15)
    assert isinstance(await store.heartbeat(TASK_ID, holder), Lease)
    submitted = await store.submit(TASK_ID, holder, "sha256:bundle", "s3://bundles/bundle")
    assert isinstance(submitted, Lease)

    reloaded = LeaseStore(tmp_path / "leases.jsonl", clock=clock)

    assert reloaded.is_enrolled(holder)
    restored = reloaded.lease_for(TASK_ID)
    assert restored is not None
    assert restored == submitted
    assert restored.worker_id == holder
    assert restored.generation == submitted.generation
    assert restored.expires_at == T0 + 15 + TTL
    assert restored.heartbeat_at == T0 + 15
    assert restored.submission is not None
    assert restored.submission.bundle_digest == "sha256:bundle"
    assert restored.submission.location == "s3://bundles/bundle"


@pytest.mark.asyncio
async def test_a_reassignment_survives_a_restart_and_is_not_repeated(tmp_path: Path) -> None:
    # Exactly-once reassignment has to hold across a process boundary, not just
    # within one.  A reap that only mutates memory leaves the claim record as the
    # last word in the log: the next process replays it, finds it expired all over
    # again, and reassigns a task that was already reassigned -- one abandoned
    # lease becoming two reassignment events, and potentially two live workers.
    #
    # The durable ``reassign`` record is what stops that, and the empty second
    # reap below is the only assertion that can tell the two designs apart.
    clock = _FakeClock()
    store = _store(tmp_path, clock)
    gone = await _worker(store)

    first = await store.claim(TASK_ID, gone, TTL)
    assert isinstance(first, Lease)

    clock.advance(TTL + 1)
    reaped = await store.reap_expired()

    assert len(reaped) == 1
    assert isinstance(reaped[0], ReassignedLease)
    assert reaped[0].task_id == TASK_ID
    assert reaped[0].worker_id == gone  # the worker that LOST it
    assert reaped[0].generation == first.generation
    assert reaped[0].reaped_at == clock.now
    assert store.lease_for(TASK_ID) is None

    restarted = LeaseStore(tmp_path / "leases.jsonl", clock=clock)

    assert restarted.lease_for(TASK_ID) is None
    assert await restarted.reap_expired() == ()
    # And still nothing after more time passes: the lease is gone, not pending.
    clock.advance(TTL * 10)
    assert await restarted.reap_expired() == ()


# ---------------------------------------------------------------------------
# Worker key: the seed on disk IS the key
# ---------------------------------------------------------------------------


def test_load_or_create_worker_key_does_not_corrupt_a_seed_ending_in_a_whitespace_byte(tmp_path: Path) -> None:
    # A raw Ed25519 seed is 32 arbitrary bytes, so roughly 4.7% of generated keys
    # begin or end with an ASCII-whitespace byte.  A ``.strip()`` on the read --
    # the reflex when a path holds "a key file" -- silently shortens those to 31
    # bytes and turns a perfectly good key into a "not 32 raw bytes" error, or
    # worse, a different key.  The seed here ends in 0x0a *by construction*: a
    # randomly generated one would miss this about 95% of the time, so this test
    # would pass on a broken implementation most of the times it ran.
    seed = bytes(range(1, 32)) + b"\x0a"
    assert len(seed) == 32
    assert seed[-1:] == b"\n"

    key_path = tmp_path / "worker.key"
    key_path.write_bytes(seed)

    loaded = load_or_create_worker_key(key_path)

    expected = Ed25519PrivateKey.from_private_bytes(seed)
    assert loaded.private_bytes_raw() == seed
    assert loaded.public_key().public_bytes_raw() == expected.public_key().public_bytes_raw()
    # The load path must not rewrite the file it just read.
    assert key_path.read_bytes() == seed
