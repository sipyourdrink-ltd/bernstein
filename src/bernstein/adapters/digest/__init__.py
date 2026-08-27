"""Tool output digesters registry and ruleset models.

This subpackage provides:
- A registry of digesters keyed by output family (pytest, git, etc.)
- Versioned ruleset definitions with stable IDs
- Trace record data classes for replay verification

Key invariants:
- Deterministic: same raw bytes + same ruleset version → byte-identical digest
- No side effects (pure functions)
- Rulesets versioned (e.g. `pytest-1`, `git-1`)
- Trace records carry all metadata for replay verification
"""

from bernstein.adapters.digest.digesters import (
    Digester,
    default_digester,
    get_digester,
    list_families,
    pytest_digester,
    register_digester,
)
from bernstein.adapters.digest.models import ByteCounts, TraceRecord
from bernstein.adapters.digest.rulesets import (
    AVAILABLE_RULESETS,
    GIT_RULESET_V1,
    PYTEST_RULESET_V1,
    Ruleset,
    get_ruleset,
    list_rulesets,
)

__all__ = [
    # rulesets
    "AVAILABLE_RULESETS",
    "GIT_RULESET_V1",
    "PYTEST_RULESET_V1",
    # models
    "ByteCounts",
    # digesters
    "Digester",
    "Ruleset",
    "TraceRecord",
    "default_digester",
    "get_digester",
    "get_ruleset",
    "list_families",
    "list_rulesets",
    "pytest_digester",
    "register_digester",
]
