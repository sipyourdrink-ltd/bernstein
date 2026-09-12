"""Each parallel worker gets its own pytest temporary root (issue #5777).

pytest places ``tmp_path`` under ``$PYTEST_DEBUG_TEMPROOT/pytest-of-<user>``
and keeps a ``pytest-current`` symlink inside that root. When every worker of
``scripts/run_tests.py --parallel N`` inherits the same root, the workers race
on that symlink and the loser fails on whichever test happened to be building
a ``tmp_path``. These tests pin the per-worker root and its cleanup.
"""

from __future__ import annotations

import multiprocessing
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from scripts import run_tests

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _restore_worker_binding() -> Iterator[None]:
    """Keep a bound worker root from leaking into the next test."""
    previous = run_tests._WORKER_TEMP_ROOT
    try:
        yield
    finally:
        run_tests._WORKER_TEMP_ROOT = previous


def _bind_workers(parent: Path, count: int) -> list[Path]:
    """Bind *count* workers against *parent* the way the pool initializer does."""
    counter = multiprocessing.Value("i", 0)
    roots: list[Path] = []
    for _ in range(count):
        run_tests._bind_worker_temp_root(str(parent), counter)
        bound = run_tests._WORKER_TEMP_ROOT
        assert bound is not None
        roots.append(bound)
    return roots


class TestPerWorkerTempRoot:
    def test_run_parent_is_unique_per_run(self) -> None:
        first = run_tests.create_run_temp_parent()
        second = run_tests.create_run_temp_parent()
        try:
            assert first != second
            assert first.is_dir()
            assert second.is_dir()
            assert first.name.startswith("bernstein-tests-")
        finally:
            run_tests.remove_run_temp_parent(first)
            run_tests.remove_run_temp_parent(second)

    def test_workers_bind_distinct_roots_under_one_parent(self, tmp_path: Path) -> None:
        parent = tmp_path / "bernstein-tests-run"
        roots = _bind_workers(parent, 4)

        assert len(set(roots)) == 4, f"workers share a temporary root: {roots}"
        assert [r.name for r in roots] == ["w0", "w1", "w2", "w3"]
        for root in roots:
            assert root.parent == parent
            assert root.is_dir()

    def test_worker_env_exports_the_root_without_touching_the_parent_env(self, tmp_path: Path) -> None:
        before = dict(os.environ)

        env = run_tests.worker_env(tmp_path / "w0")

        assert env["PYTEST_DEBUG_TEMPROOT"] == str(tmp_path / "w0")
        assert dict(os.environ) == before
        assert "PYTEST_DEBUG_TEMPROOT" not in before or before["PYTEST_DEBUG_TEMPROOT"] != str(tmp_path / "w0")

    def test_run_file_exports_the_bound_root_to_the_pytest_subprocess(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str | None] = []

        def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            env = kwargs["env"]
            seen.append(env.get("PYTEST_DEBUG_TEMPROOT"))
            return subprocess.CompletedProcess(cmd, 0, "1 passed", "")

        monkeypatch.setattr(run_tests.subprocess, "run", fake_run)

        parent = tmp_path / "bernstein-tests-run"
        counter = multiprocessing.Value("i", 0)
        for _ in range(2):
            run_tests._bind_worker_temp_root(str(parent), counter)
            run_tests.run_file(Path("tests/unit/test_example.py"), [])

        assert seen == [str(parent / "w0"), str(parent / "w1")]


class TestRunParentCleanup:
    def _capture_parent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[Path]:
        created: list[Path] = []

        def fake_parent() -> Path:
            parent = tmp_path / f"bernstein-tests-{len(created)}"
            parent.mkdir()
            created.append(parent)
            return parent

        monkeypatch.setattr(run_tests, "create_run_temp_parent", fake_parent)
        return created

    def test_parent_is_removed_after_the_run(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        created = self._capture_parent(tmp_path, monkeypatch)

        assert run_tests.run_parallel([], [], 2, False) == 0

        assert len(created) == 1
        assert not created[0].exists(), "the run parent survived the run"

    def test_parent_is_removed_on_keyboard_interrupt(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        created = self._capture_parent(tmp_path, monkeypatch)

        def interrupt(*_args: object, **_kwargs: object) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(run_tests, "split_memory_heavy", interrupt)

        with pytest.raises(KeyboardInterrupt):
            run_tests.run_parallel([Path("tests/unit/test_example.py")], [], 2, False)

        assert len(created) == 1
        assert not created[0].exists(), "the run parent survived a KeyboardInterrupt"
