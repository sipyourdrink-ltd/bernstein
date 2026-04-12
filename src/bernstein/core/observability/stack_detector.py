"""Project stack auto-detection.

Scans a project directory for marker files (``pyproject.toml``,
``package.json``, ``go.mod``, etc.) and reports detected languages,
frameworks, package managers, and CI systems.  The compatibility report
maps detected stack elements to Bernstein adapter recommendations.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path  # noqa: TC003 — used at runtime
from typing import Any

logger = logging.getLogger(__name__)

# ── Marker-file → language mapping ──────────────────────────────────────────

_LANGUAGE_MARKERS: dict[str, str] = {
    "pyproject.toml": "Python",
    "setup.py": "Python",
    "requirements.txt": "Python",
    "package.json": "JavaScript",
    "go.mod": "Go",
    "Cargo.toml": "Rust",
    "pom.xml": "Java",
    "build.gradle": "Java",
}

# ── Package-manager lock files ──────────────────────────────────────────────

_PACKAGE_MANAGER_MARKERS: dict[str, str] = {
    "uv.lock": "uv",
    "poetry.lock": "poetry",
    "Pipfile.lock": "pipenv",
    "yarn.lock": "yarn",
    "pnpm-lock.yaml": "pnpm",
    "package-lock.json": "npm",
    "Cargo.lock": "cargo",
    "go.sum": "go modules",
}

# ── CI system markers ──────────────────────────────────────────────────────

_CI_DIR_MARKERS: dict[str, str] = {
    ".github/workflows": "GitHub Actions",
}

_CI_FILE_MARKERS: dict[str, str] = {
    ".gitlab-ci.yml": "GitLab CI",
    "Jenkinsfile": "Jenkins",
    ".circleci/config.yml": "CircleCI",
    ".travis.yml": "Travis CI",
}

# ── Framework detection in dependency files ────────────────────────────────

_PYTHON_FRAMEWORKS: dict[str, str] = {
    "django": "Django",
    "flask": "Flask",
    "fastapi": "FastAPI",
    "starlette": "Starlette",
}

_JS_FRAMEWORKS: dict[str, str] = {
    "react": "React",
    "next": "Next.js",
    "express": "Express",
    "vue": "Vue",
    "angular": "Angular",
}

_GO_FRAMEWORKS: dict[str, str] = {
    "gin-gonic/gin": "Gin",
    "gorilla/mux": "Gorilla",
}

_RUST_FRAMEWORKS: dict[str, str] = {
    "actix-web": "Actix",
    "rocket": "Rocket",
    "axum": "Axum",
}

# ── Adapter recommendations ────────────────────────────────────────────────

_LANGUAGE_ADAPTERS: dict[str, list[str]] = {
    "Python": ["claude", "codex", "aider", "gemini"],
    "JavaScript": ["claude", "codex", "gemini", "cursor"],
    "Go": ["claude", "codex", "gemini"],
    "Rust": ["claude", "codex", "gemini"],
    "Java": ["claude", "codex", "gemini"],
}


@dataclass
class StackInfo:
    """Detected project stack information.

    Attributes:
        languages: Programming languages found in the project.
        frameworks: Frameworks detected from dependency manifests.
        package_managers: Package managers identified by lock files.
        ci_systems: CI/CD systems detected by config files.
    """

    languages: list[str] = field(default_factory=list[str])
    frameworks: list[str] = field(default_factory=list[str])
    package_managers: list[str] = field(default_factory=list[str])
    ci_systems: list[str] = field(default_factory=list[str])


def detect_stack(project_dir: Path) -> StackInfo:
    """Detect the technology stack of a project directory.

    Checks for language marker files, dependency manifests, lock files,
    and CI configuration to build a comprehensive stack profile.

    Args:
        project_dir: Root directory of the project to scan.

    Returns:
        Populated :class:`StackInfo` with deduplicated, sorted lists.
    """
    languages: set[str] = set()
    frameworks: set[str] = set()
    package_managers: set[str] = set()
    ci_systems: set[str] = set()

    # Languages
    for marker, lang in _LANGUAGE_MARKERS.items():
        if (project_dir / marker).exists():
            languages.add(lang)

    # Check for TypeScript (tsconfig.json or .ts files referenced in package.json)
    if (project_dir / "tsconfig.json").exists():
        languages.add("TypeScript")
        languages.discard("JavaScript")

    # Package managers
    for marker, pm in _PACKAGE_MANAGER_MARKERS.items():
        if (project_dir / marker).exists():
            package_managers.add(pm)

    # CI systems
    for marker, ci in _CI_DIR_MARKERS.items():
        if (project_dir / marker).is_dir():
            ci_systems.add(ci)
    for marker, ci in _CI_FILE_MARKERS.items():
        if (project_dir / marker).exists():
            ci_systems.add(ci)

    # Framework detection from dependency files
    _detect_python_frameworks(project_dir, frameworks)
    _detect_js_frameworks(project_dir, frameworks)
    _detect_go_frameworks(project_dir, frameworks)
    _detect_rust_frameworks(project_dir, frameworks)

    return StackInfo(
        languages=sorted(languages),
        frameworks=sorted(frameworks),
        package_managers=sorted(package_managers),
        ci_systems=sorted(ci_systems),
    )


def compatibility_report(stack: StackInfo) -> dict[str, Any]:
    """Generate a Bernstein adapter compatibility report for a detected stack.

    Args:
        stack: Stack information from :func:`detect_stack`.

    Returns:
        Dict with ``"recommended_adapters"`` (list of adapter names),
        ``"language_coverage"`` (mapping of language to adapter list),
        and ``"stack_summary"`` (human-readable summary string).
    """
    recommended: set[str] = set()
    coverage: dict[str, list[str]] = {}

    for lang in stack.languages:
        adapters = _LANGUAGE_ADAPTERS.get(lang, ["claude", "generic"])
        coverage[lang] = adapters
        recommended.update(adapters)

    if not recommended:
        recommended = {"claude", "generic"}

    summary_parts: list[str] = []
    if stack.languages:
        summary_parts.append(f"Languages: {', '.join(stack.languages)}")
    if stack.frameworks:
        summary_parts.append(f"Frameworks: {', '.join(stack.frameworks)}")
    if stack.package_managers:
        summary_parts.append(f"Package managers: {', '.join(stack.package_managers)}")
    if stack.ci_systems:
        summary_parts.append(f"CI: {', '.join(stack.ci_systems)}")

    return {
        "recommended_adapters": sorted(recommended),
        "language_coverage": coverage,
        "stack_summary": "; ".join(summary_parts) if summary_parts else "No stack detected",
    }


# ── Internal helpers ────────────────────────────────────────────────────────


def _detect_python_frameworks(project_dir: Path, frameworks: set[str]) -> None:
    """Check pyproject.toml for Python framework dependencies."""
    pyproject = project_dir / "pyproject.toml"
    if not pyproject.exists():
        return
    try:
        text = pyproject.read_text(encoding="utf-8").lower()
        for dep, name in _PYTHON_FRAMEWORKS.items():
            if dep in text:
                frameworks.add(name)
    except OSError:
        pass


def _detect_js_frameworks(project_dir: Path, frameworks: set[str]) -> None:
    """Check package.json for JavaScript/TypeScript framework dependencies."""
    pkg = project_dir / "package.json"
    if not pkg.exists():
        return
    try:
        data: dict[str, Any] = json.loads(pkg.read_text(encoding="utf-8"))
        all_deps: dict[str, str] = {
            **data.get("dependencies", {}),
            **data.get("devDependencies", {}),
        }
        for dep, name in _JS_FRAMEWORKS.items():
            if dep in all_deps:
                frameworks.add(name)
    except (OSError, json.JSONDecodeError):
        pass


def _detect_go_frameworks(project_dir: Path, frameworks: set[str]) -> None:
    """Check go.mod for Go framework dependencies."""
    gomod = project_dir / "go.mod"
    if not gomod.exists():
        return
    try:
        text = gomod.read_text(encoding="utf-8")
        for dep, name in _GO_FRAMEWORKS.items():
            if dep in text:
                frameworks.add(name)
    except OSError:
        pass


def _detect_rust_frameworks(project_dir: Path, frameworks: set[str]) -> None:
    """Check Cargo.toml for Rust framework dependencies."""
    cargo = project_dir / "Cargo.toml"
    if not cargo.exists():
        return
    try:
        text = cargo.read_text(encoding="utf-8")
        for dep, name in _RUST_FRAMEWORKS.items():
            if dep in text:
                frameworks.add(name)
    except OSError:
        pass
