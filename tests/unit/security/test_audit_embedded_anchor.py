"""The chain anchor a record embeds must be the one it was written with (#3062).

An audit record may carry ``details["prev_chain_digest"]``: a statement about
which record it chains onto. The HMAC covers the bytes of that statement, not
its truth, so a record naming a predecessor it never had signs exactly as
cleanly as one naming its real predecessor. ``verify()`` has to check the claim
against the record's own ``prev_hmac``, and has to say so in words an operator
can tell apart from tampering.

Three separate things are pinned here:

* the verifier refuses a false anchor, with its own wording;
* a false anchor is *hard* corruption, never crash-tear evidence an operator is
  invited to acknowledge away;
* the writer cannot produce one, across processes, and the re-entrant append
  section that guarantees it does not wedge on a second spelling of the same
  audit directory.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import threading
from pathlib import Path

import pytest

from bernstein.core.security.audit import AuditLog
from bernstein.core.security.audit_chain import AuditChainStore

KEY = b"a" * 32

#: Substring the verifier must use for a false anchor and must not use for a
#: MAC failure, so an operator can tell a mis-stated claim from tampering.
FALSE_ANCHOR_MARKER = "stated chain predecessor"


def _records(audit_dir: Path) -> list[dict[str, object]]:
    """Return every record on disk, in segment then line order."""
    out: list[dict[str, object]] = []
    for path in sorted(audit_dir.glob("*.jsonl")):
        for line in path.read_bytes().split(b"\n"):
            if line:
                out.append(json.loads(line))
    return out


def test_verify_refuses_a_record_that_states_a_predecessor_it_never_had(tmp_path: Path) -> None:
    """The core of #3062: a false claim must not verify.

    The record is written through the real writer, so its bytes are canonical
    and its MAC is genuine. Only the claim inside it is false.
    """
    audit_dir = tmp_path / "audit"
    log = AuditLog(audit_dir=audit_dir, key=KEY)
    log.log("chain.start", "actor", "resource", "r0", {})

    false_anchor = "de" * 32
    event = log.log("schedule.fire", "sched", "schedule", "s1", {"prev_chain_digest": false_anchor})
    assert event.prev_hmac != false_anchor, "the fixture must actually state a predecessor it never had"

    ok, errors = AuditLog(audit_dir=audit_dir, key=KEY).verify()
    assert not ok, "a record stating a predecessor it never had must not verify"
    anchor_errors = [line for line in errors if FALSE_ANCHOR_MARKER in line]
    assert len(anchor_errors) == 1, errors
    assert false_anchor[:16] in anchor_errors[0]
    assert event.prev_hmac[:16] in anchor_errors[0]


def test_false_anchor_is_worded_apart_from_a_mac_failure(tmp_path: Path) -> None:
    """An operator must be able to tell a concurrency defect from tampering."""
    audit_dir = tmp_path / "audit"
    log = AuditLog(audit_dir=audit_dir, key=KEY)
    log.log("chain.start", "actor", "resource", "r0", {})
    log.log("schedule.fire", "sched", "schedule", "s1", {"prev_chain_digest": "de" * 32})

    _, errors = AuditLog(audit_dir=audit_dir, key=KEY).verify()
    joined = "\n".join(errors)
    assert FALSE_ANCHOR_MARKER in joined
    assert "HMAC mismatch" not in joined, "an intact MAC must not be reported as tampering"
    assert "prev_hmac mismatch" not in joined, "the chain linkage itself is intact here"


def test_a_true_anchor_still_verifies(tmp_path: Path) -> None:
    """The check must not fire on the writer's own correct output."""
    chain = AuditChainStore(tmp_path / "audit", key=KEY)
    for index in range(5):
        chain.log_with_prev_digest(
            event_type="e",
            actor="a",
            resource_type="r",
            resource_id=str(index),
            details={"n": index},
        )
    ok, errors = chain.verify()
    assert ok, errors


def test_an_absent_or_empty_anchor_is_not_a_claim(tmp_path: Path) -> None:
    """Only a non-empty digest states a position; nothing else is policed.

    A writer with no chain wired records ``""``. That is an explicit "I make no
    claim", not a false one, and must not be reported.
    """
    audit_dir = tmp_path / "audit"
    log = AuditLog(audit_dir=audit_dir, key=KEY)
    log.log("chain.start", "a", "r", "r0", {})
    log.log("e", "a", "r", "r1", {})
    log.log("e", "a", "r", "r2", {"prev_chain_digest": ""})

    ok, errors = AuditLog(audit_dir=audit_dir, key=KEY).verify()
    assert ok, errors


def test_false_anchor_is_hard_corruption_not_tear_evidence(tmp_path: Path) -> None:
    """Guards the failure mode where hardening turns a FAIL into a WARN.

    Crash-tear evidence is acknowledgeable: ``bernstein audit ack-tear`` makes
    verification pass again. A false anchor is not crash damage - the bytes are
    intact and MAC-valid - so it must land in ``hard_errors``, which nothing
    can acknowledge away.
    """
    audit_dir = tmp_path / "audit"
    log = AuditLog(audit_dir=audit_dir, key=KEY)
    log.log("chain.start", "a", "r", "r0", {})
    log.log("schedule.fire", "sched", "schedule", "s1", {"prev_chain_digest": "de" * 32})

    report = AuditLog(audit_dir=audit_dir, key=KEY).verify_detailed()
    assert not report.ok
    assert report.tears == [], "a false anchor is not crash-shaped damage"
    assert any(FALSE_ANCHOR_MARKER in line for line in report.hard_errors), report.hard_errors


def test_false_anchor_survives_the_archive_boundary(tmp_path: Path) -> None:
    """The claim is policed in archived segments too, not only live ones."""
    audit_dir = tmp_path / "audit"
    log = AuditLog(audit_dir=audit_dir, key=KEY)
    log.log("chain.start", "a", "r", "r0", {})
    log.log("schedule.fire", "sched", "schedule", "s1", {"prev_chain_digest": "de" * 32})

    segment = next(iter(sorted(audit_dir.glob("*.jsonl"))))
    stale = audit_dir / "2020-01-01.jsonl"
    segment.rename(stale)
    AuditLog(audit_dir=audit_dir, key=KEY).archive()
    assert not stale.exists()

    ok, errors = AuditLog(audit_dir=audit_dir, key=KEY).verify()
    assert not ok
    assert any(FALSE_ANCHOR_MARKER in line for line in errors), errors


# ---------------------------------------------------------------------------
# writer side
# ---------------------------------------------------------------------------


def _append_batch(audit_dir: str, count: int) -> None:
    """Append *count* anchored records from a separate process."""
    chain = AuditChainStore(Path(audit_dir), key=KEY)
    for index in range(count):
        chain.log_with_prev_digest(
            event_type="e",
            actor="w",
            resource_type="r",
            resource_id=str(index),
            details={},
        )


@pytest.mark.slow
def test_concurrent_processes_embed_the_head_they_chain_onto(tmp_path: Path) -> None:
    """Six processes, one audit directory, every anchor true.

    Regression guard for the writer half of #3062: the head read and the append
    that uses it are one cross-process critical section, so no record can be
    published naming a predecessor another process overtook.
    """
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir(parents=True)
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=_append_batch, args=(str(audit_dir), 12)) for _ in range(6)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=120)
        assert proc.exitcode == 0

    records = _records(audit_dir)
    assert len(records) == 72
    for record in records:
        details = record["details"]
        assert isinstance(details, dict)
        assert details["prev_chain_digest"] == record["prev_hmac"]

    ok, errors = AuditLog(audit_dir=audit_dir, key=KEY).verify()
    assert ok, errors


def test_append_section_is_reentrant_under_a_second_spelling_of_the_dir(tmp_path: Path) -> None:
    """Re-entrancy must key on the directory, not on how it was spelled.

    ``flock`` binds to an open file description, so a nested append that fails
    to recognise it is already inside the section opens a second descriptor and
    blocks on the lock this thread already holds. The per-thread depth counter
    prevents that only if both spellings map to one key: ``Path.resolve()`` does
    not fold case, and this project builds on case-insensitive filesystems, so
    two spellings of one directory produced two counters and the nested append
    wedged forever.
    """
    lower = tmp_path / "auditdir"
    lower.mkdir()
    upper = tmp_path / "AuditDir"
    if not upper.exists():  # pragma: no cover - case-sensitive filesystem
        pytest.skip("filesystem is case-sensitive; the two spellings are different directories")

    outer = AuditLog(audit_dir=lower, key=KEY)
    inner = AuditLog(audit_dir=upper, key=KEY)
    done = threading.Event()

    def _nested() -> None:
        with outer.append_transaction():
            inner.log("e", "a", "r", "r0", {})
        done.set()

    worker = threading.Thread(target=_nested, daemon=True)
    worker.start()
    worker.join(timeout=20)
    assert done.is_set(), "nested append under a second spelling of the audit dir deadlocked"


def test_resync_head_is_reentrant_under_a_second_spelling_of_the_dir(tmp_path: Path) -> None:
    """The section-membership probe must fold the same two spellings.

    ``resync_head`` refuses outside an append section. Keyed on the spelling,
    a caller that opened the section under one spelling and reads the head
    through an instance built on the other is told it is outside a section it
    is demonstrably inside.
    """
    lower = tmp_path / "auditdir"
    lower.mkdir()
    upper = tmp_path / "AuditDir"
    if not upper.exists():  # pragma: no cover - case-sensitive filesystem
        pytest.skip("filesystem is case-sensitive; the two spellings are different directories")

    outer = AuditLog(audit_dir=lower, key=KEY)
    inner = AuditLog(audit_dir=upper, key=KEY)
    outer.log("e", "a", "r", "r0", {})

    with outer.append_transaction():
        assert inner.resync_head() == outer.resync_head()
