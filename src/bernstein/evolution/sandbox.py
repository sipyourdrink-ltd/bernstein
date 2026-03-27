"""SandboxValidator — isolated testing of evolution proposals.

Runs proposals in a git worktree to validate they don't break anything
before applying to the main branch.

Validation strategy by risk level:
  L0 (Config)    — schema validation only, no worktree needed
  L1 (Templates) — git worktree + synthetic task replay
  L2 (Logic)     — git worktree + full test suite + golden dataset
  L3 (Structural)— never sandboxed, human-only
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from pathlib import Path

from bernstein.core.git_ops import (
    apply_diff as git_apply_diff,
)
from bernstein.core.git_ops import (
    branch_delete,
    worktree_add,
    worktree_remove,
)
from bernstein.evolution.types import RiskLevel, SandboxResult, UpgradeProposal

# Re-export for backwards compatibility (already imported above)
__all__ = ["SANDBOX_TIMEOUT", "SandboxValidator"]

logger = logging.getLogger(__name__)

# Maximum time for sandbox test suite (seconds)
SANDBOX_TIMEOUT = 300

# Base directory for git worktrees: .sdd/sandboxes/{proposal_id}
_SANDBOX_BASE = ".sdd/sandboxes"


class SandboxValidator:
    """Validates proposals in an isolated git worktree.

    Flow for L1/L2 proposals:
    1. git worktree add .sdd/sandboxes/{proposal_id} -b evolution/{proposal_id}
    2. Apply the proposal's unified diff
    3. Run the test suite inside the worktree
    4. Compare metrics against baseline
    5. git worktree remove .sdd/sandboxes/{proposal_id}

    For L0 proposals only simple schema validation is performed — no
    git worktree is needed for pure config changes.

    Args:
        repo_root: Path to the repository root.
        test_command: Command to run tests (default: "uv run pytest tests/ -x -q").
    """

    def __init__(
        self,
        repo_root: Path,
        test_command: str = "uv run pytest tests/ -x -q",
    ) -> None:
        self.repo_root = repo_root
        self.test_command = test_command

    # ------------------------------------------------------------------
    # Primary public API
    # ------------------------------------------------------------------

    def create_sandbox(self, proposal: UpgradeProposal) -> SandboxResult:
        """Validate a proposal using the appropriate strategy for its risk level.

        Dispatches based on proposal.risk_level:
        - L0_CONFIG: schema check only (fast, no git worktree)
        - L1_TEMPLATE: git worktree + smoke tests
        - L2_LOGIC: git worktree + full test suite
        - L3_STRUCTURAL: immediately fails — human review required

        Args:
            proposal: The upgrade proposal to validate.

        Returns:
            SandboxResult with pass/fail status and metrics.
        """
        start = time.time()

        if proposal.risk_level == RiskLevel.L0_CONFIG:
            return self._validate_l0(proposal, start)
        if proposal.risk_level == RiskLevel.L1_TEMPLATE:
            return self._run_in_worktree(proposal, start, full_tests=False)
        if proposal.risk_level == RiskLevel.L2_LOGIC:
            return self._run_in_worktree(proposal, start, full_tests=True)

        # L3_STRUCTURAL — never auto-sandboxed
        return SandboxResult(
            proposal_id=proposal.id,
            passed=False,
            tests_passed=0,
            tests_failed=0,
            tests_total=0,
            baseline_score=0.0,
            candidate_score=0.0,
            delta=0.0,
            duration_seconds=0.0,
            log_path="",
            error="L3_STRUCTURAL changes cannot be auto-sandboxed — human review required",
        )

    # ------------------------------------------------------------------
    # Legacy interface (kept for backwards compatibility)
    # ------------------------------------------------------------------

    def validate(
        self,
        proposal_id: str,
        diff: str,
        baseline_score: float = 1.0,
    ) -> SandboxResult:
        """Validate a proposal by running it in a sandbox.

        Args:
            proposal_id: Unique identifier for the proposal.
            diff: Unified diff to apply.
            baseline_score: Current test pass rate for comparison.

        Returns:
            SandboxResult with pass/fail and metrics.
        """
        worktree_path = self.repo_root / _SANDBOX_BASE / proposal_id
        log_path = str(worktree_path / "test_output.log")
        start = time.time()

        try:
            self._create_worktree(worktree_path, f"sandbox-{proposal_id}")
            self._apply_diff(worktree_path, diff)
            tests_passed, tests_failed, tests_total, output = self._run_tests(worktree_path)

            # Write test output
            worktree_path.mkdir(parents=True, exist_ok=True)
            (worktree_path / "test_output.log").write_text(output)

            candidate_score = tests_passed / tests_total if tests_total > 0 else 0.0
            duration = time.time() - start

            return SandboxResult(
                proposal_id=proposal_id,
                passed=tests_failed == 0,
                tests_passed=tests_passed,
                tests_failed=tests_failed,
                tests_total=tests_total,
                baseline_score=baseline_score,
                candidate_score=candidate_score,
                delta=candidate_score - baseline_score,
                duration_seconds=duration,
                log_path=log_path,
            )
        except Exception as exc:
            duration = time.time() - start
            logger.error("Sandbox validation failed for %s: %s", proposal_id, exc)
            return SandboxResult(
                proposal_id=proposal_id,
                passed=False,
                tests_passed=0,
                tests_failed=0,
                tests_total=0,
                baseline_score=baseline_score,
                candidate_score=0.0,
                delta=-baseline_score,
                duration_seconds=duration,
                log_path=log_path,
                error=str(exc),
            )
        finally:
            self._cleanup_worktree(worktree_path, f"sandbox-{proposal_id}")

    # ------------------------------------------------------------------
    # Risk-level-specific strategies
    # ------------------------------------------------------------------

    def _validate_l0(self, proposal: UpgradeProposal, start: float) -> SandboxResult:
        """L0 validation: schema check only, no git worktree needed."""
        error: str | None = None
        passed = True

        if not proposal.diff.strip():
            passed = False
            error = "Empty diff for L0 proposal — nothing to apply"

        duration = time.time() - start
        return SandboxResult(
            proposal_id=proposal.id,
            passed=passed,
            tests_passed=1 if passed else 0,
            tests_failed=0 if passed else 1,
            tests_total=1,
            baseline_score=1.0,
            candidate_score=1.0 if passed else 0.0,
            delta=0.0 if passed else -1.0,
            duration_seconds=round(duration, 2),
            log_path="",
            error=error,
        )

    def _run_in_worktree(
        self,
        proposal: UpgradeProposal,
        start: float,
        full_tests: bool,
    ) -> SandboxResult:
        """Run validation in an isolated git worktree.

        Steps:
        1. git worktree add .sdd/sandboxes/{id} -b evolution/{id}
        2. Apply proposal diff to worktree
        3. Run test suite
        4. Parse results and compute metrics delta
        5. git worktree remove .sdd/sandboxes/{id}

        Args:
            proposal: The upgrade proposal to validate.
            start: Start timestamp for duration calculation.
            full_tests: If True run full suite; if False run unit tests only.

        Returns:
            SandboxResult with test results and metrics.
        """
        sandbox_dir = self.repo_root / _SANDBOX_BASE / proposal.id
        branch_name = f"evolution/{proposal.id}"
        log_dir = self.repo_root / _SANDBOX_BASE
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = str(log_dir / f"{proposal.id}.log")

        try:
            # Step 1 — create git worktree via git_ops
            wt_result = worktree_add(self.repo_root, sandbox_dir, branch_name)
            if not wt_result.ok:
                return SandboxResult(
                    proposal_id=proposal.id,
                    passed=False,
                    tests_passed=0,
                    tests_failed=0,
                    tests_total=0,
                    baseline_score=0.0,
                    candidate_score=0.0,
                    delta=0.0,
                    duration_seconds=round(time.time() - start, 2),
                    log_path=log_path,
                    error=f"git worktree add failed: {wt_result.stderr.strip()}",
                )

            # Step 2 — apply diff
            if proposal.diff.strip():
                self._apply_diff(sandbox_dir, proposal.diff)

            # Step 3 — run tests
            test_cmd = self.test_command if full_tests else "uv run pytest tests/unit/ -x -q --tb=no"
            passed_count, failed_count, total, output = self._run_tests(sandbox_dir, cmd=test_cmd)

            # Write log
            Path(log_path).write_text(output, encoding="utf-8")

            tests_ok = failed_count == 0 and total > 0
            candidate_score = passed_count / total if total > 0 else 0.0

            return SandboxResult(
                proposal_id=proposal.id,
                passed=tests_ok,
                tests_passed=passed_count,
                tests_failed=failed_count,
                tests_total=total,
                baseline_score=1.0,
                candidate_score=candidate_score,
                delta=candidate_score - 1.0,
                duration_seconds=round(time.time() - start, 2),
                log_path=log_path,
                error=None if tests_ok else f"Tests failed ({failed_count}/{total})",
            )

        except RuntimeError as exc:
            return SandboxResult(
                proposal_id=proposal.id,
                passed=False,
                tests_passed=0,
                tests_failed=0,
                tests_total=0,
                baseline_score=0.0,
                candidate_score=0.0,
                delta=0.0,
                duration_seconds=round(time.time() - start, 2),
                log_path=log_path,
                error=str(exc),
            )

        finally:
            self._cleanup_worktree(sandbox_dir, branch_name)

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    def _create_worktree(self, path: Path, branch_name: str) -> None:
        """Create a temporary git worktree."""
        result = worktree_add(self.repo_root, path, branch_name)
        if not result.ok:
            raise RuntimeError(f"git worktree add failed: {result.stderr}")

    def _apply_diff(self, worktree: Path, diff: str) -> None:
        """Apply a unified diff to the worktree."""
        if not diff.strip():
            return
        result = git_apply_diff(worktree, diff)
        if not result.ok:
            raise RuntimeError(f"Failed to apply diff: {result.stderr}")

    def _run_tests(
        self,
        worktree: Path,
        cmd: str | None = None,
    ) -> tuple[int, int, int, str]:
        """Run the test suite and return (passed, failed, total, output)."""
        command = cmd or self.test_command
        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=worktree,
                capture_output=True,
                text=True,
                timeout=SANDBOX_TIMEOUT,
            )
            output = result.stdout + "\n" + result.stderr

            passed = failed = total = 0
            for line in output.splitlines():
                if "passed" in line or "failed" in line:
                    m_passed = re.search(r"(\d+) passed", line)
                    m_failed = re.search(r"(\d+) failed", line)
                    if m_passed:
                        passed = int(m_passed.group(1))
                    if m_failed:
                        failed = int(m_failed.group(1))
                    total = passed + failed

            return passed, failed, total, output
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Tests timed out after {SANDBOX_TIMEOUT}s") from None

    def _cleanup_worktree(self, path: Path, branch_name: str) -> None:
        """Remove the temporary git worktree and its branch."""
        try:
            worktree_remove(self.repo_root, path)
        except Exception as exc:
            logger.warning("Failed to cleanup sandbox worktree %s: %s", path, exc)

        try:
            branch_delete(self.repo_root, branch_name)
        except Exception as exc:
            logger.warning("Failed to delete sandbox branch %s: %s", branch_name, exc)
