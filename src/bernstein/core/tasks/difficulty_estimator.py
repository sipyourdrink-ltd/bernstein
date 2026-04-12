"""Task difficulty estimation from description."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


@dataclass
class DifficultyScore:
    """Estimated difficulty score and level."""

    raw: float
    level: Literal["trivial", "low", "medium", "high", "critical"]


def estimate_difficulty(description: str) -> DifficultyScore:
    """Estimate task difficulty from its description."""
    word_count = len(description.split())

    backtick_matches = len(re.findall(r"`[^`]+`", description))
    func_matches = len(re.findall(r"\b[a-zA-Z_]\w*\s*\(", description))
    code_ref_count = backtick_matches + func_matches

    complexity_keywords = ["refactor", "architect", "security", "database", "migrate"]
    keyword_count = sum(1 for kw in complexity_keywords if kw in description.lower())

    raw = (word_count / 50.0) + code_ref_count + (keyword_count * 2)

    if raw < 2.0:
        level = "trivial"
    elif raw < 5.0:
        level = "low"
    elif raw < 10.0:
        level = "medium"
    elif raw < 20.0:
        level = "high"
    else:
        level = "critical"

    return DifficultyScore(raw=raw, level=level)


def minutes_for_level(level: Literal["trivial", "low", "medium", "high", "critical"]) -> int:
    """Get estimated minutes for a difficulty level."""
    mapping: dict[Literal["trivial", "low", "medium", "high", "critical"], int] = {
        "trivial": 10,
        "low": 20,
        "medium": 45,
        "high": 90,
        "critical": 120,
    }
    return mapping[level]
