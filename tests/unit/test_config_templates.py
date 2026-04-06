"""Tests for bernstein.core.config_templates (CFG-008)."""

from __future__ import annotations

from bernstein.core.config_templates import (
    ConfigTemplate,
    TemplateRegistry,
    default_registry,
)


class TestConfigTemplate:
    def test_to_yaml(self) -> None:
        t = ConfigTemplate(
            name="test",
            description="Test template",
            config={"goal": "Test", "max_agents": 2},
        )
        yaml_str = t.to_yaml()
        assert "goal: Test" in yaml_str
        assert "max_agents: 2" in yaml_str

    def test_to_dict(self) -> None:
        t = ConfigTemplate(
            name="test",
            description="Test template",
            config={"goal": "Test"},
            tags=("web", "api"),
        )
        d = t.to_dict()
        assert d["name"] == "test"
        assert d["tags"] == ["web", "api"]
        assert d["config"]["goal"] == "Test"


class TestTemplateRegistry:
    def test_register_and_get(self) -> None:
        registry = TemplateRegistry()
        t = ConfigTemplate(name="test", description="Test", config={"goal": "Test"})
        registry.register(t)
        assert registry.get("test") is t

    def test_get_missing_returns_none(self) -> None:
        registry = TemplateRegistry()
        assert registry.get("nonexistent") is None

    def test_list_all_sorted(self) -> None:
        registry = TemplateRegistry()
        registry.register(ConfigTemplate(name="z-last", description="", config={}))
        registry.register(ConfigTemplate(name="a-first", description="", config={}))
        names = [t.name for t in registry.list_all()]
        assert names == ["a-first", "z-last"]

    def test_search_by_tag(self) -> None:
        registry = TemplateRegistry()
        registry.register(ConfigTemplate(name="web", description="", config={}, tags=("web", "api")))
        registry.register(ConfigTemplate(name="cli", description="", config={}, tags=("cli",)))
        results = registry.search("web")
        assert len(results) == 1
        assert results[0].name == "web"

    def test_search_case_insensitive(self) -> None:
        registry = TemplateRegistry()
        registry.register(ConfigTemplate(name="web", description="", config={}, tags=("Web",)))
        results = registry.search("web")
        assert len(results) == 1

    def test_names(self) -> None:
        registry = TemplateRegistry()
        registry.register(ConfigTemplate(name="b", description="", config={}))
        registry.register(ConfigTemplate(name="a", description="", config={}))
        assert registry.names() == ["a", "b"]


class TestDefaultRegistry:
    def test_has_builtin_templates(self) -> None:
        registry = default_registry()
        names = registry.names()
        assert "web-app" in names
        assert "microservices" in names
        assert "monorepo" in names
        assert "data-pipeline" in names
        assert "library" in names

    def test_web_app_has_required_fields(self) -> None:
        registry = default_registry()
        t = registry.get("web-app")
        assert t is not None
        assert "goal" in t.config
        assert "team" in t.config

    def test_all_templates_have_goal(self) -> None:
        registry = default_registry()
        for t in registry.list_all():
            assert "goal" in t.config, f"Template '{t.name}' missing 'goal'"
