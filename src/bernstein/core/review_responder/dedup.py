"""File-backed dedup queue keyed by ``(comment_id, comment_updated_at)``.

The queue persists every accepted comment so a daemon restart cannot
re-process work the previous run already addressed.  Successful rounds
record the ``updated_at`` timestamp they handled; a later replay of the
same comment with the same timestamp is ignored, while an edited comment
(new ``updated_at``) re-enters the queue.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bernstein.core.review_responder.models import ReviewComment

DEFAULT_STATE_PATH = Path(".sdd/runtime/review_responder/dedup.json")


def _atomic_write(path: Path, data: str) -> None:
    """Write ``data`` atomically via a tempfile rename.

    Args:
        path: Destination file.
        data: Text contents to commit.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


@dataclass(frozen=True)
class DedupRecord:
    """A single dedup entry persisted to disk.

    Attributes:
        comment_id: GitHub comment id.
        updated_at: ISO timestamp of the comment when it was processed.
        outcome: One of the :class:`RoundOutcome` string values, recorded
            so audit/CLI can show why a comment was suppressed on replay.
        round_id: Round that consumed the comment, if any.
    """

    comment_id: int
    updated_at: str
    outcome: str = "queued"
    round_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict for JSON persistence."""
        return asdict(self)


@dataclass
class DedupQueue:
    """Persistent dedup queue.

    The queue is intentionally tiny — it stores at most one record per
    ``comment_id`` (the latest seen ``updated_at``).  Memory usage is
    proportional to the number of distinct comments observed, which on a
    real PR rarely exceeds a few dozen.

    Args:
        state_path: Override of the on-disk JSON file.  Tests use
            ``tmp_path`` to keep state isolated.
    """

    state_path: Path = field(default_factory=lambda: DEFAULT_STATE_PATH)
    _records: dict[int, DedupRecord] = field(default_factory=dict[int, DedupRecord], init=False, repr=False)

    def __post_init__(self) -> None:
        """Load persisted records on construction (no-op when missing)."""
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Populate :attr:`_records` from :attr:`state_path` if it exists."""
        if not self.state_path.exists():
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        items = raw.get("records") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                rec = DedupRecord(
                    comment_id=int(item["comment_id"]),
                    updated_at=str(item["updated_at"]),
                    outcome=str(item.get("outcome", "queued")),
                    round_id=str(item.get("round_id", "")),
                )
            except (KeyError, TypeError, ValueError):
                continue
            self._records[rec.comment_id] = rec

    def _save(self) -> None:
        """Persist :attr:`_records` atomically to :attr:`state_path`."""
        payload = {"records": [r.to_dict() for r in self._records.values()]}
        _atomic_write(self.state_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    def is_duplicate(self, comment: ReviewComment) -> bool:
        """Return ``True`` when this exact ``(id, updated_at)`` was seen before.

        Args:
            comment: Candidate comment from a listener.

        Returns:
            ``True`` if a previous record matches the dedup key, else
            ``False`` — including the "edited comment" case where the id
            is known but ``updated_at`` advanced.
        """
        rec = self._records.get(comment.comment_id)
        if rec is None:
            return False
        return rec.updated_at == comment.updated_at

    def offer(self, comment: ReviewComment) -> bool:
        """Insert / refresh a comment record.

        Args:
            comment: Comment to admit.

        Returns:
            ``True`` if the comment was newly admitted (or refreshed
            because it was edited), ``False`` when an exact replay was
            suppressed.
        """
        if self.is_duplicate(comment):
            return False
        self._records[comment.comment_id] = DedupRecord(
            comment_id=comment.comment_id,
            updated_at=comment.updated_at,
            outcome="queued",
        )
        self._save()
        return True

    def mark_outcome(
        self,
        comment_id: int,
        *,
        outcome: str,
        round_id: str = "",
    ) -> None:
        """Update the recorded outcome for a previously-admitted comment.

        Args:
            comment_id: The id whose record should be patched.
            outcome: New outcome string (mirrors :class:`RoundOutcome`).
            round_id: Round identifier the comment was consumed by.
        """
        rec = self._records.get(comment_id)
        if rec is None:
            return
        self._records[comment_id] = DedupRecord(
            comment_id=rec.comment_id,
            updated_at=rec.updated_at,
            outcome=outcome,
            round_id=round_id or rec.round_id,
        )
        self._save()

    def known(self, comment_id: int) -> DedupRecord | None:
        """Return the persisted record for ``comment_id`` if any."""
        return self._records.get(comment_id)

    def __len__(self) -> int:
        """Return the number of persisted records."""
        return len(self._records)
