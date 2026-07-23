"""Static guards for final Sonar style and API hygiene findings."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read_source(path: str) -> str:
    return ROOT.joinpath(path).read_text(encoding="utf-8")


def test_fingerprint_memoize_uses_pep695_generic_syntax() -> None:
    """The memoize decorator should not rely on module-level TypeVar boilerplate."""
    source = _read_source("src/bernstein/core/persistence/fingerprint.py")

    assert 'def memoize_persistent[F: Callable[..., Any]](store: MemoStore, *, site: str = "default")' in source
    assert "F = TypeVar" not in source
    assert "def decorator(fn: F)" not in source


def test_audit_recover_chain_tail_sorts_reverse_directly() -> None:
    """Avoid wrapping sorted log lists in reversed()."""
    source = _read_source("src/bernstein/core/security/audit.py")

    assert "sorted(self._audit_dir.glob(_JSONL_GLOB), reverse=True)" in source
    assert "reversed(live_files)" not in source


def test_prompt_caching_fifo_comment_is_not_comment_like_code() -> None:
    """FIFO eviction comments should not look like commented-out assignments."""
    source = _read_source("src/bernstein/core/tokens/prompt_caching.py")

    assert "# oldest = FIFO" not in source


def test_telegram_command_dispatch_uses_list_constructor_for_copy() -> None:
    """Copy-only comprehensions should use list()."""
    source = _read_source("src/bernstein/core/chat/drivers/telegram.py")

    assert "parts: list[str] = text.split()" in source
    assert "[p for p in text.split()]" not in source


def test_ranked_candidate_default_factory_uses_literal_constructor() -> None:
    """Empty dict defaults should use literal constructor syntax."""
    source = _read_source("src/bernstein/core/orchestration/multi_criteria_rank.py")

    assert "return {}" in source
    assert "field(default_factory=_empty_normalised_scores)" in source
    assert "field(default_factory=lambda: dict[str, float]())" not in source
