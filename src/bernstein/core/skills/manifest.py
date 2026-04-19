"""Pydantic model for ``SKILL.md`` YAML frontmatter.

A skill's manifest declares the bare minimum that the orchestrator needs to
index the skill, plus pointers to optional reference files and scripts that
the agent can load on demand.

Schema (all fields strict-validated by Pydantic):

- ``name``        — lowercase slug ``[a-z][a-z0-9-]*``
- ``description`` — 20-500 chars, shown in the index
- ``trigger_keywords`` — optional keyword hints
- ``references``  — list of files under ``<skill>/references/``
- ``scripts``     — list of files under ``<skill>/scripts/``
- ``assets``      — list of files under ``<skill>/assets/``
- ``version``     — semver-ish; defaults to ``1.0.0``
- ``author``      — optional free-form attribution

Parsing failures point at the offending file so operators can correct the
manifest without greping through 17 skill directories.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

if TYPE_CHECKING:
    from pathlib import Path

# Precompiled once — Pydantic recompiles each time if we pass a string.
_NAME_PATTERN: re.Pattern[str] = re.compile(r"^[a-z][a-z0-9-]*$")

# Matches a line containing only ``---`` (with optional trailing whitespace).
# Anchored to line boundaries and uses only a bounded character class, so it
# is immune to catastrophic backtracking.
_FENCE_LINE_RE: re.Pattern[str] = re.compile(r"^---[ \t]*$")

# Hard cap on frontmatter size (16 KiB). A real SKILL.md header is ~500 bytes;
# anything larger is either corrupt or hostile. The cap is applied to the
# frontmatter slice — not the whole file — so legitimate markdown bodies
# stay unbounded.
_MAX_FRONTMATTER_BYTES = 16 * 1024


class SkillManifestError(ValueError):
    """Raised when a ``SKILL.md`` file is missing, malformed, or invalid.

    Always carries the originating path so operators can locate the file
    without needing to re-derive it from the traceback.
    """

    def __init__(self, path: Path, detail: str) -> None:
        super().__init__(f"{path}: {detail}")
        self.path = path
        self.detail = detail


class SkillManifest(BaseModel):
    """Strict-validated ``SKILL.md`` frontmatter.

    Attributes mirror the OpenAI Agents SDK v2 Skills spec. Unknown keys are
    rejected so typos (``keywords`` vs ``trigger_keywords``) do not silently
    drop metadata.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=20, max_length=500)
    trigger_keywords: list[str] = Field(default_factory=list[str])
    references: list[str] = Field(default_factory=list[str])
    scripts: list[str] = Field(default_factory=list[str])
    assets: list[str] = Field(default_factory=list[str])
    version: str = "1.0.0"
    author: str | None = None

    @staticmethod
    def validate_name(value: str) -> str:
        """Ensure ``name`` matches the lowercase-slug regex.

        Pydantic's ``Field(pattern=...)`` validates at construction time but
        produces a less friendly error. We run the check explicitly in
        :func:`parse_skill_md` so :class:`SkillManifestError` carries the
        originating path.
        """
        if not _NAME_PATTERN.match(value):
            raise ValueError(
                f"name {value!r} must match regex ^[a-z][a-z0-9-]*$ "
                "(lowercase letters, digits, hyphens; must start with a letter)"
            )
        return value


def parse_skill_md(path: Path) -> tuple[SkillManifest, str]:
    """Parse a ``SKILL.md`` file into a manifest and its markdown body.

    Args:
        path: Path to the ``SKILL.md`` file.

    Returns:
        ``(manifest, body)`` — ``body`` is the markdown text after the
        closing ``---`` marker with surrounding whitespace stripped.

    Raises:
        SkillManifestError: When the file is missing, lacks frontmatter,
            contains invalid YAML, or fails Pydantic validation.
    """
    if not path.is_file():
        raise SkillManifestError(path, "SKILL.md file does not exist")

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SkillManifestError(path, f"cannot read file: {exc}") from exc

    try:
        front_raw, body = _split_frontmatter(raw)
    except ValueError as exc:
        raise SkillManifestError(path, str(exc)) from exc

    try:
        data: object = yaml.safe_load(front_raw)
    except yaml.YAMLError as exc:
        raise SkillManifestError(path, f"invalid YAML frontmatter: {exc}") from exc

    if not isinstance(data, dict):
        raise SkillManifestError(
            path,
            f"frontmatter must be a YAML mapping, got {type(data).__name__}",
        )

    # YAML parses into dict[Any, Any] as far as pyright is concerned.
    # Explicitly cast + validate each key so the Pydantic model receives a
    # narrow dict[str, Any] (``extra="forbid"`` catches typos anyway, but
    # we still want strict typing up to the validation boundary).
    raw_data: dict[Any, Any] = cast("dict[Any, Any]", data)
    cleaned: dict[str, Any] = {}
    for key, value in raw_data.items():
        if not isinstance(key, str):
            raise SkillManifestError(path, f"frontmatter key {key!r} must be a string")
        cleaned[key] = value

    name_value = cleaned.get("name")
    if isinstance(name_value, str):
        try:
            SkillManifest.validate_name(name_value)
        except ValueError as exc:
            raise SkillManifestError(path, str(exc)) from exc

    try:
        manifest = SkillManifest.model_validate(cleaned)
    except ValidationError as exc:
        # Pydantic's default message is fine but we prefix it with the path.
        raise SkillManifestError(path, f"invalid manifest: {exc.errors()}") from exc

    return manifest, body


def _split_frontmatter(raw: str) -> tuple[str, str]:
    """Split a ``SKILL.md`` text into ``(frontmatter_yaml, body)``.

    Implemented as a linear line-scan rather than a ``re.DOTALL``-style
    regex so there is no nested quantifier for a malicious input to exploit
    (the original pattern ``\\A---\\s*\\r?\\n(.*?)\\r?\\n---\\s*(?:\\r?\\n|\\Z)(.*)``
    tripped SonarCloud's ReDoS heuristic — see S5852). The frontmatter slice
    is bounded by :data:`_MAX_FRONTMATTER_BYTES` so even a file that never
    closes the fence can never force more than ``O(cap)`` work.

    Args:
        raw: Full ``SKILL.md`` text.

    Returns:
        ``(front, body)`` — ``front`` is the YAML between the two fences
        (unstripped, suitable for :func:`yaml.safe_load`); ``body`` is the
        markdown after the closing fence with surrounding whitespace stripped.

    Raises:
        ValueError: When the file does not start with ``---`` on its first
            line, when the closing fence is missing, or when the frontmatter
            would exceed :data:`_MAX_FRONTMATTER_BYTES`.
    """
    lines = raw.splitlines()
    if not lines or not _FENCE_LINE_RE.match(lines[0]):
        raise ValueError("missing YAML frontmatter — expected ``---`` on the first line")

    front_lines: list[str] = []
    close_idx: int | None = None
    front_bytes = 0

    for idx in range(1, len(lines)):
        line = lines[idx]
        if _FENCE_LINE_RE.match(line):
            close_idx = idx
            break
        # Count against the byte cap eagerly so a pathological input can't
        # force us to accumulate gigabytes before we notice.
        front_bytes += len(line.encode("utf-8")) + 1  # +1 for the newline
        if front_bytes > _MAX_FRONTMATTER_BYTES:
            raise ValueError(f"frontmatter exceeds {_MAX_FRONTMATTER_BYTES} bytes — refusing to parse")
        front_lines.append(line)

    if close_idx is None:
        raise ValueError("unterminated YAML frontmatter — missing closing ``---`` fence")

    front = "\n".join(front_lines)
    body = "\n".join(lines[close_idx + 1 :]).strip()
    return front, body
