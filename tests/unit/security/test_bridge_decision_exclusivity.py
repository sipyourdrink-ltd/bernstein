"""One inbound identity yields one bridge decision, under concurrency.

Both bridge paths take a decision from state they read before the section that
is supposed to make the decision exclusive, and record it after that section:

* ``admit_trigger`` reads the replay ledger, then anchors, then remembers the
  nonce. Two deliveries of one ``trigger_id`` both read "not seen" and both
  take the admitted branch, so the task graph is projected and fired twice
  while the receipt still says ``replay_protected=True``.
* ``emit_status_proof`` probes the proof cache, then anchors, then writes the
  cache. Two callbacks for one ``event_id`` both miss, both anchor their own
  ``status.proof.emitted`` event, and the later write overwrites the earlier
  proof, so a peer holds a proof that is not the proof on disk.

Serialising the appends is not the same guarantee: the chain stays linear
either way, which is exactly why neither defect shows up as chain corruption.
These tests race two real threads through the real code and pin the decision
itself as exclusive.

The rendezvous is installed on ``load_or_create_bridge_identity`` because that
is real work both paths perform after the state read and before the anchoring
section. Two threads meeting there have both already taken their decision on
the pre-fix code, which is what makes the race entered rather than merely
scheduled. Once the decision is exclusive the second thread never reaches the
rendezvous, so the wait falls through on its timeout and the barrier stays
broken for the rest of the test.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.security.audit import AuditEvent, load_or_create_audit_key
from bernstein.core.security.audit_chain import (
    EVENT_STATUS_PROOF_EMITTED,
    EVENT_TRIGGER_RECEIPT_ISSUED,
    EVENT_TRIGGER_RECEIPT_REFUSED,
    AuditChainStore,
)
from bernstein.core.trigger_sources import receipt as receipt_module
from bernstein.core.trigger_sources.receipt import (
    REFUSAL_REPLAYED_TRIGGER,
    TriggerAdmission,
    admit_trigger,
    emit_status_proof,
    status_proof_path,
)

_BODY = json.dumps({"title": "Rotate the deploy key", "description": "quarterly"}).encode()

#: How long a thread waits at the rendezvous for its twin. Pre-fix both threads
#: arrive and it costs nothing. Post-fix only one arrives, so the suite pays
#: this once per test; it is small for that reason rather than generous.
_RENDEZVOUS_S = 0.5

#: Bound on a worker thread completing once the rendezvous has resolved.
_JOIN_S = 30.0


@dataclass(frozen=True)
class _Bridge:
    """The bridge locations one test operates on."""

    root: Path
    audit_dir: Path
    hmac_key: bytes


@pytest.fixture()
def bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Bridge:
    """Return an isolated bridge root, audit dir, and HMAC key."""
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    return _Bridge(
        root=tmp_path / "automation-bridge",
        audit_dir=tmp_path / "audit",
        hmac_key=load_or_create_audit_key(tmp_path / "audit.key"),
    )


def _install_rendezvous(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make both racing threads meet after their read and before their append.

    Wraps the real identity load rather than replacing it, so the code under
    test still runs end to end; the only change is that the first thread
    through waits for its twin instead of racing ahead on scheduler luck.
    """
    barrier = threading.Barrier(2)
    real_load = receipt_module.load_or_create_bridge_identity

    def load_at_the_rendezvous(root: Path) -> tuple[str, str]:
        with contextlib.suppress(threading.BrokenBarrierError):
            barrier.wait(timeout=_RENDEZVOUS_S)
        return real_load(root)

    monkeypatch.setattr(receipt_module, "load_or_create_bridge_identity", load_at_the_rendezvous)


def _seed_chain(bridge: _Bridge, count: int = 2) -> None:
    """Put real events on the chain so the head under test is not genesis."""
    for index in range(count):
        admit_trigger(
            root=bridge.root,
            audit_dir=bridge.audit_dir,
            hmac_key=bridge.hmac_key,
            platform="n8n",
            request_path="/webhook",
            trigger_id=f"seed-{index}",
            body=_BODY,
            scope="task:create",
            timestamp=1_700_000_000,
        )


def _chain_rows(bridge: _Bridge, event_type: str, resource_id: str) -> list[AuditEvent]:
    """Return the chain rows of *event_type* recorded against *resource_id*."""
    chain = AuditChainStore(bridge.audit_dir, key=bridge.hmac_key)
    return chain.query(event_type=event_type, resource_id=resource_id)


def _race[T](call: Callable[[], T]) -> tuple[T, T]:
    """Run *call* twice on two threads and return both results."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(call) for _ in range(2)]
        return (futures[0].result(timeout=_JOIN_S), futures[1].result(timeout=_JOIN_S))


# ---------------------------------------------------------------------------
# #3150 -- the replay-nonce decision is exclusive
# ---------------------------------------------------------------------------


def test_doubly_delivered_trigger_is_admitted_exactly_once(bridge: _Bridge, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two concurrent deliveries of one trigger id yield one admission.

    ``replay_protected=True`` on the receipt states that the id was checked
    against the ledger. That is only true if the check and the record of it are
    one decision: read-then-record lets both deliveries pass the check and both
    fire the graph, and neither receipt is distinguishable from an honest one.
    """
    _seed_chain(bridge)
    _install_rendezvous(monkeypatch)

    def deliver() -> TriggerAdmission:
        return admit_trigger(
            root=bridge.root,
            audit_dir=bridge.audit_dir,
            hmac_key=bridge.hmac_key,
            platform="n8n",
            request_path="/webhook",
            trigger_id="delivered-twice",
            body=_BODY,
            scope="task:create",
            timestamp=1_700_000_100,
        )

    first, second = _race(deliver)

    admitted = [outcome for outcome in (first, second) if outcome.admitted]
    refused = [outcome for outcome in (first, second) if not outcome.admitted]
    assert len(admitted) == 1, (
        "both concurrent deliveries of one trigger id were admitted; "
        f"outcomes: {[outcome.admitted for outcome in (first, second)]}"
    )
    assert len(refused) == 1
    assert refused[0].refusal_reason == REFUSAL_REPLAYED_TRIGGER

    # The graph is projected once, so the caller fires it once.
    projected = [outcome for outcome in (first, second) if outcome.graph is not None]
    assert len(projected) == 1, "the task graph was projected twice for one trigger id"

    # The refusal is a signed, chain-anchored receipt in its own right.
    refusal_receipt = refused[0].receipt
    assert refusal_receipt is not None
    assert refusal_receipt.signature
    assert refusal_receipt.chain_entry_hash
    assert refusal_receipt.replay_protected is True

    assert len(_chain_rows(bridge, EVENT_TRIGGER_RECEIPT_ISSUED, "delivered-twice")) == 1
    assert len(_chain_rows(bridge, EVENT_TRIGGER_RECEIPT_REFUSED, "delivered-twice")) == 1

    ok, problems = AuditChainStore(bridge.audit_dir, key=bridge.hmac_key).verify()
    assert ok, problems


def test_unenforced_replay_still_admits_a_repeated_id(bridge: _Bridge, monkeypatch: pytest.MonkeyPatch) -> None:
    """A derived id must not start being refused.

    With ``enforce_replay=False`` the id was derived from the request, so it
    cannot tell a captured replay from a legitimate re-fire. Making the ledger
    decision exclusive must not turn the second re-fire into an error.
    """
    _seed_chain(bridge)
    _install_rendezvous(monkeypatch)

    def deliver() -> TriggerAdmission:
        return admit_trigger(
            root=bridge.root,
            audit_dir=bridge.audit_dir,
            hmac_key=bridge.hmac_key,
            platform="n8n",
            request_path="/webhook",
            trigger_id="derived-id",
            body=_BODY,
            scope="task:create",
            timestamp=1_700_000_200,
            enforce_replay=False,
        )

    first, second = _race(deliver)

    assert first.admitted and second.admitted
    assert first.receipt is not None and first.receipt.replay_protected is False
    assert second.receipt is not None and second.receipt.replay_protected is False
    assert len(_chain_rows(bridge, EVENT_TRIGGER_RECEIPT_ISSUED, "derived-id")) == 2


#: A second interpreter delivering the same trigger id. The rendezvous is the
#: same one the thread tests install, written as files because the two sides
#: share nothing else. Booting is waited on generously (an interpreter start
#: plus the package import); the rendezvous itself is short, because once the
#: decision is exclusive only one process arrives and the other side of that
#: wait is time the suite spends on every run.
_CHILD_DELIVERY = '''
import json, sys, time
from pathlib import Path

from bernstein.core.security.audit import load_or_create_audit_key
from bernstein.core.trigger_sources import receipt as receipt_module

root, audit_dir, key_path = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
slot, sync = sys.argv[4], Path(sys.argv[5])


def rendezvous(suffix, deadline_s):
    """Wait for the other interpreter to reach the same point, or give up."""
    (sync / f"{slot}.{suffix}").write_text("", encoding="utf-8")
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline and len(list(sync.glob(f"*.{suffix}"))) < 2:
        time.sleep(0.005)


real_load = receipt_module.load_or_create_bridge_identity


def load_at_the_rendezvous(bridge_root):
    rendezvous("ready", RENDEZVOUS_S)
    return real_load(bridge_root)


receipt_module.load_or_create_bridge_identity = load_at_the_rendezvous

rendezvous("booted", 60.0)
admission = receipt_module.admit_trigger(
    root=root,
    audit_dir=audit_dir,
    hmac_key=load_or_create_audit_key(key_path),
    platform="n8n",
    request_path="/webhook",
    trigger_id="delivered-twice-across-processes",
    body=BODY,
    scope="task:create",
    timestamp=1_700_000_300,
)
print(json.dumps({
    "admitted": admission.admitted,
    "refusal_reason": admission.refusal_reason,
    "projected": admission.graph is not None,
}))
'''


def test_doubly_delivered_trigger_is_admitted_once_across_processes(bridge: _Bridge, tmp_path: Path) -> None:
    """The admission decision is exclusive against another process, not just another thread.

    The bridge is served by whatever worker happens to receive the delivery, so
    an in-process guard would leave the guarantee to how the deployment was
    started. Two interpreters race here for that reason: a lock that only holds
    within one process passes the thread test and fails this one.
    """
    _seed_chain(bridge)
    sync = tmp_path / "sync"
    sync.mkdir()
    script = tmp_path / "deliver.py"
    script.write_text(
        f"BODY = {_BODY!r}\nRENDEZVOUS_S = {_RENDEZVOUS_S!r}\n{_CHILD_DELIVERY}",
        encoding="utf-8",
    )

    children = [
        subprocess.Popen(
            [
                sys.executable,
                str(script),
                str(bridge.root),
                str(bridge.audit_dir),
                str(tmp_path / "audit.key"),
                slot,
                str(sync),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for slot in ("a", "b")
    ]
    results: list[dict[str, Any]] = []
    for child in children:
        out, err = child.communicate(timeout=_JOIN_S * 4)
        assert child.returncode == 0, f"delivery process failed: {err}"
        results.append(json.loads(out.strip().splitlines()[-1]))

    admitted = [row for row in results if row["admitted"]]
    assert len(admitted) == 1, f"two processes both admitted one trigger id: {results}"
    refused = [row for row in results if not row["admitted"]]
    assert refused[0]["refusal_reason"] == REFUSAL_REPLAYED_TRIGGER
    assert sum(bool(row["projected"]) for row in results) == 1

    resource = "delivered-twice-across-processes"
    assert len(_chain_rows(bridge, EVENT_TRIGGER_RECEIPT_ISSUED, resource)) == 1
    assert len(_chain_rows(bridge, EVENT_TRIGGER_RECEIPT_REFUSED, resource)) == 1

    ok, problems = AuditChainStore(bridge.audit_dir, key=bridge.hmac_key).verify()
    assert ok, problems


# ---------------------------------------------------------------------------
# #3151 -- one event_id mints one proof
# ---------------------------------------------------------------------------


def test_concurrent_callbacks_mint_one_status_proof(bridge: _Bridge, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two concurrent callbacks for one event id share one anchored proof.

    The cache exists so a re-sent callback carries a byte-identical envelope
    rather than a second, differently-anchored claim. Probing before the
    section and writing after it lets both callers anchor their own event and
    lets the later write replace the proof the earlier caller already holds.
    """
    _seed_chain(bridge)
    _install_rendezvous(monkeypatch)
    payload: dict[str, Any] = {"event_id": "ev-raced", "run_id": "run-9", "severity": "error"}

    def callback() -> dict[str, Any]:
        return emit_status_proof(
            root=bridge.root,
            audit_dir=bridge.audit_dir,
            hmac_key=bridge.hmac_key,
            payload=payload,
            status="failed",
            timestamp=1_700_000_500,
        ).to_dict()

    first, second = _race(callback)

    assert first == second, (
        "two concurrent callbacks for one event id returned differently-anchored proofs: "
        f"{first.get('chain_entry_hash')!r} vs {second.get('chain_entry_hash')!r}"
    )

    rows = _chain_rows(bridge, EVENT_STATUS_PROOF_EMITTED, "ev-raced")
    assert len(rows) == 1, f"one event id anchored {len(rows)} status proofs"

    on_disk = json.loads(status_proof_path(bridge.root, "ev-raced").read_text(encoding="utf-8"))
    assert on_disk == first, "the proof on disk is not the proof the callers were handed"

    # A later re-send still returns the cached proof and appends nothing.
    assert callback() == first
    assert len(_chain_rows(bridge, EVENT_STATUS_PROOF_EMITTED, "ev-raced")) == 1

    ok, problems = AuditChainStore(bridge.audit_dir, key=bridge.hmac_key).verify()
    assert ok, problems
