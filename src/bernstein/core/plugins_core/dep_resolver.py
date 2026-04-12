"""Plugin dependency resolution with version constraint satisfaction (#650).

Provides topological sorting and version constraint checking for plugin
dependency graphs.  Pure Python — no pip or packaging internals.

Usage:
    >>> from bernstein.core.plugins_core.dep_resolver import (
    ...     PluginDependency, VersionConstraint, resolve_dependencies,
    ... )
    >>> db = PluginDependency("db-adapter", "2.0.0", dependencies=())
    >>> api = PluginDependency(
    ...     "api-server", "1.0.0",
    ...     dependencies=(VersionConstraint("db-adapter", min_version="1.5.0"),),
    ... )
    >>> result = resolve_dependencies([api, db])
    >>> result.success
    True
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VersionConstraint:
    """A version constraint on a required package.

    At most one of *min_version*, *max_version*, or *exact_version* should be
    set, though *min_version* and *max_version* can be combined as a range.

    Attributes:
        package: Name of the required plugin package.
        min_version: Minimum acceptable version (inclusive), e.g. ``"1.2.0"``.
        max_version: Maximum acceptable version (inclusive), e.g. ``"2.0.0"``.
        exact_version: Exact version required.  When set, *min_version* and
            *max_version* are ignored.
    """

    package: str
    min_version: str | None = None
    max_version: str | None = None
    exact_version: str | None = None


@dataclass(frozen=True)
class PluginDependency:
    """A plugin and its declared dependencies.

    Attributes:
        plugin_name: Unique identifier for this plugin.
        version: Semantic version of this plugin (``"1.2.3"``).
        dependencies: Version constraints on other plugins that this plugin
            requires at runtime.
    """

    plugin_name: str
    version: str
    dependencies: tuple[VersionConstraint, ...]


@dataclass(frozen=True)
class ResolutionResult:
    """Outcome of a dependency resolution attempt.

    Attributes:
        resolved: Topologically-sorted tuple of plugins (install order).
        conflicts: Human-readable conflict descriptions, if any.
        success: ``True`` when all constraints are satisfied and the graph is
            acyclic.
    """

    resolved: tuple[PluginDependency, ...]
    conflicts: tuple[str, ...]
    success: bool


# ---------------------------------------------------------------------------
# Version helpers
# ---------------------------------------------------------------------------


def parse_version(version_str: str) -> tuple[int, ...]:
    """Parse a dotted version string into a comparable integer tuple.

    Args:
        version_str: Dotted version such as ``"1.2.3"`` or ``"0.10.1"``.

    Returns:
        Tuple of integers, e.g. ``(1, 2, 3)``.

    Raises:
        ValueError: If any segment is not a non-negative integer.
    """
    parts: list[int] = []
    for segment in version_str.split("."):
        value = int(segment)
        if value < 0:
            msg = f"Version segments must be non-negative, got {value!r} in {version_str!r}"
            raise ValueError(msg)
        parts.append(value)
    return tuple(parts)


def satisfies_constraint(version: str, constraint: VersionConstraint) -> bool:
    """Check whether *version* meets *constraint*.

    Args:
        version: The version string to test (e.g. ``"2.1.0"``).
        constraint: The constraint to check against.

    Returns:
        ``True`` if *version* satisfies every bound in *constraint*.
    """
    parsed = parse_version(version)

    if constraint.exact_version is not None:
        return parsed == parse_version(constraint.exact_version)

    if constraint.min_version is not None and parsed < parse_version(constraint.min_version):
        return False

    return not (constraint.max_version is not None and parsed > parse_version(constraint.max_version))


# ---------------------------------------------------------------------------
# Conflict detection
# ---------------------------------------------------------------------------


def detect_conflicts(plugins: list[PluginDependency]) -> tuple[str, ...]:
    """Find conflicting version requirements across *plugins*.

    Two constraints conflict when they refer to the same package but no
    version can satisfy both simultaneously.  The function also reports
    missing dependencies (a required package is not in the plugin list).

    Args:
        plugins: All plugins participating in the resolution.

    Returns:
        Tuple of human-readable conflict strings (empty when clean).
    """
    by_name: dict[str, PluginDependency] = {p.plugin_name: p for p in plugins}
    conflicts: list[str] = []

    # Collect all constraints per target package.
    constraints_map: dict[str, list[tuple[str, VersionConstraint]]] = {}
    for plugin in plugins:
        for dep in plugin.dependencies:
            constraints_map.setdefault(dep.package, []).append(
                (plugin.plugin_name, dep),
            )

    # Check each target package.
    for pkg, requestors in constraints_map.items():
        # Missing dependency.
        if pkg not in by_name:
            sources = ", ".join(r[0] for r in requestors)
            conflicts.append(f"Missing dependency: '{pkg}' required by {sources}")
            continue

        provider_version = by_name[pkg].version

        # Each individual constraint must accept the provider version.
        for source_name, constraint in requestors:
            if not satisfies_constraint(provider_version, constraint):
                detail = _constraint_detail(constraint)
                conflicts.append(
                    f"Conflict: '{source_name}' requires '{pkg}' {detail}, but version {provider_version} is provided"
                )

    return tuple(conflicts)


def _constraint_detail(constraint: VersionConstraint) -> str:
    """Format a constraint as a human-readable range string."""
    if constraint.exact_version is not None:
        return f"=={constraint.exact_version}"
    parts: list[str] = []
    if constraint.min_version is not None:
        parts.append(f">={constraint.min_version}")
    if constraint.max_version is not None:
        parts.append(f"<={constraint.max_version}")
    return ", ".join(parts) if parts else "(any version)"


# ---------------------------------------------------------------------------
# Topological resolution
# ---------------------------------------------------------------------------


def resolve_dependencies(plugins: list[PluginDependency]) -> ResolutionResult:
    """Topologically sort *plugins* and verify all version constraints.

    Uses Kahn's algorithm for the sort.  When a cycle is detected, or any
    constraint is unsatisfied, ``ResolutionResult.success`` is ``False`` and
    ``conflicts`` contains explanations.

    Args:
        plugins: All plugins that should be resolved together.

    Returns:
        A ``ResolutionResult`` with the resolved ordering, any conflicts, and
        a success flag.
    """
    by_name: dict[str, PluginDependency] = {p.plugin_name: p for p in plugins}
    conflicts_list: list[str] = list(detect_conflicts(plugins))

    # Build adjacency and in-degree for Kahn's algorithm.
    # Edge: dependency-package -> dependent-plugin (install dep first).
    in_degree: dict[str, int] = {p.plugin_name: 0 for p in plugins}
    dependents: dict[str, list[str]] = {p.plugin_name: [] for p in plugins}

    for plugin in plugins:
        for dep in plugin.dependencies:
            if dep.package in by_name:
                in_degree[plugin.plugin_name] += 1
                dependents[dep.package].append(plugin.plugin_name)

    # Kahn's algorithm.
    queue: list[str] = sorted(name for name, deg in in_degree.items() if deg == 0)
    order: list[str] = []

    while queue:
        node = queue.pop(0)
        order.append(node)
        for dependent in sorted(dependents[node]):
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)
        queue.sort()

    if len(order) != len(plugins):
        remaining = sorted(set(by_name) - set(order))
        conflicts_list.append(f"Cyclic dependency detected among: {', '.join(remaining)}")

    resolved = tuple(by_name[n] for n in order if n in by_name)
    conflict_tuple = tuple(conflicts_list)
    return ResolutionResult(
        resolved=resolved,
        conflicts=conflict_tuple,
        success=len(conflict_tuple) == 0,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_dependency_tree(result: ResolutionResult) -> str:
    """Render *result* as a Markdown dependency tree.

    Args:
        result: A ``ResolutionResult`` from ``resolve_dependencies``.

    Returns:
        Markdown string suitable for display in a CLI or `.md` file.
    """
    lines: list[str] = ["# Plugin Dependency Tree", ""]

    if result.conflicts:
        lines.append("## Conflicts")
        lines.append("")
        for conflict in result.conflicts:
            lines.append(f"- {conflict}")
        lines.append("")

    lines.append("## Resolved Order")
    lines.append("")
    if result.resolved:
        for idx, plugin in enumerate(result.resolved, 1):
            deps_str = ""
            if plugin.dependencies:
                dep_names = ", ".join(d.package for d in plugin.dependencies)
                deps_str = f" (depends on: {dep_names})"
            lines.append(f"{idx}. **{plugin.plugin_name}** v{plugin.version}{deps_str}")
    else:
        lines.append("_No plugins resolved._")

    lines.append("")
    status = "Success" if result.success else "Failed"
    lines.append(f"**Status:** {status}")
    lines.append("")
    return "\n".join(lines)
