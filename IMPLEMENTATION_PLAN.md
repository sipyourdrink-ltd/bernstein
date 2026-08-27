# GitHub Issue Comment Thread Filtering Implementation

## Goal
Resolve GitHub issue #4516: Issue-seeded runs should include filtered comment threads in the goal, not just the opening body.

## Current State
- ✅ Added `build_filtered_comments_block()` function to `src/bernstein/core/volunteer/issue_sanitize.py`
- ✅ Added exports to `__all__` lists
- ❌ Integration point not yet identified/modified

## Required Changes

### 1. Find Integration Point
Issue seeding happens through seed files (YAML). The goal comes from `seed.goal`. Need to find:
- Where issue URLs are parsed in seed files
- Where goal string is built from a fetched GitHub issue
- Likely in `src/bernstein/core/config/seed_parser.py` or related

### 2. Implement Comment Fetching
Need to:
- Add function to fetch issue comments from GitHub API
- Use existing GitHub client pattern (e.g., from `issue_to_pr.py`)
- Integrate with `build_filtered_comments_block()` to get sanitized, filtered list
- Append to goal after issue body

### 3. Add CLI Flag
- Add `--no-issue-comments` flag to `src/bernstein/cli/run_bootstrap.py`
- Make it configurable in seed file (probably under `github:` section)

### 4. Add Tests
- Create `tests/unit/volunteer/test_issue_sanitize_thread.py`
- Test AC1: maintainer comment included (test_issue_sanitize.py line ~150 pattern)
- Test AC2: bernstein-context marker opt-in included; non-marked comments over budget dropped
- Test AC3: thread cap works correctly
- Test AC4: comments pass sanitizer (can't inject directives)
- Test AC5: --no-issue-comments disables feature

### 5. Verification
- Run `uv run pytest tests/unit/volunteer/test_issue_sanitize*.py -xvs`
- Run `uv run pytest tests/unit/test_nested_agents_context.py` (if modifying AGENTS.md)

## Architecture Notes
- Comments are already fetched in `issue_to_pr.py` using `list_issue_comments()` 
- Format: GitHub API returns list of dicts with: `user`, `created_at`, `body`, `author_association`
- Author associations: OWNER, MEMBER, COLLABORATOR, CONTRIBUTOR, NONE
- Sanitization: use existing `normalize_untrusted_text()` from issue_sanitize.py
- Token budget: ~2000 tokens default (configurable, using comment count for now)

## Files to Check/Modify
- src/bernstein/core/config/seed_parser.py (find issue URL parsing)
- src/bernstein/core/integrations/tickets/github_issues.py (may need to extend)
- src/bernstein/cli/run_bootstrap.py (add flag)
- tests/unit/volunteer/test_issue_sanitize_thread.py (new file)
