"""SKILL.md format — YAML frontmatter metadata + markdown body.

Supports Claude Code's SKILL.md convention: a Markdown file with a YAML
frontmatter block (delimited by ``---``) containing metadata such as name,
description, hooks, paths, context, and effort, followed by the prompt body.

Example::

    ---
    name: backend-developer
    description: Server-side engineering specialist
    hooks: [pytest, ruff]
    paths: [src/bernstein/adapters/, tests/unit/]
    context: FastAPI codebase
    effort: high
    ---
    # You are a Backend Engineer
    ...

If the file has no frontmatter, the entire content is used as the body
and all metadata fields default to empty values.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003 (used at runtime for path.read_text)
from typing import Any, TypeGuard, cast

import yaml

logger = logging.getLogger(__name__)

# Valid effort levels — used to normalise the ``effort`` field.
_VALID_EFFORT: frozenset[str] = frozenset({"max", "high", "normal", "low"})

# Fields we extract from frontmatter.
_SKILL_FIELDS: tuple[str, ...] = (
    "name",
    "description",
    "hooks",
    "paths",
    "context",
    "effort",
)


def _is_str_list(value: object) -> TypeGuard[list[str]]:
    """Return ``True`` if *value* is a list of strings."""
    if not isinstance(value, list):
        return False
    for item in cast("list[object]", value):  # noqa: SIM110 (pyright needs explicit loop)
        if not isinstance(item, str):
            return False
    return True


def _is_str(value: object) -> TypeGuard[str]:
    """Return ``True`` if *value* is a string."""
    return isinstance(value, str)


# -- Data model ---------------------------------------------------------------


@dataclass(frozen=True)
class SkillMD:
    """Parsed SKILL.md content: frontmatter metadata plus markdown body.

    Attributes:
        name: Short skill identifier (e.g. ``"backend-developer"``).
        description: One-line capability summary.
        hooks: Post-action hooks/tool names the skill declares.
        paths: File or directory path gloars the skill cares about.
        context: Project/domain context string injected into prompts.
        effort: Preferred effort level (``"max"``, ``"high"``, ``"normal"``,
            ``"low"``).
        body: Raw markdown content after the frontmatter fence.
        source: Path the skill was loaded from.
    """

    name: str
    description: str
    hooks: list[str]
    paths: list[str]
    context: str
    effort: str
    body: str
    source: str

    def to_catalog_agent_fields(self) -> dict[str, Any]:
        """Convert to the field names expected by the catalog system.

        Returns:
            Dict with ``description``, ``model``, ``effort`` keys suitable
            for ``CachedAgentEntry`` construction.
        """
        return {
            "description": self.description,
            "model": "sonnet",  # sensible default
            "effort": self.effort,
        }


# -- Parsing ------------------------------------------------------------------


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Extract YAML frontmatter and markdown body from *text*.

    The frontmatter is the block between the first ``---`` delimiter and the
    next ``---`` on its own line, following the standard SKILL.md convention.

    Args:
        text: Full file contents.

    Returns:
        ``(metadata_dict, body_string)``.  The metadata dict is empty
        when no frontmatter is found or parsing fails.
    """
    stripped = text.strip()
    if not stripped.startswith("---"):
        return {}, stripped

    rest = stripped[3:]  # text after opening "---"
    end_idx = rest.find("\n---")
    if end_idx == -1:
        # No closing fence — treat entire content as body.
        return {}, stripped

    fm_text = rest[:end_idx].strip()
    body = rest[end_idx + 4 :].lstrip("\n")  # skip "\n---"

    if not fm_text:
        return {}, body

    try:
        parsed: object = yaml.safe_load(fm_text)
    except yaml.YAMLError:
        logger.warning("Invalid YAML frontmatter in SKILL.md")
        return {}, body

    if not isinstance(parsed, dict):
        return {}, body

    return dict(cast("dict[str, Any]", parsed)), body


def normalise_skill(data: dict[str, Any], defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    """Extract and normalise known skill fields from *data*.

    Unknown keys are silently dropped.  Missing keys fall back to
    *defaults* (or built-in defaults).

    Args:
        data: Raw dict from YAML frontmatter.
        defaults: Optional override defaults.

    Returns:
        Dict containing only ``_SKILL_FIELDS`` keys with sensible values.
    """
    base: dict[str, Any] = {
        "name": "",
        "description": "",
        "hooks": [],
        "paths": [],
        "context": "",
        "effort": "normal",
    }
    if defaults:
        base.update(defaults)

    name = data.get("name", base["name"])
    desc = data.get("description", base["description"])

    hooks = data.get("hooks", base["hooks"])
    if not _is_str_list(hooks):
        hooks = base["hooks"]

    paths = data.get("paths", base["paths"])
    if not _is_str_list(paths):
        paths = base["paths"]

    ctx = data.get("context", base["context"])
    if not _is_str(ctx):
        ctx = base["context"]

    effort = data.get("effort", base["effort"])
    if not _is_str(effort):
        effort = base["effort"]
    else:
        effort = effort.strip().lower()
        if effort not in _VALID_EFFORT:
            effort = base["effort"]

    return {
        "name": str(name).strip() if name else base["name"],
        "description": str(desc).strip() if desc else base["description"],
        "hooks": hooks,
        "paths": paths,
        "context": str(ctx).strip() if ctx else base["context"],
        "effort": effort,
    }


def load_skill_md(path: Path, *, role_fallback: str | None = None) -> SkillMD | None:
    """Load and parse a SKILL.md file.

    Args:
        path: Path to the ``.md`` file.
        role_fallback: Role name to use when frontmatter has no ``name``.
            When ``None``, the function returns ``None`` for nameless skills.

    Returns:
        ``SkillMD`` instance, or ``None`` if the file cannot be read or
        yields no name (and no ``role_fallback`` is provided).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Cannot read SKILL.md %s: %s", path, exc)
        return None

    fm, body = parse_frontmatter(text)

    if not fm and not body.strip().startswith("---"):
        # No frontmatter at all — the whole file is the body.
        name = role_fallback or ""
        if not name:
            return None
        return SkillMD(
            name=name,
            description="",
            hooks=[],
            paths=[],
            context="",
            effort="normal",
            body=text.strip(),
            source=str(path),
        )

    fields = normalise_skill(fm)

    name: str = fields.get("name") or (role_fallback or "")
    if not name:
        return None

    return SkillMD(
        name=name,
        description=str(fields.get("description", "")),
        hooks=list(fields.get("hooks", [])),
        paths=list(fields.get("paths", [])),
        context=str(fields.get("context", "")),
        effort=str(fields.get("effort", "normal")),
        body=body if body else text.strip(),
        source=str(path),
    )
