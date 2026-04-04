"""Context activation based on task file scope.

Defines ``ContextRule`` — a rule that maps .gitignore-style file path patterns to
a context string.  When a task is claimed, ``activate_context_for_task()`` matches
the task's ``owned_files`` against a list of rules and returns all matching context
blocks concatenated together.

This mirrors Claude Code's ``loadSkillsDir.ts`` pattern where skill paths are used
to determine which context to activate for a given task.

Design:
- Rules use ``fnmatch`` for glob matching (same as ``.gitignore`` glob semantics).
- A rule activates when **any** of its patterns matches **any** of the task's
  owned files.
- Multiple rules can activate simultaneously; their context blocks are joined.
- Rules with empty ``file_patterns`` always activate (they are "global" rules).

Example::

    rules = [
        ContextRule(
            file_patterns=("src/backend/**", "tests/unit/**"),
            context="This task touches backend code. Follow FastAPI conventions.",
            description="backend context",
        ),
    ]
    ctx = activate_context_for_task(["src/backend/server.py"], rules)
    # ctx == "This task touches backend code. Follow FastAPI conventions."

Builtin rules are defined in ``BUILTIN_CONTEXT_RULES`` and cover common
project layouts (backend, frontend, tests, infra, docs).
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ContextRule:
    """A rule that activates context when file patterns match.

    Attributes:
        file_patterns: .gitignore-style glob patterns matched against task
            ``owned_files``.  An empty tuple means the rule always activates.
        context: Context string to inject into the agent prompt when the rule
            activates.  Should be short and actionable (1-5 sentences).
        description: Human-readable rule description for logging and debugging.
    """

    file_patterns: tuple[str, ...] = ()
    context: str = ""
    description: str = ""


# ---------------------------------------------------------------------------
# Default built-in rules
# ---------------------------------------------------------------------------

#: Built-in context rules for common project layouts.
#: These activate context based on which files a task owns.
BUILTIN_CONTEXT_RULES: list[ContextRule] = [
    ContextRule(
        file_patterns=(
            "src/backend/**",
            "src/bernstein/**",
            "*.py",
            "**/*.py",
        ),
        context=(
            "This task modifies Python source files. "
            "Follow strict typing (Pyright strict), use Google-style docstrings, "
            "prefer dataclasses over plain dicts, and run ruff before committing."
        ),
        description="backend/Python context",
    ),
    ContextRule(
        file_patterns=(
            "tests/**",
            "test_*.py",
            "**/test_*.py",
            "**/*_test.py",
        ),
        context=(
            "This task modifies test files. "
            "Run `uv run python scripts/run_tests.py -x` to verify. "
            "Never run `uv run pytest tests/ -x -q` — it leaks memory across all tests."
        ),
        description="test context",
    ),
    ContextRule(
        file_patterns=(
            "src/frontend/**",
            "**/*.tsx",
            "**/*.ts",
            "**/*.jsx",
            "**/*.js",
            "package.json",
            "package-lock.json",
        ),
        context=(
            "This task modifies frontend code. "
            "Use TypeScript strict mode, follow component naming conventions, "
            "and run the frontend test suite before marking complete."
        ),
        description="frontend context",
    ),
    ContextRule(
        file_patterns=(
            ".github/**",
            "**/*.yml",
            "**/*.yaml",
            "Dockerfile*",
            "docker-compose*",
            "terraform/**",
            "infra/**",
        ),
        context=(
            "This task modifies infrastructure or CI/CD configuration. "
            "Test pipeline changes in a branch before merging. "
            "Never expose secrets — use environment variable references."
        ),
        description="infra/CI context",
    ),
    ContextRule(
        file_patterns=(
            "docs/**",
            "**/*.md",
            "**/*.rst",
            "README*",
        ),
        context=(
            "This task modifies documentation. "
            "Write like a senior engineer: no marketing-speak, no filler. "
            "Focus on what the code does and why, not how proud we are of it."
        ),
        description="docs context",
    ),
    ContextRule(
        file_patterns=(
            "pyproject.toml",
            "setup.py",
            "setup.cfg",
            "requirements*.txt",
            "*.toml",
        ),
        context=(
            "This task modifies project configuration or dependencies. "
            "Pin versions carefully, check for security advisories, "
            "and verify the lock file is updated."
        ),
        description="project config context",
    ),
]


# ---------------------------------------------------------------------------
# Core matching logic
# ---------------------------------------------------------------------------


def _rule_matches(rule: ContextRule, owned_files: list[str]) -> bool:
    """Check if a rule's file patterns match any of the owned files.

    Args:
        rule: The context rule to evaluate.
        owned_files: File paths owned by the task.

    Returns:
        True when the rule should activate: either it has no patterns (global
        rule) or at least one owned file matches at least one pattern.
    """
    if not rule.file_patterns:
        # No patterns = global rule, always activates.
        return True

    for filepath in owned_files:
        for pattern in rule.file_patterns:
            if fnmatch.fnmatch(filepath, pattern):
                return True
            # Also match against the basename alone for simple glob patterns.
            basename = filepath.rsplit("/", 1)[-1] if "/" in filepath else filepath
            if fnmatch.fnmatch(basename, pattern):
                return True

    return False


def activate_context_for_task(
    owned_files: list[str],
    rules: list[ContextRule] | None = None,
) -> str:
    """Return activated context strings for a task's owned files.

    Called at task claim time to determine which context blocks to inject
    into the agent spawn prompt.  Each rule whose file patterns match at least
    one owned file contributes its ``context`` string to the output.

    Args:
        owned_files: File paths owned by the task (from ``task.owned_files``).
        rules: Context rules to evaluate.  Defaults to ``BUILTIN_CONTEXT_RULES``.

    Returns:
        Newline-joined context strings from all matching rules, or an empty
        string when no rules match or no owned files are provided.
    """
    if rules is None:
        rules = BUILTIN_CONTEXT_RULES

    if not owned_files:
        return ""

    activated: list[str] = []
    for rule in rules:
        if _rule_matches(rule, owned_files):
            if rule.context:
                activated.append(rule.context)
                logger.debug(
                    "context_activation: activated rule %r for files %s",
                    rule.description,
                    owned_files[:3],
                )

    return "\n".join(activated)
