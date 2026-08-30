import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from bernstein.core.models import Task

from bernstein.core.agents.spawner_core import AgentSpawner


class DummySpawner:
    """Minimal stand-in for AgentSpawner to test _render_mailbox_section."""

    def __init__(self, workdir: Path):
        self._workdir = workdir


def _create_task(task_id: str = "T-1") -> Task:
    return Task(
        id=task_id,
        title="Test Task",
        description="Test description",
        role="backend",
    )


def _write_message(journal: Path, task_id: str = "T-1", seq: int = 0, *, append: bool = False) -> None:
    """Write a single valid mailbox message to the journal.

    ``append`` keeps the messages already on disk. Writing with ``"w"``
    truncates, which silently turns a "seq 0 is not re-rendered"
    assertion into a tautology because seq 0 no longer exists.
    """
    journal.parent.mkdir(parents=True, exist_ok=True)
    msg = {
        "seq": seq,
        "task_id": task_id,
        "sender": "test-sender",
        "sender_card_fingerprint": "unregistered",
        "kind": "finding",
        "body": "test body",
        "body_hash": "sha256:dummy",
        "redaction_count": 0,
        "timestamp": 1234567890.0,
        "prev_entry_hash": "genesis",
        "entry_hash": "hmac-sha256:dummy",
        "signer_public_key_pem": "",
        "signature": "",
        "schema_version": 1,
    }
    with journal.open("a" if append else "w", encoding="utf-8") as f:
        f.write(json.dumps(msg) + "\n")


def test_a_delivered_message_produces_a_consumption_chain_entry(tmp_path: Path) -> None:
    from bernstein.core.security.audit_chain import AuditChainStore

    workdir = tmp_path
    journal = workdir / ".sdd" / "runtime" / "mailbox.jsonl"
    _write_message(journal)

    spawner = DummySpawner(workdir)
    tasks = [_create_task()]

    AgentSpawner._render_mailbox_section(spawner, tasks)

    chain = AuditChainStore(workdir / ".sdd" / "audit")
    events = chain.query(event_type="task.mailbox_consumed")
    assert len(events) == 1
    assert events[0].details["seq"] == 0
    assert events[0].details["entry_hash"] == "hmac-sha256:dummy"


def test_missing_journal_produces_a_visible_record_not_silent_empty_string(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    spawner = DummySpawner(tmp_path)
    tasks = [_create_task()]

    with caplog.at_level("INFO"):
        result = AgentSpawner._render_mailbox_section(spawner, tasks)

    assert result == ""
    assert any("Mailbox journal missing" in rec.message for rec in caplog.records), (
        "Expected INFO log for missing journal"
    )


def test_an_exception_during_render_is_visible_at_default_log_level(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Create the journal file so the code tries to instantiate TaskMailbox
    journal = tmp_path / ".sdd" / "runtime" / "mailbox.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.touch()

    spawner = DummySpawner(tmp_path)
    tasks = [_create_task()]

    # Mock TaskMailbox to raise an exception to test the except block
    with patch("bernstein.core.communication.task_mailbox.TaskMailbox", side_effect=ValueError("Test exception")):
        with caplog.at_level("DEBUG"):
            result = AgentSpawner._render_mailbox_section(spawner, tasks)

    assert result == ""
    assert any("Mailbox section rendering skipped" in rec.message and rec.levelno >= 30 for rec in caplog.records), (
        "Expected WARNING log for render exception"
    )


def test_a_consumed_message_is_not_rerendered_on_a_later_resume(tmp_path: Path) -> None:
    workdir = tmp_path
    journal = workdir / ".sdd" / "runtime" / "mailbox.jsonl"
    _write_message(journal, task_id="T-1", seq=0)

    spawner = DummySpawner(workdir)
    tasks = [_create_task(task_id="T-1")]

    # First spawn: renders and records consumption
    result1 = AgentSpawner._render_mailbox_section(spawner, tasks)
    assert "[seq 0]" in result1

    # Resume: should not re-render the consumed message
    result2 = AgentSpawner._render_mailbox_section(spawner, tasks)
    assert result2 == ""


def test_a_message_posted_after_the_cursor_is_still_rendered(tmp_path: Path) -> None:
    workdir = tmp_path
    journal = workdir / ".sdd" / "runtime" / "mailbox.jsonl"
    _write_message(journal, task_id="T-1", seq=0)

    spawner = DummySpawner(workdir)
    tasks = [_create_task(task_id="T-1")]

    # First spawn: consumes message 0
    AgentSpawner._render_mailbox_section(spawner, tasks)

    # Append a new message (seq=1), keeping seq 0 on disk so the
    # "not re-rendered" assertion below has something to be false about.
    _write_message(journal, task_id="T-1", seq=1, append=True)

    # Second spawn: should render message 1 but NOT message 0
    result = AgentSpawner._render_mailbox_section(spawner, tasks)
    assert "[seq 0]" not in result
    assert "[seq 1]" in result


def test_cursor_is_derived_from_the_chain_not_local_state(tmp_path: Path) -> None:
    workdir = tmp_path
    journal = workdir / ".sdd" / "runtime" / "mailbox.jsonl"
    _write_message(journal, task_id="T-1", seq=0)

    # Two independent spawner instances pointing at the same workdir
    spawner1 = DummySpawner(workdir)
    spawner2 = DummySpawner(workdir)
    tasks = [_create_task(task_id="T-1")]

    # First projection records consumption
    AgentSpawner._render_mailbox_section(spawner1, tasks)

    # Second projection should derive the same cursor from the chain and not re-render
    result2 = AgentSpawner._render_mailbox_section(spawner2, tasks)
    assert result2 == ""


def test_cursor_survives_audit_archival(tmp_path: Path) -> None:
    """A consumed message stays consumed after its audit segment is archived.

    ``bernstein audit archive`` gzips segments past the retention horizon.
    If the cursor query cannot see archived segments it falls back to -1 and
    re-renders every message the task ever received -- reintroducing the
    duplicate delivery this fix removes, triggered by routine maintenance
    rather than by any code change.
    """
    from bernstein.core.security.audit import AuditLog, RetentionPolicy

    workdir = tmp_path
    journal = workdir / ".sdd" / "runtime" / "mailbox.jsonl"
    _write_message(journal, task_id="T-1", seq=0)

    spawner = DummySpawner(workdir)
    tasks = [_create_task(task_id="T-1")]

    assert "[seq 0]" in AgentSpawner._render_mailbox_section(spawner, tasks)

    # Age the consumption record out of the retention window. Renaming the
    # segment is the deterministic way to do this: `archive` derives a
    # segment's age from its `YYYY-MM-DD.jsonl` filename, so no clock
    # manipulation is needed.
    audit_dir = workdir / ".sdd" / "audit"
    segments = sorted(audit_dir.glob("*.jsonl"))
    assert segments, "expected the consumption record to have been written"
    # Distinct target dates: a run that straddles UTC midnight produces two
    # segments, and renaming both onto one name would discard a record.
    for index, segment in enumerate(segments):
        segment.rename(audit_dir / f"2020-01-{index + 1:02d}.jsonl")

    result = AuditLog(audit_dir=audit_dir).archive(RetentionPolicy(retention_days=1))
    assert result.archived, "expected the aged segment to be archived"
    assert not sorted(audit_dir.glob("*.jsonl")), "archived segment should no longer be uncompressed"

    # The cursor must still resolve to 0 by reading the archived segment.
    assert AgentSpawner._render_mailbox_section(spawner, tasks) == ""


def test_cursor_is_scoped_per_task_not_globally(tmp_path: Path) -> None:
    """A lagging task still receives every message addressed to it.

    ``seq`` is a global append index across the whole mailbox, so a cursor
    derived from a global ``max`` would skip any message that landed while a
    different task was ahead. Rendering T-1 first advances only T-1.
    """
    workdir = tmp_path
    journal = workdir / ".sdd" / "runtime" / "mailbox.jsonl"
    # Interleaved global sequence: T-1 owns 0 and 2, T-2 owns 1 and 3.
    _write_message(journal, task_id="T-1", seq=0)
    _write_message(journal, task_id="T-2", seq=1, append=True)
    _write_message(journal, task_id="T-1", seq=2, append=True)
    _write_message(journal, task_id="T-2", seq=3, append=True)

    spawner = DummySpawner(workdir)

    # T-1 renders first and advances its own cursor to 2.
    first = AgentSpawner._render_mailbox_section(spawner, [_create_task(task_id="T-1")])
    assert "[seq 0]" in first
    assert "[seq 2]" in first

    # T-2 has never rendered, so it must still get both of its messages even
    # though T-1 has already consumed a higher global seq.
    second = AgentSpawner._render_mailbox_section(spawner, [_create_task(task_id="T-2")])
    assert "[seq 1]" in second
    assert "[seq 3]" in second


def test_prompt_digest_computes_digest_of_assembled_section(tmp_path: Path) -> None:
    """The prompt digest is sha256 of the assembled mailbox section."""
    workdir = tmp_path
    journal = workdir / ".sdd" / "runtime" / "mailbox.jsonl"
    _write_message(journal, task_id="T-1", seq=0)

    spawner = DummySpawner(workdir)
    tasks = [_create_task(task_id="T-1")]

    from bernstein.core.security.audit_chain import AuditChainStore

    result1 = AgentSpawner._render_mailbox_section(spawner, tasks)
    chain = AuditChainStore(workdir / ".sdd" / "audit")
    events = chain.query(event_type="task.mailbox_consumed")
    assert len(events) == 1
    prompt_digest = events[0].details["prompt_digest"]
    expected = hashlib.sha256(result1.encode("utf-8")).hexdigest()
    assert prompt_digest == expected


def test_prompt_digest_included_in_each_consumption_record(tmp_path: Path) -> None:
    """Each rendered message includes the same prompt_digest in its consumption."""
    workdir = tmp_path
    journal = workdir / ".sdd" / "runtime" / "mailbox.jsonl"
    _write_message(journal, task_id="T-1", seq=0)
    _write_message(journal, task_id="T-1", seq=1, append=True)

    spawner = DummySpawner(workdir)
    tasks = [_create_task(task_id="T-1")]

    from bernstein.core.security.audit_chain import AuditChainStore

    AgentSpawner._render_mailbox_section(spawner, tasks)

    chain = AuditChainStore(workdir / ".sdd" / "audit")
    events = chain.query(event_type="task.mailbox_consumed")
    assert len(events) == 2
    # All prompts should have the same digest (same assembled section)
    digests = [e.details["prompt_digest"] for e in events]
    assert len(set(digests)) == 1


def test_prompt_digest_changes_when_assembled_content_changes(tmp_path: Path) -> None:
    """Different assembled sections produce different digests."""
    workdir = tmp_path
    journal = workdir / ".sdd" / "runtime" / "mailbox.jsonl"
    _write_message(journal, task_id="T-1", seq=0)

    spawner = DummySpawner(workdir)
    tasks = [_create_task(task_id="T-1")]

    from bernstein.core.security.audit_chain import AuditChainStore

    AgentSpawner._render_mailbox_section(spawner, tasks)

    # Add a new message (seq=1)
    _write_message(journal, task_id="T-1", seq=1, append=True)

    result2 = AgentSpawner._render_mailbox_section(spawner, tasks)
    assert "[seq 0]" not in result2
    assert "[seq 1]" in result2

    chain = AuditChainStore(workdir / ".sdd" / "audit")
    events = chain.query(event_type="task.mailbox_consumed")
    assert len(events) == 2
    digests = [e.details["prompt_digest"] for e in events]
    assert len(set(digests)) == 2


def test_no_prompt_digest_when_no_pending_messages(tmp_path: Path) -> None:
    """Consumption records are not created when there are no pending messages."""
    workdir = tmp_path
    spawner = DummySpawner(workdir)
    tasks = [_create_task()]

    from bernstein.core.security.audit_chain import AuditChainStore

    result = AgentSpawner._render_mailbox_section(spawner, tasks)
    assert result == ""

    chain = AuditChainStore(workdir / ".sdd" / "audit")
    events = chain.query(event_type="task.mailbox_consumed")
    assert len(events) == 0
