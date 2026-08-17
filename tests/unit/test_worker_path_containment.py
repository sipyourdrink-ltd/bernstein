"""Path containment for the worker's PID-file sites (#4034).

Both sites derive ``<pid-dir>/<session>.json`` and must prove the result stays
inside the pid directory. They are covered separately because they guard
different moments:

* ``_write_pid_file`` refuses before the file is created.
* the post-spawn re-check in ``main`` refuses after it exists, which is the
  only thing standing between a pid file swapped for a symlink during the
  spawn and a write landing outside the pid directory.
"""

from __future__ import annotations

import os
import signal
import sys
from typing import TYPE_CHECKING, Any

import pytest

from bernstein.core.orchestration import worker

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def _restore_signal_handlers() -> Any:
    """``main`` installs SIGTERM/SIGINT handlers; keep them out of the session."""
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    yield
    for sig, handler in previous.items():
        signal.signal(sig, handler)


# ---------------------------------------------------------------------------
# Site 1: _write_pid_file
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("session", ["../escape", "nested/child", "..%2Fescape"])
def test_write_pid_file_refuses_session_id_that_escapes_pid_dir(tmp_path: Path, session: str) -> None:
    """A session id that names anything but a child of pid-dir is refused."""
    pid_dir = tmp_path / "pids"

    with pytest.raises(SystemExit) as excinfo:
        worker._write_pid_file(pid_dir, session, {"role": "test"})

    assert excinfo.value.code == 1
    assert not (tmp_path / "escape.json").exists()


def test_write_pid_file_accepts_a_dotted_session_id(tmp_path: Path) -> None:
    """``".."`` is not an escape here, and never was.

    The suffix is appended before the containment check, so the id ``".."``
    names the ordinary file ``...json`` inside pid-dir rather than the parent
    directory. Pinned so the reserved-segment rule is not mistaken for a
    refusal this site makes.
    """
    pid_file = worker._write_pid_file(tmp_path / "pids", "..", {"role": "test"})

    assert pid_file == (tmp_path / "pids").resolve() / "...json"
    assert pid_file.exists()


def test_write_pid_file_refuses_session_id_over_the_name_limit(tmp_path: Path) -> None:
    """The helper caps a single component at what the filesystem can store.

    The old inline check only compared resolved prefixes, so an over-long id
    got as far as ``open()`` and failed there as ``OSError(ENAMETOOLONG)``.
    """
    pid_dir = tmp_path / "pids"

    with pytest.raises(SystemExit) as excinfo:
        worker._write_pid_file(pid_dir, "s" * 300, {"role": "test"})

    assert excinfo.value.code == 1


def test_write_pid_file_refuses_a_session_name_symlinked_out_of_pid_dir(tmp_path: Path) -> None:
    """A pre-existing symlink at the target name is refused, not followed."""
    pid_dir = tmp_path / "pids"
    pid_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (pid_dir / "abc.json").symlink_to(outside / "abc.json")

    with pytest.raises(SystemExit) as excinfo:
        worker._write_pid_file(pid_dir, "abc", {"role": "test"})

    assert excinfo.value.code == 1
    assert not (outside / "abc.json").exists()


def test_write_pid_file_writes_a_contained_path(tmp_path: Path) -> None:
    """The happy path is unchanged: the file lands under pid-dir."""
    pid_dir = tmp_path / "pids"
    seen: list[Path] = []

    pid_file = worker._write_pid_file(pid_dir, "sess-1", {"role": "test"}, on_resolved=seen.append)

    assert pid_file == (pid_dir.resolve() / "sess-1.json")
    assert pid_file.exists()
    assert seen == [pid_file]


# ---------------------------------------------------------------------------
# Site 2: the post-spawn re-check in main()
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
@pytest.mark.usefixtures("_restore_signal_handlers")
def test_pid_file_update_refuses_path_outside_pid_dir_after_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The re-check refuses a pid file swapped for an escaping symlink.

    This is the only window the second check exists for: ``_write_pid_file``
    already proved the path before creating it, so the state that trips this
    branch can only appear between that write and the post-spawn update. The
    fake ``Popen`` performs the swap at exactly that instant.
    """
    pid_dir = tmp_path / "pids"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sess-1.json").write_text("{}", encoding="utf-8")

    class _SwappingPopen:
        """Stands in for the spawn, replacing the pid file as it runs."""

        pid = 4321

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            target = pid_dir / "sess-1.json"
            target.unlink()
            target.symlink_to(outside / "sess-1.json")

    monkeypatch.setattr(worker.subprocess, "Popen", _SwappingPopen)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bernstein-worker",
            "--role",
            "test",
            "--session",
            "sess-1",
            "--pid-dir",
            str(pid_dir),
            "--model",
            "test-model",
            "--",
            "true",
        ],
    )

    with pytest.raises(SystemExit) as excinfo:
        worker.main()

    assert excinfo.value.code == 1
    assert "pid file escaped pid-dir" in capsys.readouterr().err
    # The swapped-in target must be left exactly as it was found.
    assert (outside / "sess-1.json").read_text(encoding="utf-8") == "{}"
