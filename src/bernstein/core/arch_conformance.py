"""Architecture conformance checking against declared module boundaries (ROAD-171).

Validates that agent-produced code does not introduce unwanted coupling between
modules. Boundaries are declared in bernstein.yaml::

    guardrails:
      arch_conformance:
        enabled: true
        block_on_violation: true
        modules:
          - name: core
            paths: ["src/bernstein/core/**"]
            forbidden_imports: ["bernstein.cli", "bernstein.adapters"]
          - name: adapters
            paths: ["src/bernstein/adapters/**"]
            allowed_imports: ["bernstein.core", "bernstein.adapters"]

When a changed file belongs to a declared module, the import statements added in
that file's diff hunk are validated against the module's boundary rules.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field

from bernstein.core.policy_engine import DecisionType, PermissionDecision

# ---------------------------------------------------------------------------
# Config dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArchModule:
    """Boundary definition for one logical module.

    Attributes:
        name: Human-readable module name used in violation messages.
        paths: Glob patterns (relative to repo root) that identify files
            belonging to this module (e.g. ``["src/bernstein/core/**"]``).
        allowed_imports: Module prefixes that files in this module *may*
            import.  When non-empty, any import not matching a listed prefix
            is a violation. Takes precedence over ``forbidden_imports``.
        forbidden_imports: Module prefixes that files in this module must
            *not* import.  Checked only when ``allowed_imports`` is empty.
    """

    name: str
    paths: list[str] = field(default_factory=list)
    allowed_imports: list[str] = field(default_factory=list)
    forbidden_imports: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ArchConformanceConfig:
    """Configuration for architecture conformance checking.

    Attributes:
        enabled: Master switch.
        modules: Declared module boundaries.
        block_on_violation: When True, violations produce DENY decisions.
            When False, they produce ASK decisions (flag but don't hard-block).
    """

    enabled: bool = False
    modules: list[ArchModule] = field(default_factory=list)
    block_on_violation: bool = True


# ---------------------------------------------------------------------------
# Diff parsing helpers
# ---------------------------------------------------------------------------

# Matches the "diff --git a/... b/..." header line to capture the file path.
_DIFF_FILE_HEADER = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)

# Matches import lines added by the diff (lines starting with '+' but not '+++').
_ADDED_IMPORT_LINE = re.compile(
    r"^\+(?!\+\+)\s*(?:from\s+([\w.]+)|import\s+([\w.,\s]+))",
    re.MULTILINE,
)


def _extract_added_imports_per_file(diff: str) -> dict[str, list[str]]:
    """Parse a git diff and return import statements added per file.

    Only imports on added lines (prefixed with ``+`` but not ``+++``) are
    returned, so removed imports don't trigger false violations.

    Args:
        diff: Raw git diff output.

    Returns:
        Mapping of ``filepath → [import_module, ...]`` for each file that has
        newly added imports.  ``import_module`` is the top-level module string
        (e.g. ``"bernstein.cli"`` for ``from bernstein.cli import foo``).
    """
    result: dict[str, list[str]] = {}
    # Split diff into per-file chunks using the +++ b/<path> marker.
    chunks = re.split(r"(?=^\+\+\+ b/)", diff, flags=re.MULTILINE)

    for chunk in chunks:
        m = _DIFF_FILE_HEADER.match(chunk)
        if not m:
            continue
        filepath = m.group(1)
        # Only look at Python files
        if not filepath.endswith(".py"):
            continue

        imports: list[str] = []
        for im in _ADDED_IMPORT_LINE.finditer(chunk):
            # "from X import Y" → X; "import X, Y" → X (first token)
            module = im.group(1) or im.group(2).split(",")[0].strip().split()[0]
            if module:
                imports.append(module)

        if imports:
            result[filepath] = imports

    return result


# ---------------------------------------------------------------------------
# Boundary evaluation
# ---------------------------------------------------------------------------


def _file_belongs_to_module(filepath: str, module: ArchModule) -> bool:
    """Return True if ``filepath`` matches any path pattern for ``module``."""
    return any(fnmatch.fnmatch(filepath, pattern) for pattern in module.paths)


def _import_violates_module(import_module: str, module: ArchModule) -> str | None:
    """Check whether ``import_module`` violates ``module``'s boundary rules.

    Args:
        import_module: The imported module string (e.g. ``"bernstein.cli"``).
        module: The boundary definition to check against.

    Returns:
        A violation reason string, or None if no violation.
    """
    # allowed_imports takes precedence: if set, only listed prefixes are ok.
    if module.allowed_imports:
        if not any(import_module.startswith(prefix) for prefix in module.allowed_imports):
            allowed_str = ", ".join(module.allowed_imports)
            return (
                f"'{import_module}' is not in the allowed list for module "
                f"'{module.name}' (allowed: {allowed_str})"
            )
        return None

    # forbidden_imports: listed prefixes must not appear.
    for forbidden in module.forbidden_imports:
        if import_module.startswith(forbidden):
            return f"'{import_module}' is forbidden in module '{module.name}'"

    return None


# ---------------------------------------------------------------------------
# Public checker
# ---------------------------------------------------------------------------


def check_arch_conformance(
    diff: str,
    config: ArchConformanceConfig,
) -> list[PermissionDecision]:
    """Check a git diff against declared module boundary rules.

    Parses all import statements on added lines in ``diff``, then validates
    each against the module boundary rules.  Returns one decision per
    violation, plus one ALLOW decision when everything is clean.

    Args:
        diff: Git diff output from the completed agent.
        config: Architecture conformance configuration.

    Returns:
        List of :class:`PermissionDecision` objects.
    """
    if not config.enabled or not config.modules:
        return [PermissionDecision(type=DecisionType.ALLOW, reason="Architecture conformance: disabled or no modules")]

    added_imports = _extract_added_imports_per_file(diff)
    if not added_imports:
        return [PermissionDecision(type=DecisionType.ALLOW, reason="Architecture conformance: no added imports")]

    violations: list[PermissionDecision] = []
    checked_any = False

    for filepath, imports in added_imports.items():
        for module in config.modules:
            if not _file_belongs_to_module(filepath, module):
                continue
            checked_any = True
            for import_module in imports:
                reason = _import_violates_module(import_module, module)
                if reason is not None:
                    decision_type = DecisionType.DENY if config.block_on_violation else DecisionType.ASK
                    violations.append(
                        PermissionDecision(
                            type=decision_type,
                            reason=f"Arch violation in {filepath}: {reason}",
                        )
                    )

    if violations:
        return violations

    if checked_any:
        return [PermissionDecision(type=DecisionType.ALLOW, reason="Architecture conformance: boundaries respected")]

    return [PermissionDecision(type=DecisionType.ALLOW, reason="Architecture conformance: no covered files changed")]


def arch_conformance_summary(violations: list[PermissionDecision]) -> str:
    """Format a human-readable summary of architecture violations.

    Args:
        violations: List of violation decisions returned by
            :func:`check_arch_conformance`.

    Returns:
        Multi-line summary string suitable for log output.
    """
    blocked = [d for d in violations if d.type in (DecisionType.DENY, DecisionType.ASK)]
    if not blocked:
        return "No architecture violations."
    lines = [f"Architecture conformance: {len(blocked)} violation(s):"]
    for v in blocked:
        lines.append(f"  - {v.reason}")
    return "\n".join(lines)
