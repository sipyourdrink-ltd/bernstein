"""Role classification based on task content analysis."""

from __future__ import annotations

import re


def classify_role(description: str) -> str:
    """Classify the most likely role for a task based on its description.

    Args:
        description: Task description.

    Returns:
        The best matching role string (defaulting to "backend").
    """
    text = description.lower()

    # Priority mapping (more specific first)
    keyword_map: dict[str, list[str]] = {
        "security": [
            "security",
            "cve",
            "vulnerability",
            "encryption",
            "encrypt",
            "decrypt",
            "sql injection",
            "xss",
            "csrf",
            "secret",
            "permission",
            "access control",
            "rbac",
            "oauth",
            "jwt",
        ],
        "qa": [
            "test",
            "coverage",
            "benchmark",
            "lint",
            "flake8",
            "ruff",
            "pytest",
            "unittest",
            "regression",
            "sanity",
            "e2e",
            "integration",
        ],
        "devops": [
            "docker",
            "kubernetes",
            "k8s",
            "ci",
            "cd",
            "deploy",
            "pipeline",
            "workflow",
            "github action",
            "jenkins",
            "infra",
            "terraform",
            "ansible",
            "helm",
            "aws",
            "gcp",
            "azure",
            "cloud",
        ],
        "frontend": [
            "css",
            "react",
            "html",
            "ui",
            "ux",
            "component",
            "style",
            "tailwind",
            "sass",
            "less",
            "javascript",
            "js",
            "typescript",
            "ts",
            "browser",
            "frontend",
            "dom",
            "event",
            "click",
            "hover",
        ],
        "backend": [
            "api",
            "database",
            "db",
            "model",
            "logic",
            "server",
            "endpoint",
            "crud",
            "migration",
            "worker",
            "queue",
            "redis",
            "postgres",
            "sqlalchemy",
            "pydantic",
            "fastapi",
            "flask",
            "django",
            "backend",
        ],
    }

    scores = _score_roles(text, keyword_map)

    if not any(scores.values()):
        return "backend"

    best_role = max(scores.keys(), key=lambda r: (scores[r], -len(r)))

    if _is_auth_only_security(text, scores, keyword_map):
        return "backend"

    return best_role


def _score_roles(text: str, keyword_map: dict[str, list[str]]) -> dict[str, int]:
    """Score each role based on keyword matches in text."""
    scores: dict[str, int] = dict.fromkeys(keyword_map, 0)
    for role, keywords in keyword_map.items():
        for kw in keywords:
            if len(kw) <= 4:
                if re.search(rf"\b{re.escape(kw)}s?\b", text):
                    scores[role] += 1
            elif kw in text:
                scores[role] += 1
    return scores


def _is_auth_only_security(
    text: str,
    scores: dict[str, int],
    keyword_map: dict[str, list[str]],
) -> bool:
    """Check if 'security' score is just from 'auth' keyword (actually backend)."""
    if "auth" not in text or scores.get("security", 0) != 1 or "security" in text:
        return False
    return not any(kw in text for kw in keyword_map["security"] if kw != "auth")
