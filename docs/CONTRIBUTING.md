# Contributing to Bernstein

Thank you for your interest in contributing to Bernstein! This guide will help you get started.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Forking the Repository](#forking-the-repository)
- [Setting Up Development Environment](#setting-up-development-environment)
- [Running Tests](#running-tests)
- [Code Style](#code-style)
- [Submitting a Pull Request](#submitting-a-pull-request)

## Prerequisites

- Python 3.12 or higher
- `uv` package manager (https://docs.astral.sh/uv/)
- Git

## Forking the Repository

1. Go to the Bernstein repository on GitHub
2. Click the "Fork" button in the top-right corner
3. Clone your fork locally:

```bash
git clone https://github.com/YOUR_USERNAME/bernstein.git
cd bernstein
```

## Setting Up Development Environment

### 1. Create Virtual Environment

```bash
uv venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
```

### 2. Install Dependencies

```bash
uv pip install -e ".[dev]"
```

This installs Bernstein in editable mode with development dependencies including:
- pytest (testing)
- ruff (linting)
- pyright (type checking)

### 3. Verify Installation

```bash
bernstein --help
```

## Running Tests

### Run All Tests

```bash
uv run python scripts/run_tests.py -x
```

**Important:** Never run `pytest tests/` directly - it can leak 100+ GB RAM across 2000+ tests. Always use the isolated runner in `scripts/run_tests.py`.

### Run Single Test File

```bash
uv run pytest tests/unit/test_foo.py -x -q
```

### Run Tests Matching Pattern

```bash
uv run python scripts/run_tests.py -k router
```

## Code Style

### Linting

```bash
uv run ruff check src/
uv run ruff format src/
```

### Type Checking

```bash
uv run pyright src/
```

All three must pass before committing. No exceptions.

### Coding Standards

- Python 3.12+, type hints on every public function
- Max line length: 120 (enforced by ruff)
- `from __future__ import annotations` at top of every module
- Ruff rules: E, F, W, I, UP, B, SIM, TCH, RUF
- **No dict soup** — use `@dataclass` or `TypedDict`, not raw `dict[str, Any]`
- Google-style docstrings on all public symbols

## Submitting a Pull Request

### 1. Create a Branch

```bash
git checkout -b feat/your-feature-name
```

Branch naming conventions:
- `feat/` for new features
- `fix/` for bug fixes
- `docs/` for documentation
- `test/` for tests
- `refactor/` for refactoring

### 2. Make Your Changes

- Follow the coding standards above
- Add tests for new functionality
- Update documentation as needed

### 3. Run Checks

Before committing, ensure all checks pass:

```bash
uv run ruff check src/
uv run pyright src/
uv run python scripts/run_tests.py -x
```

### 4. Commit Your Changes

```bash
git add <files>
git commit -m "feat: your concise description

Longer description if needed.

Fixes #123"
```

Commit message conventions:
- `feat:` for new features
- `fix:` for bug fixes
- `docs:` for documentation
- `test:` for tests
- `refactor:` for refactoring
- `chore:` for maintenance

### 5. Push and Create PR

```bash
git push origin feat/your-feature-name
```

Then go to GitHub and create a pull request from your branch to `main`.

### PR Requirements

- [ ] All tests pass
- [ ] Ruff check passes
- [ ] Pyright type checking passes
- [ ] Code follows style guide
- [ ] Tests added for new functionality
- [ ] Documentation updated

## Questions?

- Check existing issues on GitHub
- Read AGENTS.md for development guidelines
- Read CLAUDE.md for project overview

Thank you for contributing to Bernstein!
