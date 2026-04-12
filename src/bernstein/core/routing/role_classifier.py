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

    # Count matches per role
    scores: dict[str, int] = dict.fromkeys(keyword_map, 0)
    for role, keywords in keyword_map.items():
        for kw in keywords:
            # Use word boundaries for short keywords to avoid substrings like 'dom' in 'random'
            # Also allow optional 's' at the end for plural
            if len(kw) <= 4:
                if re.search(rf"\b{re.escape(kw)}s?\b", text):
                    scores[role] += 1
            elif kw in text:
                scores[role] += 1

    # Find role with highest score
    if not any(scores.values()):
        return "backend"  # Default

    # Sort by score descending, then by role name for determinism
    best_role = max(scores.keys(), key=lambda r: (scores[r], -len(r)))

    # Special cases: 'auth' by itself is often backend
    if "auth" in text and scores["security"] == 1 and "security" not in text:
        # Check if any other security keywords besides 'auth' matched
        security_keywords = keyword_map["security"]
        other_security_matches = any(kw in text for kw in security_keywords if kw != "auth")
        if not other_security_matches:
            return "backend"

    return best_role
