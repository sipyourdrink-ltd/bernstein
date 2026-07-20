"""Tests for InvariantsGuard."""

import logging

from bernstein.evolution.invariants import (
    check_proposal_targets,
    compute_invariants,
    verify_invariants,
    write_lockfile,
)

_LOGGER_NAME = "bernstein.evolution.invariants"


class TestComputeInvariants:
    def test_computes_hashes_for_existing_files(self, tmp_path):
        src = tmp_path / "src" / "bernstein" / "core" / "quality"
        src.mkdir(parents=True)
        (src / "janitor.py").write_text("# janitor code")
        hashes = compute_invariants(tmp_path)
        assert "src/bernstein/core/quality/janitor.py" in hashes
        assert len(hashes["src/bernstein/core/quality/janitor.py"]) == 64

    def test_skips_missing_files(self, tmp_path):
        hashes = compute_invariants(tmp_path)
        assert len(hashes) == 0


class TestVerifyInvariants:
    def test_passes_when_unchanged(self, tmp_path):
        src = tmp_path / "src" / "bernstein" / "core" / "quality"
        src.mkdir(parents=True)
        (src / "janitor.py").write_text("# original")
        write_lockfile(tmp_path)
        ok, violations = verify_invariants(tmp_path)
        assert ok
        assert violations == []

    def test_fails_when_modified(self, tmp_path):
        src = tmp_path / "src" / "bernstein" / "core" / "quality"
        src.mkdir(parents=True)
        (src / "janitor.py").write_text("# original")
        write_lockfile(tmp_path)
        (src / "janitor.py").write_text("# MODIFIED")
        ok, violations = verify_invariants(tmp_path)
        assert not ok
        assert any("MODIFIED" in v for v in violations)

    def test_creates_lockfile_on_first_run(self, tmp_path):
        src = tmp_path / "src" / "bernstein" / "core" / "quality"
        src.mkdir(parents=True)
        (src / "janitor.py").write_text("# code")
        ok, _violations = verify_invariants(tmp_path)
        assert ok
        assert (tmp_path / ".sdd" / "invariants.lock").exists()


class TestWorkspaceNoise:
    """Locked-file warnings must not fire in workspaces that never had the files."""

    def test_compute_is_silent_in_non_source_workspace(self, tmp_path, caplog):
        """A workspace without a bernstein source tree gets zero warnings."""
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            compute_invariants(tmp_path)
        assert not [r for r in caplog.records if "Locked file" in r.getMessage()]

    def test_run_shaped_check_is_silent_in_empty_workspace(self, tmp_path, caplog):
        """The bootstrap invariants check emits zero Locked-file warnings in an empty workspace."""
        from bernstein.core.orchestration.bootstrap import _check_safety_invariants

        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            _check_safety_invariants(tmp_path)
        assert not [r for r in caplog.records if "Locked file" in r.getMessage()]
        # The baseline lockfile is still written for later verification.
        assert (tmp_path / ".sdd" / "invariants.lock").exists()

    def test_deleted_after_lock_still_warns(self, tmp_path, caplog):
        """A file that WAS locked and then vanished is still reported."""
        src = tmp_path / "src" / "bernstein" / "core" / "quality"
        src.mkdir(parents=True)
        janitor = src / "janitor.py"
        janitor.write_text("# original")
        write_lockfile(tmp_path)
        janitor.unlink()
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            ok, violations = verify_invariants(tmp_path)
        assert not ok
        assert any(v == "MISSING: src/bernstein/core/quality/janitor.py" for v in violations)
        assert any(r.levelno >= logging.ERROR for r in caplog.records)

    def test_missing_warning_fires_once_per_process_in_source_tree(self, tmp_path, caplog):
        """A partial source tree warns once per missing file, not once per pass."""
        src = tmp_path / "src" / "bernstein" / "core" / "quality"
        src.mkdir(parents=True)
        (src / "janitor.py").write_text("# janitor code")
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            compute_invariants(tmp_path)
            compute_invariants(tmp_path)
        warned = [r.getMessage() for r in caplog.records if "Locked file not found" in r.getMessage()]
        assert len(warned) == len(set(warned))
        assert warned  # missing files in a source tree are still surfaced


class TestCheckProposalTargets:
    def test_rejects_locked(self):
        ok, _v = check_proposal_targets(["src/bernstein/core/quality/janitor.py"])
        assert not ok

    def test_allows_safe(self):
        ok, _v = check_proposal_targets(["templates/roles/backend/system_prompt.md"])
        assert ok
