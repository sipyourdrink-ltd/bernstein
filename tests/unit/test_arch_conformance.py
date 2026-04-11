"""ROAD-171: Architecture conformance checking against declared module boundaries.

Tests for the guardrail that validates agent-produced code does not introduce
unwanted coupling between modules.
"""

from __future__ import annotations

from bernstein.core.arch_conformance import (
    ArchConformanceConfig,
    ArchModule,
    _extract_added_imports_per_file,
    _file_belongs_to_module,
    _import_violates_module,
    arch_conformance_summary,
    check_arch_conformance,
)
from bernstein.core.policy_engine import DecisionType

# ---------------------------------------------------------------------------
# Diff import extraction
# ---------------------------------------------------------------------------


class TestExtractAddedImportsPerFile:
    """Test _extract_added_imports_per_file() diff parsing."""

    def test_extracts_from_import(self) -> None:
        diff = (
            "+++ b/src/bernstein/core/foo.py\n"
            "@@ -0,0 +1,3 @@\n"
            "+from bernstein.cli import run\n"
        )
        result = _extract_added_imports_per_file(diff)
        assert "src/bernstein/core/foo.py" in result
        assert "bernstein.cli" in result["src/bernstein/core/foo.py"]

    def test_extracts_bare_import(self) -> None:
        diff = (
            "+++ b/src/bernstein/core/bar.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+import bernstein.adapters\n"
        )
        result = _extract_added_imports_per_file(diff)
        assert "src/bernstein/core/bar.py" in result
        assert "bernstein.adapters" in result["src/bernstein/core/bar.py"]

    def test_ignores_removed_lines(self) -> None:
        diff = (
            "+++ b/src/bernstein/core/baz.py\n"
            "@@ -1,2 +1,1 @@\n"
            "-from bernstein.cli import bad\n"
            "+from bernstein.core import good\n"
        )
        result = _extract_added_imports_per_file(diff)
        assert "bernstein.cli" not in result.get("src/bernstein/core/baz.py", [])
        assert "bernstein.core" in result.get("src/bernstein/core/baz.py", [])

    def test_ignores_non_python_files(self) -> None:
        diff = (
            "+++ b/README.md\n"
            "@@ -0,0 +1 @@\n"
            "+import bernstein\n"
        )
        result = _extract_added_imports_per_file(diff)
        assert result == {}

    def test_empty_diff(self) -> None:
        assert _extract_added_imports_per_file("") == {}

    def test_multiple_imports_same_file(self) -> None:
        diff = (
            "+++ b/src/bernstein/core/multi.py\n"
            "@@ -0,0 +1,4 @@\n"
            "+from bernstein.cli import cmd\n"
            "+import bernstein.adapters\n"
        )
        result = _extract_added_imports_per_file(diff)
        imports = result.get("src/bernstein/core/multi.py", [])
        assert "bernstein.cli" in imports
        assert "bernstein.adapters" in imports

    def test_import_comma_separated_takes_first(self) -> None:
        diff = (
            "+++ b/src/bernstein/core/multi.py\n"
            "@@ -0,0 +1 @@\n"
            "+import os, sys\n"
        )
        result = _extract_added_imports_per_file(diff)
        imports = result.get("src/bernstein/core/multi.py", [])
        # Should extract "os" as the first token
        assert "os" in imports


# ---------------------------------------------------------------------------
# Module membership
# ---------------------------------------------------------------------------


class TestFileBelongsToModule:
    """Test _file_belongs_to_module() glob matching."""

    def test_matches_glob_pattern(self) -> None:
        module = ArchModule(name="core", paths=["src/bernstein/core/**"])
        assert _file_belongs_to_module("src/bernstein/core/guardrails.py", module)

    def test_does_not_match_other_path(self) -> None:
        module = ArchModule(name="core", paths=["src/bernstein/core/**"])
        assert not _file_belongs_to_module("src/bernstein/adapters/claude.py", module)

    def test_multiple_path_patterns(self) -> None:
        module = ArchModule(name="multi", paths=["src/bernstein/core/**", "src/bernstein/plugins/**"])
        assert _file_belongs_to_module("src/bernstein/plugins/manager.py", module)
        assert not _file_belongs_to_module("src/bernstein/cli/run.py", module)

    def test_empty_paths_never_matches(self) -> None:
        module = ArchModule(name="empty", paths=[])
        assert not _file_belongs_to_module("src/anything.py", module)


# ---------------------------------------------------------------------------
# Import violation logic
# ---------------------------------------------------------------------------


class TestImportViolatesModule:
    """Test _import_violates_module() boundary rule evaluation."""

    def test_forbidden_import_triggers_violation(self) -> None:
        module = ArchModule(name="core", forbidden_imports=["bernstein.cli"])
        reason = _import_violates_module("bernstein.cli", module)
        assert reason is not None
        assert "forbidden" in reason.lower()

    def test_allowed_prefix_passes(self) -> None:
        module = ArchModule(name="core", allowed_imports=["bernstein.core"])
        reason = _import_violates_module("bernstein.core.models", module)
        assert reason is None

    def test_unlisted_import_violates_allowlist(self) -> None:
        module = ArchModule(name="core", allowed_imports=["bernstein.core"])
        reason = _import_violates_module("bernstein.cli", module)
        assert reason is not None
        assert "allowed" in reason.lower()

    def test_allowed_takes_precedence_over_forbidden(self) -> None:
        # When allowed_imports is set, forbidden_imports is ignored
        module = ArchModule(
            name="core",
            allowed_imports=["bernstein.core", "bernstein.cli"],
            forbidden_imports=["bernstein.cli"],
        )
        reason = _import_violates_module("bernstein.cli", module)
        # Since bernstein.cli is in allowed_imports, no violation
        assert reason is None

    def test_no_rules_means_no_violation(self) -> None:
        module = ArchModule(name="permissive")
        reason = _import_violates_module("bernstein.cli", module)
        assert reason is None

    def test_forbidden_prefix_matches_submodule(self) -> None:
        module = ArchModule(name="core", forbidden_imports=["bernstein.cli"])
        reason = _import_violates_module("bernstein.cli.run_cmd", module)
        assert reason is not None


# ---------------------------------------------------------------------------
# Full check_arch_conformance
# ---------------------------------------------------------------------------


class TestCheckArchConformance:
    """Integration tests for check_arch_conformance()."""

    def _make_config(self, modules: list[ArchModule], block: bool = True) -> ArchConformanceConfig:
        return ArchConformanceConfig(enabled=True, modules=modules, block_on_violation=block)

    def test_disabled_config_returns_allow(self) -> None:
        config = ArchConformanceConfig(enabled=False, modules=[])
        results = check_arch_conformance("anything", config)
        assert all(d.type == DecisionType.ALLOW for d in results)

    def test_no_modules_returns_allow(self) -> None:
        config = ArchConformanceConfig(enabled=True, modules=[])
        results = check_arch_conformance("any diff", config)
        assert all(d.type == DecisionType.ALLOW for d in results)

    def test_clean_diff_returns_allow(self) -> None:
        module = ArchModule(
            name="core",
            paths=["src/bernstein/core/**"],
            forbidden_imports=["bernstein.cli"],
        )
        config = self._make_config([module])
        diff = (
            "+++ b/src/bernstein/core/foo.py\n"
            "@@ -0,0 +1 @@\n"
            "+from bernstein.core import models\n"
        )
        results = check_arch_conformance(diff, config)
        assert all(d.type == DecisionType.ALLOW for d in results)

    def test_violation_produces_deny_when_block_enabled(self) -> None:
        module = ArchModule(
            name="core",
            paths=["src/bernstein/core/**"],
            forbidden_imports=["bernstein.cli"],
        )
        config = self._make_config([module], block=True)
        diff = (
            "+++ b/src/bernstein/core/foo.py\n"
            "@@ -0,0 +1 @@\n"
            "+from bernstein.cli import run\n"
        )
        results = check_arch_conformance(diff, config)
        assert any(d.type == DecisionType.DENY for d in results)

    def test_violation_produces_ask_when_block_disabled(self) -> None:
        module = ArchModule(
            name="core",
            paths=["src/bernstein/core/**"],
            forbidden_imports=["bernstein.cli"],
        )
        config = self._make_config([module], block=False)
        diff = (
            "+++ b/src/bernstein/core/foo.py\n"
            "@@ -0,0 +1 @@\n"
            "+from bernstein.cli import run\n"
        )
        results = check_arch_conformance(diff, config)
        assert any(d.type == DecisionType.ASK for d in results)
        assert not any(d.type == DecisionType.DENY for d in results)

    def test_violation_reason_includes_file_and_module(self) -> None:
        module = ArchModule(
            name="core",
            paths=["src/bernstein/core/**"],
            forbidden_imports=["bernstein.cli"],
        )
        config = self._make_config([module])
        diff = (
            "+++ b/src/bernstein/core/foo.py\n"
            "@@ -0,0 +1 @@\n"
            "+from bernstein.cli import run\n"
        )
        results = check_arch_conformance(diff, config)
        deny_decisions = [d for d in results if d.type == DecisionType.DENY]
        assert any("foo.py" in d.reason for d in deny_decisions)
        assert any("bernstein.cli" in d.reason for d in deny_decisions)

    def test_uncovered_file_returns_allow(self) -> None:
        # File is not in any module's paths → no rules apply
        module = ArchModule(
            name="core",
            paths=["src/bernstein/core/**"],
            forbidden_imports=["bernstein.cli"],
        )
        config = self._make_config([module])
        diff = (
            "+++ b/src/bernstein/adapters/claude.py\n"
            "@@ -0,0 +1 @@\n"
            "+from bernstein.cli import run\n"
        )
        results = check_arch_conformance(diff, config)
        assert all(d.type == DecisionType.ALLOW for d in results)

    def test_allowed_imports_whitelist_mode(self) -> None:
        module = ArchModule(
            name="adapters",
            paths=["src/bernstein/adapters/**"],
            allowed_imports=["bernstein.adapters", "bernstein.core"],
        )
        config = self._make_config([module])
        # bernstein.cli is NOT in the allowed list → violation
        diff = (
            "+++ b/src/bernstein/adapters/claude.py\n"
            "@@ -0,0 +1 @@\n"
            "+from bernstein.cli import run\n"
        )
        results = check_arch_conformance(diff, config)
        assert any(d.type == DecisionType.DENY for d in results)

    def test_multiple_violations_multiple_decisions(self) -> None:
        module = ArchModule(
            name="core",
            paths=["src/bernstein/core/**"],
            forbidden_imports=["bernstein.cli", "bernstein.adapters"],
        )
        config = self._make_config([module])
        diff = (
            "+++ b/src/bernstein/core/foo.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+from bernstein.cli import a\n"
            "+from bernstein.adapters import b\n"
        )
        results = check_arch_conformance(diff, config)
        deny_decisions = [d for d in results if d.type == DecisionType.DENY]
        assert len(deny_decisions) >= 2

    def test_empty_diff_returns_allow(self) -> None:
        module = ArchModule(name="core", paths=["src/**"], forbidden_imports=["bernstein.cli"])
        config = self._make_config([module])
        results = check_arch_conformance("", config)
        assert all(d.type == DecisionType.ALLOW for d in results)


# ---------------------------------------------------------------------------
# Summary formatter
# ---------------------------------------------------------------------------


class TestArchConformanceSummary:
    """Test arch_conformance_summary() output formatting."""

    def test_no_violations_returns_clean_message(self) -> None:
        from bernstein.core.policy_engine import PermissionDecision

        decisions = [PermissionDecision(type=DecisionType.ALLOW, reason="all good")]
        summary = arch_conformance_summary(decisions)
        assert "No architecture violations" in summary

    def test_violations_listed_in_summary(self) -> None:
        from bernstein.core.policy_engine import PermissionDecision

        decisions = [
            PermissionDecision(type=DecisionType.DENY, reason="bernstein.cli is forbidden in core"),
        ]
        summary = arch_conformance_summary(decisions)
        assert "1 violation" in summary
        assert "bernstein.cli" in summary


# ---------------------------------------------------------------------------
# Config schema integration
# ---------------------------------------------------------------------------


class TestArchConformanceConfigSchema:
    """Verify ArchConformanceSchema is wired into BernsteinConfig."""

    def test_schema_field_exists(self) -> None:
        from bernstein.core.config_schema import BernsteinConfig

        config = BernsteinConfig(goal="test")
        assert hasattr(config, "arch_conformance")
        assert config.arch_conformance is None  # default is None

    def test_schema_parses_yaml_config(self) -> None:
        from bernstein.core.config_schema import ArchConformanceSchema

        data = {
            "enabled": True,
            "block_on_violation": False,
            "modules": [
                {
                    "name": "core",
                    "paths": ["src/bernstein/core/**"],
                    "forbidden_imports": ["bernstein.cli"],
                }
            ],
        }
        schema = ArchConformanceSchema.model_validate(data)
        assert schema.enabled is True
        assert schema.block_on_violation is False
        assert len(schema.modules) == 1
        assert schema.modules[0].name == "core"
        assert "bernstein.cli" in schema.modules[0].forbidden_imports

    def test_schema_defaults(self) -> None:
        from bernstein.core.config_schema import ArchConformanceSchema

        schema = ArchConformanceSchema()
        assert schema.enabled is False
        assert schema.block_on_violation is True
        assert schema.modules == []


# ---------------------------------------------------------------------------
# Guardrails integration
# ---------------------------------------------------------------------------


class TestGuardrailsIntegration:
    """Verify arch_conformance is invoked from run_guardrails()."""

    def test_guardrails_config_has_arch_conformance_field(self) -> None:
        from bernstein.core.guardrails import GuardrailsConfig

        config = GuardrailsConfig()
        assert hasattr(config, "arch_conformance")
        assert config.arch_conformance is None

    def test_arch_violations_appear_in_guardrail_results(self) -> None:
        import tempfile
        from pathlib import Path

        from bernstein.core.arch_conformance import ArchConformanceConfig, ArchModule
        from bernstein.core.guardrails import GuardrailsConfig, run_guardrails
        from bernstein.core.models import Task

        module = ArchModule(
            name="core",
            paths=["src/bernstein/core/**"],
            forbidden_imports=["bernstein.cli"],
        )
        arch_config = ArchConformanceConfig(enabled=True, modules=[module], block_on_violation=True)
        config = GuardrailsConfig(
            secrets=False,
            scope=False,
            file_permissions=False,
            license_scan=False,
            readme_reminder=False,
            arch_conformance=arch_config,
        )
        task = Task(id="t1", title="test", description="d", role="qa")
        diff = (
            "+++ b/src/bernstein/core/foo.py\n"
            "@@ -0,0 +1 @@\n"
            "+from bernstein.cli import run\n"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            results = run_guardrails(diff, task, config, Path(tmpdir))

        arch_results = [r for r in results if r.check == "arch_conformance"]
        assert len(arch_results) > 0
        assert any(r.blocked for r in arch_results)
