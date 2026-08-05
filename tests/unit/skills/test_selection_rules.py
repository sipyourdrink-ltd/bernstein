"""Declarative skill-selection rules: loader and resolver units (issue #3383).

The rule layer must be corpus-immune: resolution is a pure function of
``(tasks, rules)``, unlike the TF-IDF auto-route whose scores move when
unrelated templates join the corpus. These tests pin the schema (loud
validation, no ``role`` axis), the matching semantics (fnmatch globs,
per-task axis conjunction, cross-task union), and the deterministic
output order (rule position, then template name).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from bernstein.core.models import Task

from bernstein.core.skills.routing import select_auto_route_templates
from bernstein.core.skills.selection_rules import (
    SELECTION_RULES_FILENAME,
    SelectionRuleError,
    load_selection_rules,
    resolve_rule_templates,
)
from bernstein.core.tasks.models import TaskType

if TYPE_CHECKING:
    from pathlib import Path


def _make_skills_dir(root: Path, template_names: list[str]) -> Path:
    """Create a skills source directory holding the named templates."""
    skills_dir = root / "templates" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    for name in template_names:
        (skills_dir / f"{name}.md").write_text(
            f"---\nname: {name}\ndescription: {name} skill\n---\n\n# {name}\nBody of {name}.\n",
            encoding="utf-8",
        )
    return skills_dir


def _write_rules(skills_dir: Path, text: str) -> None:
    (skills_dir / SELECTION_RULES_FILENAME).write_text(text, encoding="utf-8")


def _make_task(
    *,
    owned_files: list[str],
    task_type: TaskType = TaskType.STANDARD,
    task_id: str = "T-001",
) -> Task:
    return Task(
        id=task_id,
        title="Test task",
        description="A test task",
        role="backend",
        owned_files=owned_files,
        task_type=task_type,
    )


def test_rule_glob_matches_owned_files(tmp_path: Path) -> None:
    """A rule whose glob matches a task's owned_files selects its templates."""
    skills_dir = _make_skills_dir(tmp_path, ["api-conventions"])
    _write_rules(
        skills_dir,
        "rules:\n  - owned_files: 'src/api/*.py'\n    skills: [api-conventions]\n",
    )

    rules = load_selection_rules(skills_dir)
    task = _make_task(owned_files=["src/api/users.py"])

    assert resolve_rule_templates(rules, [task]) == ("api-conventions.md",)


def test_rule_glob_no_match_selects_nothing(tmp_path: Path) -> None:
    """A rule whose globs match no owned_files entry selects nothing."""
    skills_dir = _make_skills_dir(tmp_path, ["api-conventions"])
    _write_rules(
        skills_dir,
        "rules:\n  - owned_files: ['docs/*.md', 'src/api/*.py']\n    skills: [api-conventions]\n",
    )

    rules = load_selection_rules(skills_dir)
    task = _make_task(owned_files=["src/core/scheduler.py"])

    assert resolve_rule_templates(rules, [task]) == ()


def test_rule_task_type_conjunction_narrows(tmp_path: Path) -> None:
    """When both axes are present, both must match - on the same task."""
    skills_dir = _make_skills_dir(tmp_path, ["hotfix-guide"])
    _write_rules(
        skills_dir,
        "rules:\n  - owned_files: 'src/*.py'\n    task_type: fix\n    skills: [hotfix-guide]\n",
    )
    rules = load_selection_rules(skills_dir)

    # Glob matches but task_type does not -> not selected.
    standard = _make_task(owned_files=["src/main.py"], task_type=TaskType.STANDARD)
    assert resolve_rule_templates(rules, [standard]) == ()

    # Both axes match on the same task -> selected.
    fix = _make_task(owned_files=["src/main.py"], task_type=TaskType.FIX)
    assert resolve_rule_templates(rules, [fix]) == ("hotfix-guide.md",)

    # One task matches each axis, but no single task matches both -> not
    # selected: the conjunction is per-task, not across the task set.
    glob_only = _make_task(owned_files=["src/main.py"], task_type=TaskType.STANDARD, task_id="T-A")
    type_only = _make_task(owned_files=["docs/notes.md"], task_type=TaskType.FIX, task_id="T-B")
    assert resolve_rule_templates(rules, [glob_only, type_only]) == ()


def test_rules_have_no_role_axis(tmp_path: Path) -> None:
    """The schema rejects a ``role`` key at load - ROLE_SKILL_MAP owns roles."""
    skills_dir = _make_skills_dir(tmp_path, ["api-conventions"])
    _write_rules(
        skills_dir,
        "rules:\n  - owned_files: 'src/*'\n    role: backend\n    skills: [api-conventions]\n",
    )

    with pytest.raises(SelectionRuleError, match="'role' is not a rule axis"):
        load_selection_rules(skills_dir)


def test_rule_selection_invariant_to_unrelated_corpus(tmp_path: Path) -> None:
    """The same task and rules select the same templates whatever else the corpus holds."""
    selections: list[tuple[str, ...]] = []
    for count, unrelated in enumerate(([], ["extra-a"], [f"extra-{i}" for i in range(12)])):
        skills_dir = _make_skills_dir(tmp_path / f"corpus-{count}", ["api-conventions", *unrelated])
        _write_rules(
            skills_dir,
            "rules:\n  - owned_files: 'src/api/*.py'\n    skills: [api-conventions]\n",
        )
        rules = load_selection_rules(skills_dir)
        task = _make_task(owned_files=["src/api/users.py"])
        selections.append(resolve_rule_templates(rules, [task]))

    assert selections[0] == ("api-conventions.md",)
    assert selections[0] == selections[1] == selections[2]


def test_regression_tfidf_shifts_while_rules_hold(tmp_path: Path) -> None:
    """Adding one unrelated template shifts a TF-IDF score; the rule layer holds.

    This is the determinism motivation for the rule layer in one test:
    the auto-route's IDF weights depend on corpus document frequencies,
    so an unrelated template changes an existing template's score for
    the very same task. Rule resolution never looks at the corpus.
    """
    task = Task(
        id="T-010",
        title="Fix pytest regression",
        description="The pytest suite is failing in tests.",
        role="backend",
        owned_files=["tests/test_api.py"],
    )
    rules_text = "rules:\n  - owned_files: 'tests/*.py'\n    skills: [pytest-helper]\n"

    def _score_and_selection(root: Path, extra: list[str]) -> tuple[float, tuple[str, ...]]:
        skills_dir = _make_skills_dir(root, ["pytest-helper", "docker-deploy", *extra])
        # Overwrite with token-bearing bodies so TF-IDF has signal.
        (skills_dir / "pytest-helper.md").write_text(
            "---\nname: pytest-helper\ndescription: pytest regression help for tests\n---\n\n"
            "Run pytest on the failing tests and fix the regression.\n",
            encoding="utf-8",
        )
        for name in extra:
            (skills_dir / f"{name}.md").write_text(
                f"---\nname: {name}\ndescription: unrelated helper\n---\n\n"
                "Terraform plan output review for infrastructure tests.\n",
                encoding="utf-8",
            )
        _write_rules(skills_dir, rules_text)
        candidates = select_auto_route_templates(skills_dir, [task], excluded_templates=[])
        by_name = {c.template_name: c.score for c in candidates}
        selection = resolve_rule_templates(load_selection_rules(skills_dir), [task])
        return by_name["pytest-helper.md"], selection

    score_small, selection_small = _score_and_selection(tmp_path / "small", extra=[])
    score_grown, selection_grown = _score_and_selection(tmp_path / "grown", extra=["terraform-notes"])

    assert score_small != score_grown, "TF-IDF score should shift when the corpus grows"
    assert selection_small == selection_grown == ("pytest-helper.md",)


def test_rule_referencing_missing_template_fails_loudly_at_load(tmp_path: Path) -> None:
    """A rule naming a template with no ``<name>.md`` on disk fails at load."""
    skills_dir = _make_skills_dir(tmp_path, ["api-conventions"])
    _write_rules(
        skills_dir,
        "rules:\n"
        "  - owned_files: 'src/*'\n"
        "    skills: [api-conventions]\n"
        "  - owned_files: 'docs/*'\n"
        "    skills: [ghost-template]\n",
    )

    with pytest.raises(SelectionRuleError, match=r"rule 2.*'ghost-template'"):
        load_selection_rules(skills_dir)


def test_malformed_rule_table_fails_loudly_at_load(tmp_path: Path) -> None:
    """Bad YAML and wrong shapes raise; an empty-but-valid table means no rules."""
    skills_dir = _make_skills_dir(tmp_path, ["api-conventions"])

    # Invalid YAML.
    _write_rules(skills_dir, "rules: [unclosed\n")
    with pytest.raises(SelectionRuleError, match="invalid YAML"):
        load_selection_rules(skills_dir)

    # Top level must be a mapping, not a bare list.
    _write_rules(skills_dir, "- owned_files: 'src/*'\n  skills: [api-conventions]\n")
    with pytest.raises(SelectionRuleError, match="top level must be a mapping"):
        load_selection_rules(skills_dir)

    # Unknown top-level key.
    _write_rules(skills_dir, "rules: []\nextra: true\n")
    with pytest.raises(SelectionRuleError, match="unknown top-level key"):
        load_selection_rules(skills_dir)

    # Non-string, non-list glob axis.
    _write_rules(skills_dir, "rules:\n  - owned_files: 42\n    skills: [api-conventions]\n")
    with pytest.raises(SelectionRuleError, match="'owned_files' must be a string glob"):
        load_selection_rules(skills_dir)

    # Non-string entry inside the glob list.
    _write_rules(skills_dir, "rules:\n  - owned_files: [42]\n    skills: [api-conventions]\n")
    with pytest.raises(SelectionRuleError, match="'owned_files' entries must be non-empty strings"):
        load_selection_rules(skills_dir)

    # Missing required owned_files axis.
    _write_rules(skills_dir, "rules:\n  - skills: [api-conventions]\n")
    with pytest.raises(SelectionRuleError, match="'owned_files' is required"):
        load_selection_rules(skills_dir)

    # Unknown task_type value.
    _write_rules(
        skills_dir,
        "rules:\n  - owned_files: 'src/*'\n    task_type: refactor\n    skills: [api-conventions]\n",
    )
    with pytest.raises(SelectionRuleError, match="unknown task_type 'refactor'"):
        load_selection_rules(skills_dir)

    # Unknown rule key.
    _write_rules(
        skills_dir,
        "rules:\n  - owned_files: 'src/*'\n    priority: 3\n    skills: [api-conventions]\n",
    )
    with pytest.raises(SelectionRuleError, match=r"unknown key\(s\) \['priority'\]"):
        load_selection_rules(skills_dir)

    # Empty-but-valid tables mean no rules.
    _write_rules(skills_dir, "")
    assert load_selection_rules(skills_dir) == ()
    _write_rules(skills_dir, "rules: []\n")
    assert load_selection_rules(skills_dir) == ()
    _write_rules(skills_dir, "rules:\n")
    assert load_selection_rules(skills_dir) == ()


def test_rule_hits_deduplicated_and_ordered_by_rule_position_then_name(tmp_path: Path) -> None:
    """Output order is rule position, then template name; duplicates keep first place."""
    skills_dir = _make_skills_dir(tmp_path, ["alpha", "beta", "zeta"])
    _write_rules(
        skills_dir,
        "rules:\n"
        "  - owned_files: 'src/*'\n"
        "    skills: [zeta, alpha]\n"
        "  - owned_files: 'src/*'\n"
        "    skills: [alpha, beta]\n",
    )

    rules = load_selection_rules(skills_dir)
    task = _make_task(owned_files=["src/main.py"])

    # Rule 1 contributes its hits name-sorted (alpha, zeta); rule 2 adds
    # only beta - alpha is deduplicated at its first (rule 1) position.
    assert resolve_rule_templates(rules, [task]) == ("alpha.md", "zeta.md", "beta.md")


def test_multi_task_union_semantics(tmp_path: Path) -> None:
    """A rule matching any one task of the set selects its templates once."""
    skills_dir = _make_skills_dir(tmp_path, ["api-conventions", "docs-style"])
    _write_rules(
        skills_dir,
        "rules:\n"
        "  - owned_files: 'src/api/*'\n"
        "    skills: [api-conventions]\n"
        "  - owned_files: 'docs/*'\n"
        "    skills: [docs-style]\n",
    )
    rules = load_selection_rules(skills_dir)

    unrelated = _make_task(owned_files=["scripts/build.sh"], task_id="T-A")
    api = _make_task(owned_files=["src/api/users.py"], task_id="T-B")
    docs = _make_task(owned_files=["docs/guide.md"], task_id="T-C")

    # Only the second task matches the first rule -> union selects it.
    assert resolve_rule_templates(rules, [unrelated, api]) == ("api-conventions.md",)
    # Multiple tasks matching the same rule still yield one hit each.
    assert resolve_rule_templates(rules, [api, api, docs]) == (
        "api-conventions.md",
        "docs-style.md",
    )
    # No task matches -> nothing selected.
    assert resolve_rule_templates(rules, [unrelated]) == ()


def test_known_task_type_tokens_track_the_scheduler_enum() -> None:
    """The restated token set cannot drift from TaskType.

    selection_rules matches task types by token instead of importing the
    scheduler's enum, because the module is reached from the adapters layer
    and the import-linter contract forbids adapters importing scheduler
    internals. This pin is what makes the restatement safe: adding or
    renaming a TaskType member fails here until the token set follows.
    """
    from bernstein.core.skills.selection_rules import _KNOWN_TASK_TYPE_TOKENS

    assert {member.value for member in TaskType} == _KNOWN_TASK_TYPE_TOKENS


def test_unknown_task_type_matches_no_typed_rule(tmp_path: Path) -> None:
    """A present-but-unrecognized task_type fails closed on typed rules.

    Coercing an unknown type to "standard" would inject operator-authored
    skills into tasks that are explicitly not standard; only a genuinely
    absent field defaults. Glob-only rules still apply - the failure is
    scoped to the task_type axis.
    """
    from types import SimpleNamespace

    skills_dir = _make_skills_dir(tmp_path, ["typed-skill", "glob-skill"])
    _write_rules(
        skills_dir,
        """
rules:
  - owned_files: "src/**"
    task_type: standard
    skills: [typed-skill]
  - owned_files: "src/**"
    skills: [glob-skill]
""",
    )
    rules = load_selection_rules(skills_dir)
    future_task = SimpleNamespace(owned_files=["src/main.py"], task_type="future_type")
    assert resolve_rule_templates(rules, [future_task]) == ("glob-skill.md",)

    absent_task = SimpleNamespace(owned_files=["src/main.py"])
    assert resolve_rule_templates(rules, [absent_task]) == ("typed-skill.md", "glob-skill.md")


@pytest.mark.parametrize(
    "escape_name",
    ["/outside/secret.md", "../escape.md", "nested/dir-skill.md", "..\\win-escape.md"],
)
def test_rule_template_name_cannot_escape_the_skills_directory(tmp_path: Path, escape_name: str) -> None:
    """Template names are bare file names; paths never leave the corpus.

    An absolute entry replaces the base in a pathlib join and ".." walks
    out of the skills directory, so without this rejection a rule table
    could point the injector at any readable file on the host and have its
    content injected as a vetted skill template.
    """
    skills_dir = _make_skills_dir(tmp_path, ["real-skill"])
    _write_rules(
        skills_dir,
        f"""
rules:
  - owned_files: "src/**"
    skills: ['{escape_name}']
""",
    )
    with pytest.raises(SelectionRuleError, match="bare template file name"):
        load_selection_rules(skills_dir)
