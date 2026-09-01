"""Repository flow sampling.

Captures a lightweight, point-in-time snapshot of a repository's pull-request
flow: how many PRs are open, the depth of the merge queue, and the age spread
of the merged and open PRs. This feeds observability surfaces (latency,
queue-pressure, staleness) without the consumer having to query the VCS API
itself.

Scope: :func:`collect_repository_flow` performs a single, synchronous read of
the repository client and returns one :class:`RepositoryFlowSample`. It does
no retries, no background work, and no caching -- callers that need retries or
pacing layer that on top.

Design decision -- store raw timestamps, not pre-computed ages
--------------------------------------------------------------
:class:`PRInfo` carries the raw ``created_at`` (and ``merged_at``) instants,
and the sample carries ``sampled_at``. Consumers compute ages as
``sampled_at - created_at`` at read time. This keeps the sample a faithful
record of the repository state at *one* instant:

* The raw values are preserved for the audit trail and for later re-analysis
  against a different reference instant.
* Slices 2 and 3 of this feature (metrics on flow trends, and dashboard
  rendering) read the same sample structure and need the raw timestamps to
  bucket and display them, so computing ages eagerly here would force them to
  reconstruct the originals.

The small cost -- each consumer recomputes the trivial subtraction -- is
deliberately accepted in exchange for storing the ground truth instead of a
derived value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RepositoryFlowSample:
    """Point-in-time snapshot of the repository pull-request flow.

    Attributes:
        open_pr_count: Number of currently-open pull requests.
        merge_queue_depth: Depth of the merge queue, if the repository exposes
            one; ``None`` when there is no queue signal.
        newest_merged_pr_age_s: Age in seconds, at ``sampled_at``, of the most
            recently merged pull request; ``None`` when nothing has merged.
        oldest_open_pr_age_s: Age in seconds, at ``sampled_at``, of the oldest
            currently-open pull request; ``None`` when there are no open PRs.
        sampled_at: The instant (unix seconds) this sample was taken. This is
            the value passed by the caller, never read from the clock.
    """

    open_pr_count: int
    merge_queue_depth: int | None
    newest_merged_pr_age_s: float | None
    oldest_open_pr_age_s: float | None
    sampled_at: float


@dataclass(frozen=True)
class PRInfo:
    """Minimal pull-request metadata used by flow sampling.

    Attributes:
        created_at: When the pull request was created (unix seconds). Raw,
            unadjusted for any sampling instant.
        merged_at: When the pull request was merged (unix seconds), or
            ``None`` if the PR is not merged.
    """

    created_at: float
    merged_at: float | None = None


class RepositoryFlowClient(Protocol):
    """Narrow read surface for repository flow.

    Only the methods :func:`collect_repository_flow` calls are declared here.
    Implementations commonly wrap the GitHub/VCS API, but the sampler depends
    on nothing beyond this protocol.
    """

    def get_open_prs(self) -> list[PRInfo]:
        """Return the currently-open pull requests."""
        ...

    def get_merge_queue_depth(self) -> int | None:
        """Return the merge-queue depth, or ``None`` if not exposed."""
        ...

    def get_merged_prs(self) -> list[PRInfo]:
        """Return the merged pull requests."""
        ...


def collect_repository_flow(client: RepositoryFlowClient, *, now: float) -> RepositoryFlowSample:
    """Collect a single repository-flow sample.

    One synchronous read of *client*: open PRs, merge-queue depth, and merged
    PRs. No retries and no background work. Ages are computed from the
    caller-provided *now*; the clock is never read here.

    Args:
        client: Repository client to sample.
        now: The sampling instant (unix seconds), supplied by the caller so
            the sample is attributable to a known reference time.

    Returns:
        A :class:`RepositoryFlowSample` describing repository flow at *now*.
    """
    open_prs = client.get_open_prs()
    merge_queue_depth = client.get_merge_queue_depth()
    merged_prs = client.get_merged_prs()

    newest_merged_pr_age_s: float | None = None
    if merged_prs:
        newest_merged_at = max(pr.merged_at for pr in merged_prs if pr.merged_at is not None)
        newest_merged_pr_age_s = now - newest_merged_at

    oldest_open_pr_age_s: float | None = None
    if open_prs:
        oldest_created_at = min(pr.created_at for pr in open_prs)
        oldest_open_pr_age_s = now - oldest_created_at

    return RepositoryFlowSample(
        open_pr_count=len(open_prs),
        merge_queue_depth=merge_queue_depth,
        newest_merged_pr_age_s=newest_merged_pr_age_s,
        oldest_open_pr_age_s=oldest_open_pr_age_s,
        sampled_at=now,
    )
