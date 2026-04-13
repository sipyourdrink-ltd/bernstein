"""Unit tests for project recommendation rendering."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from bernstein.core.context_recommendations import RecommendationEngine


def _write_yaml(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_load_from_claude_md(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("NEVER run `pytest tests/`\n", encoding="utf-8")

    engine = RecommendationEngine(tmp_path)
    engine.build()
    recs = engine.for_role("backend")

    assert len(recs) == 1
    assert recs[0].severity == "critical"
    assert recs[0].category == "testing"
    assert recs[0].source == "claude_md"


def test_load_from_recommendations_yaml(tmp_path: Path) -> None:
    _write_yaml(
        tmp_path / ".sdd" / "recommendations.yaml",
        {
            "recommendations": [
                {
                    "id": "use-uv-run",
                    "category": "tool_usage",
                    "severity": "critical",
                    "text": "Always prefix Python commands with `uv run`",
                    "applies_to": [],
                },
                {
                    "id": "qa-tests",
                    "category": "testing",
                    "severity": "important",
                    "text": "Use the targeted test runner",
                    "applies_to": ["qa"],
                },
            ]
        },
    )

    engine = RecommendationEngine(tmp_path)
    engine.build()

    assert [rec.id for rec in engine.for_role("backend")] == ["use-uv-run"]
    assert {rec.id for rec in engine.for_role("qa")} == {"use-uv-run", "qa-tests"}


def test_for_role_filters_by_role(tmp_path: Path) -> None:
    _write_yaml(
        tmp_path / ".sdd" / "recommendations.yaml",
        {
            "recommendations": [
                {"id": "all", "text": "Always use `uv run`", "severity": "critical", "applies_to": []},
                {
                    "id": "backend",
                    "text": "Run backend smoke tests",
                    "severity": "important",
                    "applies_to": ["backend"],
                },
                {"id": "qa", "text": "Use QA fixtures", "severity": "important", "applies_to": ["qa"]},
            ]
        },
    )

    engine = RecommendationEngine(tmp_path)
    engine.build()

    assert {rec.id for rec in engine.for_role("backend")} == {"all", "backend"}
    assert {rec.id for rec in engine.for_role("qa")} == {"all", "qa"}
    assert [rec.id for rec in engine.for_role("frontend")] == ["all"]


def test_render_for_prompt_groups_by_severity(tmp_path: Path) -> None:
    _write_yaml(
        tmp_path / ".sdd" / "recommendations.yaml",
        {
            "recommendations": [
                {"id": "suggestion", "text": "Prefer dataclasses", "severity": "suggestion"},
                {"id": "critical", "text": "Never run `pytest tests/`", "severity": "critical"},
                {"id": "important", "text": "Always use `uv run`", "severity": "important"},
            ]
        },
    )

    rendered = RecommendationEngine(tmp_path).render_for_prompt("backend")

    assert rendered.index("**CRITICAL:**") < rendered.index("**IMPORTANT:**") < rendered.index("**SUGGESTIONS:**")


def test_render_for_prompt_truncates(tmp_path: Path) -> None:
    _write_yaml(
        tmp_path / ".sdd" / "recommendations.yaml",
        {
            "recommendations": [
                {
                    "id": f"critical-{idx}",
                    "text": f"Never do thing number {idx} in the repo because it breaks orchestration stability",
                    "severity": "critical",
                }
                for idx in range(20)
            ]
        },
    )

    rendered = RecommendationEngine(tmp_path).render_for_prompt("backend", max_chars=500)

    assert len(rendered) <= 500
    assert "CRITICAL" in rendered


def test_load_from_failure_history(tmp_path: Path) -> None:
    metrics_dir = tmp_path / ".sdd" / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    (metrics_dir / "quality_gates.jsonl").write_text(
        "\n".join(json.dumps({"gate": "lint", "result": "blocked"}) for _ in range(3)),
        encoding="utf-8",
    )

    engine = RecommendationEngine(tmp_path)
    engine.build()

    assert any("lint failures" in rec.text.lower() for rec in engine.for_role("backend"))


def test_empty_project(tmp_path: Path) -> None:
    engine = RecommendationEngine(tmp_path)
    engine.build()

    assert engine.for_role("backend") == []
    assert engine.render_for_prompt("backend") == ""
