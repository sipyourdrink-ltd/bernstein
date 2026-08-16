"""Unit tests for the new ``bernstein replay`` verbs (#1799).

These exercise the dispatch helpers in
:mod:`bernstein.cli.commands.replay_cmd` directly; the CLI integration
through ``advanced_cmd._replay_journal_dispatch`` is one indirection
above and is exercised by the integration tests.
"""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.cli.commands.replay_cmd import (
    replay_agent_view,
    replay_diff_journals,
    replay_export,
    replay_publish,
    replay_verify,
)
from bernstein.core.persistence.journal import Journal


def _populate(sdd_dir: Path, agent_id: str, n: int = 3) -> str:
    journal_dir = sdd_dir / "runtime" / "journal" / agent_id
    journal = Journal.open(journal_dir)
    head = ""
    for i in range(n):
        entry = journal.append(input_hash=f"a{i}", model="m1", prompt=f"p{i}")
        head = entry.step_hash
    journal.close()
    return head


def test_replay_agent_view_renders_chain(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    _populate(sdd, "agent-1", 3)
    rc = replay_agent_view("agent-1", sdd)
    assert rc == 0


def test_replay_agent_view_missing_returns_2(tmp_path: Path) -> None:
    rc = replay_agent_view("nope", tmp_path / ".sdd")
    assert rc == 2


def test_replay_export_writes_receipt(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    head = _populate(sdd, "agent-1", 2)
    out = tmp_path / "out.tar"
    rc = replay_export("agent-1", sdd, out)
    assert rc == 0
    assert out.exists()
    rc2 = replay_verify(out, expected_head=head, public_key_path=None)
    assert rc2 == 0


def test_replay_publish_requires_opt_in(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    _populate(sdd, "agent-1", 1)
    out = tmp_path / "redacted.tar"
    rc = replay_publish("agent-1", sdd, out, opt_in=False)
    assert rc == 2
    assert not out.exists()


def test_replay_publish_with_opt_in_succeeds(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    _populate(sdd, "agent-1", 1)
    out = tmp_path / "redacted.tar"
    rc = replay_publish("agent-1", sdd, out, opt_in=True)
    assert rc == 0
    assert out.exists()


def test_replay_diff_journals_returns_0_when_match(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    _populate(sdd, "agent-a", 3)
    _populate(sdd, "agent-b", 3)
    rc = replay_diff_journals("agent-a", "agent-b", sdd)
    assert rc == 0


def test_replay_diff_journals_returns_1_when_divergent(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"

    a_dir = sdd / "runtime" / "journal" / "agent-a"
    a_journal = Journal.open(a_dir)
    for i in range(3):
        a_journal.append(input_hash=f"a{i}", model="m1", prompt=f"p{i}")
    a_journal.close()

    b_dir = sdd / "runtime" / "journal" / "agent-b"
    b_journal = Journal.open(b_dir)
    b_journal.append(input_hash="a0", model="m1", prompt="p0")
    b_journal.append(input_hash="a1", model="m2", prompt="p1")  # diverges
    b_journal.append(input_hash="a2", model="m1", prompt="p2")
    b_journal.close()

    rc = replay_diff_journals("agent-a", "agent-b", sdd)
    assert rc == 1


def test_replay_verify_missing_receipt_returns_2(tmp_path: Path) -> None:
    rc = replay_verify(tmp_path / "missing.tar", expected_head=None, public_key_path=None)
    assert rc == 2


# ---------------------------------------------------------------------------
# issue #3976: --as-json must emit a truthful summary, not be silently ignored
# ---------------------------------------------------------------------------


def test_replay_export_as_json_summarises_the_receipt(tmp_path: Path, capsys) -> None:
    sdd = tmp_path / ".sdd"
    head = _populate(sdd, "agent-1", 2)
    out = tmp_path / "out.tar"

    rc = replay_export("agent-1", sdd, out, as_json=True)

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "agent_id": "agent-1",
        "output": str(out),
        "head_hash": head,
        "steps": 2,
        "signed": False,
    }


def test_replay_export_human_output_unchanged_when_as_json_absent(tmp_path: Path, capsys) -> None:
    sdd = tmp_path / ".sdd"
    _populate(sdd, "agent-1", 1)
    out = tmp_path / "out.tar"

    rc = replay_export("agent-1", sdd, out)

    assert rc == 0
    captured = capsys.readouterr().out
    assert "Exported receipt" in captured
    assert not captured.strip().startswith("{")


def test_replay_publish_as_json_summarises_the_receipt(tmp_path: Path, capsys) -> None:
    sdd = tmp_path / ".sdd"
    _populate(sdd, "agent-1", 1)
    out = tmp_path / "redacted.tar"

    rc = replay_publish("agent-1", sdd, out, opt_in=True, as_json=True)

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["agent_id"] == "agent-1"
    assert payload["output"] == str(out)
    assert payload["steps"] == 1
    assert payload["head_hash"]
    assert payload["original_head_hash"]


def test_replay_publish_human_output_unchanged_when_as_json_absent(tmp_path: Path, capsys) -> None:
    sdd = tmp_path / ".sdd"
    _populate(sdd, "agent-1", 1)
    out = tmp_path / "redacted.tar"

    rc = replay_publish("agent-1", sdd, out, opt_in=True)

    assert rc == 0
    captured = capsys.readouterr().out
    assert "Published redacted receipt" in captured
    assert not captured.strip().startswith("{")


def test_replay_verify_as_json_reports_the_verification_result(tmp_path: Path, capsys) -> None:
    sdd = tmp_path / ".sdd"
    head = _populate(sdd, "agent-1", 2)
    out = tmp_path / "out.tar"
    replay_export("agent-1", sdd, out)
    capsys.readouterr()  # discard export's own output

    rc = replay_verify(out, expected_head=None, public_key_path=None, as_json=True)

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["head_hash"] == head
    assert payload["steps"] == 2
