"""
bernstein-bench: leaderboard projection.

The leaderboard is a *fold of verified bundles*.  Only bundles that pass
``BenchVerifier.verify()`` appear here; every row links its bundle hash so
anyone can re-run ``bernstein bench verify <bundle>`` and reproduce the
number.

No score is listed that a third party cannot independently recompute.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Leaderboard entry
# ---------------------------------------------------------------------------


@dataclass
class LeaderboardEntry:
    bundle_hash: str
    suite_hash: str
    suite_version: str
    overall_score: float
    pass_rate: float
    num_tasks: int
    submitted_at: float
    signer_fingerprint: str = ""
    # Path to the bundle file (relative to the leaderboard root).
    bundle_path: str = ""

    def submitted_at_iso(self) -> str:
        import datetime

        return datetime.datetime.fromtimestamp(self.submitted_at, tz=datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def to_dict(self) -> dict[str, Any]:
        return {
            "bundle_hash": self.bundle_hash,
            "suite_hash": self.suite_hash,
            "suite_version": self.suite_version,
            "overall_score": self.overall_score,
            "pass_rate": self.pass_rate,
            "num_tasks": self.num_tasks,
            "submitted_at": self.submitted_at,
            "submitted_at_iso": self.submitted_at_iso(),
            "signer_fingerprint": self.signer_fingerprint,
            "bundle_path": self.bundle_path,
        }


# ---------------------------------------------------------------------------
# Leaderboard
# ---------------------------------------------------------------------------


@dataclass
class Leaderboard:
    """
    Ordered list of verified :class:`LeaderboardEntry` objects.

    Entries are sorted by ``overall_score`` descending; ties broken by
    ``submitted_at`` ascending (earlier submission wins).
    """

    suite_hash: str
    suite_version: str
    entries: list[LeaderboardEntry] = field(default_factory=list)

    def add_entry(self, entry: LeaderboardEntry) -> None:
        """Add *entry* and re-sort."""
        self.entries.append(entry)
        self.entries.sort(key=lambda e: (-e.overall_score, e.submitted_at))

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "suite_hash": self.suite_hash,
                    "suite_version": self.suite_version,
                    "generated_at": time.time(),
                    "entries": [e.to_dict() for e in self.entries],
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> Leaderboard:
        raw = json.loads(path.read_text(encoding="utf-8"))
        entries = [
            LeaderboardEntry(
                bundle_hash=e["bundle_hash"],
                suite_hash=e["suite_hash"],
                suite_version=e["suite_version"],
                overall_score=e["overall_score"],
                pass_rate=e["pass_rate"],
                num_tasks=e["num_tasks"],
                submitted_at=e["submitted_at"],
                signer_fingerprint=e.get("signer_fingerprint", ""),
                bundle_path=e.get("bundle_path", ""),
            )
            for e in raw.get("entries", [])
        ]
        return cls(
            suite_hash=raw["suite_hash"],
            suite_version=raw["suite_version"],
            entries=entries,
        )

    # ------------------------------------------------------------------
    # Markdown rendering
    # ------------------------------------------------------------------

    def to_markdown(self) -> str:
        """
        Generate a Markdown table for ``docs/eval/leaderboard.md``.

        Only verified bundles are listed (the caller is responsible for
        ensuring entries were added only after ``BenchVerifier.verify()``
        returned MATCH).
        """
        lines = [
            "# bernstein-bench leaderboard",
            "",
            "> Every row has passed `bernstein bench verify <bundle>`.  Click the bundle hash to re-verify.",
            "",
            f"Suite version: **{self.suite_version}**  ",
            f"Suite hash: `{self.suite_hash}`",
            "",
            "| Rank | Score | Pass rate | Tasks | Submitted | Bundle hash |",
            "|------|------:|----------:|------:|-----------|-------------|",
        ]

        for rank, entry in enumerate(self.entries, start=1):
            score_pct = f"{entry.overall_score * 100:.1f}%"
            pass_pct = f"{entry.pass_rate * 100:.1f}%"
            short_hash = entry.bundle_hash[:16]
            bundle_link = f"[`{short_hash}…`]({entry.bundle_path})" if entry.bundle_path else f"`{short_hash}…`"
            lines.append(
                f"| {rank} "
                f"| {score_pct} "
                f"| {pass_pct} "
                f"| {entry.num_tasks} "
                f"| {entry.submitted_at_iso()} "
                f"| {bundle_link} |"
            )

        lines += [
            "",
            "---",
            "",
            "## How to verify a row",
            "",
            "```bash",
            "# Download the bundle linked in the row, then:",
            "bernstein bench verify path/to/bundle.json",
            "```",
            "",
            "A MATCH result means the score was recomputed from the embedded "
            "run receipts and matched the claimed value exactly.",
        ]

        return "\n".join(lines)
