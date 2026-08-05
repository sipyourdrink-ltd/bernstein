"""Declarative skill-selection rules (issue #3383).

A rule layer between the role map and the opt-in TF-IDF auto-route in
:mod:`bernstein.adapters.skills_injector`. Role binding
(``ROLE_SKILL_MAP``) stays the deterministic baseline; the TF-IDF
auto-route is corpus-coupled - adding an unrelated template to
``templates/skills/`` shifts document frequencies and therefore scores.
Selection rules sit in between: an operator-authored
``selection-rules.yaml`` maps task facts to skill templates, and
resolution is a pure function of ``(tasks, rules)`` - no corpus
statistics, no environment reads, no ordering sensitivity - so the same
tasks and the same rule table always select the same templates.

A rule has two axes:

- ``owned_files`` (required): one glob or a list of globs, matched
  fnmatch-style against each task's ``owned_files`` entries.
- ``task_type`` (optional): one of the task-type tokens ``standard``,
  ``upgrade_proposal``, ``fix``, ``research`` (the ``value`` spellings of
  the scheduler's ``TaskType`` enum, matched by token so this module never
  imports scheduler internals). When present, both axes must match on the
  same task.

There is deliberately no ``role`` axis - role -> skill binding is owned
by ``ROLE_SKILL_MAP`` in the injector, and the schema rejects a ``role``
key at load so the two layers cannot drift into conflict.

The rule table lives at ``<skills_source_dir>/selection-rules.yaml``,
sibling of the skill templates it names. Validation is loud: unknown
keys, malformed globs, unknown task types, or a rule naming a template
that does not exist all raise :class:`SelectionRuleError` naming the
rule and the problem. An empty-but-valid table means no rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Final, Protocol, cast

import yaml

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

#: File name of the rule table inside the skills source directory.
SELECTION_RULES_FILENAME: Final[str] = "selection-rules.yaml"

#: Keys a rule mapping may carry. Anything else is rejected at load.
_ALLOWED_RULE_KEYS: Final[frozenset[str]] = frozenset({"owned_files", "task_type", "skills"})

#: Lowercase task-type tokens the schema accepts - the ``value`` spellings of
#: ``bernstein.core.tasks.models.TaskType``. Restated here rather than
#: imported: this module is reached from the adapters layer through the skill
#: injector, and the import-linter contract forbids adapters importing
#: scheduler internals, so task types are matched by token, never by enum
#: identity. ``test_known_task_type_tokens_track_the_scheduler_enum`` pins
#: this set against the real enum so the two cannot drift silently.
_KNOWN_TASK_TYPE_TOKENS: Final[frozenset[str]] = frozenset({"standard", "upgrade_proposal", "fix", "research"})


class SelectionRuleError(ValueError):
    """Raised when ``selection-rules.yaml`` is malformed or names a missing template."""


class RuleSelectableTask(Protocol):
    """The narrow slice of a task the rule layer matches against.

    Deliberately not a widening of ``RoutableTask`` (which the TF-IDF
    auto-route owns), and deliberately requiring only ``owned_files`` —
    the one field every caller must have. ``task_type`` is read
    dynamically via ``getattr`` and normalized to its lowercase token (an
    enum member's ``value``, or a bare string): an absent field defaults
    to ``"standard"``, a present-but-unrecognized value matches no typed
    rule. Requiring ``task_type`` here would force callers like the
    injector (whose local ``Task`` protocol has no such field) into a
    cast that silences exactly the shape mismatches static checking
    should catch. Design rationale: ``docs/sdd/skill-selection-rules.md``.
    """

    owned_files: list[str]


@dataclass(frozen=True)
class SelectionRule:
    """One validated rule: globs (+ optional task type) -> skill templates."""

    owned_files: tuple[str, ...]
    task_type: str | None
    templates: tuple[str, ...]


def selection_rules_path(skills_source_dir: Path) -> Path:
    """Return the rule-table path inside ``skills_source_dir``.

    Callers that only need to know whether a rule table exists should
    stat this path instead of invoking :func:`load_selection_rules` -
    with no table present the loader must never run.
    """
    return skills_source_dir / SELECTION_RULES_FILENAME


def load_selection_rules(skills_source_dir: Path) -> tuple[SelectionRule, ...]:
    """Load and validate ``selection-rules.yaml`` from ``skills_source_dir``.

    Args:
        skills_source_dir: Directory holding the skill templates
            (``templates/skills/``). Rule-named templates must exist here
            as ``<name>.md``.

    Returns:
        The validated rules in file order. An empty document, ``rules:``
        with no entries, or ``rules: []`` all yield an empty tuple.

    Raises:
        SelectionRuleError: On unreadable files, invalid YAML, wrong
            shapes, unknown keys (including the rejected ``role`` axis),
            unknown task types, or rules naming templates that do not
            exist. The message names the offending rule and the problem.
    """
    path = selection_rules_path(skills_source_dir)
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SelectionRuleError(f"cannot read selection rules file {path}: {exc}") from exc

    try:
        loaded: object = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise SelectionRuleError(f"invalid YAML in selection rules file {path}: {exc}") from exc

    if loaded is None:
        return ()
    if not isinstance(loaded, dict):
        raise SelectionRuleError(
            f"selection rules file {path}: top level must be a mapping with a 'rules' list, got {type(loaded).__name__}"
        )

    mapping = cast("dict[object, object]", loaded)
    unknown_top = sorted(str(key) for key in mapping if key != "rules")
    if unknown_top:
        raise SelectionRuleError(
            f"selection rules file {path}: unknown top-level key(s) {unknown_top}; only 'rules' is allowed"
        )

    rules_obj = mapping.get("rules")
    if rules_obj is None:
        return ()
    if not isinstance(rules_obj, list):
        raise SelectionRuleError(f"selection rules file {path}: 'rules' must be a list, got {type(rules_obj).__name__}")

    entries = cast("list[object]", rules_obj)
    return tuple(
        _parse_rule(entry, index=index, path=path, skills_source_dir=skills_source_dir)
        for index, entry in enumerate(entries, start=1)
    )


def resolve_rule_templates(
    rules: Sequence[SelectionRule],
    tasks: Sequence[RuleSelectableTask],
) -> tuple[str, ...]:
    """Resolve which skill templates the rules select for ``tasks``.

    A pure function of ``(tasks, rules)``: no filesystem access, no
    environment reads, no corpus statistics. A rule matches when any
    single task satisfies every axis the rule declares (``owned_files``
    always; ``task_type`` when present) - multi-task semantics are a
    union across tasks. Hits are deduplicated and ordered by rule
    position, then template name within a rule.

    Returns:
        Template file names (``<name>.md``) in deterministic order.
    """
    selected: list[str] = []
    seen: set[str] = set()
    for rule in rules:
        if not any(_rule_matches_task(rule, task) for task in tasks):
            continue
        for template_name in sorted(rule.templates):
            if template_name not in seen:
                seen.add(template_name)
                selected.append(template_name)
    return tuple(selected)


def _rule_matches_task(rule: SelectionRule, task: RuleSelectableTask) -> bool:
    """Return whether one task satisfies every axis ``rule`` declares."""
    if rule.task_type is not None and _coerce_task_type(getattr(task, "task_type", None)) != rule.task_type:
        return False
    owned = task.owned_files or []
    return any(fnmatchcase(entry, pattern) for pattern in rule.owned_files for entry in owned)


def _coerce_task_type(value: object) -> str | None:
    """Normalize a task's ``task_type`` field to its lowercase token.

    An enum member normalizes through its ``value``; a bare string through
    itself. The injector's local ``Task`` protocol does not carry
    ``task_type``, so an *absent* field (``None``) deterministically
    defaults to ``"standard"``. A field that is present but unrecognized -
    a task type this module does not know, or a non-string value - returns
    ``None`` and therefore matches no ``task_type``-scoped rule at all:
    coercing it to ``"standard"`` would inject operator-authored skills
    into tasks that are explicitly not standard. Matching by token rather
    than enum identity keeps this module import-free of scheduler
    internals (see ``_KNOWN_TASK_TYPE_TOKENS``).
    """
    if value is None:
        return "standard"
    token = getattr(value, "value", value)
    if isinstance(token, str):
        normalized = token.strip().lower()
        if normalized in _KNOWN_TASK_TYPE_TOKENS:
            return normalized
    return None


def _parse_rule(
    entry: object,
    *,
    index: int,
    path: Path,
    skills_source_dir: Path,
) -> SelectionRule:
    """Validate one rule mapping, raising :class:`SelectionRuleError` loudly."""
    label = f"selection rules file {path}: rule {index}"
    if not isinstance(entry, dict):
        raise SelectionRuleError(f"{label} must be a mapping, got {type(entry).__name__}")

    mapping = cast("dict[object, object]", entry)
    keys = {str(key) for key in mapping}
    if "role" in keys:
        raise SelectionRuleError(
            f"{label}: 'role' is not a rule axis - role -> skill binding is owned by "
            "ROLE_SKILL_MAP in bernstein.adapters.skills_injector; remove the 'role' key"
        )
    unknown = sorted(keys - _ALLOWED_RULE_KEYS)
    if unknown:
        raise SelectionRuleError(f"{label}: unknown key(s) {unknown}; allowed keys are {sorted(_ALLOWED_RULE_KEYS)}")

    owned_files = _parse_owned_files(mapping.get("owned_files"), label=label)
    task_type = _parse_task_type(mapping.get("task_type"), label=label)
    templates = _parse_skills(mapping.get("skills"), label=label, skills_source_dir=skills_source_dir)
    return SelectionRule(owned_files=owned_files, task_type=task_type, templates=templates)


def _parse_owned_files(value: object, *, label: str) -> tuple[str, ...]:
    """Validate the required ``owned_files`` axis: one glob or a list of globs."""
    if value is None:
        raise SelectionRuleError(f"{label}: 'owned_files' is required (a glob or list of globs)")
    if isinstance(value, str):
        raw_globs: list[object] = [value]
    elif isinstance(value, list):
        raw_globs = cast("list[object]", value)
    else:
        raise SelectionRuleError(
            f"{label}: 'owned_files' must be a string glob or a list of string globs, got {type(value).__name__}"
        )
    if not raw_globs:
        raise SelectionRuleError(f"{label}: 'owned_files' must not be empty")

    globs: list[str] = []
    for item in raw_globs:
        if not isinstance(item, str) or not item.strip():
            raise SelectionRuleError(f"{label}: 'owned_files' entries must be non-empty strings, got {item!r}")
        globs.append(item)
    return tuple(globs)


def _parse_task_type(value: object, *, label: str) -> str | None:
    """Validate the optional ``task_type`` axis against the known tokens."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise SelectionRuleError(f"{label}: 'task_type' must be a string, got {type(value).__name__}")
    token = value.strip().lower()
    if token not in _KNOWN_TASK_TYPE_TOKENS:
        raise SelectionRuleError(
            f"{label}: unknown task_type {value!r}; valid values are {sorted(_KNOWN_TASK_TYPE_TOKENS)}"
        )
    return token


def _parse_skills(value: object, *, label: str, skills_source_dir: Path) -> tuple[str, ...]:
    """Validate the ``skills`` list and that every named template exists on disk."""
    if value is None:
        raise SelectionRuleError(f"{label}: 'skills' is required (a list of skill template names)")
    if not isinstance(value, list):
        raise SelectionRuleError(f"{label}: 'skills' must be a list, got {type(value).__name__}")
    items = cast("list[object]", value)
    if not items:
        raise SelectionRuleError(f"{label}: 'skills' must not be empty")

    templates: list[str] = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise SelectionRuleError(f"{label}: 'skills' entries must be non-empty strings, got {item!r}")
        name = item.strip()
        template_name = name if name.endswith(".md") else f"{name}.md"
        # Containment: a template name is a bare file name inside the skills
        # directory, never a path. Without this, an absolute entry replaces
        # the base in the join and ``..`` walks out of the corpus, and the
        # injector would read and inject a file from outside the skills
        # directory as if it were a vetted template.
        if PurePosixPath(template_name).name != template_name or "\\" in template_name or template_name == ".md":
            raise SelectionRuleError(
                f"{label} names skill template {name!r}, which is not a bare template file name - "
                "rule templates must live directly in the skills directory, path components are not allowed"
            )
        if not (skills_source_dir / template_name).is_file():
            raise SelectionRuleError(
                f"{label} names skill template {name!r}, but {skills_source_dir / template_name} does not exist"
            )
        templates.append(template_name)
    return tuple(templates)


__all__ = [
    "SELECTION_RULES_FILENAME",
    "RuleSelectableTask",
    "SelectionRule",
    "SelectionRuleError",
    "load_selection_rules",
    "resolve_rule_templates",
    "selection_rules_path",
]
