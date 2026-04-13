"""Tests for task templates (TASK-015)."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
from bernstein.core.task_templates import (
    BUILTIN_TEMPLATES,
    apply_template,
    get_template,
    list_templates,
    load_custom_templates,
)

# ------------------------------------------------------------------
# Built-in templates
# ------------------------------------------------------------------


class TestBuiltinTemplates:
    """Verify all expected built-in templates exist with correct fields."""

    def test_migration_template(self) -> None:
        tpl = BUILTIN_TEMPLATES["migration"]
        assert tpl.role == "backend"
        assert tpl.scope == "large"
        assert tpl.complexity == "high"
        assert tpl.quality_gates == ["test", "lint", "typecheck"]
        assert tpl.completion_signals == ["tests_passing", "no_regressions"]

    def test_refactor_template(self) -> None:
        tpl = BUILTIN_TEMPLATES["refactor"]
        assert tpl.role == "backend"
        assert tpl.scope == "medium"
        assert tpl.complexity == "medium"
        assert tpl.quality_gates == ["test", "lint"]
        assert tpl.completion_signals == ["tests_passing"]

    def test_test_template(self) -> None:
        tpl = BUILTIN_TEMPLATES["test"]
        assert tpl.role == "qa"
        assert tpl.scope == "small"
        assert tpl.complexity == "low"
        assert tpl.quality_gates == ["lint"]
        assert tpl.completion_signals == ["coverage_threshold"]

    def test_security_audit_template(self) -> None:
        tpl = BUILTIN_TEMPLATES["security-audit"]
        assert tpl.role == "security"
        assert tpl.scope == "medium"
        assert tpl.complexity == "medium"
        assert tpl.quality_gates == ["security_scan"]
        assert tpl.completion_signals == ["no_vulnerabilities"]

    def test_docs_template(self) -> None:
        tpl = BUILTIN_TEMPLATES["docs"]
        assert tpl.role == "docs"
        assert tpl.scope == "small"
        assert tpl.complexity == "low"
        assert tpl.quality_gates == ["spell_check"]
        assert tpl.completion_signals == ["build_success"]

    def test_all_templates_frozen(self) -> None:
        for tpl in BUILTIN_TEMPLATES.values():
            with pytest.raises(AttributeError):
                tpl.role = "other"  # type: ignore[misc]

    def test_all_templates_have_tags(self) -> None:
        for tid, tpl in BUILTIN_TEMPLATES.items():
            assert isinstance(tpl.tags, list), f"{tid} has no tags list"
            assert len(tpl.tags) > 0, f"{tid} tags list is empty"


# ------------------------------------------------------------------
# get_template
# ------------------------------------------------------------------


class TestGetTemplate:
    def test_known_template(self) -> None:
        tpl = get_template("migration")
        assert tpl is not None
        assert tpl.template_id == "migration"

    def test_unknown_template(self) -> None:
        assert get_template("nonexistent") is None

    def test_all_builtins_retrievable(self) -> None:
        for tid in BUILTIN_TEMPLATES:
            assert get_template(tid) is not None


# ------------------------------------------------------------------
# list_templates
# ------------------------------------------------------------------


class TestListTemplates:
    def test_returns_sorted(self) -> None:
        result = list_templates()
        assert result == sorted(result)

    def test_contains_all_builtins(self) -> None:
        result = list_templates()
        for tid in BUILTIN_TEMPLATES:
            assert tid in result

    def test_returns_list_of_strings(self) -> None:
        for item in list_templates():
            assert isinstance(item, str)


# ------------------------------------------------------------------
# apply_template
# ------------------------------------------------------------------


class TestApplyTemplate:
    def test_no_overrides(self) -> None:
        tpl = BUILTIN_TEMPLATES["refactor"]
        result = apply_template(tpl)
        assert result["role"] == "backend"
        assert result["scope"] == "medium"
        assert result["complexity"] == "medium"
        assert result["quality_gates"] == ["test", "lint"]

    def test_with_overrides(self) -> None:
        tpl = BUILTIN_TEMPLATES["refactor"]
        result = apply_template(tpl, {"scope": "large", "role": "frontend"})
        assert result["scope"] == "large"
        assert result["role"] == "frontend"
        # Non-overridden fields stay the same.
        assert result["complexity"] == "medium"

    def test_override_adds_extra_keys(self) -> None:
        tpl = BUILTIN_TEMPLATES["test"]
        result = apply_template(tpl, {"priority": "high"})
        assert result["priority"] == "high"
        assert result["role"] == "qa"

    def test_returns_dict(self) -> None:
        tpl = BUILTIN_TEMPLATES["docs"]
        result = apply_template(tpl)
        assert isinstance(result, dict)

    def test_empty_overrides(self) -> None:
        tpl = BUILTIN_TEMPLATES["migration"]
        result = apply_template(tpl, {})
        assert result["role"] == "backend"

    def test_does_not_mutate_template(self) -> None:
        tpl = BUILTIN_TEMPLATES["migration"]
        result = apply_template(tpl, {"role": "frontend"})
        assert result["role"] == "frontend"
        assert tpl.role == "backend"

    def test_lists_are_copies(self) -> None:
        """Modifying returned lists must not affect the template."""
        tpl = BUILTIN_TEMPLATES["migration"]
        result = apply_template(tpl)
        result["quality_gates"].append("extra")
        assert "extra" not in tpl.quality_gates


# ------------------------------------------------------------------
# load_custom_templates
# ------------------------------------------------------------------


class TestLoadCustomTemplates:
    def test_load_with_task_templates_key(self, tmp_path: Path) -> None:
        yaml_content = dedent("""\
            task_templates:
              perf-test:
                name: Performance Test
                description: Run benchmarks.
                role: qa
                scope: medium
                complexity: medium
                quality_gates: [benchmark]
                completion_signals: [no_regressions]
                tags: [perf]
        """)
        p = tmp_path / "templates.yaml"
        p.write_text(yaml_content)

        result = load_custom_templates(p)
        assert "perf-test" in result
        tpl = result["perf-test"]
        assert tpl.role == "qa"
        assert tpl.quality_gates == ["benchmark"]
        assert tpl.tags == ["perf"]

    def test_load_bare_mapping(self, tmp_path: Path) -> None:
        yaml_content = dedent("""\
            hotfix:
              name: Hotfix
              description: Emergency fix.
              role: backend
              scope: small
              complexity: high
              quality_gates: [test, lint]
              completion_signals: [tests_passing]
              tags: [hotfix]
        """)
        p = tmp_path / "templates.yaml"
        p.write_text(yaml_content)

        result = load_custom_templates(p)
        assert "hotfix" in result
        assert result["hotfix"].scope == "small"

    def test_missing_file(self, tmp_path: Path) -> None:
        result = load_custom_templates(tmp_path / "missing.yaml")
        assert result == {}

    def test_malformed_yaml(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.yaml"
        p.write_text("[invalid: yaml: {{{}}}}")

        result = load_custom_templates(p)
        assert result == {}

    def test_non_mapping_yaml(self, tmp_path: Path) -> None:
        p = tmp_path / "list.yaml"
        p.write_text("- item1\n- item2\n")

        result = load_custom_templates(p)
        assert result == {}

    def test_defaults_for_missing_fields(self, tmp_path: Path) -> None:
        yaml_content = dedent("""\
            minimal:
              name: Minimal
        """)
        p = tmp_path / "templates.yaml"
        p.write_text(yaml_content)

        result = load_custom_templates(p)
        assert "minimal" in result
        tpl = result["minimal"]
        assert tpl.role == "backend"
        assert tpl.scope == "medium"
        assert tpl.complexity == "medium"
        assert tpl.quality_gates == []
        assert tpl.completion_signals == []
        assert tpl.tags == []

    def test_multiple_templates(self, tmp_path: Path) -> None:
        yaml_content = dedent("""\
            task_templates:
              a:
                name: Template A
                role: backend
              b:
                name: Template B
                role: frontend
        """)
        p = tmp_path / "templates.yaml"
        p.write_text(yaml_content)

        result = load_custom_templates(p)
        assert len(result) == 2
        assert result["a"].role == "backend"
        assert result["b"].role == "frontend"

    def test_skips_non_mapping_entries(self, tmp_path: Path) -> None:
        yaml_content = dedent("""\
            task_templates:
              valid:
                name: Valid
                role: backend
              invalid_entry: just_a_string
        """)
        p = tmp_path / "templates.yaml"
        p.write_text(yaml_content)

        result = load_custom_templates(p)
        assert "valid" in result
        assert "invalid_entry" not in result
