"""Tests for fork_from_agent — byte-identical prefix for prompt cache sharing."""

from __future__ import annotations

from bernstein.core.spawn_prompt import (
    _FORK_DIRECTIVE_MARKER,
    fork_cache_key,
    fork_from_agent,
)


def _make_parent_prompt(role: str = "backend") -> str:
    """Build a realistic parent prompt with the task assignment marker."""
    prefix = (
        f"# {role.title()} Specialist\n\n"
        "You are a backend specialist working on the Bernstein project.\n\n"
        "## Project Context\n"
        "Bernstein is a multi-agent orchestration system.\n\n"
        "## Git Safety Protocol\n"
        "Never force-push. Always use your worktree branch.\n"
    )
    tasks = (
        "### Task 1: Implement feature X (id=abc123)\n"
        "Add the new feature X to the system.\n"
    )
    return f"{prefix}{_FORK_DIRECTIVE_MARKER}{tasks}"


def test_forked_agent_shares_prefix_cache_key() -> None:
    """Forked agent achieves cache hit on parent's prefix."""
    parent_prompt = _make_parent_prompt()
    forked_prompt = fork_from_agent(parent_prompt, "Review the code for quality issues.")

    assert fork_cache_key(parent_prompt) == fork_cache_key(forked_prompt)


def test_forked_prompt_contains_directive() -> None:
    """Forked prompt includes the new directive text."""
    parent_prompt = _make_parent_prompt()
    directive = "Run security audit on all changed files."
    forked = fork_from_agent(parent_prompt, directive)

    assert directive in forked
    assert "## Fork directive" in forked


def test_forked_prompt_does_not_contain_parent_tasks() -> None:
    """Forked prompt does not include the parent's task assignments."""
    parent_prompt = _make_parent_prompt()
    forked = fork_from_agent(parent_prompt, "Do something else.")

    assert "Implement feature X" not in forked
    assert "abc123" not in forked


def test_fork_prefix_is_byte_identical() -> None:
    """The prefix portion of the forked prompt is byte-identical to parent's prefix."""
    parent_prompt = _make_parent_prompt()
    forked = fork_from_agent(parent_prompt, "Quality gate check.")

    marker_pos = parent_prompt.find(_FORK_DIRECTIVE_MARKER)
    parent_prefix = parent_prompt[:marker_pos]

    # Forked prompt must start with the exact same bytes
    assert forked.startswith(parent_prefix)


def test_fork_without_marker_uses_full_prompt() -> None:
    """When no marker exists, the entire parent prompt is the prefix."""
    prompt_no_marker = "Simple prompt with no task marker."
    forked = fork_from_agent(prompt_no_marker, "New directive.")

    assert forked.startswith(prompt_no_marker)
    assert "New directive." in forked
    assert fork_cache_key(prompt_no_marker) == fork_cache_key(forked)


def test_fork_with_session_id_injects_signal_check() -> None:
    """When session_id is provided, signal check instructions are appended."""
    parent_prompt = _make_parent_prompt()
    forked = fork_from_agent(parent_prompt, "Review task.", session_id="qa-abc12345")

    # Signal check should reference the session's signal directory
    assert "qa-abc12345" in forked


def test_multiple_forks_share_same_cache_key() -> None:
    """Multiple forks from the same parent all share the same cache key."""
    parent_prompt = _make_parent_prompt()
    fork_a = fork_from_agent(parent_prompt, "Review code.")
    fork_b = fork_from_agent(parent_prompt, "Run security audit.")
    fork_c = fork_from_agent(parent_prompt, "Check test coverage.")

    key_parent = fork_cache_key(parent_prompt)
    key_a = fork_cache_key(fork_a)
    key_b = fork_cache_key(fork_b)
    key_c = fork_cache_key(fork_c)

    assert key_parent == key_a == key_b == key_c
