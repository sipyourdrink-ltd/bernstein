"""Tests for signed, journal-anchored A2A message receipts (#2304).

Every inbound/outbound A2A message is a signed lineage receipt binding
``{message_hash, peer_card_fingerprint, task_uuid, journal_entry_hash}`` and
anchored to the message-receipt spine. The A2A task lifecycle maps 1:1 to
journal terminal states with reason codes, and ``verify_thread`` proves the
visible cross-agent thread equals the executed actions offline.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from bernstein.core.interop.a2a_lineage import (
    A2A_MESSAGE_RECEIPT_RUN_ID,
    A2A_TASK_STATES,
    JOURNAL_TERMINAL_STATES,
    A2AMessageReceipt,
    A2AThreadVerifyResult,
    compute_message_hash,
    map_task_state,
    read_message_receipt,
    record_a2a_message,
    verify_thread,
)

if TYPE_CHECKING:
    from pathlib import Path

_HMAC_KEY = b"0" * 32
_PEER_FP = "sha256:" + "ab" * 32


def _dirs(tmp_path: Path) -> dict[str, Path]:
    return {
        "workdir": tmp_path / "work",
        "lineage_root": tmp_path / "work" / ".sdd" / "lineage",
        "identity_dir": tmp_path / "identity",
    }


def _record(
    tmp_path: Path,
    *,
    task_uuid: str = "task-1",
    direction: str = "inbound",
    state: str = "submitted",
    body: bytes = b'{"parts":[{"text":"hi"}]}',
    reason: str = "",
    seq: int = 0,
) -> A2AMessageReceipt:
    d = _dirs(tmp_path)
    return record_a2a_message(
        workdir=d["workdir"],
        lineage_root=d["lineage_root"],
        hmac_key=_HMAC_KEY,
        identity_dir=d["identity_dir"],
        task_uuid=task_uuid,
        direction=direction,
        state=state,
        peer_card_fingerprint=_PEER_FP,
        body=body,
        reason=reason,
        seq=seq,
        timestamp=1000 + seq,
    )


# ---------------------------------------------------------------------------
# AC1 -- every message produces a signed receipt anchored to a journal entry
# ---------------------------------------------------------------------------


def test_message_produces_signed_receipt_anchored_to_journal(tmp_path: Path) -> None:
    receipt = _record(tmp_path)
    assert receipt.message_hash.startswith("sha256:")
    assert receipt.peer_card_fingerprint == _PEER_FP
    assert receipt.task_uuid == "task-1"
    # anchored: the spine entry hash is present and non-empty.
    assert receipt.journal_entry_hash.startswith("sha256:")
    # signed with the install Ed25519 identity.
    assert receipt.signature
    assert receipt.signer_public_key_pem
    # journal root references the message hash for the task.
    assert receipt.message_hash in receipt.journal_events


def test_receipt_binding_is_the_four_field_primary_artifact(tmp_path: Path) -> None:
    receipt = _record(tmp_path)
    binding = json.loads(receipt.to_canonical_bytes())
    # Three of the four primary-artifact fields live inside the signed binding;
    # the fourth (journal_entry_hash) is the anchor itself, which binds the
    # other three to the spine by content.
    assert binding["message_hash"] == receipt.message_hash
    assert binding["peer_card_fingerprint"] == _PEER_FP
    assert binding["task_uuid"] == "task-1"


def test_receipt_persisted_and_reloadable(tmp_path: Path) -> None:
    receipt = _record(tmp_path, seq=0)
    d = _dirs(tmp_path)
    loaded = read_message_receipt(d["workdir"], task_uuid="task-1", seq=0)
    assert loaded is not None
    assert loaded.message_hash == receipt.message_hash
    assert loaded.journal_entry_hash == receipt.journal_entry_hash


def test_message_hash_binds_direction_task_and_body(tmp_path: Path) -> None:
    a = compute_message_hash(task_uuid="t", direction="inbound", state="submitted", seq=0, body=b"x")
    b = compute_message_hash(task_uuid="t", direction="outbound", state="submitted", seq=0, body=b"x")
    c = compute_message_hash(task_uuid="t", direction="inbound", state="submitted", seq=0, body=b"y")
    assert a != b
    assert a != c


# ---------------------------------------------------------------------------
# AC5 -- A2A task states map 1:1 to journal terminal states with reason codes
# ---------------------------------------------------------------------------


def test_all_a2a_states_map_to_a_journal_state() -> None:
    assert set(A2A_TASK_STATES) == {
        "submitted",
        "working",
        "input-required",
        "completed",
        "failed",
        "canceled",
    }
    for state in A2A_TASK_STATES:
        mapped = map_task_state(state)
        assert mapped.journal_state
        assert mapped.reason_code


def test_terminal_states_map_to_terminal_journal_states() -> None:
    for state in ("completed", "failed", "canceled"):
        assert map_task_state(state).journal_state in JOURNAL_TERMINAL_STATES
        assert map_task_state(state).terminal is True
    for state in ("submitted", "working", "input-required"):
        assert map_task_state(state).terminal is False


def test_unknown_state_rejected() -> None:
    with pytest.raises(ValueError, match="unknown A2A task state"):
        map_task_state("bogus")


def test_recorded_terminal_state_is_written_to_journal(tmp_path: Path) -> None:
    receipt = _record(tmp_path, state="failed", reason="peer_5xx", seq=0)
    assert receipt.journal_state == map_task_state("failed").journal_state
    assert receipt.reason_code == "peer_5xx" or receipt.reason_code == map_task_state("failed").reason_code
    # the task_uuid is the trace root of the journal.
    assert receipt.run_id.endswith("task-1") or "task-1" in receipt.run_id


# ---------------------------------------------------------------------------
# AC2 -- verify --from-thread confirms the thread equals executed actions
# ---------------------------------------------------------------------------


def test_verify_thread_ok_for_untampered_thread(tmp_path: Path) -> None:
    _record(tmp_path, direction="inbound", state="submitted", seq=0)
    _record(tmp_path, direction="outbound", state="working", seq=1)
    _record(tmp_path, direction="outbound", state="completed", reason="ok", seq=2)
    d = _dirs(tmp_path)
    result = verify_thread(
        workdir=d["workdir"],
        lineage_root=d["lineage_root"],
        hmac_key=_HMAC_KEY,
        task_uuid="task-1",
    )
    assert isinstance(result, A2AThreadVerifyResult)
    assert result.ok, result.reason
    assert result.message_count == 3


def test_verify_thread_detects_tampered_receipt(tmp_path: Path) -> None:
    _record(tmp_path, direction="inbound", state="submitted", seq=0)
    d = _dirs(tmp_path)
    path = d["workdir"] / ".sdd" / "a2a-messages" / "task-1" / "0000.json"
    row = json.loads(path.read_text())
    row["peer_card_fingerprint"] = "sha256:" + "ff" * 32
    path.write_text(json.dumps(row, separators=(",", ":"), sort_keys=True))
    result = verify_thread(
        workdir=d["workdir"],
        lineage_root=d["lineage_root"],
        hmac_key=_HMAC_KEY,
        task_uuid="task-1",
    )
    assert not result.ok


def test_verify_thread_detects_missing_anchor(tmp_path: Path) -> None:
    _record(tmp_path, direction="inbound", state="submitted", seq=0)
    d = _dirs(tmp_path)
    # wipe the spine so the receipt is no longer anchored.
    spine_dir = d["lineage_root"] / A2A_MESSAGE_RECEIPT_RUN_ID
    for f in spine_dir.glob("*"):
        f.unlink()
    result = verify_thread(
        workdir=d["workdir"],
        lineage_root=d["lineage_root"],
        hmac_key=_HMAC_KEY,
        task_uuid="task-1",
    )
    assert not result.ok


def test_verify_thread_no_messages_is_not_ok(tmp_path: Path) -> None:
    d = _dirs(tmp_path)
    d["workdir"].mkdir(parents=True, exist_ok=True)
    result = verify_thread(
        workdir=d["workdir"],
        lineage_root=d["lineage_root"],
        hmac_key=_HMAC_KEY,
        task_uuid="nonexistent",
    )
    assert not result.ok
    assert "no" in result.reason.lower()


# ---------------------------------------------------------------------------
# Determinism -- byte-identical receipts for identical fixtures
# ---------------------------------------------------------------------------


def test_message_hash_is_deterministic() -> None:
    a = compute_message_hash(task_uuid="t", direction="inbound", state="submitted", seq=3, body=b"body")
    b = compute_message_hash(task_uuid="t", direction="inbound", state="submitted", seq=3, body=b"body")
    assert a == b
