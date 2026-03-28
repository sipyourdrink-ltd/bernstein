# 505 — One-command cross-platform installation

**Role:** devops
**Priority:** 2
**Scope:** small
**Complexity:** low
**Depends on:** [502]

## Problem
README says `git clone && uv pip install -e .` which is 2 commands and requires git + uv pre-installed. Not zero-friction.

## Implementation

### Primary: pipx / uv tool install (after PyPI publish)
```bash
# Recommended
pipx install bernstein

# or with uv (faster)
uv tool install bernstein
```
Both create isolated environments and put `bernstein` in PATH. Works on Mac/Linux/Windows with Python 3.12+.

### Why NOT curl|sh
`curl | sh` works for prebuilt native binaries (ruff, uv are Rust). Bernstein is Python — it needs a Python runtime. Building cross-platform binaries (PyInstaller) adds enormous CI complexity for marginal benefit. Our users are developers who have Python.

### README update
Replace current install section with:
```bash
pipx install bernstein    # recommended
# or: uv tool install bernstein
# or: pip install bernstein
```

### Runtime version check
Add Python version check at CLI entry:
```python
if sys.version_info < (3, 12):
    sys.exit("Bernstein requires Python 3.12+. You have {sys.version}")
```

## Files
- README.md — update install instructions
- src/bernstein/cli/main.py — add version check
- pyproject.toml — verify package name matches PyPI

## Completion signals
- file_contains: README.md :: pipx install
