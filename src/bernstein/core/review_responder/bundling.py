"""Quiet-window bundler that turns a stream of comments into rounds.

A round is sealed once no new comment for the same ``(repo, pr)`` has
arrived inside :attr:`ResponderConfig.quiet_window_s` seconds.  Once
sealed, the round is handed to :class:`ReviewResponder.run_round`.

The bundler is intentionally clock-injectable so tests can drive it
deterministically — :meth:`drain` accepts a ``now`` argument and the
constructor accepts a ``clock`` callable.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bernstein.core.review_responder.models import ReviewRound

if TYPE_CHECKING:
    from collections.abc import Callable

    from bernstein.core.review_responder.models import (
        ResponderConfig,
        ReviewComment,
    )


@dataclass
class _PendingBundle:
    """Internal mutable state for a not-yet-sealed bundle.

    Attributes:
        repo: Repository slug.
        pr_number: PR number.
        comments: List of comments collected so far.
        opened_at: Unix timestamp of the first comment.
        last_at: Unix timestamp of the most recent comment.
    """

    repo: str
    pr_number: int
    comments: list[ReviewComment] = field(default_factory=list)
    opened_at: float = 0.0
    last_at: float = 0.0


@dataclass
class RoundBundler:
    """Group incoming comments by ``(repo, pr_number)`` into round bundles.

    Args:
        config: Responder configuration; only :attr:`quiet_window_s` and
            :attr:`max_comments_per_round` are read.
        clock: Function returning the current monotonic-ish wall clock.
            Tests inject a fake here to control round sealing.
    """

    config: ResponderConfig
    clock: Callable[[], float] = field(default=time.time)
    _bundles: dict[tuple[str, int], _PendingBundle] = field(
        default_factory=dict[tuple[str, int], _PendingBundle], init=False, repr=False
    )

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def add(self, comment: ReviewComment) -> None:
        """Admit ``comment`` into its bundle, opening a new one if needed.

        Args:
            comment: Already-deduplicated, already-normalised comment.
        """
        key = (comment.repo, comment.pr_number)
        bundle = self._bundles.get(key)
        now = self.clock()
        if bundle is None:
            bundle = _PendingBundle(repo=comment.repo, pr_number=comment.pr_number)
            bundle.opened_at = now
            self._bundles[key] = bundle
        bundle.comments.append(comment)
        bundle.last_at = now

    # ------------------------------------------------------------------
    # Query / drain
    # ------------------------------------------------------------------

    def pending_keys(self) -> list[tuple[str, int]]:
        """Return ``(repo, pr_number)`` keys with at least one comment buffered."""
        return [k for k, b in self._bundles.items() if b.comments]

    def drain(self, *, now: float | None = None, force: bool = False) -> list[ReviewRound]:
        """Seal every bundle whose quiet window has elapsed.

        Args:
            now: Wall-clock to compare against.  Defaults to ``self.clock()``.
            force: When ``True``, seal every non-empty bundle regardless of
                the quiet-window — used at shutdown / drain-mode.

        Returns:
            List of sealed :class:`ReviewRound` instances, ordered by
            ``opened_at``.  Bundles that exceed
            :attr:`ResponderConfig.max_comments_per_round` are split into
            consecutive rounds so each round stays bounded.
        """
        cutoff = now if now is not None else self.clock()
        sealed: list[ReviewRound] = []
        for key in list(self._bundles.keys()):
            bundle = self._bundles[key]
            if not bundle.comments:
                self._bundles.pop(key, None)
                continue
            elapsed = cutoff - bundle.last_at
            if not force and elapsed < self.config.quiet_window_s:
                continue
            sealed.extend(self._seal(bundle, sealed_at=cutoff))
            self._bundles.pop(key, None)
        sealed.sort(key=lambda r: r.opened_at)
        return sealed

    def _seal(self, bundle: _PendingBundle, *, sealed_at: float) -> list[ReviewRound]:
        """Convert ``bundle`` into one or more :class:`ReviewRound`.

        Args:
            bundle: The pending bundle to consume.
            sealed_at: Wall-clock timestamp for ``ReviewRound.sealed_at``.

        Returns:
            List of sealed rounds (always non-empty when the bundle has
            at least one comment).
        """
        cap = max(1, self.config.max_comments_per_round)
        chunks: list[list[ReviewComment]] = [bundle.comments[i : i + cap] for i in range(0, len(bundle.comments), cap)]
        rounds: list[ReviewRound] = []
        for chunk in chunks:
            rounds.append(
                ReviewRound(
                    round_id=f"rnd-{uuid.uuid4().hex[:12]}",
                    repo=bundle.repo,
                    pr_number=bundle.pr_number,
                    comments=tuple(chunk),
                    opened_at=bundle.opened_at,
                    sealed_at=sealed_at,
                )
            )
        return rounds
