"""The head a bridge receipt signs is the head its own chain record sits on.

Both bridge receipt paths embed the chain head into the payload they
Ed25519-sign, and only then append their own record. The head is opaque bytes
inside that signature, so nothing downstream can notice when the two disagree:
a verifier recomputes the binding, matches it, and accepts a receipt that names
a chain position its record does not occupy.

The window between the head read and the append is real work -- an Ed25519
signature over the canonical payload -- so another writer appending in that
window is not a theoretical interleaving. These tests put a genuine concurrent
append there (a second thread, as a second process would) and pin the invariant
that closes the gap: the head the receipt carries equals the ``prev_hmac`` of
the record the receipt is anchored to.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.security.audit import load_or_create_audit_key
from bernstein.core.trigger_sources.receipt import admit_trigger, emit_status_proof

_BODY = json.dumps({"title": "Rotate the deploy key", "description": "quarterly"}).encode()
_PAYLOAD: dict[str, Any] = {"event_id": "ev-77", "run_id": "run-77", "severity": "error"}


@dataclass(frozen=True)
class _Bridge:
    """The bridge locations one test operates on."""

    root: Path
    audit_dir: Path
    hmac_key: bytes


# The other writer signals that it is about to append, which is waited on
# deterministically; only the append itself is then given a bounded grace
# period. Unserialised that append takes about a millisecond, so the grace still
# leaves two orders of magnitude of slack; serialised it blocks until the section
# ends, and the grace is then dead wait the suite pays on every run -- which is
# why it is small rather than generous.
_INTERLOPER_START_S = 10.0
_INTERLOPER_GRACE_S = 0.2


@pytest.fixture()
def bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Bridge:
    """Return an isolated bridge root, audit dir, and HMAC key."""
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    return _Bridge(
        root=tmp_path / "automation-bridge",
        audit_dir=tmp_path / "audit",
        hmac_key=load_or_create_audit_key(tmp_path / "audit.key"),
    )


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


def _record_by_hmac(audit_dir: Path, entry_hmac: str) -> dict[str, Any]:
    """Return the chain record whose ``hmac`` is *entry_hmac*.

    Reads with the writer's framing (``b"\\n"``-delimited canonical JSON) so a
    record fused onto an unterminated neighbour is reported as missing rather
    than silently half-parsed.
    """
    for log_path in sorted(audit_dir.glob("*.jsonl")):
        for raw_line in log_path.read_bytes().split(b"\n"):
            if not raw_line:
                continue
            try:
                parsed = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(parsed, dict) and parsed.get("hmac") == entry_hmac:
                return parsed
    raise AssertionError(f"no chain record carries hmac {entry_hmac!r}")


class _ConcurrentAppender:
    """Append to the chain from another thread inside the signing window.

    Patched over ``sign_payload``, which is the real work sitting between the
    head read and the append. The append runs on its own thread because that is
    what a second writer is: a thread of execution the receipt path does not
    control, which an in-process re-entrant lock must not wave through.
    """

    def __init__(self, audit_dir: Path, hmac_key: bytes) -> None:
        self._audit_dir = audit_dir
        self._hmac_key = hmac_key
        self._fired = False
        self._thread: threading.Thread | None = None
        self.started = threading.Event()
        self.landed = threading.Event()

    def _append(self) -> None:
        from bernstein.core.security.audit_chain import AuditChainStore

        store = AuditChainStore(self._audit_dir, key=self._hmac_key)
        self.started.set()
        store.log(
            event_type="test.interloper",
            actor="other-writer",
            resource_type="test",
            resource_id="interloper",
            details={},
        )
        self.landed.set()

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from bernstein.core.skills.catalog import signature as signature_module

        real_sign = signature_module.sign_payload

        def sign_with_a_concurrent_append(payload: bytes, private_pem: str) -> str:
            if not self._fired:
                self._fired = True
                self._thread = threading.Thread(target=self._append, daemon=True)
                self._thread.start()
                # Wait for the other writer to actually reach its append, so the
                # race is entered rather than merely scheduled...
                assert self.started.wait(_INTERLOPER_START_S), "the other writer never started"
                # ...then give that append every chance to land first.
                # Unserialised it does; serialised it blocks and we fall through.
                self.landed.wait(_INTERLOPER_GRACE_S)
            return real_sign(payload, private_pem)

        monkeypatch.setattr(signature_module, "sign_payload", sign_with_a_concurrent_append)

    def join(self) -> None:
        """Let the other writer finish so it cannot outlive the test."""
        if self._thread is not None:
            self._thread.join(timeout=_INTERLOPER_START_S)
            assert not self._thread.is_alive(), "the other writer never completed its append"


def test_trigger_receipt_head_is_its_own_records_predecessor(bridge: _Bridge, monkeypatch: pytest.MonkeyPatch) -> None:
    """A concurrent append cannot move the head out from under a trigger receipt."""
    _seed_chain(bridge)
    appender = _ConcurrentAppender(bridge.audit_dir, bridge.hmac_key)
    appender.install(monkeypatch)

    admission = admit_trigger(
        root=bridge.root,
        audit_dir=bridge.audit_dir,
        hmac_key=bridge.hmac_key,
        platform="n8n",
        request_path="/webhook",
        trigger_id="trigger-under-test",
        body=_BODY,
        scope="task:create",
        timestamp=1_700_000_100,
    )
    appender.join()

    receipt = admission.receipt
    assert receipt is not None
    record = _record_by_hmac(bridge.audit_dir, receipt.chain_entry_hash)
    assert receipt.admission_chain_head == record["prev_hmac"], (
        "the receipt signed a chain head its own record does not sit on: "
        f"signed {receipt.admission_chain_head!r}, record follows {record['prev_hmac']!r}"
    )


def test_status_proof_head_is_its_own_records_predecessor(bridge: _Bridge, monkeypatch: pytest.MonkeyPatch) -> None:
    """A concurrent append cannot move the head out from under a status proof."""
    _seed_chain(bridge)
    appender = _ConcurrentAppender(bridge.audit_dir, bridge.hmac_key)
    appender.install(monkeypatch)

    proof = emit_status_proof(
        root=bridge.root,
        audit_dir=bridge.audit_dir,
        hmac_key=bridge.hmac_key,
        payload=_PAYLOAD,
        status="failed",
        timestamp=1_700_000_500,
    )
    appender.join()

    record = _record_by_hmac(bridge.audit_dir, proof.chain_entry_hash)
    assert proof.chain_head == record["prev_hmac"], (
        "the proof signed a chain head its own record does not sit on: "
        f"signed {proof.chain_head!r}, record follows {record['prev_hmac']!r}"
    )


def test_resync_head_outside_a_transaction_is_refused(bridge: _Bridge) -> None:
    """Reading the head for signing is only meaningful while the chain is held.

    ``resync_head`` re-points the append fast path, so a call made outside the
    section lets a writer land between the read and that bookkeeping, leaving the
    recorded size describing bytes the head does not cover. The next append then
    skips a re-sync it needed and chains onto a stale head. The precondition is
    enforced rather than documented so that misuse is a loud error instead of a
    silently forked chain.
    """
    from bernstein.core.security.audit_chain import AuditChainStore

    chain = AuditChainStore(bridge.audit_dir, key=bridge.hmac_key)

    with pytest.raises(RuntimeError, match="append_transaction"):
        chain.resync_head()

    # Observing the head never needs the section.
    assert isinstance(chain.prev_chain_digest, str)

    # Inside the section it is the supported read.
    with chain.chain_transaction():
        assert chain.resync_head() == chain.prev_chain_digest
