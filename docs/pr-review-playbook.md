# Pull Request Review Playbook

Standard process for reviewing and merging community pull requests.

## Steps

### 1. Triage (30 seconds)

- Read the PR title, body, and file list
- Check the author: first-time contributor? returning contributor?
- Check PR size: files changed, additions/deletions
- Check if it references an open issue (`Closes #NNN`)

### 2. Code Review (2-5 minutes)

Read the diff. Check for:

- **Correctness**: Does the code do what the PR says it does?
- **Tests**: Are there tests? Do they cover the main paths?
- **Style**: Follows project conventions? (frozen dataclasses, Google docstrings, no dict soup)
- **Security**: No hardcoded secrets, no command injection, no path traversal
- **Scope**: Does it stay within the issue scope? No unrelated changes?
- **Breaking changes**: Does it change any public API signatures?

### 3. Approve with Thank-You Comment

Write a review comment that:

1. **Thanks the contributor by name** (`@username`)
2. **Summarizes what you liked** (specific, not generic — mention the design decisions)
3. **Notes any non-blocking observations** (prefix with "Minor notes (non-blocking):")
4. **References the issue** if applicable (`Closes #NNN`)

Template:
```
[Specific praise about the implementation approach]

[1-2 sentences about what the code does well]

[Optional: Minor notes (non-blocking):
- observation 1
- observation 2]

Thanks @username!
```

### 4. Merge

- Use **merge commit** (not squash) to preserve contributor's commit history
- Use `--admin` flag if CI is slow or has pre-existing failures unrelated to the PR

### 5. Post-Merge

- **Close linked issues** with comment: "Implemented via PR #NNN. Merged to main."
- **Update CONTRIBUTORS.md**: Add the contributor with their PRs listed
- **Pull latest** to local: `git pull --rebase origin main`

## Quality Bar

### Auto-approve (merge immediately)

- Bug fixes with tests
- Documentation improvements
- New modules with tests that don't modify existing code
- CI/config improvements

### Request changes

- No tests for new functionality
- Modifies existing public APIs without backward compat
- Security concerns (injection, path traversal, hardcoded creds)
- Scope creep (PR does more than the issue asks)

### Close without merging

- Spam / AI-generated garbage with no real value
- Duplicate of existing functionality with no improvement
- Fundamentally wrong approach that can't be fixed with review feedback

## Anti-Patterns

- Don't leave PRs open for days without response — review within 24 hours
- Don't ask for trivial style changes that ruff/pyright would catch
- Don't block on SonarCloud quality gate failures caused by pre-existing issues
- Don't forget to update CONTRIBUTORS.md — recognition matters

## Commands Cheat Sheet

```bash
# List open PRs
gh pr list --repo chernistry/bernstein --state open

# View PR details
gh pr view NNN --repo chernistry/bernstein --json title,body,files,additions,deletions

# View diff
gh pr diff NNN --repo chernistry/bernstein

# Approve with comment
gh pr review NNN --repo chernistry/bernstein --approve --body "Message"

# Merge
gh pr merge NNN --repo chernistry/bernstein --merge --admin

# Close issue
gh issue close NNN --repo chernistry/bernstein --comment "Implemented via PR #NNN."
```
