"""Run the standalone TaskStore benchmark in a disposable workspace."""

import runpy
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from bernstein.core.task_store import TaskStore

_SCRIPT = Path(__file__).resolve().parents[2] / "benchmarks" / "bench_task_store.py"


def test_benchmark_workspace_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    notes = tmp_path / "bench_workdir" / "existing-project-notes.txt"
    notes.parent.mkdir()
    notes.write_text("keep me", encoding="utf-8")
    # Sentinel at the default archive path: a regression to TaskStore's
    # default archive_path would append 1,000 completions here.
    archive = tmp_path / ".sdd" / "archive" / "tasks.jsonl"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"sentinel\n")
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))

    with patch("bernstein.core.task_store.TaskStore", wraps=TaskStore) as make_store:
        runpy.run_path(str(_SCRIPT), run_name="__main__")
        output = capsys.readouterr().out
        for timing in ("Creations:", "Claims:", "Completions:", "Flush Latency"):
            assert timing in output
        assert notes.is_file()
        assert notes.read_text(encoding="utf-8") == "keep me"
        assert not list(temp_root.iterdir())

        with (
            patch.object(TaskStore, "create", side_effect=RuntimeError("create failed")),
            pytest.raises(RuntimeError, match="create failed"),
        ):
            runpy.run_path(str(_SCRIPT), run_name="__main__")

    first, second = [call.kwargs["jsonl_path"].resolve().parent for call in make_store.call_args_list]
    assert first.is_relative_to(temp_root) and second.is_relative_to(temp_root)
    assert first != second
    assert not list(temp_root.iterdir())
    assert notes.read_text(encoding="utf-8") == "keep me"
    assert archive.read_bytes() == b"sentinel\n"
