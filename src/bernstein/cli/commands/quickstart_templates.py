"""Per-language quickstart templates for Bernstein project scaffolding."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LanguageTemplate:
    """Immutable description of a language-specific quickstart scaffold.

    Attributes:
        language: Machine-readable language key (e.g. "python").
        display_name: Human-readable label (e.g. "Python").
        roles: Agent roles relevant to this language stack.
        quality_gates: Linter/test tool names used as quality gates.
        example_tasks: Sample backlog tasks, each a dict with 'title' and 'goal'.
        bernstein_yaml_snippet: Extra YAML lines appended to bernstein.yaml.
    """

    language: str
    display_name: str
    roles: list[str]
    quality_gates: list[str]
    example_tasks: list[dict[str, str]]
    bernstein_yaml_snippet: str


# ---------------------------------------------------------------------------
# Built-in templates
# ---------------------------------------------------------------------------

TEMPLATES: dict[str, LanguageTemplate] = {
    "python": LanguageTemplate(
        language="python",
        display_name="Python",
        roles=["backend", "qa", "security"],
        quality_gates=["ruff", "pytest", "pyright"],
        example_tasks=[
            {
                "title": "Add input validation",
                "goal": "Validate request payloads and return 400 on malformed input.",
            },
            {
                "title": "Write unit tests",
                "goal": "Create a pytest suite covering all API endpoints.",
            },
            {
                "title": "Add security headers",
                "goal": "Add CORS, CSP, and X-Content-Type-Options headers to responses.",
            },
        ],
        bernstein_yaml_snippet=(
            "# Python-specific settings\nlanguage: python\ntest_command: pytest tests/ -q\nlint_command: ruff check .\n"
        ),
    ),
    "typescript": LanguageTemplate(
        language="typescript",
        display_name="TypeScript",
        roles=["frontend", "backend", "qa"],
        quality_gates=["eslint", "jest", "tsc"],
        example_tasks=[
            {
                "title": "Add request validation middleware",
                "goal": "Create Zod schemas and validate incoming requests.",
            },
            {
                "title": "Write Jest test suite",
                "goal": "Add unit tests for all route handlers with Jest.",
            },
            {
                "title": "Add error handling middleware",
                "goal": "Implement centralized Express error handler with typed responses.",
            },
        ],
        bernstein_yaml_snippet=(
            "# TypeScript-specific settings\n"
            "language: typescript\n"
            "test_command: npx jest --passWithNoTests\n"
            "lint_command: npx eslint .\n"
        ),
    ),
    "rust": LanguageTemplate(
        language="rust",
        display_name="Rust",
        roles=["backend", "qa", "security"],
        quality_gates=["clippy", "cargo-test"],
        example_tasks=[
            {
                "title": "Add input deserialization",
                "goal": "Use serde to validate and deserialize request bodies.",
            },
            {
                "title": "Write integration tests",
                "goal": "Add tests in tests/ covering all API endpoints.",
            },
            {
                "title": "Add error types",
                "goal": "Define an AppError enum with proper Into<Response> conversion.",
            },
        ],
        bernstein_yaml_snippet=(
            "# Rust-specific settings\n"
            "language: rust\n"
            "test_command: cargo test\n"
            "lint_command: cargo clippy -- -D warnings\n"
        ),
    ),
    "go": LanguageTemplate(
        language="go",
        display_name="Go",
        roles=["backend", "qa"],
        quality_gates=["golint", "go-test"],
        example_tasks=[
            {
                "title": "Add request validation",
                "goal": "Validate JSON payloads and return 400 on missing fields.",
            },
            {
                "title": "Write table-driven tests",
                "goal": "Add table-driven tests for all HTTP handlers.",
            },
            {
                "title": "Add structured error responses",
                "goal": "Return consistent JSON error bodies with status codes.",
            },
        ],
        bernstein_yaml_snippet=(
            "# Go-specific settings\nlanguage: go\ntest_command: go test ./...\nlint_command: golangci-lint run\n"
        ),
    ),
    "java": LanguageTemplate(
        language="java",
        display_name="Java",
        roles=["backend", "qa"],
        quality_gates=["checkstyle", "junit"],
        example_tasks=[
            {
                "title": "Add Bean Validation",
                "goal": "Annotate DTOs with Jakarta validation constraints.",
            },
            {
                "title": "Write JUnit test suite",
                "goal": "Add JUnit 5 tests for all REST controller endpoints.",
            },
            {
                "title": "Add global exception handler",
                "goal": "Create @ControllerAdvice class for consistent error responses.",
            },
        ],
        bernstein_yaml_snippet=(
            "# Java-specific settings\nlanguage: java\ntest_command: mvn test -q\nlint_command: mvn checkstyle:check\n"
        ),
    ),
}


def get_template(language: str) -> LanguageTemplate | None:
    """Return the template for *language*, or None if not found.

    Args:
        language: Case-insensitive language key (e.g. "Python", "go").

    Returns:
        Matching LanguageTemplate or None.
    """
    return TEMPLATES.get(language.lower())


def list_available_templates() -> list[str]:
    """Return sorted list of available template language keys.

    Returns:
        List of language strings, e.g. ["go", "java", "python", ...].
    """
    return sorted(TEMPLATES.keys())


def generate_bernstein_yaml(template: LanguageTemplate) -> str:
    """Generate a complete bernstein.yaml for the given language template.

    The output is valid YAML ready to be written to a project directory.

    Args:
        template: Language template to generate config from.

    Returns:
        YAML string with roles, quality gates, and agent configuration.
    """
    roles_list = "\n".join(f"  - {r}" for r in template.roles)
    gates_list = "\n".join(f"  - {g}" for g in template.quality_gates)

    return (
        f"# Bernstein configuration for {template.display_name} project\n"
        f"#\n"
        f"# Generated by: bernstein quickstart --language {template.language}\n"
        f"\n"
        f'goal: "Build and test a {template.display_name} application"\n'
        f"\n"
        f"roles:\n"
        f"{roles_list}\n"
        f"\n"
        f"quality_gates:\n"
        f"{gates_list}\n"
        f"\n"
        f"agent:\n"
        f"  max_workers: 3\n"
        f"  default_model: sonnet\n"
        f"  default_effort: normal\n"
        f"\n"
        f"{template.bernstein_yaml_snippet}"
    )


def generate_example_plan(template: LanguageTemplate) -> str:
    """Generate a sample plan.yaml with language-appropriate tasks.

    The plan contains a single stage with one step per example task
    from the template.

    Args:
        template: Language template to generate the plan from.

    Returns:
        YAML string describing a multi-step plan.
    """
    lines: list[str] = [
        f"# Example plan for {template.display_name} project",
        f"# Execute with: bernstein run plans/example-{template.language}.yaml",
        "",
        "stages:",
        f"  - name: {template.language}-setup",
        f'    description: "Initial {template.display_name} project tasks"',
        "    steps:",
    ]

    for i, task in enumerate(template.example_tasks, start=1):
        if i < len(template.example_tasks):
            role = template.roles[0]
        elif len(template.roles) > 1:
            role = template.roles[1]
        else:
            role = template.roles[0]
        lines.extend(
            [
                f'      - goal: "{task["goal"]}"',
                f"        role: {role}",
                f"        priority: {i}",
                "        complexity: normal",
            ]
        )

    return "\n".join(lines) + "\n"
