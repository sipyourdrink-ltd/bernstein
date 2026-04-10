"""Tests for per-language quickstart templates."""

from __future__ import annotations

import yaml

from bernstein.cli.quickstart_templates import (
    TEMPLATES,
    LanguageTemplate,
    generate_bernstein_yaml,
    generate_example_plan,
    get_template,
    list_available_templates,
)

_EXPECTED_LANGUAGES = ["go", "java", "python", "rust", "typescript"]


class TestTemplateRegistry:
    """All five templates exist with required fields."""

    def test_all_five_templates_present(self) -> None:
        assert sorted(TEMPLATES.keys()) == _EXPECTED_LANGUAGES

    def test_templates_are_frozen_dataclasses(self) -> None:
        for tmpl in TEMPLATES.values():
            assert isinstance(tmpl, LanguageTemplate)

    def test_each_template_has_language_and_display_name(self) -> None:
        for key, tmpl in TEMPLATES.items():
            assert tmpl.language == key
            assert isinstance(tmpl.display_name, str)
            assert len(tmpl.display_name) > 0

    def test_each_template_has_roles(self) -> None:
        for tmpl in TEMPLATES.values():
            assert isinstance(tmpl.roles, list)
            assert len(tmpl.roles) >= 2
            for role in tmpl.roles:
                assert isinstance(role, str)

    def test_each_template_has_quality_gates(self) -> None:
        for tmpl in TEMPLATES.values():
            assert isinstance(tmpl.quality_gates, list)
            assert len(tmpl.quality_gates) >= 2
            for gate in tmpl.quality_gates:
                assert isinstance(gate, str)

    def test_each_template_has_three_example_tasks(self) -> None:
        for tmpl in TEMPLATES.values():
            assert len(tmpl.example_tasks) == 3
            for task in tmpl.example_tasks:
                assert "title" in task
                assert "goal" in task
                assert isinstance(task["title"], str)
                assert isinstance(task["goal"], str)

    def test_each_template_has_yaml_snippet(self) -> None:
        for tmpl in TEMPLATES.values():
            assert isinstance(tmpl.bernstein_yaml_snippet, str)
            assert len(tmpl.bernstein_yaml_snippet) > 0

    def test_python_template_specifics(self) -> None:
        tmpl = TEMPLATES["python"]
        assert tmpl.roles == ["backend", "qa", "security"]
        assert tmpl.quality_gates == ["ruff", "pytest", "pyright"]

    def test_typescript_template_specifics(self) -> None:
        tmpl = TEMPLATES["typescript"]
        assert tmpl.roles == ["frontend", "backend", "qa"]
        assert tmpl.quality_gates == ["eslint", "jest", "tsc"]

    def test_rust_template_specifics(self) -> None:
        tmpl = TEMPLATES["rust"]
        assert tmpl.roles == ["backend", "qa", "security"]
        assert tmpl.quality_gates == ["clippy", "cargo-test"]

    def test_go_template_specifics(self) -> None:
        tmpl = TEMPLATES["go"]
        assert tmpl.roles == ["backend", "qa"]
        assert tmpl.quality_gates == ["golint", "go-test"]

    def test_java_template_specifics(self) -> None:
        tmpl = TEMPLATES["java"]
        assert tmpl.roles == ["backend", "qa"]
        assert tmpl.quality_gates == ["checkstyle", "junit"]


class TestGetTemplate:
    """get_template returns correct template or None."""

    def test_returns_correct_template(self) -> None:
        for lang in _EXPECTED_LANGUAGES:
            tmpl = get_template(lang)
            assert tmpl is not None
            assert tmpl.language == lang

    def test_case_insensitive(self) -> None:
        assert get_template("Python") is not None
        assert get_template("RUST") is not None
        assert get_template("TypeScript") is not None

    def test_returns_none_for_unknown(self) -> None:
        assert get_template("cobol") is None
        assert get_template("") is None
        assert get_template("fortran") is None


class TestListAvailableTemplates:
    """list_available_templates returns all five, sorted."""

    def test_returns_sorted_list(self) -> None:
        result = list_available_templates()
        assert result == _EXPECTED_LANGUAGES

    def test_returns_list_of_strings(self) -> None:
        result = list_available_templates()
        for item in result:
            assert isinstance(item, str)

    def test_count(self) -> None:
        assert len(list_available_templates()) == 5


class TestGenerateBernsteinYaml:
    """generate_bernstein_yaml produces valid YAML."""

    def test_valid_yaml_for_each_template(self) -> None:
        for tmpl in TEMPLATES.values():
            output = generate_bernstein_yaml(tmpl)
            parsed = yaml.safe_load(output)
            assert isinstance(parsed, dict)

    def test_contains_roles(self) -> None:
        tmpl = TEMPLATES["python"]
        output = generate_bernstein_yaml(tmpl)
        parsed = yaml.safe_load(output)
        assert parsed["roles"] == ["backend", "qa", "security"]

    def test_contains_quality_gates(self) -> None:
        tmpl = TEMPLATES["rust"]
        output = generate_bernstein_yaml(tmpl)
        parsed = yaml.safe_load(output)
        assert parsed["quality_gates"] == ["clippy", "cargo-test"]

    def test_contains_agent_config(self) -> None:
        tmpl = TEMPLATES["go"]
        output = generate_bernstein_yaml(tmpl)
        parsed = yaml.safe_load(output)
        assert "agent" in parsed
        assert parsed["agent"]["max_workers"] == 3

    def test_contains_goal(self) -> None:
        tmpl = TEMPLATES["typescript"]
        output = generate_bernstein_yaml(tmpl)
        parsed = yaml.safe_load(output)
        assert "goal" in parsed
        assert "TypeScript" in parsed["goal"]

    def test_contains_language_specific_fields(self) -> None:
        tmpl = TEMPLATES["java"]
        output = generate_bernstein_yaml(tmpl)
        parsed = yaml.safe_load(output)
        assert parsed["language"] == "java"
        assert "mvn test" in parsed["test_command"]


class TestGenerateExamplePlan:
    """generate_example_plan contains task definitions."""

    def test_valid_yaml_for_each_template(self) -> None:
        for tmpl in TEMPLATES.values():
            output = generate_example_plan(tmpl)
            parsed = yaml.safe_load(output)
            assert isinstance(parsed, dict)

    def test_contains_stages(self) -> None:
        for tmpl in TEMPLATES.values():
            output = generate_example_plan(tmpl)
            parsed = yaml.safe_load(output)
            assert "stages" in parsed
            assert len(parsed["stages"]) == 1

    def test_stage_has_steps_matching_example_tasks(self) -> None:
        tmpl = TEMPLATES["python"]
        output = generate_example_plan(tmpl)
        parsed = yaml.safe_load(output)
        steps = parsed["stages"][0]["steps"]
        assert len(steps) == 3

    def test_steps_have_required_fields(self) -> None:
        tmpl = TEMPLATES["typescript"]
        output = generate_example_plan(tmpl)
        parsed = yaml.safe_load(output)
        for step in parsed["stages"][0]["steps"]:
            assert "goal" in step
            assert "role" in step
            assert "priority" in step
            assert "complexity" in step

    def test_step_goals_match_example_tasks(self) -> None:
        tmpl = TEMPLATES["rust"]
        output = generate_example_plan(tmpl)
        parsed = yaml.safe_load(output)
        steps = parsed["stages"][0]["steps"]
        for step, task in zip(steps, tmpl.example_tasks, strict=True):
            assert step["goal"] == task["goal"]

    def test_stage_name_includes_language(self) -> None:
        for tmpl in TEMPLATES.values():
            output = generate_example_plan(tmpl)
            parsed = yaml.safe_load(output)
            stage_name = parsed["stages"][0]["name"]
            assert tmpl.language in stage_name
