"""Unit tests for conversation export (CLAUDE-019)."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.conversation_export import (
    ConversationExport,
    ConversationMessage,
    export_conversation,
    parse_ndjson_log,
    save_export,
    serialize_export,
)

# ---------------------------------------------------------------------------
# ConversationMessage creation
# ---------------------------------------------------------------------------


def test_conversation_message_defaults() -> None:
    msg = ConversationMessage(role="user", content="hello")
    assert msg.role == "user"
    assert msg.content == "hello"
    assert msg.timestamp is None
    assert msg.tool_name is None
    assert msg.turn_number == 0


def test_conversation_message_all_fields() -> None:
    msg = ConversationMessage(
        role="tool_result",
        content="ok",
        timestamp=1000.0,
        tool_name="Bash",
        turn_number=3,
    )
    assert msg.role == "tool_result"
    assert msg.timestamp == 1000.0
    assert msg.tool_name == "Bash"
    assert msg.turn_number == 3


def test_conversation_message_is_frozen() -> None:
    msg = ConversationMessage(role="assistant", content="hi")
    try:
        msg.role = "user"  # type: ignore[misc]
        raised = False
    except AttributeError:
        raised = True
    assert raised, "ConversationMessage should be frozen"


# ---------------------------------------------------------------------------
# parse_ndjson_log
# ---------------------------------------------------------------------------


def _write_ndjson(tmp_path: Path, lines: list[dict[str, object]]) -> Path:
    log = tmp_path / "session.ndjson"
    log.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n",
        encoding="utf-8",
    )
    return log


def test_parse_system_event(tmp_path: Path) -> None:
    log = _write_ndjson(tmp_path, [{"type": "system", "message": "init done", "timestamp": 1.0}])
    msgs = parse_ndjson_log(log)
    assert len(msgs) == 1
    assert msgs[0].role == "system"
    assert msgs[0].content == "init done"
    assert msgs[0].timestamp == 1.0


def test_parse_human_event(tmp_path: Path) -> None:
    log = _write_ndjson(tmp_path, [{"type": "human", "message": "fix the bug"}])
    msgs = parse_ndjson_log(log)
    assert len(msgs) == 1
    assert msgs[0].role == "user"
    assert msgs[0].content == "fix the bug"


def test_parse_assistant_text(tmp_path: Path) -> None:
    log = _write_ndjson(
        tmp_path,
        [
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "I will fix the bug."}],
                },
                "timestamp": 2.5,
            }
        ],
    )
    msgs = parse_ndjson_log(log)
    assert len(msgs) == 1
    assert msgs[0].role == "assistant"
    assert msgs[0].content == "I will fix the bug."
    assert msgs[0].timestamp == 2.5


def test_parse_assistant_tool_use(tmp_path: Path) -> None:
    log = _write_ndjson(
        tmp_path,
        [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Let me check."},
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "id": "tool-1",
                            "input": {"command": "ls"},
                        },
                    ],
                },
            }
        ],
    )
    msgs = parse_ndjson_log(log)
    assert len(msgs) == 2
    assert msgs[0].role == "assistant"
    assert msgs[0].content == "Let me check."
    assert msgs[1].role == "assistant"
    assert msgs[1].tool_name == "Bash"
    assert "ls" in msgs[1].content


def test_parse_tool_result(tmp_path: Path) -> None:
    log = _write_ndjson(
        tmp_path,
        [{"type": "tool_result", "tool": "Bash", "content": "file.py", "timestamp": 3.0}],
    )
    msgs = parse_ndjson_log(log)
    assert len(msgs) == 1
    assert msgs[0].role == "tool_result"
    assert msgs[0].tool_name == "Bash"
    assert msgs[0].content == "file.py"
    assert msgs[0].timestamp == 3.0


def test_parse_mixed_events(tmp_path: Path) -> None:
    log = _write_ndjson(
        tmp_path,
        [
            {"type": "system", "message": "init"},
            {"type": "human", "message": "do something"},
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "ok"}]},
            },
            {"type": "tool_result", "tool": "Read", "content": "data"},
        ],
    )
    msgs = parse_ndjson_log(log)
    assert len(msgs) == 4
    assert [m.role for m in msgs] == ["system", "user", "assistant", "tool_result"]
    assert [m.turn_number for m in msgs] == [0, 1, 2, 3]


def test_parse_empty_file(tmp_path: Path) -> None:
    log = tmp_path / "empty.ndjson"
    log.write_text("", encoding="utf-8")
    msgs = parse_ndjson_log(log)
    assert msgs == []


def test_parse_missing_file(tmp_path: Path) -> None:
    msgs = parse_ndjson_log(tmp_path / "nonexistent.ndjson")
    assert msgs == []


def test_parse_invalid_json_lines(tmp_path: Path) -> None:
    log = tmp_path / "bad.ndjson"
    log.write_text("not json\n{invalid\n", encoding="utf-8")
    msgs = parse_ndjson_log(log)
    assert msgs == []


# ---------------------------------------------------------------------------
# export_conversation
# ---------------------------------------------------------------------------


def test_export_conversation(tmp_path: Path) -> None:
    log = _write_ndjson(
        tmp_path,
        [
            {"type": "human", "message": "hi"},
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "hello"}]},
            },
        ],
    )
    export = export_conversation(
        session_id="sess-1",
        task_id="task-42",
        role="backend",
        model="claude-sonnet-4-20250514",
        log_path=log,
        tokens=5000,
        cost=0.05,
        outcome="success",
    )
    assert export.session_id == "sess-1"
    assert export.task_id == "task-42"
    assert export.agent_role == "backend"
    assert export.model == "claude-sonnet-4-20250514"
    assert export.total_tokens == 5000
    assert export.cost_usd == 0.05
    assert export.outcome == "success"
    assert export.exported_at != ""
    assert len(export.messages) == 2
    assert export.messages[0].role == "user"
    assert export.messages[1].role == "assistant"


def test_export_conversation_missing_log(tmp_path: Path) -> None:
    export = export_conversation(
        session_id="sess-2",
        task_id="task-99",
        role="qa",
        model="claude-haiku-4-20250414",
        log_path=tmp_path / "missing.ndjson",
        tokens=0,
        cost=0.0,
        outcome="no_log",
    )
    assert export.messages == []
    assert export.session_id == "sess-2"


# ---------------------------------------------------------------------------
# serialize_export
# ---------------------------------------------------------------------------


def test_serialize_export_valid_json() -> None:
    export = ConversationExport(
        session_id="s1",
        task_id="t1",
        agent_role="qa",
        model="m",
        messages=[ConversationMessage(role="user", content="hi")],
        total_tokens=100,
        cost_usd=0.01,
        outcome="success",
        exported_at="2026-04-10T00:00:00+00:00",
    )
    text = serialize_export(export)
    parsed = json.loads(text)
    assert parsed["session_id"] == "s1"
    assert parsed["total_tokens"] == 100
    assert len(parsed["messages"]) == 1
    assert parsed["messages"][0]["role"] == "user"
    assert parsed["messages"][0]["content"] == "hi"


def test_serialize_export_roundtrip() -> None:
    export = ConversationExport(
        session_id="s2",
        task_id="t2",
        agent_role="backend",
        model="m2",
        messages=[
            ConversationMessage(role="system", content="init", timestamp=1.0, turn_number=0),
            ConversationMessage(
                role="tool_result",
                content="ok",
                tool_name="Bash",
                turn_number=1,
            ),
        ],
        total_tokens=200,
        cost_usd=0.02,
        outcome="failure",
        exported_at="2026-04-10T12:00:00+00:00",
    )
    text = serialize_export(export)
    parsed = json.loads(text)
    assert parsed["cost_usd"] == 0.02
    assert parsed["messages"][0]["timestamp"] == 1.0
    assert parsed["messages"][1]["tool_name"] == "Bash"


# ---------------------------------------------------------------------------
# save_export
# ---------------------------------------------------------------------------


def test_save_export_writes_file(tmp_path: Path) -> None:
    export = ConversationExport(
        session_id="sess-save",
        task_id="t3",
        agent_role="frontend",
        model="m3",
        messages=[ConversationMessage(role="user", content="go")],
        total_tokens=50,
        cost_usd=0.001,
        outcome="success",
        exported_at="2026-04-10T00:00:00+00:00",
    )
    out_dir = tmp_path / "exports"
    path = save_export(export, out_dir)
    assert path == out_dir / "sess-save.json"
    assert path.exists()

    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["session_id"] == "sess-save"
    assert loaded["messages"][0]["content"] == "go"


def test_save_export_creates_directory(tmp_path: Path) -> None:
    export = ConversationExport(
        session_id="sess-dir",
        task_id="t4",
        agent_role="devops",
        model="m4",
        exported_at="2026-04-10T00:00:00+00:00",
    )
    deep_dir = tmp_path / "a" / "b" / "c"
    path = save_export(export, deep_dir)
    assert path.exists()
    assert deep_dir.exists()
