"""Tests for #650 — plugin dependency resolution with version constraints."""

from __future__ import annotations

import pytest

from bernstein.core.plugins_core.dep_resolver import (
    PluginDependency,
    ResolutionResult,
    VersionConstraint,
    detect_conflicts,
    parse_version,
    render_dependency_tree,
    resolve_dependencies,
    satisfies_constraint,
)

# ---------------------------------------------------------------------------
# parse_version
# ---------------------------------------------------------------------------


class TestParseVersion:
    def test_three_segments(self) -> None:
        assert parse_version("1.2.3") == (1, 2, 3)

    def test_two_segments(self) -> None:
        assert parse_version("3.11") == (3, 11)

    def test_single_segment(self) -> None:
        assert parse_version("7") == (7,)

    def test_zero_version(self) -> None:
        assert parse_version("0.0.0") == (0, 0, 0)

    def test_large_numbers(self) -> None:
        assert parse_version("100.200.300") == (100, 200, 300)

    def test_negative_segment_raises(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            parse_version("-1.0.0")

    def test_non_integer_segment_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_version("1.two.3")


# ---------------------------------------------------------------------------
# satisfies_constraint
# ---------------------------------------------------------------------------


class TestSatisfiesConstraint:
    def test_exact_version_match(self) -> None:
        c = VersionConstraint("pkg", exact_version="1.0.0")
        assert satisfies_constraint("1.0.0", c) is True

    def test_exact_version_mismatch(self) -> None:
        c = VersionConstraint("pkg", exact_version="1.0.0")
        assert satisfies_constraint("1.0.1", c) is False

    def test_min_version_satisfied(self) -> None:
        c = VersionConstraint("pkg", min_version="1.0.0")
        assert satisfies_constraint("1.0.0", c) is True
        assert satisfies_constraint("2.0.0", c) is True

    def test_min_version_not_satisfied(self) -> None:
        c = VersionConstraint("pkg", min_version="2.0.0")
        assert satisfies_constraint("1.9.9", c) is False

    def test_max_version_satisfied(self) -> None:
        c = VersionConstraint("pkg", max_version="3.0.0")
        assert satisfies_constraint("3.0.0", c) is True
        assert satisfies_constraint("2.0.0", c) is True

    def test_max_version_not_satisfied(self) -> None:
        c = VersionConstraint("pkg", max_version="2.0.0")
        assert satisfies_constraint("2.0.1", c) is False

    def test_range_constraint_within(self) -> None:
        c = VersionConstraint("pkg", min_version="1.0.0", max_version="2.0.0")
        assert satisfies_constraint("1.5.0", c) is True

    def test_range_constraint_below(self) -> None:
        c = VersionConstraint("pkg", min_version="1.0.0", max_version="2.0.0")
        assert satisfies_constraint("0.9.0", c) is False

    def test_range_constraint_above(self) -> None:
        c = VersionConstraint("pkg", min_version="1.0.0", max_version="2.0.0")
        assert satisfies_constraint("2.0.1", c) is False

    def test_unconstrained(self) -> None:
        c = VersionConstraint("pkg")
        assert satisfies_constraint("99.99.99", c) is True

    def test_exact_takes_precedence_over_range(self) -> None:
        """When exact_version is set, min/max are ignored."""
        c = VersionConstraint("pkg", min_version="1.0.0", max_version="3.0.0", exact_version="2.0.0")
        assert satisfies_constraint("2.0.0", c) is True
        assert satisfies_constraint("1.5.0", c) is False


# ---------------------------------------------------------------------------
# detect_conflicts
# ---------------------------------------------------------------------------


class TestDetectConflicts:
    def test_no_conflicts(self) -> None:
        db = PluginDependency("db", "2.0.0", dependencies=())
        api = PluginDependency(
            "api",
            "1.0.0",
            dependencies=(VersionConstraint("db", min_version="1.0.0"),),
        )
        assert detect_conflicts([api, db]) == ()

    def test_missing_dependency(self) -> None:
        api = PluginDependency(
            "api",
            "1.0.0",
            dependencies=(VersionConstraint("db", min_version="1.0.0"),),
        )
        conflicts = detect_conflicts([api])
        assert len(conflicts) == 1
        assert "Missing dependency" in conflicts[0]
        assert "'db'" in conflicts[0]

    def test_version_too_low(self) -> None:
        db = PluginDependency("db", "0.5.0", dependencies=())
        api = PluginDependency(
            "api",
            "1.0.0",
            dependencies=(VersionConstraint("db", min_version="1.0.0"),),
        )
        conflicts = detect_conflicts([api, db])
        assert len(conflicts) == 1
        assert "Conflict" in conflicts[0]

    def test_version_too_high(self) -> None:
        db = PluginDependency("db", "3.0.0", dependencies=())
        api = PluginDependency(
            "api",
            "1.0.0",
            dependencies=(VersionConstraint("db", max_version="2.0.0"),),
        )
        conflicts = detect_conflicts([api, db])
        assert len(conflicts) == 1
        assert "Conflict" in conflicts[0]

    def test_multiple_requestors_one_fails(self) -> None:
        db = PluginDependency("db", "1.5.0", dependencies=())
        api = PluginDependency(
            "api",
            "1.0.0",
            dependencies=(VersionConstraint("db", min_version="1.0.0"),),
        )
        web = PluginDependency(
            "web",
            "1.0.0",
            dependencies=(VersionConstraint("db", min_version="2.0.0"),),
        )
        conflicts = detect_conflicts([api, web, db])
        assert len(conflicts) == 1
        assert "'web'" in conflicts[0]


# ---------------------------------------------------------------------------
# resolve_dependencies — happy path
# ---------------------------------------------------------------------------


class TestResolveDependencies:
    def test_single_plugin_no_deps(self) -> None:
        p = PluginDependency("solo", "1.0.0", dependencies=())
        result = resolve_dependencies([p])
        assert result.success is True
        assert result.resolved == (p,)
        assert result.conflicts == ()

    def test_linear_chain(self) -> None:
        a = PluginDependency("a", "1.0.0", dependencies=())
        b = PluginDependency("b", "1.0.0", dependencies=(VersionConstraint("a", min_version="1.0.0"),))
        c = PluginDependency("c", "1.0.0", dependencies=(VersionConstraint("b", min_version="1.0.0"),))
        result = resolve_dependencies([c, b, a])
        assert result.success is True
        names = [p.plugin_name for p in result.resolved]
        assert names.index("a") < names.index("b") < names.index("c")

    def test_diamond_dependency(self) -> None:
        base = PluginDependency("base", "1.0.0", dependencies=())
        left = PluginDependency("left", "1.0.0", dependencies=(VersionConstraint("base"),))
        right = PluginDependency("right", "1.0.0", dependencies=(VersionConstraint("base"),))
        top = PluginDependency(
            "top",
            "1.0.0",
            dependencies=(VersionConstraint("left"), VersionConstraint("right")),
        )
        result = resolve_dependencies([top, right, left, base])
        assert result.success is True
        names = [p.plugin_name for p in result.resolved]
        assert names.index("base") < names.index("left")
        assert names.index("base") < names.index("right")
        assert names.index("left") < names.index("top")
        assert names.index("right") < names.index("top")

    def test_empty_list(self) -> None:
        result = resolve_dependencies([])
        assert result.success is True
        assert result.resolved == ()
        assert result.conflicts == ()


# ---------------------------------------------------------------------------
# resolve_dependencies — failure cases
# ---------------------------------------------------------------------------


class TestResolveDependenciesFailures:
    def test_cycle_detected(self) -> None:
        a = PluginDependency("a", "1.0.0", dependencies=(VersionConstraint("b"),))
        b = PluginDependency("b", "1.0.0", dependencies=(VersionConstraint("a"),))
        result = resolve_dependencies([a, b])
        assert result.success is False
        assert any("Cyclic" in c for c in result.conflicts)

    def test_self_cycle(self) -> None:
        a = PluginDependency("a", "1.0.0", dependencies=(VersionConstraint("a"),))
        result = resolve_dependencies([a])
        assert result.success is False
        assert any("Cyclic" in c for c in result.conflicts)

    def test_missing_dep_fails(self) -> None:
        a = PluginDependency("a", "1.0.0", dependencies=(VersionConstraint("missing"),))
        result = resolve_dependencies([a])
        assert result.success is False
        assert any("Missing" in c for c in result.conflicts)

    def test_version_conflict_fails(self) -> None:
        db = PluginDependency("db", "0.1.0", dependencies=())
        api = PluginDependency(
            "api",
            "1.0.0",
            dependencies=(VersionConstraint("db", min_version="1.0.0"),),
        )
        result = resolve_dependencies([api, db])
        assert result.success is False
        assert len(result.conflicts) == 1


# ---------------------------------------------------------------------------
# render_dependency_tree
# ---------------------------------------------------------------------------


class TestRenderDependencyTree:
    def test_successful_render(self) -> None:
        db = PluginDependency("db", "2.0.0", dependencies=())
        api = PluginDependency(
            "api",
            "1.0.0",
            dependencies=(VersionConstraint("db", min_version="1.0.0"),),
        )
        result = resolve_dependencies([api, db])
        md = render_dependency_tree(result)
        assert "# Plugin Dependency Tree" in md
        assert "## Resolved Order" in md
        assert "**db** v2.0.0" in md
        assert "**api** v1.0.0" in md
        assert "**Status:** Success" in md

    def test_conflict_render(self) -> None:
        a = PluginDependency("a", "1.0.0", dependencies=(VersionConstraint("b"),))
        b = PluginDependency("b", "1.0.0", dependencies=(VersionConstraint("a"),))
        result = resolve_dependencies([a, b])
        md = render_dependency_tree(result)
        assert "## Conflicts" in md
        assert "**Status:** Failed" in md

    def test_empty_result_render(self) -> None:
        result = ResolutionResult(resolved=(), conflicts=(), success=True)
        md = render_dependency_tree(result)
        assert "_No plugins resolved._" in md
        assert "**Status:** Success" in md

    def test_depends_on_shown(self) -> None:
        base = PluginDependency("base", "1.0.0", dependencies=())
        top = PluginDependency(
            "top",
            "2.0.0",
            dependencies=(VersionConstraint("base"),),
        )
        result = resolve_dependencies([top, base])
        md = render_dependency_tree(result)
        assert "(depends on: base)" in md


# ---------------------------------------------------------------------------
# Dataclass immutability
# ---------------------------------------------------------------------------


class TestFrozenDataclasses:
    def test_version_constraint_frozen(self) -> None:
        c = VersionConstraint("pkg", min_version="1.0.0")
        with pytest.raises(AttributeError):
            c.package = "other"  # type: ignore[misc]

    def test_plugin_dependency_frozen(self) -> None:
        p = PluginDependency("p", "1.0.0", dependencies=())
        with pytest.raises(AttributeError):
            p.plugin_name = "q"  # type: ignore[misc]

    def test_resolution_result_frozen(self) -> None:
        r = ResolutionResult(resolved=(), conflicts=(), success=True)
        with pytest.raises(AttributeError):
            r.success = False  # type: ignore[misc]
