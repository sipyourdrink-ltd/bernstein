"""Tests for project stack auto-detection."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from bernstein.core.stack_detector import StackInfo, compatibility_report, detect_stack

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# detect_stack — language detection
# ---------------------------------------------------------------------------


def test_detect_python(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")
    stack = detect_stack(tmp_path)
    assert "Python" in stack.languages


def test_detect_javascript(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    stack = detect_stack(tmp_path)
    assert "JavaScript" in stack.languages


def test_detect_typescript_replaces_js(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
    stack = detect_stack(tmp_path)
    assert "TypeScript" in stack.languages
    assert "JavaScript" not in stack.languages


def test_detect_go(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/foo\n", encoding="utf-8")
    stack = detect_stack(tmp_path)
    assert "Go" in stack.languages


def test_detect_rust(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    stack = detect_stack(tmp_path)
    assert "Rust" in stack.languages


def test_detect_java(tmp_path: Path) -> None:
    (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
    stack = detect_stack(tmp_path)
    assert "Java" in stack.languages


# ---------------------------------------------------------------------------
# detect_stack — package managers
# ---------------------------------------------------------------------------


def test_detect_uv_lock(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    stack = detect_stack(tmp_path)
    assert "uv" in stack.package_managers


def test_detect_yarn_lock(tmp_path: Path) -> None:
    (tmp_path / "yarn.lock").write_text("", encoding="utf-8")
    stack = detect_stack(tmp_path)
    assert "yarn" in stack.package_managers


def test_detect_cargo_lock(tmp_path: Path) -> None:
    (tmp_path / "Cargo.lock").write_text("", encoding="utf-8")
    stack = detect_stack(tmp_path)
    assert "cargo" in stack.package_managers


# ---------------------------------------------------------------------------
# detect_stack — CI systems
# ---------------------------------------------------------------------------


def test_detect_github_actions(tmp_path: Path) -> None:
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    stack = detect_stack(tmp_path)
    assert "GitHub Actions" in stack.ci_systems


def test_detect_gitlab_ci(tmp_path: Path) -> None:
    (tmp_path / ".gitlab-ci.yml").write_text("stages:\n", encoding="utf-8")
    stack = detect_stack(tmp_path)
    assert "GitLab CI" in stack.ci_systems


def test_detect_jenkins(tmp_path: Path) -> None:
    (tmp_path / "Jenkinsfile").write_text("pipeline {}\n", encoding="utf-8")
    stack = detect_stack(tmp_path)
    assert "Jenkins" in stack.ci_systems


# ---------------------------------------------------------------------------
# detect_stack — framework detection
# ---------------------------------------------------------------------------


def test_detect_python_framework_fastapi(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["fastapi>=0.100"]\n',
        encoding="utf-8",
    )
    stack = detect_stack(tmp_path)
    assert "FastAPI" in stack.frameworks


def test_detect_js_framework_react(tmp_path: Path) -> None:
    pkg = {"dependencies": {"react": "^18.0.0"}}
    (tmp_path / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
    stack = detect_stack(tmp_path)
    assert "React" in stack.frameworks


def test_detect_go_framework_gin(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text(
        "module example.com/foo\nrequire github.com/gin-gonic/gin v1.9.0\n",
        encoding="utf-8",
    )
    stack = detect_stack(tmp_path)
    assert "Gin" in stack.frameworks


def test_detect_rust_framework_actix(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text(
        '[dependencies]\nactix-web = "4"\n',
        encoding="utf-8",
    )
    stack = detect_stack(tmp_path)
    assert "Actix" in stack.frameworks


# ---------------------------------------------------------------------------
# detect_stack — empty project
# ---------------------------------------------------------------------------


def test_detect_empty_project(tmp_path: Path) -> None:
    stack = detect_stack(tmp_path)
    assert stack.languages == []
    assert stack.frameworks == []
    assert stack.package_managers == []
    assert stack.ci_systems == []


# ---------------------------------------------------------------------------
# detect_stack — multi-language
# ---------------------------------------------------------------------------


def test_detect_multi_language(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[build]\n", encoding="utf-8")
    (tmp_path / "go.mod").write_text("module m\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    stack = detect_stack(tmp_path)
    assert "Python" in stack.languages
    assert "Go" in stack.languages
    assert "uv" in stack.package_managers
    assert "GitHub Actions" in stack.ci_systems


# ---------------------------------------------------------------------------
# compatibility_report
# ---------------------------------------------------------------------------


def test_compatibility_report_python() -> None:
    stack = StackInfo(languages=["Python"], frameworks=["FastAPI"], package_managers=["uv"])
    report = compatibility_report(stack)
    assert "claude" in report["recommended_adapters"]
    assert "Python" in report["language_coverage"]
    assert "Python" in report["stack_summary"]
    assert "FastAPI" in report["stack_summary"]


def test_compatibility_report_empty_stack() -> None:
    stack = StackInfo()
    report = compatibility_report(stack)
    assert "claude" in report["recommended_adapters"]
    assert "generic" in report["recommended_adapters"]
    assert report["stack_summary"] == "No stack detected"


def test_compatibility_report_multi_lang() -> None:
    stack = StackInfo(languages=["Python", "Rust"])
    report = compatibility_report(stack)
    assert "Python" in report["language_coverage"]
    assert "Rust" in report["language_coverage"]
    # Adapters from both languages should be merged
    assert "claude" in report["recommended_adapters"]
