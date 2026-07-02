"""Security tests for the opt-in builtin tools.

These cover the hard conditions the builtins must satisfy:

1. Path confinement (file tools): absolute paths and ``..`` escapes are
   rejected; an in-workdir path is accepted.
2. ``run_command`` uses an argv list with ``shell=False`` - a metacharacter
   in an argument is passed literally, never interpreted by a shell.
3. ``run_command`` rejects absolute / path-separator / shell-interpreter
   ``argv[0]`` (the proven filesystem-escape vectors) and only runs bare
   commands resolved on ``PATH``.
4. ``run_command`` is only exposed (registered) under an OS sandbox provider
   or the explicit opt-in.
5. Every builtin call emits an audit event to the run event stream.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from bernstein.adapters.openai_agents_builtins import (
    BUILTIN_TOOL_NAMES,
    CommandNotAllowedError,
    WorkdirEscapeError,
    list_dir_in_workdir,
    read_file_in_workdir,
    resolve_command,
    resolve_in_workdir,
    run_command_available,
    run_command_in_workdir,
    selected_builtin_names,
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


class TestRunCommandArgvRestriction:
    """The two proven escapes must now fail closed at the builtin layer."""

    def test_absolute_argv0_rejected(self, tmp_path: Path) -> None:
        # Proven escape #1: ['/bin/cat', '/outside'] read an outside file.
        outside = tmp_path.parent / "secret.txt"
        outside.write_text("TOPSECRET")
        events, emit = _sink()
        out = run_command_in_workdir(tmp_path, ["/bin/cat", str(outside)], emit=emit)
        assert out.startswith("error:")
        assert "TOPSECRET" not in out
        result = next(e for e in events if e["type"] == "tool_result")
        assert result["status"] == "error"

    def test_shell_interpreter_argv0_rejected(self, tmp_path: Path) -> None:
        # Proven escape #2: ['/bin/sh', '-c', ...] wrote outside the workdir.
        outside = tmp_path.parent / "via_sh.txt"
        _events, emit = _sink()
        out = run_command_in_workdir(
            tmp_path,
            ["/bin/sh", "-c", f"echo INJECTED > {outside}"],
            emit=emit,
        )
        assert out.startswith("error:")
        assert not outside.exists()

    def test_bare_shell_name_rejected(self, tmp_path: Path) -> None:
        # Even a bare interpreter name (resolvable on PATH) is blocked.
        _events, emit = _sink()
        out = run_command_in_workdir(tmp_path, ["sh", "-c", "echo hi"], emit=emit)
        assert out.startswith("error:")

    def test_relative_path_separator_argv0_rejected(self, tmp_path: Path) -> None:
        _events, emit = _sink()
        out = run_command_in_workdir(tmp_path, ["../x"], emit=emit)
        assert out.startswith("error:")

    def test_bare_allowed_command_runs_and_is_audited(self, tmp_path: Path) -> None:
        events, emit = _sink()
        out = run_command_in_workdir(tmp_path, ["echo", "hi"], emit=emit)
        stdout = out.split("stdout:\n", 1)[1].split("\nstderr:", 1)[0].strip()
        assert stdout == "hi"
        result = next(e for e in events if e["type"] == "tool_result")
        assert result["status"] == "ok"
        assert result["exit_code"] == 0

    def test_resolve_command_returns_absolute_path(self) -> None:
        resolved = resolve_command("echo")
        assert resolved.startswith("/")
        assert resolved.endswith("echo") or "echo" in resolved

    def test_resolve_command_rejects_unresolvable(self) -> None:
        with pytest.raises(CommandNotAllowedError, match="PATH"):
            resolve_command("definitely-not-a-real-command-xyz")


class TestRunCommandGating:
    """``run_command`` is only registered under a sandbox provider or opt-in."""

    def test_not_available_under_bare_local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BERNSTEIN_BUILTIN_ALLOW_RUN_COMMAND", raising=False)
        assert run_command_available("unix_local") is False
        assert run_command_available(None) is False

    def test_available_under_os_sandbox_providers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BERNSTEIN_BUILTIN_ALLOW_RUN_COMMAND", raising=False)
        assert run_command_available("docker") is True
        assert run_command_available("e2b") is True
        assert run_command_available("modal") is True

    def test_available_under_explicit_opt_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_BUILTIN_ALLOW_RUN_COMMAND", "1")
        assert run_command_available("unix_local") is True

    def test_opt_in_requires_exact_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_BUILTIN_ALLOW_RUN_COMMAND", "true")
        assert run_command_available("unix_local") is False

    def test_selected_names_exclude_run_command_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BERNSTEIN_BUILTIN_ALLOW_RUN_COMMAND", raising=False)
        names = selected_builtin_names("unix_local")
        assert "run_command" not in names
        assert set(names) == {"read_file", "write_file", "list_dir"}

    def test_selected_names_include_run_command_under_sandbox(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BERNSTEIN_BUILTIN_ALLOW_RUN_COMMAND", raising=False)
        names = selected_builtin_names("docker")
        assert "run_command" in names
        assert set(names) == set(BUILTIN_TOOL_NAMES)


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
