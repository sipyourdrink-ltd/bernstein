FIXED: 0 of 0 blocking findings

--- VERDICT REFUTATION ---

All blocking findings in the verdict describe test failures in the broken state
*before* commits 41488dd/4ea2239/6cae968. Those commits are already in HEAD.

1. Repo hygiene (.sdd tracked) — REFUTED
   - CI check: `git ls-files '.sdd'` must be empty
   - Verified: `git ls-files '.sdd'` returns empty — no .sdd files are tracked
   - The .sdd directory in the worktree is harness state (bernstein.yaml, runtime/),
     not committed content

2. pyright errors — REFUTED (per verdict itself)
   - Verdict states: "21 errors, byte-identical to the same three files at origin/main"
   - Not introduced by this diff

3. Test failures in evolve dry-run — FIXED in diff
   - Commits 41488dd/4ea2239/6cae968 implement the fingerprint-based dedup
   - Verified: `uv run pytest tests/unit/cli/test_evolve_dry_run.py tests/unit/test_github.py tests/unit/test_runs_report.py` → 84 passed

4. _FINGERPRINT_LABEL_PREFIX / _short_fingerprint missing — FIXED in diff
   - Now exists in `src/bernstein/core/git/github.py`

5. find_by_fingerprint missing — FIXED in diff
   - Now exists in `src/bernstein/core/git/github.py`
   - Tested in `tests/unit/test_github.py::test_find_by_fingerprint_*`

6. create_issue fingerprint kwarg missing — FIXED in diff
   - `fingerprint: str | None = None` added to create_issue signature

--- CURRENT STATE ---

- Working tree clean: `git status` shows nothing to commit
- Ruff lint: all checks passed
- Ruff format: 4911 files already formatted
- MyPy: no issues in changed files
- Tests: 84 passed (11 evolve-dry-run, 45 github, 28 runs-report)
