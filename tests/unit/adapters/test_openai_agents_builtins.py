"""Security tests for the opt-in workdir-sandboxed builtin tools.

These cover the three hard conditions the builtins must satisfy:

1. Path confinement: absolute paths and ``..`` escapes are rejected; an
   in-workdir path is accepted.
2. ``run_command`` uses an argv list with ``shell=False`` - a metacharacter
   in an argument is passed literally, never interpreted by a shell.
3. Every builtin call emits an audit event to the run event stream.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from bernstein.adapters.openai_agents_builtins import (
    BUILTIN_TOOL_NAMES,
    WorkdirEscapeError,
    list_dir_in_workdir,
    read_file_in_workdir,
    resolve_in_workdir,
    run_command_in_workdir,
    write_file_in_workdir,
)

if TYPE_CHECKING:
    from pathlib import Path


def _sink() -> tuple[list[dict[str, Any]], Any]:
    events: list[dict[str, Any]] = []

    def emit(event: dict[str, Any]) -> None:
        events.append(event)

    return events, emit


class TestPathConfinement:
    def test_in_workdir_path_accepted(self, tmp_path: Path) -> None:
        resolved = resolve_in_workdir(tmp_path, "sub/dir/file.txt")
        assert str(resolved).startswith(str(tmp_path.resolve()))

    def test_workdir_root_itself_accepted(self, tmp_path: Path) -> None:
        assert resolve_in_workdir(tmp_path, ".") == tmp_path.resolve()

    def test_absolute_path_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(WorkdirEscapeError, match="absolute"):
            resolve_in_workdir(tmp_path, "/etc/passwd")

    def test_parent_escape_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(WorkdirEscapeError, match="escapes"):
            resolve_in_workdir(tmp_path, "../../etc/passwd")

    def test_sneaky_midpath_escape_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(WorkdirEscapeError, match="escapes"):
            resolve_in_workdir(tmp_path, "sub/../../outside")

    def test_read_file_absolute_returns_error_and_does_not_read(self, tmp_path: Path) -> None:
        events, emit = _sink()
        out = read_file_in_workdir(tmp_path, "/etc/passwd", emit=emit)
        assert out.startswith("error:")
        assert any(e.get("status") == "error" for e in events)

    def test_write_file_parent_escape_rejected(self, tmp_path: Path) -> None:
        _events, emit = _sink()
        out = write_file_in_workdir(tmp_path, "../evil.txt", "x", emit=emit)
        assert out.startswith("error:")
        assert not (tmp_path.parent / "evil.txt").exists()

    def test_write_then_read_roundtrip_in_workdir(self, tmp_path: Path) -> None:
        _events, emit = _sink()
        write_file_in_workdir(tmp_path, "a/b.txt", "hello", emit=emit)
        assert (tmp_path / "a" / "b.txt").read_text() == "hello"
        assert read_file_in_workdir(tmp_path, "a/b.txt", emit=emit) == "hello"


class TestRunCommandNoShell:
    def test_metacharacter_arg_passed_literally(self, tmp_path: Path) -> None:
        # If a shell interpreted this, ``;``/``|``/``$(...)`` would split or
        # substitute. With shell=False + argv list it is one literal arg.
        _events, emit = _sink()
        payload = "a;b|c$(whoami)&&d"
        out = run_command_in_workdir(tmp_path, ["printf", "%s", payload], emit=emit)
        stdout = out.split("stdout:\n", 1)[1].split("\nstderr:", 1)[0]
        assert stdout == payload

    def test_no_shell_expansion_of_glob(self, tmp_path: Path) -> None:
        (tmp_path / "one.txt").write_text("")
        (tmp_path / "two.txt").write_text("")
        _events, emit = _sink()
        # A shell would expand ``*.txt``; argv passes it literally.
        out = run_command_in_workdir(tmp_path, ["printf", "%s", "*.txt"], emit=emit)
        stdout = out.split("stdout:\n", 1)[1].split("\nstderr:", 1)[0]
        assert stdout == "*.txt"

    def test_runs_in_workdir(self, tmp_path: Path) -> None:
        _events, emit = _sink()
        out = run_command_in_workdir(tmp_path, ["pwd"], emit=emit)
        stdout = out.split("stdout:\n", 1)[1].split("\nstderr:", 1)[0].strip()
        assert stdout == str(tmp_path.resolve())

    def test_empty_argv_rejected(self, tmp_path: Path) -> None:
        _events, emit = _sink()
        out = run_command_in_workdir(tmp_path, [], emit=emit)
        assert out.startswith("error:")


class TestAuditEvents:
    def test_read_emits_call_and_result(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_text("data")
        events, emit = _sink()
        read_file_in_workdir(tmp_path, "f.txt", emit=emit)
        types = [e["type"] for e in events]
        assert types == ["tool_call", "tool_result"]
        assert events[0]["name"] == "read_file"
        assert events[0]["tool_source"] == "builtin"

    def test_write_emits_event_with_args(self, tmp_path: Path) -> None:
        events, emit = _sink()
        write_file_in_workdir(tmp_path, "f.txt", "data", emit=emit)
        call = next(e for e in events if e["type"] == "tool_call")
        assert call["name"] == "write_file"
        assert call["args"]["path"] == "f.txt"

    def test_list_dir_emits_event(self, tmp_path: Path) -> None:
        events, emit = _sink()
        list_dir_in_workdir(tmp_path, ".", emit=emit)
        assert [e["type"] for e in events] == ["tool_call", "tool_result"]
        assert events[0]["name"] == "list_dir"

    def test_run_command_emits_event_per_call(self, tmp_path: Path) -> None:
        events, emit = _sink()
        run_command_in_workdir(tmp_path, ["true"], emit=emit)
        call = next(e for e in events if e["type"] == "tool_call")
        result = next(e for e in events if e["type"] == "tool_result")
        assert call["name"] == "run_command"
        assert call["args"]["argv"] == ["true"]
        assert result["status"] == "ok"
        assert "exit_code" in result

    def test_every_builtin_records_the_tool_name(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_text("x")
        events, emit = _sink()
        read_file_in_workdir(tmp_path, "f.txt", emit=emit)
        write_file_in_workdir(tmp_path, "g.txt", "y", emit=emit)
        list_dir_in_workdir(tmp_path, ".", emit=emit)
        run_command_in_workdir(tmp_path, ["true"], emit=emit)
        recorded = {e["name"] for e in events if e["type"] == "tool_call"}
        assert recorded == set(BUILTIN_TOOL_NAMES)
