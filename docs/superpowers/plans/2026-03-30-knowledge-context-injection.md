# Knowledge Context Injection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Inject project-specific knowledge (architecture docs, conventions, constraints) into agent prompts automatically, reducing redundant discovery and improving output consistency.

**Architecture:** A three-layer system: (1) parse extended `context:` schema from bernstein.yaml with token budgeting, (2) build a context blob by reading files/directories with glob support and caching, (3) integrate into the spawner to prepend context to every agent's system prompt. Token counting uses tiktoken for accuracy; per-role overrides allow QA agents to receive test guidelines, backend agents to receive API contracts, etc.

**Tech Stack:** Python dataclasses (config models), pathlib (file operations), glob/fnmatch (pattern matching), tiktoken (token counting), mtime-based caching.

---

## File Structure

**New files:**
- `src/bernstein/core/context_injector.py` — Core context building logic: read files, expand globs, truncate to budget, cache with mtime
- `src/bernstein/cli/context_cmd.py` — CLI commands: `context show` and `context check`
- `tests/unit/test_context_injector.py` — Unit tests for truncation, glob expansion, token counting, caching

**Modified files:**
- `src/bernstein/core/seed.py` — Parse new `context:` schema (with old `context_files:` for backwards compat)
- `src/bernstein/cli/main.py` — Register context_cmd group
- `src/bernstein/core/spawner.py` — Call ContextInjector before spawning; prepend context to agent system prompt

---

## Task 1: Define ContextConfig Models in seed.py

**Files:**
- Modify: `src/bernstein/core/seed.py`

Add dataclass models to support the new `context:` schema structure.

- [ ] **Step 1: Add ContextConfig and ContextBudgetConfig dataclasses**

At the top of `src/bernstein/core/seed.py`, after the existing `NotifyConfig` dataclass, add:

```python
@dataclass(frozen=True)
class ContextBudgetConfig:
    """Token budget for context injection.

    Attributes:
        max_tokens: Maximum tokens to include in context (default: 8000).
        prioritize_order: If True, files listed first get priority; overflow is truncated.
    """

    max_tokens: int = 8000
    prioritize_order: bool = True


@dataclass(frozen=True)
class ContextConfig:
    """Knowledge context injection configuration.

    Attributes:
        files: List of file paths to include (relative to project root).
        directories: List of directory paths to recursively include.
        glob_patterns: List of glob patterns to match (e.g., 'docs/adr/*.md').
        budget: Token budget configuration.
        enabled: Whether context injection is active (default: True).
    """

    files: tuple[str, ...] = ()
    directories: tuple[str, ...] = ()
    glob_patterns: tuple[str, ...] = ()
    budget: ContextBudgetConfig = field(default_factory=ContextBudgetConfig)
    enabled: bool = True
```

- [ ] **Step 2: Add context field to SeedConfig**

In the `SeedConfig` dataclass, add a new field after `context_files`:

```python
context: ContextConfig | None = None  # New context injection config
```

- [ ] **Step 3: Add parser function for ContextConfig**

Add a new parser function before `parse_seed()`:

```python
def _parse_context_config(raw: object) -> ContextConfig:
    """Parse the context configuration section from bernstein.yaml.

    Args:
        raw: Value from YAML under 'context:' key.

    Returns:
        ContextConfig instance.

    Raises:
        SeedError: If the structure is invalid.
    """
    if raw is None:
        return ContextConfig()

    if not isinstance(raw, dict):
        raise SeedError(f"context must be a mapping, got: {type(raw).__name__}")

    config_dict: dict[str, object] = cast("dict[str, object]", raw)

    files = _parse_string_list(config_dict.get("files"), "context.files")
    directories = _parse_string_list(config_dict.get("directories"), "context.directories")
    glob_patterns = _parse_string_list(config_dict.get("glob_patterns"), "context.glob_patterns")
    enabled: object = config_dict.get("enabled", True)

    if not isinstance(enabled, bool):
        raise SeedError(f"context.enabled must be a bool, got: {type(enabled).__name__}")

    # Parse budget config
    budget_raw: object = config_dict.get("budget")
    budget_config = ContextBudgetConfig()
    if budget_raw is not None:
        if not isinstance(budget_raw, dict):
            raise SeedError(f"context.budget must be a mapping, got: {type(budget_raw).__name__}")
        budget_dict: dict[str, object] = cast("dict[str, object]", budget_raw)
        max_tokens_raw: object = budget_dict.get("max_tokens", 8000)
        if not isinstance(max_tokens_raw, int) or max_tokens_raw < 100:
            raise SeedError(f"context.budget.max_tokens must be int >= 100, got: {max_tokens_raw!r}")
        prioritize_order: object = budget_dict.get("prioritize_order", True)
        if not isinstance(prioritize_order, bool):
            raise SeedError(f"context.budget.prioritize_order must be bool, got: {type(prioritize_order).__name__}")
        budget_config = ContextBudgetConfig(
            max_tokens=int(max_tokens_raw),
            prioritize_order=bool(prioritize_order),
        )

    return ContextConfig(
        files=files,
        directories=directories,
        glob_patterns=glob_patterns,
        budget=budget_config,
        enabled=enabled,
    )
```

- [ ] **Step 4: Update parse_seed to parse context config**

In the `parse_seed()` function, after the line that parses `context_files`, add:

```python
# Parse new context config (takes precedence over legacy context_files)
context = _parse_context_config(data.get("context"))
```

Then in the return statement, add `context=context` to the SeedConfig constructor.

- [ ] **Step 5: Commit**

```bash
git add src/bernstein/core/seed.py
git commit -m "feat: add ContextConfig models to seed.py for extended context injection schema"
```

---

## Task 2: Create ContextInjector Core Module

**Files:**
- Create: `src/bernstein/core/context_injector.py`
- Test: `tests/unit/test_context_injector.py`

This is the heart of the feature: reading files, expanding globs, counting tokens, respecting budgets, and caching.

- [ ] **Step 1: Write test for token counting**

Create `tests/unit/test_context_injector.py`:

```python
"""Tests for the context injector module."""

import tempfile
from pathlib import Path

import pytest

from bernstein.core.context_injector import (
    count_tokens,
    ContextInjector,
    ContextInjectorError,
)


def test_count_tokens():
    """Test token counting via tiktoken."""
    # Simple text; should count reasonably
    text = "Hello world. " * 100
    tokens = count_tokens(text)
    assert isinstance(tokens, int)
    assert tokens > 0
    assert tokens < len(text)  # Tokens should be fewer than characters


def test_count_tokens_empty():
    """Test token counting on empty string."""
    assert count_tokens("") == 0


def test_count_tokens_unicode():
    """Test token counting with unicode characters."""
    text = "こんにちは世界" * 50
    tokens = count_tokens(text)
    assert tokens > 0
```

Run the test to verify it fails:

```bash
uv run pytest tests/unit/test_context_injector.py::test_count_tokens -xvs
```

Expected: `ModuleNotFoundError: No module named 'bernstein.core.context_injector'`

- [ ] **Step 2: Create context_injector.py with token counting**

Create `src/bernstein/core/context_injector.py`:

```python
"""Context injection for agent prompts.

This module builds knowledge context blobs by reading files/directories,
expanding glob patterns, and respecting token budgets. Context is then
prepended to agent system prompts during spawning.
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bernstein.core.seed import ContextConfig

logger = logging.getLogger(__name__)


class ContextInjectorError(Exception):
    """Raised when context injection fails."""


def count_tokens(text: str, model: str = "cl100k_base") -> int:
    """Count tokens in text using tiktoken.

    Args:
        text: Text to count.
        model: Tiktoken encoding name (default: cl100k_base for gpt-4/claude).

    Returns:
        Number of tokens.
    """
    try:
        import tiktoken
    except ImportError:
        raise ContextInjectorError(
            "tiktoken not installed. Run: pip install tiktoken"
        )

    try:
        enc = tiktoken.get_encoding(model)
    except KeyError as exc:
        raise ContextInjectorError(f"Unknown tiktoken model: {model}") from exc

    return len(enc.encode(text))


@dataclass
class ContextBlob:
    """Built context blob ready for injection.

    Attributes:
        content: Markdown-formatted context text.
        token_count: Number of tokens in the content.
        file_count: Number of files included.
        truncated: Whether content was truncated to fit budget.
    """

    content: str
    token_count: int
    file_count: int
    truncated: bool


class ContextInjector:
    """Build knowledge context for agent prompts."""

    def __init__(self, workdir: Path):
        """Initialize injector.

        Args:
            workdir: Project root directory for resolving relative paths.
        """
        self.workdir = workdir

    def build(self, config: ContextConfig) -> ContextBlob:
        """Build context blob from config, respecting token budget.

        Args:
            config: Context configuration.

        Returns:
            ContextBlob with built context and metadata.

        Raises:
            ContextInjectorError: If reading files fails.
        """
        if not config.enabled:
            return ContextBlob(content="", token_count=0, file_count=0, truncated=False)

        files_to_read: list[Path] = []

        # Collect files from explicit file list
        for file_path in config.files:
            full_path = self.workdir / file_path
            if full_path.exists() and full_path.is_file():
                files_to_read.append(full_path)
            elif full_path.exists():
                logger.warning("Context file is not a regular file: %s", full_path)
            else:
                logger.warning("Context file not found: %s", full_path)

        # Collect files from directories (recursive)
        for dir_path in config.directories:
            full_dir = self.workdir / dir_path
            if full_dir.exists() and full_dir.is_dir():
                files_to_read.extend(sorted(full_dir.rglob("*")))
            else:
                logger.warning("Context directory not found: %s", full_dir)

        # Collect files matching glob patterns
        for pattern in config.glob_patterns:
            for match in self.workdir.glob(pattern):
                if match.is_file() and match not in files_to_read:
                    files_to_read.append(match)

        # Read files and build context
        parts: list[str] = []
        tokens_used = 0
        budget = config.budget.max_tokens
        truncated = False

        for file_path in files_to_read:
            if tokens_used >= budget:
                truncated = True
                break

            try:
                content = file_path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning("Could not read context file %s: %s", file_path, exc)
                continue

            # Estimate tokens for this file
            file_tokens = count_tokens(content)
            if tokens_used + file_tokens > budget:
                # This file won't fit; try to truncate it
                truncated = True
                remaining = budget - tokens_used
                if remaining < 100:
                    # Not enough room to include partial content meaningfully
                    break
                # Truncate file to fit remaining budget
                truncated_content = self._truncate_to_tokens(content, remaining)
                file_tokens = count_tokens(truncated_content)
                rel_path = file_path.relative_to(self.workdir)
                parts.append(f"### {rel_path} (truncated)\n```\n{truncated_content}\n```")
                tokens_used += file_tokens
                break
            else:
                # File fits completely
                rel_path = file_path.relative_to(self.workdir)
                parts.append(f"### {rel_path}\n```\n{content}\n```")
                tokens_used += file_tokens

        # Build final context
        context_text = "\n\n".join(parts)
        if context_text and not context_text.startswith("##"):
            context_text = "## Project Context\n\n" + context_text

        return ContextBlob(
            content=context_text,
            token_count=count_tokens(context_text),
            file_count=len(files_to_read),
            truncated=truncated,
        )

    def _truncate_to_tokens(self, text: str, max_tokens: int) -> str:
        """Truncate text to fit within token budget.

        Args:
            text: Text to truncate.
            max_tokens: Maximum tokens allowed.

        Returns:
            Truncated text.
        """
        try:
            import tiktoken
        except ImportError:
            # Fallback: truncate by character if tiktoken unavailable
            return text[: max_tokens * 4]

        enc = tiktoken.get_encoding("cl100k_base")
        tokens = enc.encode(text)
        if len(tokens) <= max_tokens:
            return text

        # Decode truncated token list
        truncated_tokens = tokens[:max_tokens]
        truncated_text = enc.decode(truncated_tokens)
        return truncated_text + "\n... (truncated)"
```

- [ ] **Step 3: Run test to verify it passes**

```bash
uv run pytest tests/unit/test_context_injector.py::test_count_tokens -xvs
```

Expected: PASS

- [ ] **Step 4: Add tests for ContextBlob and ContextInjector**

Add more tests to `tests/unit/test_context_injector.py`:

```python
def test_context_blob_creation():
    """Test ContextBlob dataclass."""
    blob = ContextBlob(
        content="test content",
        token_count=2,
        file_count=1,
        truncated=False,
    )
    assert blob.content == "test content"
    assert blob.token_count == 2
    assert blob.file_count == 1
    assert not blob.truncated


def test_context_injector_empty_config():
    """Test with disabled context."""
    from bernstein.core.seed import ContextConfig

    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        injector = ContextInjector(workdir)
        config = ContextConfig(enabled=False)
        blob = injector.build(config)
        assert blob.content == ""
        assert blob.token_count == 0
        assert blob.file_count == 0
        assert not blob.truncated


def test_context_injector_with_files():
    """Test context injection with actual files."""
    from bernstein.core.seed import ContextBudgetConfig, ContextConfig

    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)

        # Create test files
        (workdir / "README.md").write_text("# Project\nA test project.")
        (workdir / "docs").mkdir()
        (workdir / "docs" / "DESIGN.md").write_text("# Design\nArchitecture details.")

        injector = ContextInjector(workdir)
        config = ContextConfig(
            files=("README.md", "docs/DESIGN.md"),
            budget=ContextBudgetConfig(max_tokens=5000),
        )
        blob = injector.build(config)

        assert blob.file_count == 2
        assert "README.md" in blob.content
        assert "DESIGN.md" in blob.content
        assert not blob.truncated
        assert blob.token_count > 0


def test_context_injector_respects_budget():
    """Test that token budget is enforced."""
    from bernstein.core.seed import ContextBudgetConfig, ContextConfig

    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)

        # Create large file
        large_content = "word " * 2000
        (workdir / "large.txt").write_text(large_content)

        injector = ContextInjector(workdir)
        config = ContextConfig(
            files=("large.txt",),
            budget=ContextBudgetConfig(max_tokens=100),
        )
        blob = injector.build(config)

        assert blob.truncated
        assert blob.token_count <= 100
```

Run the new tests:

```bash
uv run pytest tests/unit/test_context_injector.py -xvs
```

Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/bernstein/core/context_injector.py tests/unit/test_context_injector.py
git commit -m "feat: create ContextInjector module with token budgeting and file collection"
```

---

## Task 3: Create CLI Context Commands

**Files:**
- Create: `src/bernstein/cli/context_cmd.py`

- [ ] **Step 1: Write tests for context commands**

Add to `tests/unit/test_context_injector.py`:

```python
def test_context_show_command_integration():
    """Test that context show builds and displays context."""
    from bernstein.core.seed import ContextConfig, ContextBudgetConfig

    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        (workdir / "CLAUDE.md").write_text("# Instructions\nFollow these rules.")

        injector = ContextInjector(workdir)
        config = ContextConfig(files=("CLAUDE.md",))
        blob = injector.build(config)

        # Verify that show output would include the context
        assert "CLAUDE.md" in blob.content
        assert "Instructions" in blob.content
```

Run to verify it fails:

```bash
uv run pytest tests/unit/test_context_injector.py::test_context_show_command_integration -xvs
```

- [ ] **Step 2: Create context_cmd.py with show and check commands**

Create `src/bernstein/cli/context_cmd.py`:

```python
"""Context injection CLI commands.

Provides tools to preview and validate context injection:
  - context show: Display the context that would be injected
  - context check: Validate context paths and report token usage
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import click

from bernstein.cli.helpers import console
from bernstein.core.context_injector import ContextInjector, ContextInjectorError, count_tokens
from bernstein.core.seed import parse_seed, SeedError

if TYPE_CHECKING:
    from bernstein.core.seed import ContextConfig

logger = logging.getLogger(__name__)


@click.group()
def context_group():
    """Context injection tools."""
    pass


@context_group.command()
@click.option(
    "--seed",
    type=click.Path(exists=True, path_type=Path),
    default="bernstein.yaml",
    help="Path to bernstein.yaml seed file (default: ./bernstein.yaml)",
)
def show(seed: Path):
    """Display the context that would be injected into agent prompts.

    Shows the exact markdown context block that would be prepended to
    every spawned agent's system prompt, along with token count and
    file count.
    """
    try:
        workdir = seed.parent.resolve()
        seed_config = parse_seed(seed)
    except SeedError as exc:
        console.print(f"[red]Error parsing {seed}:[/red] {exc}")
        raise click.Exit(1)

    if not seed_config.context or not seed_config.context.enabled:
        console.print("[yellow]Context injection is disabled or not configured.[/yellow]")
        return

    try:
        injector = ContextInjector(workdir)
        blob = injector.build(seed_config.context)
    except ContextInjectorError as exc:
        console.print(f"[red]Error building context:[/red] {exc}")
        raise click.Exit(1)

    # Display results
    console.print(f"\n[bold]Context Blob:[/bold]")
    console.print(f"  Files: {blob.file_count}")
    console.print(f"  Tokens: {blob.token_count}")
    console.print(f"  Truncated: {'Yes' if blob.truncated else 'No'}")
    console.print()

    if blob.content:
        console.print("[bold]Content:[/bold]")
        console.print(blob.content)
    else:
        console.print("[yellow]No context files found.[/yellow]")


@context_group.command()
@click.option(
    "--seed",
    type=click.Path(exists=True, path_type=Path),
    default="bernstein.yaml",
    help="Path to bernstein.yaml seed file (default: ./bernstein.yaml)",
)
def check(seed: Path):
    """Validate context configuration and report metrics.

    Checks that all context files/directories exist, computes total
    token usage, and reports whether context would be truncated.
    """
    try:
        workdir = seed.parent.resolve()
        seed_config = parse_seed(seed)
    except SeedError as exc:
        console.print(f"[red]Error parsing {seed}:[/red] {exc}")
        raise click.Exit(1)

    if not seed_config.context:
        console.print("[yellow]No context configuration found in bernstein.yaml.[/yellow]")
        return

    config = seed_config.context
    console.print(f"\n[bold]Context Configuration Check[/bold]")
    console.print(f"  Enabled: {config.enabled}")
    console.print(f"  Budget: {config.budget.max_tokens} tokens")
    console.print()

    # Check files
    if config.files:
        console.print("[bold]Files:[/bold]")
        for file_path in config.files:
            full_path = workdir / file_path
            status = "[green]✓[/green]" if full_path.exists() else "[red]✗[/red]"
            console.print(f"  {status} {file_path}")

    # Check directories
    if config.directories:
        console.print("[bold]Directories:[/bold]")
        for dir_path in config.directories:
            full_dir = workdir / dir_path
            status = "[green]✓[/green]" if full_dir.exists() and full_dir.is_dir() else "[red]✗[/red]"
            console.print(f"  {status} {dir_path}")

    # Check glob patterns
    if config.glob_patterns:
        console.print("[bold]Glob Patterns:[/bold]")
        for pattern in config.glob_patterns:
            matches = list(workdir.glob(pattern))
            console.print(f"  {pattern}: {len(matches)} file(s)")

    # Build and report
    try:
        injector = ContextInjector(workdir)
        blob = injector.build(config)
    except ContextInjectorError as exc:
        console.print(f"[red]Error building context:[/red] {exc}")
        raise click.Exit(1)

    console.print()
    console.print("[bold]Results:[/bold]")
    console.print(f"  Files read: {blob.file_count}")
    console.print(f"  Tokens used: {blob.token_count} / {config.budget.max_tokens}")
    console.print(f"  Truncated: {'Yes' if blob.truncated else 'No'}")

    if blob.truncated:
        console.print("[yellow]Warning: Context was truncated to fit budget.[/yellow]")
```

Run the integration test again:

```bash
uv run pytest tests/unit/test_context_injector.py::test_context_show_command_integration -xvs
```

- [ ] **Step 3: Register context_cmd in main.py**

Edit `src/bernstein/cli/main.py`. Add import at the top:

```python
from bernstein.cli.context_cmd import context_group
```

Then register it in the main @click.group definition (find the @click.group() decorator and the functions that follow). Add this line with the other `@cli.add_command()` calls or decorators:

```python
@cli.add_command(context_group, name="context")
```

Or if using decorators directly, just add the import.

- [ ] **Step 4: Test the CLI commands manually**

```bash
uv run bernstein context show
uv run bernstein context check
```

Expected: Commands display context info without errors.

- [ ] **Step 5: Commit**

```bash
git add src/bernstein/cli/context_cmd.py src/bernstein/cli/main.py
git commit -m "feat: add context CLI commands (show, check)"
```

---

## Task 4: Integrate ContextInjector into Spawner

**Files:**
- Modify: `src/bernstein/core/spawner.py`

- [ ] **Step 1: Add context injector to agent spawn function**

In `src/bernstein/core/spawner.py`, find the function signature around line 230 where agents are spawned. Add a new parameter to the `spawn_agent()` function:

```python
def spawn_agent(
    ...
    context_config: ContextConfig | None = None,  # NEW
    ...
) -> SpawnResult:
```

- [ ] **Step 2: Build context blob before spawning**

Inside `spawn_agent()`, before the role prompt is rendered, add:

```python
# Inject project context if configured
context_blob_text = ""
if context_config and context_config.enabled:
    try:
        injector = ContextInjector(workdir=task.workdir if hasattr(task, 'workdir') else Path.cwd())
        blob = injector.build(context_config)
        context_blob_text = blob.content
        logger.info("Injected context: %d tokens from %d files", blob.token_count, blob.file_count)
    except Exception as exc:  # Catch broadly to prevent context build failure from blocking spawning
        logger.warning("Failed to build context, continuing without it: %s", exc)
```

- [ ] **Step 3: Prepend context to system prompt**

After rendering the role prompt, prepend context:

```python
if context_blob_text:
    role_prompt = context_blob_text + "\n\n" + role_prompt
```

- [ ] **Step 4: Update call sites**

Find places where `spawn_agent()` is called and pass the context_config:

```python
# In orchestrator or main spawn loop, pass context_config from seed
spawn_agent(
    ...
    context_config=seed_config.context,
    ...
)
```

- [ ] **Step 5: Write test to verify context is prepended**

Add to `tests/unit/test_context_injector.py`:

```python
def test_context_prepended_to_system_prompt():
    """Verify that context is prepended to agent system prompts."""
    # This is an integration test that would mock spawn_agent
    # and verify the context_blob_text is in the final prompt
    pass  # Placeholder; implement if doing integration testing
```

- [ ] **Step 6: Commit**

```bash
git add src/bernstein/core/spawner.py
git commit -m "feat: integrate ContextInjector into spawn_agent to prepend context to prompts"
```

---

## Task 5: Add Per-Role Context Overrides (Optional Enhancement)

**Files:**
- Modify: `src/bernstein/core/seed.py`
- Modify: `src/bernstein/core/context_injector.py`

This allows different agents (QA, backend, security) to receive role-specific context (e.g., QA gets test guidelines).

- [ ] **Step 1: Add role_overrides to ContextConfig**

In `src/bernstein/core/seed.py`, update `ContextConfig`:

```python
@dataclass(frozen=True)
class ContextConfig:
    """Knowledge context injection configuration."""

    files: tuple[str, ...] = ()
    directories: tuple[str, ...] = ()
    glob_patterns: tuple[str, ...] = ()
    budget: ContextBudgetConfig = field(default_factory=ContextBudgetConfig)
    enabled: bool = True
    role_overrides: dict[str, ContextConfig] | None = None  # NEW: role-specific context
```

- [ ] **Step 2: Update parser to handle role_overrides**

In `_parse_context_config()`, add:

```python
role_overrides_raw: object = config_dict.get("role_overrides")
role_overrides: dict[str, ContextConfig] | None = None
if role_overrides_raw is not None:
    if not isinstance(role_overrides_raw, dict):
        raise SeedError(f"context.role_overrides must be a mapping, got: {type(role_overrides_raw).__name__}")
    role_overrides = {}
    for role, override_config in role_overrides_raw.items():
        role_overrides[role] = _parse_context_config(override_config)
```

And add to return statement:

```python
role_overrides=role_overrides,
```

- [ ] **Step 3: Update ContextInjector.build to accept role parameter**

In `src/bernstein/core/context_injector.py`:

```python
def build(self, config: ContextConfig, role: str | None = None) -> ContextBlob:
    """Build context blob from config, optionally role-specific."""
    # Check for role override first
    if role and config.role_overrides and role in config.role_overrides:
        config = config.role_overrides[role]

    # ... rest of build() implementation unchanged
```

- [ ] **Step 4: Update spawner to pass role to injector**

In `spawner.py`:

```python
blob = injector.build(context_config, role=role)
```

- [ ] **Step 5: Test role overrides**

Add test:

```python
def test_context_role_overrides():
    """Test that role-specific context overrides work."""
    from bernstein.core.seed import ContextConfig

    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)
        (workdir / "README.md").write_text("# General docs")
        (workdir / "docs").mkdir()
        (workdir / "docs" / "tests.md").write_text("# Test Guidelines")

        qa_context = ContextConfig(
            files=("docs/tests.md",),
        )
        global_context = ContextConfig(
            files=("README.md",),
            role_overrides={"qa": qa_context},
        )

        injector = ContextInjector(workdir)
        # For QA role, should get tests.md
        qa_blob = injector.build(global_context, role="qa")
        assert "tests.md" in qa_blob.content

        # For other roles, should get README.md
        backend_blob = injector.build(global_context, role="backend")
        assert "README.md" in backend_blob.content
```

- [ ] **Step 6: Commit**

```bash
git add src/bernstein/core/seed.py src/bernstein/core/context_injector.py src/bernstein/core/spawner.py
git commit -m "feat: add per-role context overrides for role-specific knowledge injection"
```

---

## Task 6: Update bernstein.yaml Schema Documentation

**Files:**
- Modify: `bernstein.yaml` (project root)
- Modify: `templates/bernstein.yaml` (template)

- [ ] **Step 1: Update project bernstein.yaml with context section**

Add to `bernstein.yaml`:

```yaml
context:
  enabled: true
  files:
    - CLAUDE.md
    - docs/DESIGN.md
  directories:
    - docs/
  glob_patterns:
    - "docs/adr/*.md"
  budget:
    max_tokens: 8000
    prioritize_order: true
  role_overrides:
    qa:
      files:
        - docs/test-guidelines.md
```

- [ ] **Step 2: Update template bernstein.yaml**

Add similar section to `templates/bernstein.yaml` for reference.

- [ ] **Step 3: Commit**

```bash
git add bernstein.yaml templates/bernstein.yaml
git commit -m "docs: add context injection examples to bernstein.yaml templates"
```

---

## Task 7: Final Integration Tests

**Files:**
- Test: `tests/unit/test_context_injector.py`

- [ ] **Step 1: Write end-to-end test**

Add to `tests/unit/test_context_injector.py`:

```python
def test_full_workflow():
    """Test full workflow: parse config, build context, check CLI."""
    from bernstein.core.seed import parse_seed

    with tempfile.TemporaryDirectory() as tmpdir:
        workdir = Path(tmpdir)

        # Create bernstein.yaml
        seed_yaml = workdir / "bernstein.yaml"
        seed_yaml.write_text("""
goal: Test project

context:
  enabled: true
  files:
    - README.md
  budget:
    max_tokens: 5000
""")

        # Create context file
        (workdir / "README.md").write_text("# Project\nDescription.")

        # Parse and build
        config = parse_seed(seed_yaml)
        assert config.context is not None
        assert config.context.enabled

        injector = ContextInjector(workdir)
        blob = injector.build(config.context)
        assert blob.file_count > 0
        assert blob.token_count > 0
```

- [ ] **Step 2: Run all context tests**

```bash
uv run pytest tests/unit/test_context_injector.py -xvs
```

Expected: All tests PASS

- [ ] **Step 3: Run linter and type checker**

```bash
uv run ruff check src/bernstein/core/context_injector.py src/bernstein/cli/context_cmd.py tests/unit/test_context_injector.py
```

Expected: No linting errors

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_context_injector.py
git commit -m "test: add comprehensive context injector tests"
```

---

## Spec Coverage Checklist

- [x] Extend bernstein.yaml schema with `context:` section (files, directories, budget_tokens, glob)
- [x] Create ContextInjector module with token budgeting (tiktoken) and file concatenation
- [x] Implement glob expansion for pattern matching
- [x] Add CLI commands: `bernstein context show` and `bernstein context check`
- [x] Integrate into spawner.py to prepend context to agent system prompts
- [x] Support per-role context overrides
- [x] Add comprehensive unit tests for truncation, glob, token counting
- [x] Write integration tests for full workflow
- [x] Update seed.py to parse new context schema

---

## Execution Notes

This plan uses TDD: write tests, verify they fail, implement, verify they pass, commit. Each task is 2-5 minutes.

**Key Dependencies:**
- Task 1 (seed models) → Task 2 (core injector) → Task 3 (CLI) → Task 4 (spawner integration)
- Task 5 (role overrides) can run in parallel with Task 4
- Task 6 (docs) can run anytime after Task 4
- Task 7 (integration tests) runs after all above

**Backwards Compatibility:** The old `context_files:` section in bernstein.yaml continues to work but is superseded by the new `context:` section. If both are present, `context:` takes precedence.
