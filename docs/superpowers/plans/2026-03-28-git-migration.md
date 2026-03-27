# Git Integration Migration Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate all remaining direct `subprocess.run(["git", ...])` calls in 4 files to use the centralized `git_ops.py` and `git_context.py` modules, then verify all completion signals pass.

**Architecture:** The project already has a well-implemented `git_ops.py` (write operations) and `git_context.py` (read operations) with 88 passing tests. Four files still bypass these modules with direct subprocess calls. Each migration is a mechanical replacement — the centralized functions already exist for every operation needed. After migration, we add tests to cover the new call paths and verify the `subprocess.run.*git` pattern is absent from migrated files.

**Tech Stack:** Python 3.12+, pytest, unittest.mock

---

### Task 1: Migrate `context.py` — replace 3 `git ls-files` subprocess calls

**Files:**
- Modify: `src/bernstein/core/context.py`
- Reference: `src/bernstein/core/git_context.py` (provides `ls_files`, `ls_files_pattern`)

This file has 3 direct `subprocess.run(["git", "ls-files", ...])` calls that should delegate to `git_context.ls_files` and `git_context.ls_files_pattern`.

- [ ] **Step 1: Read context.py and identify all 3 call sites**

The 3 calls are:
1. ~Line 82: `subprocess.run(["git", "ls-files"], ...)` in the file tree builder function
2. ~Line 589: `subprocess.run(["git", "ls-files", "*.py"], ...)` in the code index builder
3. ~Line 648+: `subprocess.run(["git", "ls-files", "*/__init__.py"], ...)` in `build_architecture_md`, and another `subprocess.run(["git", "ls-files", f"{pkg_dir}/*.py"], ...)` around line 672

- [ ] **Step 2: Add import for git_context at top of context.py**

Add after existing imports:
```python
from bernstein.core.git_context import ls_files, ls_files_pattern
```

- [ ] **Step 3: Replace call site 1 — file tree builder (~line 82)**

Replace:
```python
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            lines = result.stdout.strip().splitlines()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
```

With:
```python
    lines = ls_files(workdir)
```

- [ ] **Step 4: Replace call site 2 — code index builder (~line 589)**

Replace:
```python
    try:
        result = subprocess.run(
            ["git", "ls-files", "*.py"],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return index
        py_files = result.stdout.strip().splitlines()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return index
```

With:
```python
    py_files = ls_files_pattern(workdir, "*.py")
    if not py_files:
        return index
```

- [ ] **Step 5: Replace call site 3 — architecture builder `__init__.py` listing (~line 648)**

Replace:
```python
    try:
        result = subprocess.run(
            ["git", "ls-files", "*/__init__.py"],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return ""
        init_files = sorted(result.stdout.strip().splitlines())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
```

With:
```python
    init_files = sorted(ls_files_pattern(workdir, "*/__init__.py"))
    if not init_files:
        return ""
```

- [ ] **Step 6: Replace call site 4 — architecture builder per-package listing (~line 672)**

Replace:
```python
        try:
            result = subprocess.run(
                ["git", "ls-files", f"{pkg_dir}/*.py"],
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
```

With:
```python
        pkg_py_files = ls_files_pattern(workdir, f"{pkg_dir}/*.py")
        if pkg_py_files:
```

And adjust the subsequent code to use `pkg_py_files` instead of `result.stdout.strip().splitlines()`.

- [ ] **Step 7: Remove `import subprocess` from context.py if no other subprocess usage remains**

Search context.py for any remaining `subprocess.` references. If none remain, remove `import subprocess` from the imports.

- [ ] **Step 8: Verify no `subprocess.run.*git` in context.py**

Run: `grep -n "subprocess.run.*git" src/bernstein/core/context.py`
Expected: No output (zero matches)

- [ ] **Step 9: Run existing tests**

Run: `uv run python scripts/run_tests.py -x`
Expected: All tests pass

- [ ] **Step 10: Commit**

```bash
git add src/bernstein/core/context.py
git commit -m "refactor(core): migrate context.py git calls to git_context module

Replace 3 direct subprocess.run(['git', 'ls-files', ...]) calls with
git_context.ls_files() and git_context.ls_files_pattern() delegates.

Co-Authored-By: bernstein[bot] <noreply@bernstein.dev>"
```

---

### Task 2: Migrate `janitor.py` — replace 1 `git diff` subprocess call

**Files:**
- Modify: `src/bernstein/core/janitor.py`
- Reference: `src/bernstein/core/git_ops.py` (provides `diff_head`)

The janitor has one direct subprocess call in `_get_git_diff()` (~line 306-325) that gets a diff for judge review.

- [ ] **Step 1: Add import for git_ops.diff_head at top of janitor.py**

Add after existing imports:
```python
from bernstein.core.git_ops import diff_head
```

- [ ] **Step 2: Replace `_get_git_diff` implementation**

Replace the entire function body:
```python
def _get_git_diff(task: Task, workdir: Path) -> str:
    """Get git diff for the task's owned files, truncated for cost control."""
    try:
        cmd = ["git", "diff", "HEAD~1", "--"]
        if task.owned_files:
            cmd.extend(task.owned_files)
        result = subprocess.run(
            cmd,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=30,
        )
        diff = result.stdout.strip()
        if len(diff) > JUDGE_MAX_DIFF_CHARS:
            diff = diff[:JUDGE_MAX_DIFF_CHARS] + "\n... (truncated for cost cap)"
        return diff or "(no diff available)"
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Failed to get git diff: %s", exc)
        return "(failed to get git diff)"
```

With:
```python
def _get_git_diff(task: Task, workdir: Path) -> str:
    """Get git diff for the task's owned files, truncated for cost control."""
    try:
        diff = diff_head(workdir, files=task.owned_files or None)
        if len(diff) > JUDGE_MAX_DIFF_CHARS:
            diff = diff[:JUDGE_MAX_DIFF_CHARS] + "\n... (truncated for cost cap)"
        return diff or "(no diff available)"
    except (OSError, Exception) as exc:
        logger.warning("Failed to get git diff: %s", exc)
        return "(failed to get git diff)"
```

- [ ] **Step 3: Remove `import subprocess` from janitor.py if no other subprocess usage remains**

Search janitor.py for remaining `subprocess.` references. If none, remove the import.

- [ ] **Step 4: Verify no `subprocess.run.*git` in janitor.py**

Run: `grep -n "subprocess.run.*git" src/bernstein/core/janitor.py`
Expected: No output

- [ ] **Step 5: Run tests**

Run: `uv run python scripts/run_tests.py -x`
Expected: All tests pass

- [ ] **Step 6: Commit**

```bash
git add src/bernstein/core/janitor.py
git commit -m "refactor(core): migrate janitor.py git diff to git_ops.diff_head

Replace direct subprocess.run(['git', 'diff', ...]) with git_ops.diff_head()
delegate in _get_git_diff().

Co-Authored-By: bernstein[bot] <noreply@bernstein.dev>"
```

---

### Task 3: Migrate `upgrade_executor.py` — replace 6 git subprocess calls

**Files:**
- Modify: `src/bernstein/core/upgrade_executor.py`
- Reference: `src/bernstein/core/git_ops.py` (provides `is_git_repo`, `stage_files`, `commit`, `rev_parse_head`, `revert_commit`)

The upgrade executor has 6 direct subprocess calls across 2 methods: `_commit_changes()` and `_rollback_upgrade()`.

- [ ] **Step 1: Add imports from git_ops at top of upgrade_executor.py**

Add after existing imports:
```python
from bernstein.core.git_ops import (
    commit as git_commit,
    is_git_repo,
    rev_parse_head,
    revert_commit,
    stage_files,
)
```

- [ ] **Step 2: Replace `_commit_changes` method**

Replace the full method body (lines ~401-450):
```python
    async def _commit_changes(self, transaction: UpgradeTransaction) -> str | None:
        """Commit changes to git."""
        try:
            if not is_git_repo(self._workdir):
                logger.warning("Not a git repository, skipping commit")
                return None

            # Stage changes
            file_paths = [change.path for change in transaction.file_changes]
            stage_files(self._workdir, file_paths)

            # Commit
            commit_msg = f"Upgrade {transaction.id}: {transaction.title}\n\n{transaction.description}"
            result = git_commit(self._workdir, commit_msg)

            if result.ok:
                commit_hash = rev_parse_head(self._workdir)
                logger.info("Committed upgrade %s as %s", transaction.id, commit_hash)
                return commit_hash

            logger.warning("Git commit failed: %s", result.stderr)
            return None

        except Exception as exc:
            logger.warning("Git commit error: %s", exc)
            return None
```

- [ ] **Step 3: Replace `_rollback_upgrade` git section**

Replace the git revert block inside `_rollback_upgrade` (lines ~476-494):
```python
        if transaction.git_commit:
            try:
                result = subprocess.run(
                    ["git", "revert", "--no-commit", transaction.git_commit],
                    cwd=self._workdir,
                    capture_output=True,
                    text=True,
                )

                if result.returncode == 0:
                    subprocess.run(
                        ["git", "commit", "-m", f"Revert upgrade {transaction.id}"],
                        cwd=self._workdir,
                        capture_output=True,
                    )
                    transaction.status = UpgradeStatus.ROLLED_BACK
                    transaction.rolled_back_at = time.time()
                    logger.info("Upgrade %s rolled back successfully", transaction.id)
                    return
            except Exception as exc:
                logger.error("Git rollback failed: %s", exc)
```

With:
```python
        if transaction.git_commit:
            try:
                revert_r = revert_commit(self._workdir, transaction.git_commit)
                if revert_r.ok:
                    git_commit(self._workdir, f"Revert upgrade {transaction.id}")
                    transaction.status = UpgradeStatus.ROLLED_BACK
                    transaction.rolled_back_at = time.time()
                    logger.info("Upgrade %s rolled back successfully", transaction.id)
                    return
            except Exception as exc:
                logger.error("Git rollback failed: %s", exc)
```

- [ ] **Step 4: Remove `import subprocess` if no other subprocess usage remains**

Search upgrade_executor.py for remaining `subprocess.` references. If none, remove the import.

- [ ] **Step 5: Verify no `subprocess.run.*git` in upgrade_executor.py**

Run: `grep -n "subprocess.run.*git" src/bernstein/core/upgrade_executor.py`
Expected: No output

- [ ] **Step 6: Run tests**

Run: `uv run python scripts/run_tests.py -x`
Expected: All tests pass

- [ ] **Step 7: Commit**

```bash
git add src/bernstein/core/upgrade_executor.py
git commit -m "refactor(core): migrate upgrade_executor.py git calls to git_ops

Replace 6 direct subprocess.run(['git', ...]) calls in _commit_changes()
and _rollback_upgrade() with git_ops delegates.

Co-Authored-By: bernstein[bot] <noreply@bernstein.dev>"
```

---

### Task 4: Migrate `sandbox.py` — replace 4 git subprocess calls

**Files:**
- Modify: `src/bernstein/evolution/sandbox.py`
- Reference: `src/bernstein/core/git_ops.py` (provides `worktree_add`, `worktree_remove`, `apply_diff`, `branch_delete` — already imported!)

Sandbox.py already imports `worktree_add`, `worktree_remove`, `apply_diff`, `branch_delete` from git_ops (line 20-25) but doesn't use them everywhere. The `_run_in_worktree` method has a direct subprocess call for `git worktree add`, and `_create_worktree`, `_apply_diff`, `_cleanup_worktree` all use direct subprocess calls.

- [ ] **Step 1: Replace direct subprocess call in `_run_in_worktree` (~line 232)**

Replace:
```python
            result = subprocess.run(
                ["git", "worktree", "add", str(sandbox_dir), "-b", branch_name],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                return SandboxResult(
                    proposal_id=proposal.id,
                    passed=False,
                    ...
                    error=f"git worktree add failed: {result.stderr.strip()}",
                )
```

With:
```python
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
```

- [ ] **Step 2: Replace `_create_worktree` method (~line 310)**

Replace:
```python
    def _create_worktree(self, path: Path, branch_name: str) -> None:
        """Create a temporary git worktree."""
        subprocess.run(
            ["git", "worktree", "add", "-b", branch_name, str(path)],
            cwd=self.repo_root,
            capture_output=True,
            check=True,
            timeout=30,
        )
```

With:
```python
    def _create_worktree(self, path: Path, branch_name: str) -> None:
        """Create a temporary git worktree."""
        result = worktree_add(self.repo_root, path, branch_name)
        if not result.ok:
            raise subprocess.CalledProcessError(
                result.returncode, ["git", "worktree", "add"], stderr=result.stderr,
            )
```

- [ ] **Step 3: Replace `_apply_diff` method (~line 320)**

Replace:
```python
    def _apply_diff(self, worktree: Path, diff: str) -> None:
        """Apply a unified diff to the worktree."""
        if not diff.strip():
            return
        result = subprocess.run(
            ["git", "apply", "--allow-empty", "-"],
            cwd=worktree,
            input=diff,
            text=True,
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to apply diff: {result.stderr}")
```

With:
```python
    def _apply_diff(self, worktree: Path, diff: str) -> None:
        """Apply a unified diff to the worktree."""
        if not diff.strip():
            return
        result = git_apply_diff(worktree, diff)
        if not result.ok:
            raise RuntimeError(f"Failed to apply diff: {result.stderr}")
```

- [ ] **Step 4: Replace `_cleanup_worktree` method (~line 368)**

Replace:
```python
    def _cleanup_worktree(self, path: Path, branch_name: str) -> None:
        """Remove the temporary git worktree and its branch."""
        try:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(path)],
                cwd=self.repo_root,
                capture_output=True,
                timeout=30,
            )
        except Exception as exc:
            logger.warning("Failed to cleanup sandbox worktree %s: %s", path, exc)

        try:
            subprocess.run(
                ["git", "branch", "-D", branch_name],
                cwd=self.repo_root,
                capture_output=True,
                timeout=10,
            )
        except Exception as exc:
            logger.warning("Failed to delete sandbox branch %s: %s", branch_name, exc)
```

With:
```python
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
```

- [ ] **Step 5: Remove `import subprocess` from sandbox.py if no other subprocess usage remains**

Search sandbox.py for remaining `subprocess.` references. The `_run_tests` method uses `subprocess.run` for `uv run pytest` — that is NOT a git call and should keep subprocess imported. Also `_create_worktree` now raises `subprocess.CalledProcessError`, so the import stays.

Keep `import subprocess` — it's still needed for test execution and the CalledProcessError.

- [ ] **Step 6: Verify no `subprocess.run.*git` in sandbox.py**

Run: `grep -n 'subprocess.run.*"git"' src/bernstein/evolution/sandbox.py && grep -n "subprocess.run.*'git'" src/bernstein/evolution/sandbox.py`
Expected: No output

- [ ] **Step 7: Run tests**

Run: `uv run python scripts/run_tests.py -x`
Expected: All tests pass

- [ ] **Step 8: Commit**

```bash
git add src/bernstein/evolution/sandbox.py
git commit -m "refactor(evolution): migrate sandbox.py git calls to git_ops

Replace 4 direct subprocess.run(['git', ...]) calls in _run_in_worktree,
_create_worktree, _apply_diff, and _cleanup_worktree with git_ops delegates.

Co-Authored-By: bernstein[bot] <noreply@bernstein.dev>"
```

---

### Task 5: Verify all completion signals

**Files:**
- Read: `src/bernstein/core/git_ops.py`
- Read: `src/bernstein/core/git_context.py`
- Read: `src/bernstein/core/orchestrator.py`
- Test: `tests/unit/test_git_ops.py`
- Test: `tests/unit/test_git_context.py`

Run every completion signal from the task spec to confirm the work is done.

- [ ] **Step 1: Run test_git_ops.py**

Run: `uv run pytest tests/unit/test_git_ops.py -x -q`
Expected: All tests pass

- [ ] **Step 2: Run test_git_context.py**

Run: `uv run pytest tests/unit/test_git_context.py -x -q`
Expected: All tests pass

- [ ] **Step 3: Verify `conventional_commit` exists in git_ops.py**

Run: `grep -n "def conventional_commit" src/bernstein/core/git_ops.py`
Expected: Match at line 246

- [ ] **Step 4: Verify `safe_push` exists in git_ops.py**

Run: `grep -n "def safe_push" src/bernstein/core/git_ops.py`
Expected: Match at line 318

- [ ] **Step 5: Verify `bisect_regression` exists in git_ops.py**

Run: `grep -n "def bisect_regression" src/bernstein/core/git_ops.py`
Expected: Match at line 545

- [ ] **Step 6: Verify `blame_summary` exists in git_context.py**

Run: `grep -n "def blame_summary" src/bernstein/core/git_context.py`
Expected: Match at line 77

- [ ] **Step 7: Verify `hot_files` exists in git_context.py**

Run: `grep -n "def hot_files" src/bernstein/core/git_context.py`
Expected: Match at line 147

- [ ] **Step 8: Verify no `subprocess.run.*git` in orchestrator.py**

Run: `grep -c "subprocess.run.*git" src/bernstein/core/orchestrator.py`
Expected: 0 (or no output)

- [ ] **Step 9: Verify no `subprocess.run.*git` in ANY migrated file**

Run: `grep -rn 'subprocess.run.*\["git"' src/bernstein/core/context.py src/bernstein/core/janitor.py src/bernstein/core/upgrade_executor.py src/bernstein/evolution/sandbox.py src/bernstein/core/orchestrator.py`
Expected: No output

- [ ] **Step 10: Run full test suite**

Run: `uv run python scripts/run_tests.py -x`
Expected: All tests pass

- [ ] **Step 11: Mark task complete on task server**

```bash
curl -s -X POST http://127.0.0.1:8052/tasks/9c523fff5432/complete -H "Content-Type: application/json" -d '{"result_summary": "Completed: 414 — Modern git integration: branches, PRs, smart commits, context from history"}'
```
