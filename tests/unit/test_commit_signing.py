"""Tests for commit signing and provenance (commit_signing.py).

Covers:
- CommitProvenance construction and defaults
- build_provenance_trailers: trailer format correctness
- _append_trailers: message + trailer assembly
- sign_and_commit: unsigned path (no signing key)
- sign_and_commit: fallback to unsigned when signing fails
- read_commit_provenance: parsing trailers from commit log
- is_agent_commit: detection of Bernstein-tagged commits
- verify_commit_signature: basic call path
- SignedCommitResult.ok property
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bernstein.core.commit_signing import (
    CommitProvenance,
    SignedCommitResult,
    SigningConfig,
    build_provenance_trailers,
    is_agent_commit,
    read_commit_provenance,
    sign_and_commit,
    verify_commit_signature,
)
from bernstein.core.git_basic import GitResult

# ---------------------------------------------------------------------------
# CommitProvenance
# ---------------------------------------------------------------------------


class TestCommitProvenance:
    def test_defaults(self) -> None:
        p = CommitProvenance(agent_id="agent-1", task_id="task-abc", run_id="run-xyz")
        assert p.agent_id == "agent-1"
        assert p.task_id == "task-abc"
        assert p.run_id == "run-xyz"
        assert p.role == ""
        assert p.model == ""
        assert p.timestamp  # auto-generated

    def test_all_fields(self) -> None:
        p = CommitProvenance(
            agent_id="agent-2",
            task_id="task-def",
            run_id="run-uvw",
            role="security",
            model="claude-sonnet-4-6",
            timestamp="2026-04-11T00:00:00+00:00",
        )
        assert p.role == "security"
        assert p.model == "claude-sonnet-4-6"
        assert p.timestamp == "2026-04-11T00:00:00+00:00"


# ---------------------------------------------------------------------------
# build_provenance_trailers
# ---------------------------------------------------------------------------


class TestBuildProvenanceTrailers:
    def test_all_fields_present(self) -> None:
        p = CommitProvenance(
            agent_id="a1",
            task_id="t1",
            run_id="r1",
            role="qa",
            model="gpt-4o",
            timestamp="2026-01-01T00:00:00+00:00",
        )
        trailers = build_provenance_trailers(p)
        assert any("Bernstein-Agent-ID: a1" in t for t in trailers)
        assert any("Bernstein-Task-ID: t1" in t for t in trailers)
        assert any("Bernstein-Run-ID: r1" in t for t in trailers)
        assert any("Bernstein-Role: qa" in t for t in trailers)
        assert any("Bernstein-Model: gpt-4o" in t for t in trailers)
        assert any("Bernstein-Timestamp:" in t for t in trailers)

    def test_empty_optional_fields_omitted(self) -> None:
        p = CommitProvenance(agent_id="a1", task_id="t1", run_id="r1")
        trailers = build_provenance_trailers(p)
        assert not any("Bernstein-Role:" in t for t in trailers)
        assert not any("Bernstein-Model:" in t for t in trailers)

    def test_no_agent_id_omits_trailer(self) -> None:
        p = CommitProvenance(agent_id="", task_id="t1", run_id="r1")
        trailers = build_provenance_trailers(p)
        assert not any("Bernstein-Agent-ID:" in t for t in trailers)

    def test_trailer_key_value_format(self) -> None:
        p = CommitProvenance(agent_id="agent-abc", task_id="task-xyz", run_id="run-123")
        trailers = build_provenance_trailers(p)
        for trailer in trailers:
            assert ": " in trailer, f"Trailer missing ': ' separator: {trailer!r}"


# ---------------------------------------------------------------------------
# sign_and_commit — integration with a real git repo
# ---------------------------------------------------------------------------


def _init_git_repo(path: Path) -> None:
    """Create a minimal git repo with an initial commit."""
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True, capture_output=True)
    # Initial commit so HEAD exists
    dummy = path / "README.md"
    dummy.write_text("hello\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=path, check=True, capture_output=True)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Yield a temporary git repo with an initial commit."""
    _init_git_repo(tmp_path)
    return tmp_path


def _make_staged_change(repo: Path, filename: str = "change.txt") -> None:
    """Write and stage a file so there's something to commit."""
    (repo / filename).write_text("content\n")
    subprocess.run(["git", "add", filename], cwd=repo, check=True, capture_output=True)


class TestSignAndCommit:
    def test_unsigned_commit_succeeds(self, git_repo: Path) -> None:
        _make_staged_change(git_repo)
        provenance = CommitProvenance(agent_id="a1", task_id="t1", run_id="r1")
        result = sign_and_commit(git_repo, "feat: test commit", provenance)
        assert result.ok
        assert result.sha
        assert not result.signed
        assert result.signing_mode == "none"

    def test_provenance_trailers_in_commit_message(self, git_repo: Path) -> None:
        _make_staged_change(git_repo)
        provenance = CommitProvenance(
            agent_id="agent-sec",
            task_id="task-dlp",
            run_id="run-security",
            role="security",
            model="claude-sonnet-4-6",
        )
        result = sign_and_commit(git_repo, "feat: add dlp", provenance)
        assert result.ok
        log = subprocess.run(
            ["git", "log", "-1", "--format=%B"],
            cwd=git_repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "Bernstein-Agent-ID: agent-sec" in log
        assert "Bernstein-Task-ID: task-dlp" in log
        assert "Bernstein-Run-ID: run-security" in log
        assert "Bernstein-Role: security" in log
        assert "Bernstein-Model: claude-sonnet-4-6" in log

    def test_commit_message_preserved(self, git_repo: Path) -> None:
        _make_staged_change(git_repo)
        provenance = CommitProvenance(agent_id="a", task_id="t", run_id="r")
        result = sign_and_commit(git_repo, "fix: important bug", provenance)
        assert result.ok
        log = subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            cwd=git_repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert log == "fix: important bug"

    def test_ssh_signing_falls_back_when_key_missing(self, git_repo: Path) -> None:
        _make_staged_change(git_repo)
        provenance = CommitProvenance(agent_id="a", task_id="t", run_id="r")
        config = SigningConfig(mode="ssh", signing_key="/nonexistent/key")
        result = sign_and_commit(git_repo, "feat: ssh test", provenance, signing_config=config)
        # Should fall back to unsigned but commit should still succeed
        assert result.ok
        assert not result.signed

    def test_gpg_signing_falls_back_when_key_missing(self, git_repo: Path) -> None:
        _make_staged_change(git_repo)
        provenance = CommitProvenance(agent_id="a", task_id="t", run_id="r")
        config = SigningConfig(mode="gpg", signing_key="nonexistent@example.com")
        result = sign_and_commit(git_repo, "feat: gpg test", provenance, signing_config=config)
        # Should fall back to unsigned
        assert result.ok
        assert not result.signed

    def test_result_trailers_populated(self, git_repo: Path) -> None:
        _make_staged_change(git_repo)
        provenance = CommitProvenance(agent_id="a1", task_id="t1", run_id="r1")
        result = sign_and_commit(git_repo, "feat: trailers", provenance)
        assert result.trailers
        assert any("Bernstein-Agent-ID" in t for t in result.trailers)

    def test_no_staged_changes_fails_gracefully(self, git_repo: Path) -> None:
        provenance = CommitProvenance(agent_id="a", task_id="t", run_id="r")
        result = sign_and_commit(git_repo, "feat: nothing", provenance)
        assert not result.ok
        assert result.sha == ""

    def test_signed_commit_result_ok_property(self) -> None:
        ok_git = GitResult(returncode=0, stdout="", stderr="")
        fail_git = GitResult(returncode=1, stdout="", stderr="error")
        prov = CommitProvenance(agent_id="a", task_id="t", run_id="r")
        ok_result = SignedCommitResult(
            git_result=ok_git,
            sha="abc",
            signed=False,
            signing_mode="none",
            provenance=prov,
            trailers=[],
        )
        fail_result = SignedCommitResult(
            git_result=fail_git,
            sha="",
            signed=False,
            signing_mode="none",
            provenance=prov,
            trailers=[],
        )
        assert ok_result.ok is True
        assert fail_result.ok is False


# ---------------------------------------------------------------------------
# read_commit_provenance
# ---------------------------------------------------------------------------


class TestReadCommitProvenance:
    def test_reads_bernstein_trailers(self, git_repo: Path) -> None:
        _make_staged_change(git_repo)
        provenance = CommitProvenance(
            agent_id="agent-xyz",
            task_id="task-123",
            run_id="run-abc",
            role="backend",
            model="claude-haiku-4-5",
        )
        sign_and_commit(git_repo, "feat: read test", provenance)
        read_prov = read_commit_provenance(git_repo)
        assert read_prov.get("Agent-ID") == "agent-xyz"
        assert read_prov.get("Task-ID") == "task-123"
        assert read_prov.get("Run-ID") == "run-abc"
        assert read_prov.get("Role") == "backend"
        assert read_prov.get("Model") == "claude-haiku-4-5"

    def test_empty_dict_for_non_agent_commit(self, git_repo: Path) -> None:
        # The initial commit from _init_git_repo has no Bernstein trailers
        first_sha = subprocess.run(
            ["git", "rev-list", "--max-parents=0", "HEAD"],
            cwd=git_repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        prov = read_commit_provenance(git_repo, first_sha)
        assert prov == {}

    def test_returns_empty_dict_on_bad_ref(self, git_repo: Path) -> None:
        prov = read_commit_provenance(git_repo, "nonexistent-ref-xyz")
        assert prov == {}


# ---------------------------------------------------------------------------
# is_agent_commit
# ---------------------------------------------------------------------------


class TestIsAgentCommit:
    def test_agent_commit_identified(self, git_repo: Path) -> None:
        _make_staged_change(git_repo)
        provenance = CommitProvenance(agent_id="bernstein-1", task_id="t", run_id="r")
        sign_and_commit(git_repo, "feat: agent work", provenance)
        assert is_agent_commit(git_repo) is True

    def test_non_agent_commit_not_identified(self, git_repo: Path) -> None:
        # The initial commit has no Bernstein trailers
        first_sha = subprocess.run(
            ["git", "rev-list", "--max-parents=0", "HEAD"],
            cwd=git_repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert is_agent_commit(git_repo, first_sha) is False


# ---------------------------------------------------------------------------
# verify_commit_signature
# ---------------------------------------------------------------------------


class TestVerifyCommitSignature:
    def test_unsigned_commit_returns_false(self, git_repo: Path) -> None:
        _make_staged_change(git_repo)
        provenance = CommitProvenance(agent_id="a", task_id="t", run_id="r")
        sign_and_commit(git_repo, "feat: unsigned", provenance)
        verified, _detail = verify_commit_signature(git_repo)
        # Unsigned commits should fail verification
        assert verified is False

    def test_bad_ref_returns_false(self, git_repo: Path) -> None:
        verified, detail = verify_commit_signature(git_repo, "nonexistent-sha")
        assert verified is False
        assert detail  # some error message
