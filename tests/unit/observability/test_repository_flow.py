"""Tests for repository flow sampling.

Covers :func:`collect_repository_flow` and :class:`RepositoryFlowSample`
with fake clients that exercise edge cases around None values and empty lists.
"""

from __future__ import annotations

from bernstein.core.observability.repository_flow import (
    PRInfo,
    collect_repository_flow,
)


class _FakeClient:
    """Fake client for testing."""

    def __init__(
        self,
        open_prs: list[PRInfo] | None = None,
        merge_queue_depth: int | None = None,
        merged_prs: list[PRInfo] | None = None,
    ) -> None:
        self._open_prs = open_prs if open_prs is not None else []
        self._merge_queue_depth = merge_queue_depth
        self._merged_prs = merged_prs if merged_prs is not None else []

    def get_open_prs(self) -> list[PRInfo]:
        return self._open_prs

    def get_merge_queue_depth(self) -> int | None:
        return self._merge_queue_depth

    def get_merged_prs(self) -> list[PRInfo]:
        return self._merged_prs


def test_fully_populated_client() -> None:
    """A fake client that returns open_prs=[created_at=100], merge_queue_depth=5,
    merged_prs=[created_at=200, created_at=150], sampled with now=300.
    Assert open_pr_count=1, merge_queue_depth=5, newest_merged_pr_age_s=100.0 (300-200),
    oldest_open_pr_age_s=200.0 (300-100).
    """
    client = _FakeClient(
        open_prs=[PRInfo(created_at=100.0, merged_at=None)],
        merge_queue_depth=5,
        merged_prs=[
            PRInfo(created_at=200.0, merged_at=200.0),
            PRInfo(created_at=150.0, merged_at=150.0),
        ],
    )
    sample = collect_repository_flow(client, now=300.0)

    assert sample.open_pr_count == 1
    assert sample.merge_queue_depth == 5
    assert sample.newest_merged_pr_age_s == 100.0
    assert sample.oldest_open_pr_age_s == 200.0
    assert sample.sampled_at == 300.0


def test_merge_queue_depth_none() -> None:
    """A fake client whose get_merge_queue_depth returns None.
    collect_repository_flow must succeed and sample.merge_queue_depth must be None
    (assert exact None, not just falsy).
    """
    client = _FakeClient(
        open_prs=[],
        merge_queue_depth=None,
        merged_prs=[],
    )
    sample = collect_repository_flow(client, now=300.0)

    assert sample.merge_queue_depth is None


def test_no_merged_pr() -> None:
    """A fake client with merged_prs=[] and open_prs=[].
    Assert newest_merged_pr_age_s is None (assert exact None value).
    """
    client = _FakeClient(
        open_prs=[],
        merge_queue_depth=None,
        merged_prs=[],
    )
    sample = collect_repository_flow(client, now=300.0)

    assert sample.newest_merged_pr_age_s is None


def test_empty_repository() -> None:
    """A fake client returning empty lists and None for everything.
    Assert open_pr_count=0, merge_queue_depth=None, newest_merged_pr_age_s=None,
    oldest_open_pr_age_s=None.
    """
    client = _FakeClient(
        open_prs=[],
        merge_queue_depth=None,
        merged_prs=[],
    )
    sample = collect_repository_flow(client, now=300.0)

    assert sample.open_pr_count == 0
    assert sample.merge_queue_depth is None
    assert sample.newest_merged_pr_age_s is None
    assert sample.oldest_open_pr_age_s is None


def test_deterministic() -> None:
    """The same fake client and same now value, sampled twice.
    The two samples must compare equal (s1 == s2). Use a client with realistic data.
    """
    client = _FakeClient(
        open_prs=[PRInfo(created_at=50.0, merged_at=None)],
        merge_queue_depth=3,
        merged_prs=[PRInfo(created_at=100.0, merged_at=100.0)],
    )

    s1 = collect_repository_flow(client, now=200.0)
    s2 = collect_repository_flow(client, now=200.0)

    assert s1 == s2
