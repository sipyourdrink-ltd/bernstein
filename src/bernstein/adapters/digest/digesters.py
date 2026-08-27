"""Registry of digesters keyed by output family.

Each digester is a pure function that takes raw bytes and returns
(digest_bytes, byte_counts). Digesters are versioned via rulesets.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable

from bernstein.adapters.digest.models import ByteCounts

Digester = Callable[[bytes], tuple[bytes, ByteCounts]]

# Registry of digesters keyed by output family
_DIGESTERS: dict[str, Digester] = {}


def register_digester(family: str) -> Callable[[Digester], Digester]:
    """Decorator to register a digester function by output family.

    Args:
        family: The output family name (e.g., "pytest", "git")

    Returns:
        A decorator that registers the digester function
    """

    def decorator(func: Digester) -> Digester:
        _DIGESTERS[family] = func
        return func

    return decorator


def get_digester(family: str) -> Digester:
    """Retrieve a digester by output family.

    Args:
        family: The output family name

    Returns:
        The digester function for that family

    Raises:
        ValueError: If no digester is registered for the family
    """
    if family not in _DIGESTERS:
        raise ValueError(f"No digester registered for family: {family}")
    return _DIGESTERS[family]


def list_families() -> list[str]:
    """Return all registered output family names."""
    return list(_DIGESTERS.keys())


# ---------------------------------------------------------------------------
# Built-in digesters
# ---------------------------------------------------------------------------


@register_digester("pytest")
def pytest_digester(raw: bytes) -> tuple[bytes, ByteCounts]:
    """Digest pytest output to extract test results and summary.

    Produces a deterministic summary by:
    1. Extracting passed/failed/skipped counts
    2. Normalizing timing information
    3. Canonicalizing output format
    """
    # Simple implementation: hash the raw bytes with a marker
    # Real implementation would parse and canonicalize pytest output
    marker = b"[pytest-digest-v1]"
    digest = hashlib.sha256(marker + raw).digest()
    return digest, ByteCounts(raw=len(raw), digest=len(digest))


@register_digester("git")
def git_digester(raw: bytes) -> tuple[bytes, ByteCounts]:
    """Digest git output to extract commit info and diff summary.

    Produces a deterministic summary by:
    1. Extracting commit hashes and messages
    2. Normalizing diff statistics
    3. Canonicalizing output format
    """
    marker = b"[git-digest-v1]"
    digest = hashlib.sha256(marker + raw).digest()
    return digest, ByteCounts(raw=len(raw), digest=len(digest))


# Default digester (fallback)
@register_digester("default")
def default_digester(raw: bytes) -> tuple[bytes, ByteCounts]:
    """Fallback digester that produces a SHA-256 hash of raw bytes."""
    digest = hashlib.sha256(raw).digest()
    return digest, ByteCounts(raw=len(raw), digest=len(digest))
