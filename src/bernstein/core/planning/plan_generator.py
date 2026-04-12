"""AI-powered plan generator from natural language descriptions.

Analyzes a free-text goal, scans the project for relevant files, and
produces a multi-stage execution plan with role assignments, dependency
graphs, and cost estimates.  Pure heuristic analysis -- no LLM calls.

Typical usage::

    from bernstein.core.planning.plan_generator import generate_plan, render_plan_yaml

    plan = generate_plan("Add REST API for user management", Path("."))
    print(render_plan_yaml(plan))
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bernstein.core.planning.plan_schema import KNOWN_ROLES

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Action verbs that indicate implementation work.
_IMPL_VERBS: frozenset[str] = frozenset(
    {
        "add",
        "build",
        "create",
        "implement",
        "develop",
        "write",
        "construct",
        "introduce",
        "generate",
        "make",
        "set up",
        "setup",
        "design",
        "define",
        "compose",
        "scaffold",
        "bootstrap",
    }
)

#: Action verbs that indicate modification / enhancement work.
_MOD_VERBS: frozenset[str] = frozenset(
    {
        "update",
        "modify",
        "change",
        "refactor",
        "improve",
        "enhance",
        "extend",
        "upgrade",
        "migrate",
        "convert",
        "optimize",
        "rework",
        "revise",
        "rewrite",
        "restructure",
        "rename",
    }
)

#: Action verbs that indicate fix / debug work.
_FIX_VERBS: frozenset[str] = frozenset(
    {
        "fix",
        "repair",
        "resolve",
        "debug",
        "patch",
        "correct",
        "address",
        "handle",
        "recover",
        "restore",
    }
)

#: Action verbs that indicate removal work.
_REMOVE_VERBS: frozenset[str] = frozenset(
    {
        "remove",
        "delete",
        "drop",
        "deprecate",
        "clean",
        "strip",
        "eliminate",
        "prune",
    }
)

#: Action verbs that indicate testing work.
_TEST_VERBS: frozenset[str] = frozenset(
    {
        "test",
        "verify",
        "validate",
        "check",
        "assert",
        "ensure",
        "audit",
        "review",
        "inspect",
        "scan",
    }
)

#: Action verbs that indicate documentation work.
_DOC_VERBS: frozenset[str] = frozenset(
    {
        "document",
        "describe",
        "explain",
        "annotate",
        "comment",
    }
)

_ALL_VERBS: frozenset[str] = _IMPL_VERBS | _MOD_VERBS | _FIX_VERBS | _REMOVE_VERBS | _TEST_VERBS | _DOC_VERBS

#: Keywords that suggest particular domain concerns.
_DOMAIN_KEYWORDS: dict[str, str] = {
    "api": "backend",
    "rest": "backend",
    "graphql": "backend",
    "grpc": "backend",
    "endpoint": "backend",
    "route": "backend",
    "server": "backend",
    "database": "backend",
    "db": "backend",
    "model": "backend",
    "schema": "backend",
    "migration": "backend",
    "orm": "backend",
    "query": "backend",
    "sql": "backend",
    "ui": "frontend",
    "frontend": "frontend",
    "component": "frontend",
    "page": "frontend",
    "dashboard": "frontend",
    "form": "frontend",
    "layout": "frontend",
    "css": "frontend",
    "style": "frontend",
    "template": "frontend",
    "widget": "frontend",
    "react": "frontend",
    "vue": "frontend",
    "html": "frontend",
    "test": "qa",
    "tests": "qa",
    "testing": "qa",
    "coverage": "qa",
    "lint": "qa",
    "quality": "qa",
    "e2e": "qa",
    "integration test": "qa",
    "unit test": "qa",
    "security": "security",
    "auth": "security",
    "authentication": "security",
    "authorization": "security",
    "permission": "security",
    "encrypt": "security",
    "token": "security",
    "oauth": "security",
    "jwt": "security",
    "vulnerability": "security",
    "deploy": "devops",
    "deployment": "devops",
    "ci": "devops",
    "cd": "devops",
    "pipeline": "devops",
    "docker": "devops",
    "kubernetes": "devops",
    "k8s": "devops",
    "terraform": "devops",
    "helm": "devops",
    "infra": "devops",
    "infrastructure": "devops",
    "monitoring": "devops",
    "docs": "docs",
    "documentation": "docs",
    "readme": "docs",
    "docstring": "docs",
    "changelog": "docs",
    "guide": "docs",
    "tutorial": "docs",
    "architecture": "architect",
    "design": "architect",
    "pattern": "architect",
    "module": "architect",
    "interface": "architect",
    "protocol": "architect",
    "ml": "ml-engineer",
    "machine learning": "ml-engineer",
    "training": "ml-engineer",
    "inference": "ml-engineer",
    "neural": "ml-engineer",
}

#: File-path patterns that indicate roles.
_PATH_ROLE_HINTS: list[tuple[str, str]] = [
    ("test", "qa"),
    ("spec", "qa"),
    ("security", "security"),
    ("auth", "security"),
    ("deploy", "devops"),
    ("docker", "devops"),
    ("ci", "devops"),
    (".github", "devops"),
    ("docs", "docs"),
    ("frontend", "frontend"),
    ("ui", "frontend"),
    ("templates", "frontend"),
    ("static", "frontend"),
]

#: Cost per stage by scope (USD estimate, rough heuristic).
_COST_PER_STAGE: dict[str, float] = {
    "small": 0.05,
    "medium": 0.15,
    "large": 0.40,
}

#: Model cost multipliers relative to default (sonnet).
_MODEL_COST_MULTIPLIER: dict[str, float] = {
    "haiku": 0.25,
    "sonnet": 1.0,
    "opus": 5.0,
    "auto": 1.0,
}

#: Minutes per scope tier.
_MINUTES_PER_SCOPE: dict[str, int] = {
    "small": 15,
    "medium": 45,
    "large": 90,
}

#: Directories to skip when scanning for target files.
_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".sdd",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".eggs",
        "*.egg-info",
    }
)

#: File extensions to consider when scanning.
_CODE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".go",
        ".rs",
        ".java",
        ".yaml",
        ".yml",
        ".toml",
        ".json",
        ".md",
        ".html",
        ".css",
        ".scss",
        ".sql",
        ".sh",
        ".dockerfile",
    }
)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanStage:
    """A single stage in a generated plan.

    Attributes:
        name: Human-readable stage name.
        role: Specialist role assigned to this stage.
        goal: What this stage should accomplish.
        depends_on: Names of stages that must complete first.
        estimated_minutes: Estimated agent time in minutes.
        scope: Duration tier (small / medium / large).
        complexity: Reasoning difficulty (low / medium / high).
    """

    name: str
    role: str
    goal: str
    depends_on: tuple[str, ...] = ()
    estimated_minutes: int = 30
    scope: str = "medium"
    complexity: str = "medium"


@dataclass(frozen=True)
class GeneratedPlan:
    """A complete plan generated from a natural language goal.

    Attributes:
        goal: The original free-text goal.
        stages: Ordered tuple of plan stages.
        total_estimated_minutes: Sum of all stage estimates.
        total_estimated_cost_usd: Estimated total cost in USD.
        target_files: Files identified as relevant to the goal.
    """

    goal: str
    stages: tuple[PlanStage, ...]
    total_estimated_minutes: int
    total_estimated_cost_usd: float
    target_files: tuple[str, ...]


# ---------------------------------------------------------------------------
# Goal analysis
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GoalAnalysis:
    """Internal result of parsing a natural language goal.

    Attributes:
        action_verbs: Verbs found in the goal text.
        target_components: Domain components referenced.
        scope_estimate: Estimated scope tier.
        detected_roles: Roles inferred from keywords.
    """

    action_verbs: tuple[str, ...]
    target_components: tuple[str, ...]
    scope_estimate: str
    detected_roles: tuple[str, ...]


def analyze_goal(goal_text: str, project_root: Path) -> GoalAnalysis:
    """Parse a natural language goal into structured components.

    Extracts action verbs, identifies target components, estimates scope,
    and detects relevant specialist roles based on keyword analysis.

    Args:
        goal_text: Free-text description of what needs to be done.
        project_root: Root directory of the project (used for scope estimation
            via file count).

    Returns:
        A ``GoalAnalysis`` containing extracted verbs, components, scope,
        and detected roles.
    """
    lower = goal_text.lower()

    # Extract action verbs
    verbs: list[str] = sorted(v for v in _ALL_VERBS if v in lower)

    # Extract target components via domain keywords
    components: list[str] = []
    detected_roles: list[str] = []
    for keyword, role in _DOMAIN_KEYWORDS.items():
        if keyword in lower:
            components.append(keyword)
            if role not in detected_roles:
                detected_roles.append(role)

    # Scope estimation heuristic: more components / longer description = larger scope
    word_count = len(goal_text.split())
    component_count = len(components)

    if word_count > 50 or component_count > 5:
        scope = "large"
    elif word_count > 20 or component_count > 2:
        scope = "medium"
    else:
        scope = "small"

    return GoalAnalysis(
        action_verbs=tuple(verbs),
        target_components=tuple(sorted(set(components))),
        scope_estimate=scope,
        detected_roles=tuple(detected_roles),
    )


# ---------------------------------------------------------------------------
# File scanning
# ---------------------------------------------------------------------------


def _should_skip_dir(name: str) -> bool:
    """Check whether a directory name should be skipped during scanning.

    Args:
        name: Directory basename.

    Returns:
        True if the directory should be excluded from scanning.
    """
    if name in _SKIP_DIRS:
        return True
    return name.endswith(".egg-info")


def _extract_module_docstring(path: Path) -> str:
    """Extract the module-level docstring from a Python file.

    Reads only the first 2 KB to keep scanning fast.

    Args:
        path: Path to a ``.py`` file.

    Returns:
        The docstring text (lowercased), or empty string on failure.
    """
    try:
        head = path.read_text(encoding="utf-8", errors="ignore")[:2048]
    except OSError:
        return ""
    # Match triple-quoted strings at start (after optional comments/blank lines)
    match = re.search(r'^(?:\s*#[^\n]*\n)*\s*(?:"""(.*?)"""|\'\'\'(.*?)\'\'\')', head, re.DOTALL)
    if match:
        return (match.group(1) or match.group(2) or "").lower()
    return ""


def identify_target_files(goal_text: str, project_root: Path) -> tuple[str, ...]:
    """Scan project files and identify those relevant to the goal.

    Matches goal keywords against file paths and Python module docstrings.
    Returns paths relative to ``project_root``.

    Args:
        goal_text: Free-text description of the goal.
        project_root: Root directory of the project.

    Returns:
        Tuple of relative file paths that appear relevant.
    """
    lower = goal_text.lower()
    # Build keyword set from meaningful words (3+ chars)
    keywords = {w for w in re.findall(r"[a-z]{3,}", lower)}

    # Also add multi-word domain terms
    for kw in _DOMAIN_KEYWORDS:
        if kw in lower:
            keywords.add(kw)

    if not keywords:
        return ()

    matches: list[str] = []
    root = project_root.resolve()

    for item in _walk_project(root):
        rel = str(item.relative_to(root))
        rel_lower = rel.lower()

        # Check path segments against keywords
        path_parts = set(re.findall(r"[a-z]{3,}", rel_lower))
        if path_parts & keywords:
            matches.append(rel)
            continue

        # For Python files, also check docstrings
        if item.suffix == ".py":
            docstring = _extract_module_docstring(item)
            if docstring and any(kw in docstring for kw in keywords):
                matches.append(rel)

    return tuple(sorted(matches))


def _walk_project(root: Path) -> list[Path]:
    """Walk the project tree, skipping ignored directories.

    Args:
        root: Root directory to walk.

    Returns:
        List of file paths with code-relevant extensions.
    """
    results: list[Path] = []
    if not root.is_dir():
        return results

    try:
        entries = sorted(root.iterdir())
    except PermissionError:
        return results

    for entry in entries:
        if entry.is_dir():
            if not _should_skip_dir(entry.name):
                results.extend(_walk_project(entry))
        elif entry.is_file() and entry.suffix in _CODE_EXTENSIONS:
            results.append(entry)

    return results


# ---------------------------------------------------------------------------
# Plan generation
# ---------------------------------------------------------------------------


def _determine_complexity(analysis: GoalAnalysis) -> str:
    """Determine task complexity from goal analysis.

    Args:
        analysis: Parsed goal analysis result.

    Returns:
        Complexity string: "low", "medium", or "high".
    """
    # Fix/remove verbs with few components = low complexity
    fix_or_remove = set(analysis.action_verbs) & (_FIX_VERBS | _REMOVE_VERBS)
    impl_verbs = set(analysis.action_verbs) & _IMPL_VERBS

    if len(analysis.detected_roles) >= 3 or analysis.scope_estimate == "large":
        return "high"
    if fix_or_remove and not impl_verbs and len(analysis.target_components) <= 2:
        return "low"
    return "medium"


def _role_for_stage(stage_type: str, analysis: GoalAnalysis) -> str:
    """Pick the best role for a stage type.

    Args:
        stage_type: Internal stage category (e.g. "implementation", "testing").
        analysis: Parsed goal analysis.

    Returns:
        A valid role string from KNOWN_ROLES.
    """
    stage_role_map: dict[str, str] = {
        "design": "architect",
        "implementation": "backend",
        "testing": "qa",
        "security_review": "security",
        "documentation": "docs",
        "frontend": "frontend",
        "devops": "devops",
    }
    role = stage_role_map.get(stage_type, "backend")

    # Override with detected role if stage is implementation
    if stage_type == "implementation" and analysis.detected_roles:
        primary = analysis.detected_roles[0]
        if primary in KNOWN_ROLES:
            role = primary

    return role


def generate_plan(goal_text: str, project_root: Path) -> GeneratedPlan:
    """Generate a multi-stage execution plan from a natural language goal.

    Orchestrates goal analysis, file identification, and stage creation
    with appropriate roles, dependencies, and estimates.

    Args:
        goal_text: Free-text description of what needs to be done.
        project_root: Root directory of the project.

    Returns:
        A complete ``GeneratedPlan`` ready for rendering or execution.

    Raises:
        ValueError: If ``goal_text`` is empty or blank.
    """
    if not goal_text or not goal_text.strip():
        raise ValueError("goal_text must not be empty")

    analysis = analyze_goal(goal_text, project_root)
    target_files = identify_target_files(goal_text, project_root)
    complexity = _determine_complexity(analysis)

    stages: list[PlanStage] = []

    # Stage 1: Design / Architecture (for non-trivial goals)
    if analysis.scope_estimate != "small" or len(analysis.detected_roles) >= 2:
        stages.append(
            PlanStage(
                name="Design and Planning",
                role="architect",
                goal=f"Design the approach for: {goal_text.strip()}",
                depends_on=(),
                estimated_minutes=_MINUTES_PER_SCOPE.get(analysis.scope_estimate, 30),
                scope=analysis.scope_estimate,
                complexity=complexity,
            )
        )

    # Stage 2: Implementation (always present)
    impl_deps = ("Design and Planning",) if stages else ()
    impl_role = _role_for_stage("implementation", analysis)
    stages.append(
        PlanStage(
            name="Implementation",
            role=impl_role,
            goal=goal_text.strip(),
            depends_on=impl_deps,
            estimated_minutes=_MINUTES_PER_SCOPE.get(analysis.scope_estimate, 30),
            scope=analysis.scope_estimate,
            complexity=complexity,
        )
    )

    # Stage 3: Frontend (if frontend components detected)
    if "frontend" in analysis.detected_roles and impl_role != "frontend":
        stages.append(
            PlanStage(
                name="Frontend Implementation",
                role="frontend",
                goal=f"Implement frontend components for: {goal_text.strip()}",
                depends_on=("Implementation",),
                estimated_minutes=_MINUTES_PER_SCOPE.get(analysis.scope_estimate, 30),
                scope=analysis.scope_estimate,
                complexity=complexity,
            )
        )

    # Stage 4: Security review (if security keywords detected)
    if "security" in analysis.detected_roles:
        last_impl = stages[-1].name
        stages.append(
            PlanStage(
                name="Security Review",
                role="security",
                goal=f"Review security aspects of: {goal_text.strip()}",
                depends_on=(last_impl,),
                estimated_minutes=_MINUTES_PER_SCOPE.get("small", 15),
                scope="small",
                complexity="medium",
            )
        )

    # Stage 5: Testing (always present)
    test_deps_name = stages[-1].name
    stages.append(
        PlanStage(
            name="Testing",
            role="qa",
            goal=f"Write and run tests for: {goal_text.strip()}",
            depends_on=(test_deps_name,),
            estimated_minutes=_MINUTES_PER_SCOPE.get(analysis.scope_estimate, 30),
            scope=analysis.scope_estimate,
            complexity=complexity,
        )
    )

    # Stage 6: Documentation (for medium+ scope or if doc keywords present)
    if analysis.scope_estimate != "small" or "docs" in analysis.detected_roles:
        stages.append(
            PlanStage(
                name="Documentation",
                role="docs",
                goal=f"Document changes for: {goal_text.strip()}",
                depends_on=("Testing",),
                estimated_minutes=_MINUTES_PER_SCOPE.get("small", 15),
                scope="small",
                complexity="low",
            )
        )

    total_minutes = sum(s.estimated_minutes for s in stages)
    plan = GeneratedPlan(
        goal=goal_text.strip(),
        stages=tuple(stages),
        total_estimated_minutes=total_minutes,
        total_estimated_cost_usd=0.0,  # Placeholder, filled by estimate_cost
        target_files=target_files,
    )

    # Compute cost and return updated plan
    cost = estimate_cost(plan, "sonnet")
    return GeneratedPlan(
        goal=plan.goal,
        stages=plan.stages,
        total_estimated_minutes=plan.total_estimated_minutes,
        total_estimated_cost_usd=cost,
        target_files=plan.target_files,
    )


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------


def estimate_cost(plan: GeneratedPlan, model: str = "sonnet") -> float:
    """Estimate total cost in USD for executing a plan.

    Uses a heuristic based on stage count, scope tiers, and model pricing.

    Args:
        plan: The generated plan to estimate.
        model: Model identifier (haiku / sonnet / opus / auto).

    Returns:
        Estimated cost in USD.
    """
    multiplier = _MODEL_COST_MULTIPLIER.get(model, 1.0)
    total = 0.0
    for stage in plan.stages:
        base = _COST_PER_STAGE.get(stage.scope, 0.15)
        total += base * multiplier
    return round(total, 4)


# ---------------------------------------------------------------------------
# YAML rendering
# ---------------------------------------------------------------------------


def render_plan_yaml(plan: GeneratedPlan) -> str:
    """Render a GeneratedPlan as Bernstein plan YAML format.

    Produces YAML compatible with ``bernstein run plan.yaml``.

    Args:
        plan: The plan to render.

    Returns:
        YAML string ready to write to a file.
    """
    lines: list[str] = []
    lines.append(f"name: {_yaml_quote(plan.goal[:80])}")
    lines.append(f"description: {_yaml_quote(plan.goal)}")
    lines.append("")
    lines.append("stages:")

    for stage in plan.stages:
        lines.append(f"  - name: {_yaml_quote(stage.name)}")
        if stage.depends_on:
            deps_str = ", ".join(_yaml_quote(d) for d in stage.depends_on)
            lines.append(f"    depends_on: [{deps_str}]")
        lines.append("    steps:")
        lines.append(f"      - title: {_yaml_quote(stage.goal)}")
        lines.append(f"        role: {stage.role}")
        lines.append(f"        scope: {stage.scope}")
        lines.append(f"        complexity: {stage.complexity}")
        lines.append(f"        estimated_minutes: {stage.estimated_minutes}")
        if plan.target_files:
            # Assign files relevant to this stage's role
            stage_files = _files_for_role(stage.role, plan.target_files)
            if stage_files:
                lines.append("        files:")
                for f in stage_files[:10]:
                    lines.append(f"          - {_yaml_quote(f)}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _yaml_quote(value: str) -> str:
    """Quote a YAML string value if it contains special characters.

    Args:
        value: String to potentially quote.

    Returns:
        Quoted string if necessary, otherwise the original value.
    """
    # Quote if contains YAML-special characters or starts with special chars
    needs_quote = any(c in value for c in ":{}[]&*?|>!%@`#,") or value.startswith(("'", '"'))
    if needs_quote:
        escaped = value.replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _files_for_role(role: str, all_files: tuple[str, ...]) -> list[str]:
    """Filter files relevant to a specific role.

    Args:
        role: The specialist role.
        all_files: All identified target files.

    Returns:
        Subset of files matching the role's domain.
    """
    role_patterns: dict[str, list[str]] = {
        "qa": ["test", "spec", "conftest"],
        "frontend": ["frontend", "ui", "component", "template", "static", "css", "html"],
        "security": ["security", "auth", "permission", "crypto"],
        "devops": ["deploy", "docker", "ci", ".github", "terraform", "helm"],
        "docs": ["doc", "readme", "changelog", "guide"],
        "architect": ["architecture", "design", "interface", "protocol"],
    }
    patterns = role_patterns.get(role, [])
    if not patterns:
        # Backend / default: return non-test source files
        return [f for f in all_files if "test" not in f.lower()]

    return [f for f in all_files if any(p in f.lower() for p in patterns)]
