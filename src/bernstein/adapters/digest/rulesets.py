"""Versioned ruleset definitions for digest operations.

Each ruleset has a stable ID and version. The combination of ID and version
uniquely identifies a specific digest algorithm and parameters.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bernstein.adapters.digest.digesters import Digester


@dataclass(frozen=True)
class Ruleset:
    """A versioned ruleset that defines a specific digest algorithm.

    Attributes:
        id: Stable identifier for the ruleset family (e.g., "pytest", "git")
        version: Version identifier for this specific ruleset (e.g., "1")
        description: Human-readable description of what this ruleset produces
    """

    id: str
    version: str
    description: str

    @property
    def ruleset_id(self) -> str:
        """Return the fully-qualified ruleset identifier."""
        return f"{self.id}-{self.version}"

    def get_digester(self) -> Digester:
        """Get the digester function for this ruleset.

        Returns:
            A pure function that takes raw bytes and returns (digest_bytes, byte_counts).
        """
        from bernstein.adapters.digest import digesters

        return digesters.get_digester(self.id)


# Predefined rulesets for common output families
PYTEST_RULESET_V1 = Ruleset(
    id="pytest",
    version="1",
    description="Pytest output digester - extracts test results and summary",
)

GIT_RULESET_V1 = Ruleset(
    id="git",
    version="1",
    description="Git output digester - extracts commit info and diff summary",
)

# Registry of available rulesets
AVAILABLE_RULESETS: dict[str, Ruleset] = {
    PYTEST_RULESET_V1.ruleset_id: PYTEST_RULESET_V1,
    GIT_RULESET_V1.ruleset_id: GIT_RULESET_V1,
}


def get_ruleset(ruleset_id: str) -> Ruleset:
    """Retrieve a ruleset by its fully-qualified ID.

    Args:
        ruleset_id: The fully-qualified ruleset ID (e.g., "pytest-1")

    Returns:
        The corresponding Ruleset object

    Raises:
        ValueError: If the ruleset is not found
    """
    if ruleset_id not in AVAILABLE_RULESETS:
        raise ValueError(f"Unknown ruleset: {ruleset_id}")
    return AVAILABLE_RULESETS[ruleset_id]


def list_rulesets() -> list[Ruleset]:
    """Return all available rulesets."""
    return list(AVAILABLE_RULESETS.values())
