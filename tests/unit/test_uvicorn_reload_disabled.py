"""Regression tests for audit-115 — uvicorn ``--reload`` must never be enabled.

Context:
    On 2026-04-11 a bernstein evolve run launched uvicorn with ``--reload`` so
    that source changes made by agents would be hot-reloaded. Every file write
    by an agent triggered a uvicorn restart, which dropped in-flight HTTP
    connections, raced on the bind port, and replayed the WAL with duplicate
    task claims. The server hung for 127s and the orchestrator gave up.

    Audit-115 removes ``--reload`` from the production launch paths entirely.
    Evolve mode is the self-modifying flow — it MUST NOT enable auto-reload.

These tests ensure both server-launch code paths (``server_supervisor`` and
``server_launch``) never pass ``--reload`` / ``reload=True`` to uvicorn,
regardless of the ``evolve_mode`` flag.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from bernstein.core.server_launch import _start_server

from bernstein.core import server_supervisor


def _popen_argv(mock_popen: object) -> list[str]:
    """Return the argv list passed to the most recent Popen call."""
    call = mock_popen.call_args  # type: ignore[attr-defined]
    assert call is not None, "subprocess.Popen was not invoked"
    return list(call.args[0])


@pytest.mark.parametrize("evolve_mode", [False, True])
def test_server_supervisor_never_passes_reload_flag(tmp_path: Path, evolve_mode: bool) -> None:
    """``_launch_server`` must never include ``--reload`` in the uvicorn argv.

    Regression for audit-115: in evolve mode every agent file write used to
    trigger a uvicorn restart → dropped requests, WAL replay duplicates.
    """
    (tmp_path / ".sdd" / "runtime").mkdir(parents=True, exist_ok=True)
    # Create src/bernstein so the old buggy code path (which extended the
    # argv with --reload-dir on directory presence) would have fired.
    (tmp_path / "src" / "bernstein").mkdir(parents=True, exist_ok=True)

    state = server_supervisor._SupervisorState(
        workdir=tmp_path,
        port=8052,
        bind_host="127.0.0.1",
        cluster_enabled=False,
        auth_token=None,
        evolve_mode=evolve_mode,
    )

    with (
        patch("bernstein.core.server.server_supervisor.rotate_log_file"),
        patch("bernstein.core.server.server_supervisor.write_supervisor_state"),
        patch(
            "bernstein.core.server.server_supervisor.subprocess.Popen",
            return_value=SimpleNamespace(pid=4242),
        ) as mock_popen,
    ):
        server_supervisor._launch_server(state)

    argv = _popen_argv(mock_popen)
    assert "--reload" not in argv, f"supervisor must not pass --reload (evolve_mode={evolve_mode}); argv={argv}"
    assert "--reload-dir" not in argv, f"supervisor must not pass --reload-dir; argv={argv}"


@pytest.mark.parametrize("evolve_mode", [False, True])
def test_server_launch_start_server_never_passes_reload_flag(tmp_path: Path, evolve_mode: bool) -> None:
    """``_start_server`` must never include ``--reload`` in the uvicorn argv.

    Regression for audit-115.  Evolve mode is the self-modifying flow and
    must not enable auto-reload.
    """
    (tmp_path / ".sdd" / "runtime").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "bernstein").mkdir(parents=True, exist_ok=True)

    with (
        patch("bernstein.core.server.server_launch._read_pid", return_value=None),
        patch("bernstein.core.server.server_launch._is_alive", return_value=False),
        patch("bernstein.core.server.server_launch.rotate_log_file"),
        patch(
            "bernstein.core.server.server_launch.subprocess.Popen",
            return_value=SimpleNamespace(pid=5353),
        ) as mock_popen,
    ):
        _start_server(tmp_path, 8052, evolve_mode=evolve_mode)

    argv = _popen_argv(mock_popen)
    assert "--reload" not in argv, f"server_launch must not pass --reload (evolve_mode={evolve_mode}); argv={argv}"
    assert "--reload-dir" not in argv, f"server_launch must not pass --reload-dir; argv={argv}"


def test_server_supervisor_module_has_no_reload_literal() -> None:
    """Static check: the supervisor source must contain no ``--reload`` argv literal.

    A future contributor who re-adds ``--reload`` to the uvicorn command will
    trip this test. (Comment text mentioning ``--reload`` in documentation is
    allowed; what we forbid is a raw string literal that would end up in argv.)
    """
    source = Path(server_supervisor.__file__).read_text(encoding="utf-8")
    # The only tolerated occurrences are inside comments / docstrings.
    # Strip comment lines before checking for the literal argv string.
    code_lines = [ln for ln in source.splitlines() if not ln.lstrip().startswith("#")]
    code_body = "\n".join(code_lines)
    assert '"--reload"' not in code_body, "server_supervisor must not pass '--reload' to uvicorn (audit-115)"
    assert "'--reload'" not in code_body, "server_supervisor must not pass '--reload' to uvicorn (audit-115)"


def test_server_launch_module_has_no_reload_literal() -> None:
    """Static check: server_launch source must contain no ``--reload`` argv literal."""
    from bernstein.core.server import server_launch

    source = Path(server_launch.__file__).read_text(encoding="utf-8")
    code_lines = [ln for ln in source.splitlines() if not ln.lstrip().startswith("#")]
    code_body = "\n".join(code_lines)
    assert '"--reload"' not in code_body, "server_launch must not pass '--reload' to uvicorn (audit-115)"
    assert "'--reload'" not in code_body, "server_launch must not pass '--reload' to uvicorn (audit-115)"
