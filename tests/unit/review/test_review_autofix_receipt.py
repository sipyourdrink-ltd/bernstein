"""Autofix finding-to-fix-to-test receipt tests (issue #2296, AC3).

Autofix spawns a worker in an isolated git worktree, runs the fix, runs the
gate, and emits a second receipt linking the reviewer finding to the fix
commit and the test result. These tests exercise the isolation boundary and
the receipt binding.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bernstein.core.review.receipt import (
    AutofixReceipt,
    Finding,
    load_or_create_review_identity,
    run_autofix_in_worktree,
    verify_autofix_receipt,
)

_KEY = b"0" * 32


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "x.py").write_text("def f():\n    leak()\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _identity(tmp_path: Path) -> tuple[str, str]:
    return load_or_create_review_identity(tmp_path / ".sdd" / "identity")


def test_autofix_runs_in_isolated_worktree(repo: Path, tmp_path: Path) -> None:
    priv, pub = _identity(tmp_path)
    finding = Finding(rule="BLE001", path="x.py", line=2, summary="broad leak")

    seen: dict[str, str] = {}

    def fix(worktree: Path) -> None:
        # The fix runs inside a checkout that is NOT the primary repo.
        assert worktree != repo
        seen["worktree"] = str(worktree)
        (worktree / "x.py").write_text("def f():\n    redact()\n", encoding="utf-8")

    def gate(worktree: Path) -> tuple[bool, str]:
        return True, "1 passed"

    receipt = run_autofix_in_worktree(
        repo=repo,
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        private_key_pem=priv,
        public_key_pem=pub,
        finding=finding,
        apply_fix=fix,
        run_gate=gate,
        task_id="task-1",
        timestamp=2000,
    )
    assert isinstance(receipt, AutofixReceipt)
    assert receipt.finding_hash
    assert receipt.fix_commit_hash
    assert receipt.gate_passed is True
    assert receipt.gate_summary == "1 passed"
    assert receipt.journal_entry_hash
    # The primary repo working tree was left untouched by the fix.
    assert (repo / "x.py").read_text(encoding="utf-8") == "def f():\n    leak()\n"


def test_autofix_receipt_verifies(repo: Path, tmp_path: Path) -> None:
    priv, pub = _identity(tmp_path)
    finding = Finding(rule="BLE001", path="x.py", line=2, summary="broad leak")

    def fix(worktree: Path) -> None:
        (worktree / "x.py").write_text("def f():\n    redact()\n", encoding="utf-8")

    def gate(worktree: Path) -> tuple[bool, str]:
        return True, "ok"

    receipt = run_autofix_in_worktree(
        repo=repo,
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        private_key_pem=priv,
        public_key_pem=pub,
        finding=finding,
        apply_fix=fix,
        run_gate=gate,
        task_id="task-1",
        timestamp=2000,
    )
    result = verify_autofix_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        finding=finding,
    )
    assert result.ok, result.reason
    assert result.receipt is not None
    assert result.receipt.fix_commit_hash == receipt.fix_commit_hash


def test_autofix_worktree_removed_after_run(repo: Path, tmp_path: Path) -> None:
    priv, pub = _identity(tmp_path)
    finding = Finding(rule="X", path="x.py", line=1, summary="s")
    captured: dict[str, Path] = {}

    def fix(worktree: Path) -> None:
        captured["wt"] = worktree
        (worktree / "x.py").write_text("fixed\n", encoding="utf-8")

    def gate(worktree: Path) -> tuple[bool, str]:
        return True, "ok"

    run_autofix_in_worktree(
        repo=repo,
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        private_key_pem=priv,
        public_key_pem=pub,
        finding=finding,
        apply_fix=fix,
        run_gate=gate,
        task_id="task-1",
        timestamp=2000,
    )
    # Isolation is torn down: the ephemeral worktree no longer exists.
    assert not captured["wt"].exists()


def test_autofix_records_gate_failure(repo: Path, tmp_path: Path) -> None:
    priv, pub = _identity(tmp_path)
    finding = Finding(rule="X", path="x.py", line=1, summary="s")

    def fix(worktree: Path) -> None:
        (worktree / "x.py").write_text("still broken\n", encoding="utf-8")

    def gate(worktree: Path) -> tuple[bool, str]:
        return False, "1 failed"

    receipt = run_autofix_in_worktree(
        repo=repo,
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        private_key_pem=priv,
        public_key_pem=pub,
        finding=finding,
        apply_fix=fix,
        run_gate=gate,
        task_id="task-1",
        timestamp=2000,
    )
    assert receipt.gate_passed is False
    assert receipt.gate_summary == "1 failed"
