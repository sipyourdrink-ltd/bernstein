"""Smart error autocorrect for common plan mistakes.

Scans YAML plan data for typos in role, complexity, and scope fields,
suggests corrections using fuzzy matching, and optionally applies fixes.
"""

from __future__ import annotations

import copy
import difflib
from dataclasses import dataclass

KNOWN_ROLES: list[str] = [
    "manager",
    "backend",
    "frontend",
    "qa",
    "security",
    "devops",
    "architect",
    "docs",
    "reviewer",
    "ml-engineer",
]

KNOWN_COMPLEXITIES: list[str] = [
    "low",
    "medium",
    "high",
    "critical",
]

KNOWN_SCOPES: list[str] = [
    "small",
    "medium",
    "large",
]

# Maps plan field names to their known valid values.
_FIELD_CANDIDATES: dict[str, list[str]] = {
    "role": KNOWN_ROLES,
    "complexity": KNOWN_COMPLEXITIES,
    "scope": KNOWN_SCOPES,
}


@dataclass(frozen=True)
class CorrectionSuggestion:
    """A single suggested correction for a plan field value.

    Attributes:
        field: The plan field name (e.g. "role", "complexity", "scope").
        original: The original (likely incorrect) value found in the plan.
        suggested: The suggested replacement value.
        confidence: Match confidence between 0 and 1.
        reason: Human-readable explanation of why this correction is suggested.
    """

    field: str
    original: str
    suggested: str
    confidence: float
    reason: str


def fuzzy_match(
    value: str,
    candidates: list[str],
    threshold: float = 0.6,
) -> str | None:
    """Find the closest match for *value* among *candidates*.

    Uses ``difflib.SequenceMatcher`` ratio.  Returns the best candidate
    if its similarity ratio meets or exceeds *threshold*, otherwise ``None``.

    Args:
        value: The string to match.
        candidates: Valid values to match against.
        threshold: Minimum similarity ratio (0-1) to accept a match.

    Returns:
        The best matching candidate, or ``None`` if nothing exceeds the
        threshold.
    """
    if not value or not candidates:
        return None

    best_match: str | None = None
    best_ratio: float = 0.0

    normalised = value.lower().strip()

    for candidate in candidates:
        ratio = difflib.SequenceMatcher(None, normalised, candidate.lower()).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_match = candidate

    if best_ratio >= threshold:
        return best_match
    return None


def _iter_plan_steps(plan_data: dict[str, object]) -> list[dict[str, object]]:
    """Extract all step dicts from a plan's stages list."""
    stages = plan_data.get("stages")
    if not isinstance(stages, list):
        return []
    steps: list[dict[str, object]] = []
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        stage_steps = stage.get("steps")
        if not isinstance(stage_steps, list):
            continue
        for step in stage_steps:
            if isinstance(step, dict):
                steps.append(step)
    return steps


def _check_step_field(
    step: dict[str, object],
    field_name: str,
    candidates: frozenset[str],
) -> CorrectionSuggestion | None:
    """Check a single field in a step for typos, returning a suggestion or None."""
    raw = step.get(field_name)
    if raw is None or not isinstance(raw, str):
        return None
    normalised = raw.lower().strip()
    if normalised in candidates:
        return None
    match = fuzzy_match(normalised, candidates)
    if match is None:
        return None
    ratio = difflib.SequenceMatcher(None, normalised, match.lower()).ratio()
    return CorrectionSuggestion(
        field=field_name,
        original=raw,
        suggested=match,
        confidence=round(ratio, 2),
        reason=f"{field_name.capitalize()} '{raw}' not found. Did you mean '{match}'?",
    )


def check_plan_for_typos(plan_data: dict[str, object]) -> list[CorrectionSuggestion]:
    """Scan all steps in *plan_data* for typos in role/complexity/scope.

    Iterates through ``stages -> steps`` and compares each field value
    against the known valid values.  Exact matches are silently accepted;
    near-matches produce a :class:`CorrectionSuggestion`.

    Args:
        plan_data: Parsed YAML plan dictionary with a ``stages`` key.

    Returns:
        A list of correction suggestions (may be empty).
    """
    suggestions: list[CorrectionSuggestion] = []
    for step in _iter_plan_steps(plan_data):
        for field_name, candidates in _FIELD_CANDIDATES.items():
            suggestion = _check_step_field(step, field_name, candidates)
            if suggestion is not None:
                suggestions.append(suggestion)
    return suggestions


def apply_corrections(
    plan_data: dict[str, object],
    corrections: list[CorrectionSuggestion],
) -> dict[str, object]:
    """Return a copy of *plan_data* with *corrections* applied.

    Each correction replaces the original value in every matching step.
    The input dict is not mutated.

    Args:
        plan_data: Parsed YAML plan dictionary.
        corrections: Corrections to apply.

    Returns:
        A new plan dict with corrected values.
    """
    if not corrections:
        return plan_data

    result: dict[str, object] = copy.deepcopy(plan_data)
    lookup: dict[tuple[str, str], str] = {(c.field, c.original): c.suggested for c in corrections}

    for step in _iter_plan_steps(result):
        for field_name in _FIELD_CANDIDATES:
            raw = step.get(field_name)
            if not isinstance(raw, str):
                continue
            key = (field_name, raw)
            if key in lookup:
                step[field_name] = lookup[key]

    return result


def format_correction_prompt(suggestions: list[CorrectionSuggestion]) -> str:
    """Format suggestions as a human-readable prompt.

    Each suggestion is rendered on its own line in the form:
    ``Role 'backnd' not found. Did you mean 'backend'? (confidence: 0.83)``

    Args:
        suggestions: Corrections to format.

    Returns:
        Multi-line string, or empty string if no suggestions.
    """
    if not suggestions:
        return ""
    lines: list[str] = []
    for s in suggestions:
        lines.append(f"{s.reason} (confidence: {s.confidence})")
    return "\n".join(lines)
