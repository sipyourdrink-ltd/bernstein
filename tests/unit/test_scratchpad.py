"""Tests for the ScratchpadManager class."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.communication.scratchpad import ScratchpadManager


class TestScratchpadManager:
    """Tests for ScratchpadManager."""

    def test_create_scratchpad(self, tmp_path: Path) -> None:
        mgr = ScratchpadManager(tmp_path, run_id="run-001")
        path = mgr.create_scratchpad()
        assert path.exists()
        assert path.is_dir()
        assert "run-001" in str(path)

    def test_default_run_id(self, tmp_path: Path) -> None:
        mgr = ScratchpadManager(tmp_path)
        path = mgr.create_scratchpad()
        assert "default" in str(path)

    def test_scratchpad_path_none_before_create(self, tmp_path: Path) -> None:
        mgr = ScratchpadManager(tmp_path)
        assert mgr.scratchpad_path is None

    def test_scratchpad_path_set_after_create(self, tmp_path: Path) -> None:
        mgr = ScratchpadManager(tmp_path)
        path = mgr.create_scratchpad()
        assert mgr.scratchpad_path == path

    def test_get_worker_scratchpad(self, tmp_path: Path) -> None:
        mgr = ScratchpadManager(tmp_path)
        mgr.create_scratchpad()
        worker_path = mgr.get_worker_scratchpad("worker-42")
        assert worker_path.exists()
        assert worker_path.is_dir()
        assert "worker-42" in str(worker_path)

    def test_get_worker_scratchpad_auto_creates(self, tmp_path: Path) -> None:
        mgr = ScratchpadManager(tmp_path)
        # Should auto-create the scratchpad
        worker_path = mgr.get_worker_scratchpad("w1")
        assert worker_path.exists()
        assert mgr.scratchpad_path is not None

    def test_write_and_read_shared_note(self, tmp_path: Path) -> None:
        mgr = ScratchpadManager(tmp_path)
        mgr.create_scratchpad()
        mgr.write_shared_note("findings.txt", "Found issue in auth module")
        content = mgr.read_shared_note("findings.txt")
        assert content == "Found issue in auth module"

    def test_read_nonexistent_note_returns_none(self, tmp_path: Path) -> None:
        mgr = ScratchpadManager(tmp_path)
        mgr.create_scratchpad()
        assert mgr.read_shared_note("nonexistent.txt") is None

    def test_get_shared_file_auto_creates(self, tmp_path: Path) -> None:
        mgr = ScratchpadManager(tmp_path)
        # Should auto-create the scratchpad
        file_path = mgr.get_shared_file("test.txt")
        assert mgr.scratchpad_path is not None
        assert file_path.parent == mgr.scratchpad_path

    def test_list_shared_files(self, tmp_path: Path) -> None:
        mgr = ScratchpadManager(tmp_path)
        mgr.create_scratchpad()
        mgr.write_shared_note("a.txt", "aaa")
        mgr.write_shared_note("b.txt", "bbb")
        files = mgr.list_shared_files()
        names = {f.name for f in files}
        assert "a.txt" in names
        assert "b.txt" in names

    def test_list_shared_files_empty_before_create(self, tmp_path: Path) -> None:
        mgr = ScratchpadManager(tmp_path)
        assert mgr.list_shared_files() == []

    def test_cleanup(self, tmp_path: Path) -> None:
        mgr = ScratchpadManager(tmp_path)
        mgr.create_scratchpad()
        mgr.write_shared_note("temp.txt", "temporary")
        count = mgr.cleanup()
        assert count >= 1
        assert mgr.scratchpad_path is None

    def test_context_manager(self, tmp_path: Path) -> None:
        with ScratchpadManager(tmp_path, run_id="ctx-run") as mgr:
            assert mgr.scratchpad_path is not None
            mgr.write_shared_note("note.txt", "inside context")
            path = mgr.scratchpad_path

        # After context exit with auto_cleanup=True, dir should be gone
        assert not path.exists()

    def test_context_manager_no_cleanup(self, tmp_path: Path) -> None:
        with ScratchpadManager(tmp_path, run_id="no-clean", auto_cleanup=False) as mgr:
            path = mgr.scratchpad_path
            assert path is not None

        # Should still exist since auto_cleanup=False
        assert path.exists()

    def test_get_env_vars(self, tmp_path: Path) -> None:
        mgr = ScratchpadManager(tmp_path)
        mgr.create_scratchpad()
        env = mgr.get_env_vars()
        assert "BERNSTEIN_SCRATCHPAD" in env
        assert "BERNSTEIN_SCRATCHPAD_SHARED" in env

    def test_get_env_vars_empty_before_create(self, tmp_path: Path) -> None:
        mgr = ScratchpadManager(tmp_path)
        assert mgr.get_env_vars() == {}

    def test_get_prompt_contract(self, tmp_path: Path) -> None:
        mgr = ScratchpadManager(tmp_path)
        mgr.create_scratchpad()
        contract = mgr.get_prompt_contract()
        assert "Scratchpad Directory" in contract
        assert str(mgr.scratchpad_path) in contract

    def test_get_prompt_contract_empty_before_create(self, tmp_path: Path) -> None:
        mgr = ScratchpadManager(tmp_path)
        assert mgr.get_prompt_contract() == ""
