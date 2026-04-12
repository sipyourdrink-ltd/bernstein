"""Contextual tips system with cooldown for Bernstein CLI.

Provides a catalog of tips organised by category, random selection with a
10-minute cooldown so the same tip is not shown twice in rapid succession,
and a Rich-formatted display helper.

Tips are persisted in ``.sdd/tips/active.json`` and loaded from
``.sdd/tips/catalog.json`` on startup.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from rich.console import Console

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

COOLDOWN_SECONDS: int = 600  # 10 minutes

_DEFAULT_CATALOG_TIPS: list[dict[str, str]] = [
    {"category": "general", "tip": "Use `bernstein status` to get a quick overview of your run."},
    {"category": "general", "tip": "Run `bernstein agents list` to see which CLI agents are available."},
    {"category": "general", "tip": "Set BERNSTEIN_MAX_COST to enforce a spending cap on every run."},
    {"category": "productivity", "tip": "Small tasks finish faster — decompose goals before running."},
    {"category": "productivity", "tip": "Use git worktrees to isolate each agent session."},
    {"category": "productivity", "tip": "Review agent traces in .sdd/traces/ to understand decisions."},
    {"category": "troubleshooting", "tip": "Check `.sdd/runtime/logs/` for detailed orchestration logs."},
    {"category": "troubleshooting", "tip": "Use `bernstein doctor` to diagnose common configuration issues."},
    {"category": "troubleshooting", "tip": "Stuck tasks can be re-queued with `bernstein task retry <id>`."},
]


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class TipEntry:
    """A single tip with category and text.

    Attributes:
        category: Tip category (e.g. "general", "productivity").
        tip: The tip text.
    """

    category: str
    tip: str

    def to_dict(self) -> dict[str, str]:
        """Serialise to a JSON-safe dict."""
        return {"category": self.category, "tip": self.tip}

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> TipEntry:
        """Deserialise from a dict."""
        return cls(
            category=str(d.get("category", "general")),
            tip=str(d.get("tip", "")),
        )


@dataclass
class TipState:
    """Runtime state tracking last-seen tip timestamps for cooldown.

    Attributes:
        last_seen: Mapping of tip text to the Unix timestamp it was last shown.
    """

    last_seen: dict[str, float] = field(default_factory=dict[str, float])

    def to_dict(self) -> dict[str, dict[str, float]]:
        """Serialise to a JSON-safe dict."""
        return {"last_seen": dict(self.last_seen)}

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> TipState:
        """Deserialise from a dict."""
        raw = d.get("last_seen")
        if not isinstance(raw, dict):
            return cls()
        typed_last_seen = cast("dict[str, float]", raw)
        return cls(last_seen=typed_last_seen)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


class TipsCatalog:
    """Thread-unsafe tip catalog (sufficient for single-process CLI usage).

    Loads tips from a catalog JSON file on startup, provides random selection
    with cooldown, and persists active state to a separate JSON file.

    Args:
        catalog_path: Path to catalog.json (read on init).
        active_path: Path to active.json (read/write for cooldown state).
    """

    def __init__(
        self,
        catalog_path: Path | None = None,
        active_path: Path | None = None,
    ) -> None:
        self._catalog_path = catalog_path
        self._active_path = active_path
        self._tips: list[TipEntry] = []
        self._state = TipState()

        self._load_catalog()
        self._load_state()

    # -- Public API ------------------------------------------------------------

    def get_tip(self, category: str = "general", now: float | None = None) -> str | None:
        """Return a random tip for the category, respecting the cooldown.

        Each tip has a 10-minute cooldown after it is shown.  If every tip
        for the category is in cooldown, returns ``None``.

        Args:
            category: Tip category to filter on.
            now: Current Unix timestamp (defaults to ``time.time()``).

        Returns:
            A random tip string or ``None`` if all tips are cooling down.
        """
        if now is None:
            now = time.time()

        candidates = [t for t in self._tips if t.category == category]
        eligible = [
            t for t in candidates if (now - self._state.last_seen.get(t.tip, float("-inf"))) >= COOLDOWN_SECONDS
        ]
        if not eligible:
            return None
        chosen = random.choice(eligible)
        self._state.last_seen[chosen.tip] = now
        self._save_state()
        return chosen.tip

    def add_tip(self, category: str, tip: str) -> None:
        """Add a tip to the catalog and persist it.

        If the same (category, tip) pair already exists, it is a no-op.

        Args:
            category: Tip category.
            tip: Tip text.
        """
        entry = TipEntry(category=category, tip=tip)
        if entry in self._tips:
            return
        self._tips.append(entry)
        self._save_catalog()

    def get_all_tips(self, category: str | None = None) -> list[TipEntry]:
        """Return all tips, optionally filtered by category.

        Args:
            category: If given, only return tips in this category.

        Returns:
            List of matching TipEntry objects.
        """
        if category is None:
            return list(self._tips)
        return [t for t in self._tips if t.category == category]

    def get_categories(self) -> list[str]:
        """Return sorted list of unique categories in the catalog."""
        return sorted({t.category for t in self._tips})

    # -- Persistence -----------------------------------------------------------

    def _load_catalog(self) -> None:
        """Load tips from the catalog JSON file."""
        if self._catalog_path is None or not self._catalog_path.exists():
            self._tips = [TipEntry(category=d["category"], tip=d["tip"]) for d in _DEFAULT_CATALOG_TIPS]
            return
        raw = json.loads(self._catalog_path.read_text(encoding="utf-8"))
        self._tips = [TipEntry.from_dict(cast("dict[str, object]", d)) for d in raw if isinstance(d, dict)]

    def _save_catalog(self) -> None:
        """Persist tips to the catalog JSON file."""
        if self._catalog_path is None:
            return
        self._catalog_path.parent.mkdir(parents=True, exist_ok=True)
        data = [t.to_dict() for t in self._tips]
        self._catalog_path.write_text(
            json.dumps(data, indent=2, default=str),
            encoding="utf-8",
        )

    def _load_state(self) -> None:
        """Load active state (cooldown timestamps) from JSON file."""
        if self._active_path is None or not self._active_path.exists():
            return
        try:
            raw = json.loads(self._active_path.read_text(encoding="utf-8"))
            self._state = TipState.from_dict(raw)
        except (ValueError, KeyError):
            self._state = TipState()

    def _save_state(self) -> None:
        """Persist active state to JSON file."""
        if self._active_path is None:
            return
        self._active_path.parent.mkdir(parents=True, exist_ok=True)
        self._active_path.write_text(
            json.dumps(self._state.to_dict(), indent=2, default=str),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# CLI display helper
# ---------------------------------------------------------------------------


def show_tip(
    catalog: TipsCatalog | None = None,
    category: str = "general",
    console: Console | None = None,
    now: float | None = None,
) -> str | None:
    """Fetch a random tip and display it with Rich formatting.

    If no catalog is given, creates one using default paths under
    ``.sdd/tips/``.

    Args:
        catalog: Existing TipsCatalog (creates one if ``None``).
        category: Tip category to draw from.
        console: Rich Console instance (creates one if ``None``).
        now: Current Unix timestamp for cooldown testing.

    Returns:
        The tip text, or ``None`` if no eligible tip exists.
    """
    from rich.console import Console as RichConsole
    from rich.panel import Panel

    if catalog is None:
        sdd = Path(".sdd")
        catalog = TipsCatalog(
            catalog_path=sdd / "tips" / "catalog.json",
            active_path=sdd / "tips" / "active.json",
        )

    tip_text = catalog.get_tip(category=category, now=now)
    if tip_text is None:
        return None

    display = RichConsole() if console is None else console
    panel = Panel(
        tip_text,
        title="💡 Tip",
        border_style="yellow",
        padding=(0, 1),
    )
    display.print(panel)
    return tip_text
