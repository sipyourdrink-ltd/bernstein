"""Tests for bernstein.core.example_gallery.

Covers plan discovery, validation, rendering, and gallery filtering.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from bernstein.core.example_gallery import (
    ExampleGallery,
    ExamplePlan,
    discover_examples,
    render_gallery_index,
    validate_example_plan,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_plan(tmp_path: Path, name: str, **overrides: object) -> Path:
    """Write a minimal valid plan YAML and return its path."""
    plan = {
        "name": name,
        "description": f"Description for {name}",
        "budget": "$10",
        "cli": "auto",
        "stages": [
            {
                "name": "Stage 1",
                "steps": [
                    {
                        "title": "Step 1",
                        "role": "backend",
                        "description": "Do the thing",
                        "completion_signals": [
                            {"type": "path_exists", "path": "src/main.py"},
                        ],
                    }
                ],
            }
        ],
    }
    plan.update(overrides)
    path = tmp_path / f"{name.lower().replace(' ', '-')}.yaml"
    with open(path, "w") as f:
        yaml.dump(plan, f)
    return path


@pytest.fixture
def examples_dir(tmp_path: Path) -> Path:
    """Create an examples directory with plans."""
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()

    # Infrastructure plan
    _write_plan(
        plans_dir, "CI CD Pipeline",
        budget="$15",
        stages=[{
            "name": "Setup",
            "steps": [
                {
                    "title": "Create workflow",
                    "role": "devops",
                    "description": "Set up GitHub Actions",
                    "scope": "small",
                    "complexity": "low",
                    "completion_signals": [{"type": "path_exists", "path": ".github/workflows/ci.yml"}],
                },
                {
                    "title": "Add tests",
                    "role": "qa",
                    "description": "Write unit tests",
                    "scope": "small",
                    "complexity": "low",
                    "completion_signals": [{"type": "test_passes", "command": "pytest"}],
                },
            ],
        }],
    )

    # Backend plan
    _write_plan(
        plans_dir, "REST API",
        budget="$20",
        stages=[{
            "name": "Implementation",
            "steps": [
                {
                    "title": "Build API",
                    "role": "backend",
                    "description": "Create REST endpoints",
                    "scope": "medium",
                    "complexity": "medium",
                    "completion_signals": [{"type": "path_exists", "path": "src/api.py"}],
                },
                {
                    "title": "Add auth",
                    "role": "security",
                    "description": "Add JWT auth",
                    "scope": "medium",
                    "complexity": "high",
                    "completion_signals": [{"type": "command", "run": "pytest"}],
                },
            ],
        }],
    )

    # Simple plan (no stages)
    simple_path = tmp_path / "simple.yaml"
    with open(simple_path, "w") as f:
        yaml.dump({
            "name": "Simple Plan",
            "description": "A minimal plan",
            "budget": "$5",
            "goal": "Do something simple",
        }, f)

    return tmp_path


# ---------------------------------------------------------------------------
# ExamplePlan
# ---------------------------------------------------------------------------


class TestExamplePlan:
    """Tests for the ExamplePlan dataclass."""

    def test_repr(self) -> None:
        plan = ExamplePlan(
            name="Test", description="desc", category="backend",
            difficulty="beginner", estimated_cost_usd=10.0,
            agent_count=2, plan_path=Path("test.yaml"),
            raw={"name": "Test"},
        )
        r = repr(plan)
        assert "Test" in r
        assert "backend" in r


# ---------------------------------------------------------------------------
# discover_examples
# ---------------------------------------------------------------------------


class TestDiscoverExamples:
    """Tests for example discovery."""

    def test_discovers_plans_subdir(self, examples_dir: Path) -> None:
        gallery = discover_examples(examples_dir)
        assert len(gallery) >= 2

    def test_discovers_top_level_yaml(self, examples_dir: Path) -> None:
        gallery = discover_examples(examples_dir)
        names = [e.name for e in gallery.examples]
        assert "Simple Plan" in names

    def test_no_duplicates(self, examples_dir: Path) -> None:
        """Plans with same filename in root and plans/ should not duplicate."""
        gallery = discover_examples(examples_dir)
        names = [e.name for e in gallery.examples]
        assert len(names) == len(set(names))

    def test_missing_dir_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            discover_examples(tmp_path / "nonexistent")

    def test_empty_dir(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        gallery = discover_examples(empty)
        assert len(gallery) == 0

    def test_non_yaml_ignored(self, tmp_path: Path) -> None:
        (tmp_path / "readme.txt").write_text("not yaml")
        gallery = discover_examples(tmp_path)
        assert len(gallery) == 0

    def test_invalid_yaml_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "bad.yaml").write_text("{invalid yaml: [")
        gallery = discover_examples(tmp_path)
        assert len(gallery) == 0

    def test_non_dict_yaml_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "list.yaml").write_text("- just\n- a\n- list")
        gallery = discover_examples(tmp_path)
        assert len(gallery) == 0

    def test_categories_populated(self, examples_dir: Path) -> None:
        gallery = discover_examples(examples_dir)
        assert len(gallery.categories) >= 1

    def test_plan_path_set(self, examples_dir: Path) -> None:
        gallery = discover_examples(examples_dir)
        for plan in gallery.examples:
            assert plan.plan_path.is_file()


# ---------------------------------------------------------------------------
# validate_example_plan
# ---------------------------------------------------------------------------


class TestValidateExamplePlan:
    """Tests for plan validation."""

    def test_valid_plan_no_errors(self, examples_dir: Path) -> None:
        plan_path = examples_dir / "plans" / "ci-cd-pipeline.yaml"
        errors = validate_example_plan(plan_path)
        assert errors == []

    def test_missing_file(self, tmp_path: Path) -> None:
        errors = validate_example_plan(tmp_path / "missing.yaml")
        assert any("not found" in e for e in errors)

    def test_missing_required_field(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        with open(path, "w") as f:
            yaml.dump({"description": "no name or budget"}, f)
        errors = validate_example_plan(path)
        assert any("name" in e for e in errors)
        assert any("budget" in e for e in errors)

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text("{bad yaml: [")
        errors = validate_example_plan(path)
        assert any("YAML" in e for e in errors)

    def test_non_dict_root(self, tmp_path: Path) -> None:
        path = tmp_path / "list.yaml"
        path.write_text("- just a list")
        errors = validate_example_plan(path)
        assert any("mapping" in e.lower() for e in errors)

    def test_invalid_role(self, tmp_path: Path) -> None:
        path = _write_plan(
            tmp_path, "Bad Role",
            stages=[{
                "name": "S1",
                "steps": [{
                    "title": "Step",
                    "role": "nonexistent_role",
                    "description": "desc",
                }],
            }],
        )
        errors = validate_example_plan(path)
        assert any("unknown role" in e for e in errors)

    def test_invalid_complexity(self, tmp_path: Path) -> None:
        path = _write_plan(
            tmp_path, "Bad Complexity",
            stages=[{
                "name": "S1",
                "steps": [{
                    "title": "Step",
                    "role": "backend",
                    "description": "desc",
                    "complexity": "extreme",
                }],
            }],
        )
        errors = validate_example_plan(path)
        assert any("unknown complexity" in e for e in errors)

    def test_invalid_scope(self, tmp_path: Path) -> None:
        path = _write_plan(
            tmp_path, "Bad Scope",
            stages=[{
                "name": "S1",
                "steps": [{
                    "title": "Step",
                    "role": "backend",
                    "description": "desc",
                    "scope": "massive",
                }],
            }],
        )
        errors = validate_example_plan(path)
        assert any("unknown scope" in e for e in errors)

    def test_invalid_signal_type(self, tmp_path: Path) -> None:
        path = _write_plan(
            tmp_path, "Bad Signal",
            stages=[{
                "name": "S1",
                "steps": [{
                    "title": "Step",
                    "role": "backend",
                    "description": "desc",
                    "completion_signals": [{"type": "magic_check"}],
                }],
            }],
        )
        errors = validate_example_plan(path)
        assert any("unknown signal type" in e for e in errors)

    def test_missing_step_fields(self, tmp_path: Path) -> None:
        path = _write_plan(
            tmp_path, "Missing Step Fields",
            stages=[{
                "name": "S1",
                "steps": [{"title": "No role or description"}],
            }],
        )
        errors = validate_example_plan(path)
        assert any("role" in e for e in errors)
        assert any("description" in e for e in errors)

    def test_negative_budget(self, tmp_path: Path) -> None:
        path = _write_plan(tmp_path, "Negative", budget="-$5")
        errors = validate_example_plan(path)
        assert any("non-negative" in e for e in errors)

    def test_validates_existing_plans(self) -> None:
        """Validate all existing plan files in the repo."""
        plans_dir = Path("examples/plans")
        if not plans_dir.is_dir():
            pytest.skip("examples/plans/ not found")
        all_errors: list[str] = []
        for plan_file in sorted(plans_dir.glob("*.yaml")):
            errors = validate_example_plan(plan_file)
            if errors:
                all_errors.append(f"{plan_file.name}: {errors}")
        # Existing plans should be valid or have minimal issues
        for line in all_errors:
            print(line)
        assert len(all_errors) == 0


# ---------------------------------------------------------------------------
# render_gallery_index
# ---------------------------------------------------------------------------


class TestRenderGalleryIndex:
    """Tests for gallery index rendering."""

    def test_renders_heading(self, examples_dir: Path) -> None:
        gallery = discover_examples(examples_dir)
        md = render_gallery_index(gallery)
        assert "# Bernstein Example Gallery" in md

    def test_includes_plan_count(self, examples_dir: Path) -> None:
        gallery = discover_examples(examples_dir)
        md = render_gallery_index(gallery)
        assert f"{len(gallery)} curated" in md

    def test_has_category_sections(self, examples_dir: Path) -> None:
        gallery = discover_examples(examples_dir)
        md = render_gallery_index(gallery)
        for cat in gallery.categories:
            assert cat.title() in md

    def test_has_markdown_table(self, examples_dir: Path) -> None:
        gallery = discover_examples(examples_dir)
        md = render_gallery_index(gallery)
        assert "| Plan |" in md

    def test_includes_plan_links(self, examples_dir: Path) -> None:
        gallery = discover_examples(examples_dir)
        md = render_gallery_index(gallery)
        for plan in gallery.examples:
            assert plan.name in md

    def test_empty_gallery(self) -> None:
        gallery = ExampleGallery(())
        md = render_gallery_index(gallery)
        assert "0 curated" in md


# ---------------------------------------------------------------------------
# ExampleGallery filtering
# ---------------------------------------------------------------------------


class TestExampleGalleryFiltering:
    """Tests for gallery filtering."""

    def test_filter_by_category(self, examples_dir: Path) -> None:
        gallery = discover_examples(examples_dir)
        if len(gallery.categories) < 2:
            pytest.skip("Need multiple categories")
        cat = gallery.categories[0]
        filtered = gallery.filter_by_category(cat)
        assert len(filtered) > 0
        assert all(e.category == cat for e in filtered.examples)

    def test_filter_by_difficulty(self, examples_dir: Path) -> None:
        gallery = discover_examples(examples_dir)
        difficulties = set(e.difficulty for e in gallery.examples)
        if len(difficulties) < 1:
            pytest.skip("Need difficulties")
        diff = next(iter(difficulties))
        filtered = gallery.filter_by_difficulty(diff)
        assert all(e.difficulty == diff for e in filtered.examples)

    def test_filter_empty_result(self, examples_dir: Path) -> None:
        gallery = discover_examples(examples_dir)
        filtered = gallery.filter_by_category("nonexistent-category-xyz")
        assert len(filtered) == 0
