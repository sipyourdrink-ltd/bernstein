"""Shared parser for backlog markdown task files."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from pathlib import Path


class BacklogParseError(ValueError):
    """Raised when a backlog file carries a malformed declaration (#3110).

    Distinct from the "not a task file" case (which returns ``None``): the
    file *is* a task file, but one of its typed declarations - today the
    ``artifact_spec`` block - is malformed. Fail-closed: the file must not
    silently become a default ``code_diff`` task. Scan loops catch this
    per-file, surface the message (which names the offending field), and
    continue with the other files.
    """


@dataclass(frozen=True)
class ParsedBacklogTask:
    """Normalized metadata extracted from one backlog file.

    Extended fields from Ticket Format v1 (YAML frontmatter) are used
    by the orchestrator for model routing, quality gates, and file locking.
    """

    title: str
    description: str
    role: str
    priority: int
    scope: str
    complexity: str
    source_file: str
    # ── Extended fields (Ticket Format v1) ──
    ticket_id: str = ""
    model: str = "auto"
    effort: str = "normal"
    ticket_type: str = "feature"
    tags: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    affected_paths: tuple[str, ...] = ()
    context_files: tuple[str, ...] = ()
    estimated_minutes: int = 45
    require_review: bool = False
    require_human_approval: bool = False
    janitor_signals: tuple[dict[str, str], ...] = ()
    # Issue #1634: declarative parallel-safety flag and user-story rollback
    # grouping. ``parallel_safe`` defaults to False (serial-only) so that
    # absence of the ``[P]`` marker is conservative.
    parallel_safe: bool = False
    story_id: str | None = None
    # Issue #3110: the declared artifact contract, as the validated
    # ``ArtifactSpec.to_dict()`` payload (kept as a plain mapping so this
    # module's import surface stays stdlib-only). ``None`` = no declaration:
    # the task keeps the default code_diff contract.
    artifact_spec: dict[str, object] | None = None

    def to_task_payload(self) -> dict[str, object]:
        """Convert to POST /tasks payload."""
        payload: dict[str, object] = {
            "title": self.title,
            "description": self.description,
            "role": self.role,
            "priority": self.priority,
            "scope": self.scope,
            "complexity": self.complexity,
        }
        if self.model != "auto":
            payload["model"] = self.model
        if self.effort != "normal":
            payload["effort"] = self.effort
        if self.estimated_minutes != 45:
            payload["estimated_minutes"] = self.estimated_minutes
        if self.parallel_safe:
            payload["parallel_safe"] = True
        if self.story_id:
            payload["story_id"] = self.story_id
        if self.artifact_spec is not None:
            # A validated declaration must reach the server; dropping it here
            # would silently downgrade the task to code_diff (#3110).
            payload["artifact_spec"] = dict(self.artifact_spec)
        # Issue #3375: declared context files must reach the worker. The
        # spawner reads ``task.metadata["context_files"]`` at dispatch, and
        # POST /tasks persists ``metadata`` verbatim onto the stored task -
        # dropping the field here was the break in that wire. ``ticket_type``
        # and ``affected_paths`` fall out at the same spot and ride in the
        # same metadata mapping; ``depends_on`` needs ticket-id resolution
        # and is deliberately not carried here. Every key is conditional so
        # a ticket declaring nothing produces a byte-identical payload.
        metadata: dict[str, object] = {}
        if self.context_files:
            metadata["context_files"] = list(self.context_files)
        if self.affected_paths:
            metadata["affected_paths"] = list(self.affected_paths)
        if self.ticket_type != "feature":
            metadata["ticket_type"] = self.ticket_type
        if metadata:
            payload["metadata"] = metadata
        return payload


@dataclass(frozen=True)
class ParsedTaskLine:
    """One row parsed from a task DAG markdown checkbox file.

    The DAG format is one task per markdown checkbox line::

        - [ ] [T001] [P] [US1] Add YAML loader

    Markers (all optional except ``[T<id>]``):

    * ``[T<id>]`` task identifier (required to differentiate from prose).
    * ``[P]`` parallel-safe flag.
    * ``[US<n>]`` user-story rollback grouping.
    * ``-> T002, T003`` inline dependency arrow on the same line.
    """

    task_id: str
    description: str
    parallel_safe: bool = False
    story_id: str | None = None
    depends_on: tuple[str, ...] = ()


def parse_backlog_text(filename: str, content: str) -> ParsedBacklogTask | None:
    """Parse backlog markdown text into normalized task metadata.

    Returns ``None`` for text that is not a task file at all. Raises
    :class:`BacklogParseError` for a task file whose ``artifact_spec``
    declaration is malformed - the refusal names the offending field and the
    file never becomes a task (issue #3110).
    """
    text = content.strip()
    if not text:
        return None

    parsed = _parse_yaml_frontmatter(filename, content)
    if parsed is not None:
        return parsed
    return _parse_markdown_fields(filename, content)


def parse_backlog_path(path: Path) -> ParsedBacklogTask | None:
    """Parse backlog file from disk."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return parse_backlog_text(path.name, content)


def _parse_yaml_frontmatter(filename: str, content: str) -> ParsedBacklogTask | None:
    if not content.startswith("---"):
        return None
    end = content.find("\n---", 3)
    if end == -1:
        return None
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return None
    try:
        loaded: object = yaml.safe_load(content[3:end]) or {}
    except Exception:
        return None
    if not isinstance(loaded, dict):
        return None
    raw = cast("dict[str, object]", loaded)

    title = str(raw.get("title", "")).strip()
    if not title:
        body_lines = content[end + 4 :].splitlines()
        title = _extract_h1_title(body_lines)
    if not title:
        return None

    # Extended fields
    tags_raw = raw.get("tags", [])
    tags = tuple(str(t) for t in tags_raw) if isinstance(tags_raw, list) else ()
    deps_raw = raw.get("depends_on", [])
    depends_on = tuple(str(d) for d in deps_raw) if isinstance(deps_raw, list) else ()
    affected_raw = raw.get("affected_paths", [])
    affected = tuple(str(p) for p in affected_raw) if isinstance(affected_raw, list) else ()
    context_raw = raw.get("context_files", [])
    context = tuple(str(p) for p in context_raw) if isinstance(context_raw, list) else ()
    janitor_raw = raw.get("janitor_signals", [])
    janitor = (
        tuple(
            {"type": str(s.get("type", "")), "value": str(s.get("value", ""))}
            for s in janitor_raw
            if isinstance(s, dict)
        )
        if isinstance(janitor_raw, list)
        else ()
    )

    # Issue #3110: strict artifact-contract declaration, parsed by the one
    # shared parser (imported lazily to keep this module's import surface
    # stdlib-only). Fail-closed: a malformed block raises with the offending
    # field named instead of being dropped, because a dropped declaration
    # silently turns the task into a default code_diff task.
    artifact_payload: dict[str, object] | None = None
    raw_artifact = raw.get("artifact_spec")
    if raw_artifact is not None:
        from bernstein.core.tasks.artifacts import ArtifactSpecError, parse_artifact_spec

        try:
            artifact_payload = cast("dict[str, object]", parse_artifact_spec(raw_artifact).to_dict())
        except ArtifactSpecError as exc:
            raise BacklogParseError(f"{filename}: {exc}") from exc

    # Use only the body after the YAML frontmatter as the description.
    # The frontmatter metadata is already extracted into typed fields.
    body = content[end + 4 :].strip()
    description = body or str(raw.get("description", title))

    story_raw = raw.get("story_id")
    story_id = str(story_raw).strip() if story_raw else None

    return ParsedBacklogTask(
        title=title,
        description=description,
        role=str(raw.get("role", "backend")).strip() or "backend",
        priority=_parse_priority(raw.get("priority", 2)),
        scope=_parse_scope(str(raw.get("scope", "medium"))),
        complexity=_parse_complexity(str(raw.get("complexity", "medium"))),
        source_file=filename,
        ticket_id=str(raw.get("id", "")).strip(),
        model=str(raw.get("model", "auto")).strip(),
        effort=str(raw.get("effort", "normal")).strip(),
        ticket_type=str(raw.get("type", "feature")).strip(),
        tags=tags,
        depends_on=depends_on,
        affected_paths=affected,
        context_files=context,
        estimated_minutes=int(cast(int, raw.get("estimated_minutes", 45))),
        require_review=bool(raw.get("require_review", False)),
        require_human_approval=bool(raw.get("require_human_approval", False)),
        janitor_signals=janitor,
        parallel_safe=bool(raw.get("parallel_safe", False)),
        story_id=story_id,
        artifact_spec=artifact_payload,
    )


def _parse_markdown_fields(filename: str, content: str) -> ParsedBacklogTask | None:
    lines = content.splitlines()
    title = _extract_h1_title(lines)
    if not title:
        return None

    role_match = re.search(r"\*\*Role:\*\*\s*(.+)", content, flags=re.IGNORECASE)
    priority_match = re.search(r"\*\*Priority:\*\*\s*(.+)", content, flags=re.IGNORECASE)
    scope_match = re.search(r"\*\*Scope:\*\*\s*(.+)", content, flags=re.IGNORECASE)
    complexity_match = re.search(r"\*\*Complexity:\*\*\s*(.+)", content, flags=re.IGNORECASE)

    return ParsedBacklogTask(
        title=title,
        description=content.strip(),
        role=(role_match.group(1).strip() if role_match else "backend"),
        priority=_parse_priority(priority_match.group(1) if priority_match else 2),
        scope=_parse_scope(scope_match.group(1) if scope_match else "medium"),
        complexity=_parse_complexity(complexity_match.group(1) if complexity_match else "medium"),
        source_file=filename,
    )


def _extract_h1_title(lines: list[str]) -> str:
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return ""


def _parse_priority(raw: object) -> int:
    """Normalise a raw priority value to the internal 1-3 scale.

    Ticket YAML files may use a 0-4 scale (p0=critical ... p4=future).
    The task server and all routing/scheduling code use 1-3:
      0,1 -> 1 (critical)  |  2 -> 2 (normal)  |  3,4 -> 3 (nice-to-have)
    """
    match = re.search(r"\d+", str(raw))
    if not match:
        return 2
    value = int(match.group(0))
    if value <= 1:
        return 1
    if value >= 3:
        return 3
    return 2


def _parse_scope(raw: str) -> str:
    value = raw.strip().lower()
    return value if value in {"small", "medium", "large"} else "medium"


def _parse_complexity(raw: str) -> str:
    value = raw.strip().lower()
    return value if value in {"low", "medium", "high"} else "medium"


# ---------------------------------------------------------------------------
# Task DAG markdown checkbox format (issue #1634)
# ---------------------------------------------------------------------------

# Matches the leading checkbox token at the start of a stripped line.
_CHECKBOX_PREFIX = re.compile(r"^[-*]\s+\[[ xX]\]\s+")
# A single marker token like ``[T001]``, ``[P]``, ``[US1]`` at the
# beginning of the description.  We strip these iteratively so the
# remainder is the human-readable task description.
_MARKER_TOKEN = re.compile(r"^\[([^\[\]]+)\]\s*")
# Inline dependency arrow: ``-> T002, T003`` at the end of a line.
_DEPENDS_INLINE = re.compile(r"(?:->|→)\s*([\w,\s-]+?)\s*$", re.ASCII)
_TASK_ID = re.compile(r"^T\d+[\w-]*$", re.ASCII)
_STORY_ID = re.compile(r"^US\d+[\w-]*$", re.IGNORECASE | re.ASCII)


def parse_task_line(line: str) -> ParsedTaskLine | None:
    """Parse one task DAG markdown checkbox line.

    Returns ``None`` if the line is not a task row (header, blank, prose).
    The format is::

        - [ ] [T001] [P] [US1] Add YAML loader -> T000

    Markers may appear in any order and any combination after the
    checkbox.  An optional ``-> dep1, dep2`` arrow at the end of the line
    declares inline dependencies.
    """
    stripped = line.strip()
    if not stripped:
        return None
    match = _CHECKBOX_PREFIX.match(stripped)
    if match is None:
        return None
    remainder = stripped[match.end() :]

    task_id = ""
    parallel_safe = False
    story_id: str | None = None

    while True:
        m = _MARKER_TOKEN.match(remainder)
        if m is None:
            break
        token = m.group(1).strip()
        consumed = True
        if _TASK_ID.match(token):
            task_id = token
        elif token.upper() == "P":
            parallel_safe = True
        elif _STORY_ID.match(token):
            story_id = token.upper()
        else:
            consumed = False
        if not consumed:
            break
        remainder = remainder[m.end() :]

    if not task_id:
        return None

    depends_on: tuple[str, ...] = ()
    dep_match = _DEPENDS_INLINE.search(remainder)
    description = remainder
    if dep_match is not None:
        raw_deps = dep_match.group(1)
        depends_on = tuple(d.strip() for d in raw_deps.split(",") if d.strip())
        description = remainder[: dep_match.start()].rstrip(" -→")

    return ParsedTaskLine(
        task_id=task_id,
        description=description.strip(),
        parallel_safe=parallel_safe,
        story_id=story_id,
        depends_on=depends_on,
    )


def parse_task_lines(content: str) -> list[ParsedTaskLine]:
    """Parse all task DAG rows from a markdown document.

    Lines that are not task rows (headings, prose, blanks) are skipped.
    """
    out: list[ParsedTaskLine] = []
    for line in content.splitlines():
        parsed = parse_task_line(line)
        if parsed is not None:
            out.append(parsed)
    return out
