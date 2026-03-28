"""Tests for InvariantsGuard."""

from bernstein.evolution.invariants import (
    check_proposal_targets,
    compute_invariants,
    verify_invariants,
    write_lockfile,
)


class TestComputeInvariants:
    def test_computes_hashes_for_existing_files(self, tmp_path):
        src = tmp_path / "src" / "bernstein" / "core"
        src.mkdir(parents=True)
        (src / "janitor.py").write_text("# janitor code")
        hashes = compute_invariants(tmp_path)
        assert "src/bernstein/core/janitor.py" in hashes
        assert len(hashes["src/bernstein/core/janitor.py"]) == 64

    def test_skips_missing_files(self, tmp_path):
        hashes = compute_invariants(tmp_path)
        assert len(hashes) == 0


class TestVerifyInvariants:
    def test_passes_when_unchanged(self, tmp_path):
        src = tmp_path / "src" / "bernstein" / "core"
        src.mkdir(parents=True)
        (src / "janitor.py").write_text("# original")
        write_lockfile(tmp_path)
        ok, violations = verify_invariants(tmp_path)
        assert ok
        assert violations == []

    def test_fails_when_modified(self, tmp_path):
        src = tmp_path / "src" / "bernstein" / "core"
        src.mkdir(parents=True)
        (src / "janitor.py").write_text("# original")
        write_lockfile(tmp_path)
        (src / "janitor.py").write_text("# MODIFIED")
        ok, violations = verify_invariants(tmp_path)
        assert not ok
        assert any("MODIFIED" in v for v in violations)

    def test_creates_lockfile_on_first_run(self, tmp_path):
        src = tmp_path / "src" / "bernstein" / "core"
        src.mkdir(parents=True)
        (src / "janitor.py").write_text("# code")
        ok, violations = verify_invariants(tmp_path)
        assert ok
        assert (tmp_path / ".sdd" / "invariants.lock").exists()


class TestCheckProposalTargets:
    def test_rejects_locked(self):
        ok, v = check_proposal_targets(["src/bernstein/core/janitor.py"])
        assert not ok

    def test_allows_safe(self):
        ok, v = check_proposal_targets(["templates/roles/backend/system_prompt.md"])
        assert ok
