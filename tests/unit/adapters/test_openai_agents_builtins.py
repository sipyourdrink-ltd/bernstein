"""Security tests for the opt-in builtin tools.

These cover the hard conditions the builtins must satisfy:

1. Path confinement (file tools): absolute paths and ``..`` escapes are
   rejected; an in-workdir path is accepted.
2. ``run_command`` given an argv LIST still uses ``shell=False`` - a
   metacharacter in an argument is passed literally, never interpreted by a
   shell.
3. ``run_command`` rejects absolute / path-separator / shell-interpreter
   ``argv[0]`` (the proven filesystem-escape vectors) and only runs bare
   commands resolved on ``PATH`` when given the argv-list form.
4. ``run_command`` is only exposed (registered) under an OS sandbox provider
   or the explicit opt-in.
5. Every builtin call emits an audit event to the run event stream.
6. ``run_command`` given a single command STRING runs it through a real
   shell (``/bin/bash -lc``), so variable/command substitution, pipes, and
   background jobs work - this is the regression coverage for the D2
   OpenRouter FAIL-NOTE: the manager prompt's task-server auth headers
   (``$(cat <token-file>)`` / ``$BERNSTEIN_AUTH_TOKEN``) only resolve when
   ``run_command`` is given the shell-string form.
"""

from __future__ import annotations

import os
import sys
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

    def test_absolute_path_outside_workdir_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(WorkdirEscapeError, match="escapes"):
            resolve_in_workdir(tmp_path, "/etc/passwd")

    def test_absolute_path_inside_workdir_allowed(self, tmp_path: Path) -> None:
        # Runner-issued absolute paths (e.g. heartbeat writes to
        # /workspace/.sdd/runtime/heartbeats/<id>.json) must resolve when
        # they normalize inside the workdir - only escapes are rejected.
        absolute_inside = tmp_path / "sub" / "file.txt"
        resolved = resolve_in_workdir(tmp_path, str(absolute_inside))
        assert resolved == absolute_inside.resolve()

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

    def test_write_file_absolute_path_inside_workdir_succeeds(self, tmp_path: Path) -> None:
        # Defect B regression: worker/manager heartbeat writes use an
        # absolute path under the workspace root (e.g.
        # /workspace/.sdd/runtime/heartbeats/<id>.json). A blanket
        # absolute-path ban made every such write fail with
        # WorkdirEscapeError; only escapes outside the workdir must fail.
        heartbeat = tmp_path / ".sdd" / "runtime" / "heartbeats" / "backend-1.json"
        _events, emit = _sink()
        out = write_file_in_workdir(tmp_path, str(heartbeat), '{"ts": 1}', emit=emit)
        assert out.startswith("wrote")
        assert heartbeat.read_text() == '{"ts": 1}'

    def test_write_file_absolute_path_outside_workdir_rejected(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / "outside-heartbeat.json"
        _events, emit = _sink()
        out = write_file_in_workdir(tmp_path, str(outside), "x", emit=emit)
        assert out.startswith("error:")
        assert not outside.exists()

    def test_write_file_dotdot_traversal_still_rejected(self, tmp_path: Path) -> None:
        _events, emit = _sink()
        out = write_file_in_workdir(tmp_path, "../../etc/evil.txt", "x", emit=emit)
        assert out.startswith("error:")
        assert not (tmp_path.parent.parent / "etc" / "evil.txt").exists()


class TestRunCommandNoShell:
    def test_metacharacter_arg_passed_literally(self, tmp_path: Path) -> None:
        # If a shell interpreted this, ``;``/``|``/``$(...)`` would split or
        # substitute. With shell=False + argv list it is one literal arg.
        _events, emit = _sink()
        payload = "a;b|c$(whoami)&&d"
        out = run_command_in_workdir(tmp_path, ["printf", "%s", payload], emit=emit)
        stdout = out.split("stdout:\n", 1)[1].split("\nstderr:", 1)[0]
        assert stdout == payload

    def test_no_shell_expansion_of_glob(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "one.txt").write_text("")
        (tmp_path / "two.txt").write_text("")
        _events, emit = _sink()

        # Echo argv[1] back from the running interpreter rather than from a
        # coreutils helper. On Windows the ``printf`` that resolves on PATH is
        # the MSYS2 build Git for Windows ships, and the MSYS runtime
        # glob-expands argv itself before ``main()`` when the parent is not an
        # MSYS process - so a literal ``*.txt`` arrives already split into
        # ``one.txt two.txt`` with no shell anywhere in the chain, and the
        # assertion measured the helper instead of this function. CPython never
        # globs argv on any platform, so the shell=False contract is what is
        # under test here.
        #
        # ``argv[0]`` must be a bare name (resolve_command rejects absolute
        # paths and path separators), and the interpreter's own directory is
        # not necessarily on PATH - pytest is invoked as
        # ``sys.executable -m pytest``, which does not activate the venv. Put
        # it on PATH so the bare name resolves deterministically everywhere.
        monkeypatch.setenv("PATH", os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", ""))
        out = run_command_in_workdir(
            tmp_path,
            [os.path.basename(sys.executable), "-c", "import sys; sys.stdout.write(sys.argv[1])", "*.txt"],
            emit=emit,
        )
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


class TestRunCommandShellString:
    """A single command STRING gets a real shell (``/bin/bash -lc``).

    Regression coverage for the D2 OpenRouter FAIL-NOTE root cause: the
    manager prompt asks the agent to resolve ``$(cat <token-file>)`` and
    ``$BERNSTEIN_AUTH_TOKEN`` into an Authorization header. Both require
    shell substitution; an argv-list call (``shell=False``) cannot do this.
    """

    def test_command_substitution_resolves(self, tmp_path: Path) -> None:
        token_file = tmp_path / "manager.token"
        token_file.write_text("secret-token-value")
        _events, emit = _sink()
        out = run_command_in_workdir(
            tmp_path,
            "echo Bearer $(cat manager.token)",
            emit=emit,
        )
        stdout = out.split("stdout:\n", 1)[1].split("\nstderr:", 1)[0].strip()
        assert stdout == "Bearer secret-token-value"

    def test_env_var_fallback_resolves(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_AUTH_TOKEN", "env-fallback-token")
        _events, emit = _sink()
        out = run_command_in_workdir(tmp_path, "echo Bearer $BERNSTEIN_AUTH_TOKEN", emit=emit)
        stdout = out.split("stdout:\n", 1)[1].split("\nstderr:", 1)[0].strip()
        assert stdout == "Bearer env-fallback-token"

    def test_arithmetic_substitution(self, tmp_path: Path) -> None:
        _events, emit = _sink()
        out = run_command_in_workdir(tmp_path, "echo $((2+2))", emit=emit)
        stdout = out.split("stdout:\n", 1)[1].split("\nstderr:", 1)[0].strip()
        assert stdout == "4"

    def test_pipe_works(self, tmp_path: Path) -> None:
        _events, emit = _sink()
        out = run_command_in_workdir(tmp_path, "echo hello | tr a-z A-Z", emit=emit)
        stdout = out.split("stdout:\n", 1)[1].split("\nstderr:", 1)[0].strip()
        assert stdout == "HELLO"

    def test_runs_in_workdir(self, tmp_path: Path) -> None:
        _events, emit = _sink()
        out = run_command_in_workdir(tmp_path, "pwd", emit=emit)
        stdout = out.split("stdout:\n", 1)[1].split("\nstderr:", 1)[0].strip()
        assert stdout == str(tmp_path.resolve())

    def test_nonzero_exit_surfaces_stderr(self, tmp_path: Path) -> None:
        _events, emit = _sink()
        out = run_command_in_workdir(
            tmp_path,
            "echo boom-error-message 1>&2; exit 1",
            emit=emit,
        )
        assert out.startswith("exit_code=1")
        stderr = out.split("stderr:\n", 1)[1]
        assert "boom-error-message" in stderr

    def test_empty_string_rejected(self, tmp_path: Path) -> None:
        _events, emit = _sink()
        out = run_command_in_workdir(tmp_path, "   ", emit=emit)
        assert out.startswith("error:")

    def test_shell_string_form_was_never_subject_to_the_denylist(self, tmp_path: Path) -> None:
        # Finding: the shell-string form builds exec_argv as
        # ["/bin/bash", "-lc", <string>] directly - "/bin/bash" never passes
        # through resolve_command, so it was never blockable by the old
        # interpreter denylist (which rejected bare "bash"/"sh"/"python"...
        # only on the argv-LIST path). The D2 MiniMax outage was entirely on
        # the argv-list path; this pins down that the shell fix from
        # 0be0dd2e was never dead-on-arrival for its own callers.
        _events, emit = _sink()
        out = run_command_in_workdir(tmp_path, "python3 -c 'print(1+1)'", emit=emit)
        stdout = out.split("stdout:\n", 1)[1].split("\nstderr:", 1)[0].strip()
        assert stdout == "2"

    def test_bare_pytest_argv0_resolves_when_on_path(self, tmp_path: Path) -> None:
        import shutil as _shutil

        if _shutil.which("pytest") is None:
            pytest.skip("pytest not on PATH in this test environment")
        _events, emit = _sink()
        out = run_command_in_workdir(tmp_path, ["pytest", "--version"], emit=emit)
        assert not out.startswith("error:")

    def test_argv_list_form_unaffected_by_shell_support(self, tmp_path: Path) -> None:
        """List form still runs with no shell - no regression from adding string support."""
        _events, emit = _sink()
        out = run_command_in_workdir(tmp_path, ["echo", "hello"], emit=emit)
        stdout = out.split("stdout:\n", 1)[1].split("\nstderr:", 1)[0].strip()
        assert stdout == "hello"

    def test_background_job_does_not_hang_the_call(self, tmp_path: Path) -> None:
        """A backgrounded job (heartbeat-loop shape) must not block the tool call.

        Previously an OPEN xfail: ``subprocess.run(capture_output=True)``
        backs stdout/stderr with OS pipes, and a backgrounded grandchild
        (``sleep 30 & disown``) inherits those pipe fds even though
        ``disown`` detaches job-control/SIGHUP tracking, not stdio - so the
        pipe never reaches EOF and the call blocked for the full 30s.
        Fixed by capturing via temp files (:func:`_run_captured`): the direct
        child (bash) exits immediately after the foreground statement, and
        ``Popen.wait()`` returns without waiting on the backgrounded
        grandchild's fds the way a pipe-based read-until-EOF would.
        """
        import time

        start = time.monotonic()
        _events, emit = _sink()
        out = run_command_in_workdir(
            tmp_path,
            "sleep 30 & disown; echo started",
            emit=emit,
        )
        elapsed = time.monotonic() - start
        stdout = out.split("stdout:\n", 1)[1].split("\nstderr:", 1)[0].strip()
        assert stdout == "started"
        assert elapsed < 10, f"run_command took {elapsed:.1f}s - background job blocked the call"

    def test_heartbeat_loop_shape_does_not_hang_the_call(self, tmp_path: Path) -> None:
        """The manager's actual heartbeat-loop shape must return promptly.

        The command shape mirrors a real backgrounded heartbeat loop (a
        subshell that forks a loop and detaches), but with two safety nets so
        this test never leaks an orphaned process onto the host:
          1. The loop is bounded (``seq 1 5`` + ``sleep 1``) instead of
             ``while true`` + ``sleep 30`` - even if cleanup below fails, the
             spawned process self-terminates in ~5s instead of running
             forever.
          2. The subshell records its own pid (via ``$!``) to a pidfile in
             the test's ``tmp_path``, and a unique per-test marker string is
             embedded directly in the shell command line (the process's own
             argv, not its stdout - so it never pollutes the captured
             output) so it can be found/reaped with ``pgrep -f``/``pkill -f``
             regardless of whether the pidfile write raced the loop's exit.
        The try/finally guarantees the kill runs even if an assertion below
        fails.
        """
        import os
        import signal
        import subprocess
        import time
        import uuid

        marker = f"hb-loop-test-{uuid.uuid4().hex}"
        pidfile = tmp_path / "hb_loop.pid"
        start = time.monotonic()
        _events, emit = _sink()
        try:
            out = run_command_in_workdir(
                tmp_path,
                # The marker appears only in the process's own command line
                # (via the ``{marker}.txt`` redirect target) - never echoed
                # to stdout - so it doesn't affect the captured output while
                # still being greppable via `ps`/`pgrep -f`.
                f"(for i in $(seq 1 5); do date +%s > {marker}.txt; "
                f"sleep 1; done & echo $! > {pidfile}) ; echo started",
                emit=emit,
            )
            elapsed = time.monotonic() - start
            stdout = out.split("stdout:\n", 1)[1].split("\nstderr:", 1)[0].strip()
            assert stdout == "started"
            assert elapsed < 10, f"run_command took {elapsed:.1f}s - heartbeat loop blocked the call"
        finally:
            # Belt-and-braces cleanup: kill by recorded pid, then sweep by
            # marker in case the pidfile write lost the race with process
            # exit. Never let this loop outlive the test.
            if pidfile.exists():
                try:
                    pid = int(pidfile.read_text().strip())
                    os.kill(pid, signal.SIGTERM)
                except (ValueError, ProcessLookupError, OSError):
                    pass
            subprocess.run(["pkill", "-f", marker], check=False)

    def test_normal_foreground_output_capture_unaffected(self, tmp_path: Path) -> None:
        """File-backed capture must preserve exact stdout/stderr for normal calls."""
        _events, emit = _sink()
        out = run_command_in_workdir(
            tmp_path,
            "echo out-line; echo err-line 1>&2",
            emit=emit,
        )
        stdout = out.split("stdout:\n", 1)[1].split("\nstderr:", 1)[0].strip()
        stderr = out.split("stderr:\n", 1)[1].strip()
        assert stdout == "out-line"
        assert stderr == "err-line"


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

    def test_bare_basic_toolchain_interpreters_allowed(self, tmp_path: Path) -> None:
        # Defect A regression: bare standard-toolchain interpreter names
        # (bash, sh, python, python3, ...) used to be denylisted outright,
        # making every Python/pytest-driven run_command-only task
        # structurally unsolvable (D2 MiniMax KILL-NOTE). They are ordinary
        # bare commands now - the shell-string form already grants an
        # unrestricted shell, so denylisting them here added no real
        # containment.
        _events, emit = _sink()
        out = run_command_in_workdir(tmp_path, ["sh", "-c", "echo hi"], emit=emit)
        stdout = out.split("stdout:\n", 1)[1].split("\nstderr:", 1)[0].strip()
        assert stdout == "hi"

    def test_bare_bash_argv0_allowed(self, tmp_path: Path) -> None:
        _events, emit = _sink()
        out = run_command_in_workdir(tmp_path, ["bash", "-c", "echo bash-ok"], emit=emit)
        stdout = out.split("stdout:\n", 1)[1].split("\nstderr:", 1)[0].strip()
        assert stdout == "bash-ok"

    def test_bare_python3_argv0_allowed(self, tmp_path: Path) -> None:
        _events, emit = _sink()
        out = run_command_in_workdir(tmp_path, ["python3", "-c", "print(1)"], emit=emit)
        stdout = out.split("stdout:\n", 1)[1].split("\nstderr:", 1)[0].strip()
        assert stdout == "1"

    def test_dangerous_command_still_rejected(self, tmp_path: Path) -> None:
        # The rework shrank the denylist to genuinely dangerous operations -
        # confirm at least one of those still fails closed.
        _events, emit = _sink()
        out = run_command_in_workdir(tmp_path, ["sudo", "true"], emit=emit)
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

    def test_manifest_opt_in_enables_run_command_without_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Manifest allow_run_command=True must not depend on the (filtered) env."""
        monkeypatch.delenv("BERNSTEIN_BUILTIN_ALLOW_RUN_COMMAND", raising=False)
        assert run_command_available("unix_local", allow_run_command=True) is True
        names = selected_builtin_names("unix_local", allow_run_command=True)
        assert "run_command" in names

    def test_manifest_opt_out_wins_over_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Manifest allow_run_command=False beats the env fallback."""
        monkeypatch.setenv("BERNSTEIN_BUILTIN_ALLOW_RUN_COMMAND", "1")
        assert run_command_available("unix_local", allow_run_command=False) is False
        names = selected_builtin_names("unix_local", allow_run_command=False)
        assert "run_command" not in names

    def test_manifest_none_falls_back_to_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """allow_run_command=None (direct invocation) keeps the env behavior."""
        monkeypatch.setenv("BERNSTEIN_BUILTIN_ALLOW_RUN_COMMAND", "1")
        assert run_command_available("unix_local", allow_run_command=None) is True

    def test_os_sandbox_still_wins_over_manifest_opt_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An OS sandbox provider confines run_command regardless of the flag."""
        monkeypatch.delenv("BERNSTEIN_BUILTIN_ALLOW_RUN_COMMAND", raising=False)
        assert run_command_available("docker", allow_run_command=False) is True


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


class TestRunCommandDuplicateSuppression:
    """Regression: D2 attempt-4 defect 2 - double-delivered tool_calls.

    A chat-completions translation endpoint delivered the SAME tool_call
    twice per assistant message; the SDK invoked ``run_command`` twice, so
    ``bash create_tasks.sh`` double-POSTed duplicate task pairs. These tests
    count REAL executions (a command that appends to a file) and assert
    exactly-once for a duplicated invocation, in both calling forms.
    """

    @pytest.fixture(autouse=True)
    def _fresh_dedupe_state(self) -> Any:
        from bernstein.adapters import openai_agents_builtins as mod

        mod._recent_invocations.clear()
        yield
        mod._recent_invocations.clear()

    def test_single_shell_string_executes_exactly_once(self, tmp_path: Path) -> None:
        _events, emit = _sink()
        run_command_in_workdir(tmp_path, "echo run >> marker.txt", emit=emit)
        assert (tmp_path / "marker.txt").read_text().count("run") == 1

    def test_single_argv_executes_exactly_once(self, tmp_path: Path) -> None:
        _events, emit = _sink()
        run_command_in_workdir(tmp_path, ["bash", "-c", "echo run >> marker.txt"], emit=emit)
        assert (tmp_path / "marker.txt").read_text().count("run") == 1

    def test_duplicate_shell_string_executes_exactly_once(self, tmp_path: Path) -> None:
        events, emit = _sink()
        first = run_command_in_workdir(tmp_path, "echo run >> marker.txt", emit=emit)
        second = run_command_in_workdir(tmp_path, "echo run >> marker.txt", emit=emit)
        assert (tmp_path / "marker.txt").read_text().count("run") == 1
        assert second == first
        results = [e for e in events if e["type"] == "tool_result"]
        assert results[1].get("deduped") is True
        assert results[1]["duplicate_of"] == results[0]["invocation_id"]

    def test_duplicate_argv_executes_exactly_once(self, tmp_path: Path) -> None:
        events, emit = _sink()
        argv = ["bash", "-c", "echo run >> marker.txt"]
        first = run_command_in_workdir(tmp_path, argv, emit=emit)
        second = run_command_in_workdir(tmp_path, argv, emit=emit)
        assert (tmp_path / "marker.txt").read_text().count("run") == 1
        assert second == first
        results = [e for e in events if e["type"] == "tool_result"]
        assert results[1].get("deduped") is True

    def test_distinct_commands_both_execute(self, tmp_path: Path) -> None:
        _events, emit = _sink()
        run_command_in_workdir(tmp_path, "echo a >> marker.txt", emit=emit)
        run_command_in_workdir(tmp_path, "echo b >> marker.txt", emit=emit)
        text = (tmp_path / "marker.txt").read_text()
        assert "a" in text
        assert "b" in text

    def test_window_zero_disables_suppression(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_RUN_COMMAND_DEDUPE_WINDOW_S", "0")
        _events, emit = _sink()
        run_command_in_workdir(tmp_path, "echo run >> marker.txt", emit=emit)
        run_command_in_workdir(tmp_path, "echo run >> marker.txt", emit=emit)
        assert (tmp_path / "marker.txt").read_text().count("run") == 2

    def test_invocation_id_and_events_carry_ids(self, tmp_path: Path) -> None:
        events, emit = _sink()
        run_command_in_workdir(tmp_path, ["true"], emit=emit)
        call = next(e for e in events if e["type"] == "tool_call")
        result = next(e for e in events if e["type"] == "tool_result")
        assert isinstance(call["invocation_id"], int)
        assert result["invocation_id"] == call["invocation_id"]
