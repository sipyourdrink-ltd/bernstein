"""Prompt rendering utilities for agent spawning."""

from __future__ import annotations

import fnmatch
import logging
import os
import re as _re
import shlex as _shlex
import subprocess as _subprocess
import time as _time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.agents import project_context as _project_context
from bernstein.core.agents.heartbeat import HeartbeatMonitor
from bernstein.core.agents.project_context import resolve_project_context
from bernstein.core.context_recommendations import RecommendationEngine
from bernstein.core.defaults import SPAWN
from bernstein.core.lessons import gather_lessons_for_context
from bernstein.core.memory.jsonl_log import JSONLMemoryLog
from bernstein.templates.renderer import TemplateError, render_role_prompt

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.agency_loader import AgencyAgent
    from bernstein.core.context import TaskContextBuilder
    from bernstein.core.knowledge.task_graph import TaskGraph
    from bernstein.core.models import Task

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Conditional context activation (T677)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SectionRule:
    """Declares when a named prompt section should be activated.

    A section is included when **any** of its activation conditions are met.
    Sections with no conditions (all fields empty/None) are always included.

    Attributes:
        roles: Roles for which the section is relevant (empty = all roles).
        exclude_roles: Roles for which the section is irrelevant.
        file_patterns: Gitignore-style globs matched against task ``owned_files``.
            If set, the section activates only when at least one file matches.
        require_session: When True, section requires a non-empty ``session_id``.
        min_scope: Minimum task scope ordinal (small=0, medium=1, large=2).
            None means no scope constraint.
    """

    roles: frozenset[str] = frozenset()
    exclude_roles: frozenset[str] = frozenset()
    file_patterns: tuple[str, ...] = ()
    require_session: bool = False
    min_scope: int | None = None


def _scope_ordinal(scope_value: str) -> int:
    """Map a scope enum value to an ordinal for comparison.

    Args:
        scope_value: Scope string ("small", "medium", "large").

    Returns:
        Integer ordinal (0, 1, 2).
    """
    return {"small": 0, "medium": 1, "large": 2}.get(scope_value, 1)


def _files_match_patterns(files: list[str], patterns: tuple[str, ...]) -> bool:
    """Check if any file in *files* matches any gitignore-style glob in *patterns*.

    Uses ``fnmatch.fnmatch`` for glob matching - supports ``*``, ``**``, ``?``,
    and ``[seq]`` syntax.

    Args:
        files: List of file paths from the task.
        patterns: Gitignore-style globs.

    Returns:
        True if at least one file matches at least one pattern.
    """
    for fp in files:
        for pat in patterns:
            if fnmatch.fnmatch(fp, pat):
                return True
    return False


# Section relevance rules.  Sections not listed here are always included
# (they are "critical" - role, tasks, instructions, git_safety).
SECTION_RULES: dict[str, SectionRule] = {
    "specialists": SectionRule(roles=frozenset({"manager"})),
    "team awareness": SectionRule(
        exclude_roles=frozenset({"docs", "analyst", "visionary"}),
        require_session=True,
    ),
    "team coordination": SectionRule(
        exclude_roles=frozenset({"docs", "analyst", "visionary"}),
        require_session=True,
    ),
    "file ownership": SectionRule(
        exclude_roles=frozenset({"manager", "docs", "analyst", "visionary"}),
        require_session=True,
    ),
    "heartbeat": SectionRule(require_session=True, min_scope=1),
    "recommendations": SectionRule(
        exclude_roles=frozenset({"manager", "visionary"}),
    ),
    "lessons": SectionRule(
        exclude_roles=frozenset({"manager", "visionary"}),
    ),
    "memory_lessons": SectionRule(),  # Auto-injected from JSONL log; gated by env var
    "predecessor": SectionRule(),  # Always included when present (already gated by data)
    "project": SectionRule(),  # Always included when present (already gated by data)
    "meta nudges": SectionRule(),  # Always included when present
}


def section_is_relevant(
    section_name: str,
    *,
    role: str,
    scope: str,
    owned_files: list[str],
    session_id: str,
    rules: dict[str, SectionRule] | None = None,
) -> bool:
    """Decide whether a named section should be included in the prompt.

    Sections not present in the rules table are always included (they are
    considered critical).  For sections with rules, all applicable conditions
    must pass:

    - ``roles``: if non-empty, the task role must be in the set.
    - ``exclude_roles``: if non-empty, the task role must NOT be in the set.
    - ``file_patterns``: if non-empty, at least one owned file must match.
    - ``require_session``: if True, session_id must be non-empty.
    - ``min_scope``: if set, the task scope ordinal must be >= this value.

    Args:
        section_name: Name of the prompt section.
        role: Task role (lowercase).
        scope: Task scope value ("small", "medium", "large").
        owned_files: File paths owned by the task.
        session_id: Agent session identifier.
        rules: Optional override rules (defaults to ``SECTION_RULES``).

    Returns:
        True if the section should be included.
    """
    rule = (rules if rules is not None else SECTION_RULES).get(section_name)
    if rule is None:
        # No rule means always include (critical section).
        return True

    role_lower = role.lower()

    # Check role inclusion
    if rule.roles and role_lower not in rule.roles:
        return False

    # Check role exclusion
    if rule.exclude_roles and role_lower in rule.exclude_roles:
        return False

    # Check file pattern matching
    if rule.file_patterns and not _files_match_patterns(owned_files, rule.file_patterns):
        return False

    # Check session requirement
    if rule.require_session and not session_id:
        return False

    # Check scope threshold
    return not (rule.min_scope is not None and _scope_ordinal(scope) < rule.min_scope)


def filter_sections(
    sections: list[tuple[str, str]],
    *,
    role: str,
    scope: str,
    owned_files: list[str],
    session_id: str,
    rules: dict[str, SectionRule] | None = None,
) -> list[tuple[str, str]]:
    """Filter a named-sections list, keeping only relevant sections.

    Args:
        sections: List of ``(section_name, content)`` tuples.
        role: Task role.
        scope: Task scope value.
        owned_files: Files owned by the task.
        session_id: Agent session identifier.
        rules: Optional override rules.

    Returns:
        Filtered list with irrelevant sections removed.
    """
    kept: list[tuple[str, str]] = []
    dropped: list[str] = []
    for name, content in sections:
        if section_is_relevant(
            name,
            role=role,
            scope=scope,
            owned_files=owned_files,
            session_id=session_id,
            rules=rules,
        ):
            kept.append((name, content))
        else:
            dropped.append(name)
    if dropped:
        logger.info("Conditional context: dropped %d sections (%s)", len(dropped), ", ".join(dropped))
    return kept


# ---------------------------------------------------------------------------
# Lesson extraction cache (per-role, TTL-based)
# ---------------------------------------------------------------------------
_lesson_cache: dict[str, tuple[float, str]] = {}  # role -> (timestamp, text)
_LESSON_CACHE_TTL = SPAWN.lesson_cache_ttl_s

# ---------------------------------------------------------------------------
# Memory-log auto-injection (BERNSTEIN_MEMORY_AUTO_INJECT)
# ---------------------------------------------------------------------------

#: Environment switch - flip to "1" to enable JSONL memory injection into
#: leaf-agent prompts.  Default off so existing flows are unchanged.
_MEMORY_AUTO_INJECT_ENV_VAR = "BERNSTEIN_MEMORY_AUTO_INJECT"

#: JSONL key under ``.bernstein/memory/`` that the spawner reads when
#: auto-injection is enabled.  Matches the convention documented in
#: :mod:`bernstein.core.memory.jsonl_log`.
_MEMORY_LESSONS_KEY = "lessons"

#: Cap on the number of recent entries injected into the prompt.  Bounded
#: so a runaway log cannot blow the prompt budget.
_MEMORY_LESSONS_MAX = 10

#: Stable separator used to demarcate the lessons block.  Kept as plain
#: tags (not Markdown-only) so the boundary survives any downstream
#: dedup / compression pass that strips Markdown headers.
_MEMORY_LESSONS_OPEN = "<lessons>"
_MEMORY_LESSONS_CLOSE = "</lessons>"


def _memory_auto_inject_enabled() -> bool:
    """Return True when ``BERNSTEIN_MEMORY_AUTO_INJECT`` is truthy.

    The env-var-driven gate keeps the integration off by default so existing
    runs are byte-identical until an operator flips the switch.

    Returns:
        ``True`` when the env var is set to ``1`` / ``true`` / ``yes`` / ``on``.
    """
    raw = os.environ.get(_MEMORY_AUTO_INJECT_ENV_VAR, "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _format_memory_lesson(entry: dict[str, Any]) -> str:
    """Render a single memory entry as a compact bullet.

    Args:
        entry: One JSONL record from ``.bernstein/memory/lessons.jsonl``.

    Returns:
        A single-line bullet describing the lesson.  Returns an empty
        string when the entry has no human-readable text - caller skips
        empty bullets so the rendered block stays tight.
    """
    text = entry.get("lesson") or entry.get("text") or entry.get("message")
    if not text:
        # Fall back to a compact JSON dump so callers that store
        # arbitrary structured records still get *something* useful.
        try:
            import json as _json

            return f"- {_json.dumps(entry, ensure_ascii=False, sort_keys=True)}"
        except Exception:
            return ""
    task = entry.get("task")
    if task:
        return f"- ({task}) {text}"
    return f"- {text}"


def _render_memory_lessons_block(workdir: Path) -> str:
    """Read the most recent memory lessons and render the injection block.

    Applies age bounding, stepwise weighting, and per-author caps to the
    lessons before rendering them into the prompt. The block is sandwiched
    between :data:`_MEMORY_LESSONS_OPEN` and :data:`_MEMORY_LESSONS_CLOSE`
    so the orchestrator-side renderer can grep for it deterministically
    (e.g. when auditing prompts).

    Args:
        workdir: Project working directory.  The JSONL log lives at
            ``<workdir>/.bernstein/memory/lessons.jsonl``.

    Returns:
        Rendered block string, or empty string when the log is absent /
        empty.  No-op semantics: a missing log MUST NOT raise; the
        spawner relies on this for first-run robustness.
    """
    root = workdir / ".bernstein" / "memory"
    log = JSONLMemoryLog(root=root)
    try:
        entries = log.read(_MEMORY_LESSONS_KEY)
    except Exception:  # pragma: no cover - defensive, never block spawns
        logger.debug("memory auto-inject: read failed", exc_info=True)
        return ""
    if not entries:
        return ""

    now = _time.time()
    horizon = SPAWN.memory_lessons_horizon_s

    # 1. Age bounding - filter entries older than horizon
    recent_entries = []
    for idx, entry in enumerate(entries):
        ts = entry.get("timestamp")
        if ts is None:
            # No timestamp - treat as newest for age bounding (always include)
            age = 0.0
            weight = 1.0
        else:
            age = now - ts
            if age >= horizon + 1.0:
                continue
            # Compute weight with stepwise decay based on age
            bucket_size = horizon * SPAWN.memory_lessons_weight_decay_factor
            if bucket_size <= 0:
                bucket_size = horizon / 2
            bucket = int(age // bucket_size)
            weight = SPAWN.memory_lessons_weight_decay_factor**bucket
        entry_copy = dict(entry)
        entry_copy["_weight"] = weight
        entry_copy["_age"] = age
        entry_copy["_has_ts"] = ts is not None
        entry_copy["_insertion_order"] = idx
        recent_entries.append(entry_copy)

    # 2. Sort all entries for deterministic output
    # Entries with timestamps: sort by (age asc = newer first, weight desc = higher weight first)
    # Entries without timestamps: preserve FIFO order (newest = highest insertion_order kept first)
    def _sort_key(e: dict[str, Any]) -> tuple:
        if e["_has_ts"]:
            # Timestamped: age ascending (newest first), then weight desc, then insertion
            return (0, e["_age"], -e["_weight"], e["_insertion_order"])
        else:
            # Untimestamped: insertion_order descending (newest last = kept first)
            return (1, -e["_insertion_order"])

    recent_entries.sort(key=_sort_key)

    # 3. Apply global cap
    capped_by_global = recent_entries[:_MEMORY_LESSONS_MAX]

    # 4. Per-author cap - only for timestamped entries (not for backward compat)
    ts_capped = [e for e in capped_by_global if e["_has_ts"]]
    no_ts_capped = [e for e in capped_by_global if not e["_has_ts"]]

    # Apply per-author cap only to timestamped entries
    author_entries: dict[str, list[dict[str, Any]]] = {}
    for entry in ts_capped:
        author = entry.get("author", "")
        if author not in author_entries:
            author_entries[author] = []
        author_entries[author].append(entry)

    ts_final: list[dict[str, Any]] = []
    for _author, entries_list in author_entries.items():
        ts_final.extend(entries_list[: SPAWN.memory_lessons_max_per_author])

    # Untimestamped entries already capped globally, just pass through
    final_entries = no_ts_capped + ts_final

    bullets: list[str] = []
    for entry in final_entries:
        rendered = _format_memory_lesson(entry)
        if rendered:
            bullets.append(rendered)
    if not bullets:
        return ""

    body = "\n".join(bullets)
    return f"\n{_MEMORY_LESSONS_OPEN}\n{body}\n{_MEMORY_LESSONS_CLOSE}\n"


# ---------------------------------------------------------------------------
# Cache-safe parameters for forked agents
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CacheSafeParams:
    """Parameters that forked agents must preserve for prompt-cache stability.

    When an agent spawns a child (forked) agent, the cache key depends on
    the system prompt prefix remaining identical.  This structure documents
    which values MUST be preserved unchanged to keep the cache valid, and
    which may vary per fork without breaking it.

    Stable fields (must match parent for cache reuse):
        role: Agent role name used for the role template.
        templates_hash: SHA-256 hash of the templates directory contents.
        project_context_hash: SHA-256 hash of ``.sdd/project.md``.
        git_safety_protocol: The git safety rules injected into every prompt.
        agent_protocol_prefix: Any protocol prefix shared across all agents.

    Variable fields (allowed to differ without breaking the cache):
        task_descriptions: Text describing the assigned tasks.
        specialist_descriptions: Descriptions of available specialist agents.
        fork_messages: Additional messages appended to the user content.
        session_id: Agent session identifier for signal file paths.
    """

    # Stable fields (cache key depends on these)
    role: str
    templates_hash: str
    project_context_hash: str
    git_safety_protocol: str
    agent_protocol_prefix: str = ""

    # Variable fields (do NOT affect cache key)
    task_descriptions: str = ""
    specialist_descriptions: str = ""
    fork_messages: list[str] = field(default_factory=list[str])
    session_id: str = ""

    def compute_cache_key(self) -> str:
        """Compute the stable cache key from the invariant fields.

        Returns:
            SHA-256 hex digest of the cache-stable prefix.
        """
        import hashlib

        stable = (
            f"{self.role}\n"
            f"{self.templates_hash}\n"
            f"{self.project_context_hash}\n"
            f"{self.git_safety_protocol}\n"
            f"{self.agent_protocol_prefix}\n"
        )
        return hashlib.sha256(stable.encode("utf-8")).hexdigest()

    def validate_against(self, parent: CacheSafeParams) -> list[str]:
        """Compare this fork's stable fields against the parent's.

        Args:
            parent: The parent agent's cache-safe parameters.

        Returns:
            List of field names that have changed (cache break).
        """
        breaks: list[str] = []
        stable_fields = (
            "role",
            "templates_hash",
            "project_context_hash",
            "git_safety_protocol",
            "agent_protocol_prefix",
        )
        for field_name in stable_fields:
            if getattr(self, field_name) != getattr(parent, field_name):
                breaks.append(field_name)
        return breaks


# ---------------------------------------------------------------------------
# Module-level file cache (mtime-keyed, automatically invalidates on change)
# ---------------------------------------------------------------------------
_DIR_CACHE: dict[str, tuple[float, list[str]]] = {}

# The implementation moved to ``project_context`` so this module and
# ``spawn_prompt`` share one cache and one resolver. These names stay: the
# warm pool reads role YAML through the reader, ``spawner.py`` re-exports
# both, and tests reach for the cache to assert invalidation.
_FILE_CACHE = _project_context._FILE_CACHE
_read_cached = _project_context.read_cached


def _list_subdirs_cached(path: Path) -> list[str]:
    """Return sorted list of immediate subdirectory names, cached by mtime.

    Args:
        path: Directory to list.

    Returns:
        Sorted subdirectory names, or empty list if path is not a directory.
    """
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _DIR_CACHE.pop(key, None)
        return []
    cached = _DIR_CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    names = sorted(d.name for d in path.iterdir() if d.is_dir())
    _DIR_CACHE[key] = (mtime, names)
    return names


def _render_signal_check(session_id: str) -> str:
    """Return signal-check instructions to append to every agent's system prompt.

    Args:
        session_id: The session ID assigned to this agent.

    Returns:
        Markdown block instructing the agent to poll signal files.
    """
    return (
        "\n## Signal files (check periodically)\n"
        "Every 60 seconds, check for orchestrator signals:\n"
        "```bash\n"
        f"cat .sdd/runtime/signals/{session_id}/WAKEUP 2>/dev/null\n"
        f"cat .sdd/runtime/signals/{session_id}/SHUTDOWN 2>/dev/null\n"
        f"cat .sdd/runtime/signals/{session_id}/COMMAND 2>/dev/null\n"
        "```\n"
        "If **SHUTDOWN** exists:\n"
        "```bash\n"
        'git add -u && git commit -m "[WIP] <task title>" 2>/dev/null || true\n'
        "exit 0\n"
        "```\n"
        "If **WAKEUP** exists: read it, address the concern, then continue working.\n"
        "If **COMMAND** exists: read its content as an instruction from the user, "
        "execute it, then delete the file:\n"
        "```bash\n"
        f"rm .sdd/runtime/signals/{session_id}/COMMAND\n"
        "```\n"
    )


def _render_git_safety_protocol() -> str:
    """Return git safety rules injected into every agent prompt (T727).

    Prevents agents from performing dangerous git operations.

    Returns:
        Markdown block with git safety rules.
    """
    return (
        "## Git safety protocol\n"
        "You MUST follow these git safety rules at all times:\n"
        "- NEVER use ``--force`` or ``-f`` with ``git push``.  Force-push is prohibited.\n"
        "- NEVER skip or bypass git hooks (e.g. ``--no-verify``, ``--no-commit-hooks``).\n"
        "- NEVER commit secrets, API keys, tokens, or credentials.\n"
        "- ALWAYS review changes with ``git diff`` before staging.\n"
        "- ALWAYS commit from a worktree branch (``agent/<session_id>``), never ``main``.\n"
        "- If a hook blocks, do not bypass it.  Ask the orchestrator for guidance.\n"
    )


def _extract_tags_from_tasks(tasks: list[Task]) -> list[str]:
    """Derive lesson-retrieval tags from a batch of tasks.

    Uses the role and significant title words as tags.

    Args:
        tasks: Batch of tasks.

    Returns:
        List of lowercase tags for lesson lookup.
    """
    stop_words = {
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "has",
        "have",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "must",
        "shall",
        "can",
        "not",
        "no",
        "all",
        "each",
        "every",
        "both",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "than",
        "too",
        "very",
        "just",
        "into",
        "out",
        "up",
        "down",
        "over",
        "this",
        "that",
        "it",
        "its",
    }
    tags: set[str] = set()
    for task in tasks:
        tags.add(task.role.lower())
        for word in task.title.lower().split():
            cleaned = word.strip("-_.,;:!?()[]{}\"'`#")
            if len(cleaned) > 2 and cleaned not in stop_words:
                tags.add(cleaned)
    return sorted(tags)


def _render_predecessor_context(tasks: list[Task], task_graph: TaskGraph | None) -> str:
    """Build a context section from INFORMS/TRANSFORMS predecessor outputs.

    Args:
        tasks: Batch of tasks being assigned.
        task_graph: Optional task graph for looking up typed edges.

    Returns:
        Markdown section with predecessor results, or empty string.
    """
    if task_graph is None:
        return ""

    lines: list[str] = []
    for task in tasks:
        pred_ctx = task_graph.predecessor_context(task.id)
        for item in pred_ctx:
            summary = item["result_summary"]
            if not summary:
                continue
            edge_label = "informed by" if item["edge_type"] == "informs" else "transforms output of"
            lines.append(f"- **{item['title']}** ({edge_label}): {summary}")

    if not lines:
        return ""
    return (
        "\n## Predecessor context\n"
        "The following completed tasks provide context for your work:\n" + "\n".join(lines) + "\n"
    )


def _resolve_role_prompt(
    role: str,
    tasks: list[Task],
    context: dict[str, Any],
    templates_dir: Path,
    catalog_system_prompt: str | None,
    agency_catalog: dict[str, AgencyAgent] | None,
) -> str:
    """Resolve the role prompt from skills, catalog, template, or fallback.

    Resolution order (oai-004):

    1. ``catalog_system_prompt`` - explicit Agency override.
    2. Skill pack - ``templates/skills/<role>/SKILL.md`` plus a compact
       index for the other skills, injected via
       :func:`bernstein.core.planning.role_resolver.resolve_role_prompt`.
    3. Legacy role template - ``templates/roles/<role>/system_prompt.md``.
    4. Agency catalog fallback or the generic ``"You are a <role> specialist."``
       stub.
    """
    del tasks  # reserved for future task-specific overrides
    if catalog_system_prompt:
        return catalog_system_prompt

    # Try the new skill-pack progressive-disclosure path first. It falls
    # back to the legacy template automatically when no skill exists.
    try:
        from bernstein.core.planning.role_resolver import resolve_role_prompt

        resolved = resolve_role_prompt(
            role,
            templates_dir=templates_dir,
            legacy_context={k: str(v) for k, v in context.items()},
        )
    except Exception as exc:
        logger.debug("Skill-backed role resolution failed for %s: %s", role, exc)
    else:
        if resolved.source in {"skill", "legacy"}:
            logger.debug(
                "Resolved role %s via %s (skill=%s)",
                role,
                resolved.source,
                resolved.skill_name,
            )
            return resolved.body
        # ``fallback`` means neither skill nor legacy template matched - fall
        # through to the agency / default-stub path below.

    try:
        return render_role_prompt(role, context, templates_dir=templates_dir)
    except (FileNotFoundError, TemplateError) as exc:
        logger.debug("Template render failed for role %s, using fallback: %s", role, exc)
        return _render_fallback(role, templates_dir, agency_catalog)


def _get_lesson_context(role: str, tasks: list[Task], workdir: Path) -> str:
    """Load and cache lesson context for a role, enforcing budget."""
    sdd_dir = workdir / ".sdd"
    lesson_tags = _extract_tags_from_tasks(tasks)
    cache_key = f"{role}:{','.join(lesson_tags)}"
    now = _time.monotonic()
    cached_lesson = _lesson_cache.get(cache_key)
    if cached_lesson is not None and (now - cached_lesson[0]) < _LESSON_CACHE_TTL:
        lesson_context = cached_lesson[1]
        logger.debug("Lesson cache hit for role=%s (%d chars)", role, len(lesson_context))
    else:
        lesson_context = gather_lessons_for_context(sdd_dir, lesson_tags)
        _lesson_cache[cache_key] = (now, lesson_context)
        logger.debug("Lesson cache miss for role=%s, extracted %d chars", role, len(lesson_context))

    # Evict expired entries
    if len(_lesson_cache) > 50:
        expired_keys = [k for k, (ts, _) in _lesson_cache.items() if now - ts > _LESSON_CACHE_TTL]
        for k in expired_keys:
            del _lesson_cache[k]
        if expired_keys:
            logger.debug("Lesson cache cleaned %d expired entries", len(expired_keys))

    # Enforce budget
    from bernstein.core.context_compression import DEFAULT_CATEGORY_BUDGETS

    lesson_budget = DEFAULT_CATEGORY_BUDGETS.get("lessons", 5_000)
    if lesson_context and (len(lesson_context) // 4) > lesson_budget:
        logger.info("Truncating lessons: exceeded budget of %d tokens", lesson_budget)
        lesson_context = lesson_context[: lesson_budget * 4] + "..."
    return lesson_context


def _legacy_completion_instructions(tasks: list[Task]) -> str:
    """Fallback completion instructions when the contract include is absent.

    Mirrors the pre-contract prose: concrete curl commands with
    --retry-connrefused (not --retry-all-errors) so curl only retries
    transient connection failures, NOT 4xx errors like 409 Conflict.
    """
    completion_cmds = "\n".join(
        f"curl -s -w '\\n%{{http_code}}' --retry 3 --retry-delay 2 --retry-connrefused "
        f"-X POST http://127.0.0.1:8052/tasks/{t.id}/complete "
        f'-H "Content-Type: application/json" '
        f'-d \'{{"result_summary": "Completed: {t.title}"}}\''
        for t in tasks
    )
    return (
        f"Complete these tasks. When ALL are done, mark each complete on the task server:\n\n"
        f"```bash\n{completion_cmds}\n```\n\n"
        f"**Important:** Only retry on connection refused / network errors. "
        f"If the server returns HTTP 409 or any other 4xx error, do NOT retry. "
        f"The task state has changed and retrying will not help. Just exit.\n\n"
        f"Then exit."
    )


def _render_completion_instructions(tasks: list[Task]) -> str:
    """Build the terminal-outcome instruction block for the worker prompt.

    Renders the shared ``templates/roles/_includes/completion_contract.md``
    partial (#2244) so the runtime prompt and the role task templates emit
    the same schema-enforced completion/refusal instructions. Falls back
    to the legacy prose-summary instructions when the include is missing
    (e.g. a stripped install), so spawns never lose the done signal.
    """
    from bernstein import _BUNDLED_TEMPLATES_DIR

    include_path = _BUNDLED_TEMPLATES_DIR / "roles" / "_includes" / "completion_contract.md"
    try:
        template = include_path.read_text(encoding="utf-8")
    except OSError:
        logger.warning("Completion contract include missing at %s; using legacy instructions", include_path)
        return _legacy_completion_instructions(tasks)

    ids = [t.id for t in tasks]
    if len(ids) == 1:
        header = "Complete this task. When done, report a terminal outcome for it:\n\n"
        block = template.replace("{{TASK_ID}}", ids[0])
    else:
        header = (
            "Complete these tasks. When ALL are done, report a terminal outcome "
            "for each of these task ids (one POST per id): " + ", ".join(ids) + "\n\n"
        )
        block = template.replace("{{TASK_ID}}", "<task_id>")
    return f"{header}{block}\nThen exit."


def render_artifact_contract(tasks: list[Task]) -> str:
    """Render the artifact contract section for artifact-mode tasks (#4539).

    An artifact-mode task completes on a signed lineage receipt over its
    produced artifact, not on a git SHA - but until now the spawn prompt never
    surfaced the contract the completion side (:mod:`bernstein.core.tasks.artifact_completion`)
    actually enforces. This helper closes that gap: the agent is shown the
    exact kind, the exact output path the verifier reads, and every declared
    acceptance criterion.

    Reads the *same* ``task.artifact_spec`` object and the *same*
    :func:`~bernstein.core.tasks.artifact_completion.artifact_output_path`
    resolver as the completion path, so the prompt and the verifier cannot
    drift - there is no second parse and no parallel rendering of the spec.

    Returns ``""`` when every task completes through the git path
    (``code_diff``), so the default coding-task prompt stays byte-unchanged.
    """
    from bernstein.core.tasks.artifact_completion import artifact_output_path, is_artifact_mode

    artifact_tasks = [t for t in tasks if is_artifact_mode(t)]
    if not artifact_tasks:
        return ""

    lines: list[str] = [
        "## Artifact contract",
        "",
        "Your work is judged by the artifacts declared below, not by a source "
        "diff. Produce exactly the declared artifact; when it is complete the "
        "orchestrator records a signed lineage receipt over its canonical bytes.",
    ]
    for t in artifact_tasks:
        spec = t.artifact_spec
        out_path = artifact_output_path(t)
        lines.extend(("", f"### {t.id}: {t.title}"))
        lines.append(f"- **Kind**: `{spec.kind.value}`")
        lines.append(f"- **Output path**: `{out_path}` (relative to the working directory)")
        criteria = list(spec.criteria)
        if criteria:
            lines.append("- **Acceptance criteria** (every one must hold):")
            for criterion in criteria:
                lines.append(f"  - `{criterion.type}`: {criterion.value}")
        else:
            lines.append("- **Acceptance criteria**: none declared")
        lines.append(
            f"- Completion is judged by this contract. Write the artifact to `{out_path}` "
            f"and leave it there; if you cannot satisfy a criterion, do not mark the "
            f"task complete - report a typed refusal instead."
        )
    return "\n".join(lines) + "\n"


def _render_prompt(
    tasks: list[Task],
    templates_dir: Path,
    workdir: Path,
    agency_catalog: dict[str, AgencyAgent] | None = None,
    catalog_system_prompt: str | None = None,
    context_builder: TaskContextBuilder | None = None,
    session_id: str = "",
    bulletin_summary: str = "",
    task_graph: TaskGraph | None = None,
    meta_messages: list[str] | None = None,
    file_ownership: dict[str, str] | None = None,
    max_turns: int | None = None,
    mailbox_section: str = "",
    model: str = "",
) -> str:
    """Build the full agent prompt from role template + tasks + context.

    Uses the Jinja2-style template renderer for proper variable substitution.
    Falls back to simple string concatenation if rendering fails.  When the
    template renderer fallback is used, the agency catalog is checked for
    roles not covered by templates/roles/.

    If *catalog_system_prompt* is provided it replaces the built-in role
    template entirely, so the spawner can inject catalog-defined personas.

    Args:
        tasks: Batch of 1-3 tasks (all same role).
        templates_dir: Root of templates/roles/ directory.
        workdir: Project working directory.
        agency_catalog: Optional Agency agent catalog for extended roles.
        catalog_system_prompt: Optional system prompt from a catalog agent.
            When set, this replaces the template/role-based role prompt.
        context_builder: Optional TaskContextBuilder for rich context injection.
        bulletin_summary: Optional recent bulletin activity to inject as a
            team-awareness section. Empty string means no section is added.
        task_graph: Optional task graph for context retrieval.
        meta_messages: Optional list of operational nudges/hints (T423).
        file_ownership: Optional mapping of filepath -> agent_id for files
            currently being edited by other agents.
        max_turns: Optional best-effort resolution of the agent's tool-use
            turn cap, known at the caller's spawn call site. When present,
            renders a static "## Turn budget" section so the model
            self-polices instead of exploring until MaxTurnsExceeded fires
            with zero output (work/bernstein/m27-nudge-plan.md, Approach C
            MINIMAL). ``None`` means no value was resolvable at
            prompt-build time - the section is skipped, not rendered with
            a placeholder.
        mailbox_section: Pre-rendered coordination-mailbox section (#2357),
            produced by
            :func:`bernstein.core.communication.task_mailbox.render_mailbox_section`
            from the task's pending messages. A pure projection of the
            mailbox journal, so every adapter type receives the identical
            bytes. Empty string means no section is added.

    Returns:
        Complete prompt string ready for the CLI adapter.
    """
    role = tasks[0].role

    # Build task descriptions block
    task_lines: list[str] = []
    for i, task in enumerate(tasks, 1):
        task_lines.extend((f"### Task {i}: {task.title} (id={task.id})", task.description))
        if task.owned_files:
            task_lines.append(f"Files: {', '.join(task.owned_files)}")
        task_lines.append("")
    task_block = "\n".join(task_lines)

    # Project context from .sdd/project.md if it exists
    project_context = resolve_project_context(tasks, workdir)

    # Completion-contract instructions shared with templates/roles via the
    # single _includes/completion_contract.md partial (#2244).
    instructions = _render_completion_instructions(tasks)

    # Available roles from templates directory
    available_roles = ""
    if templates_dir.is_dir():
        available_roles = ", ".join(_list_subdirs_cached(templates_dir))

    # Specialist agents from agency catalog
    specialist_block = ""
    if agency_catalog and role == "manager":
        specialists: list[str] = [
            f"- **{agent.name}** ({agent.role}): {agent.description}"
            for agent in sorted(agency_catalog.values(), key=lambda a: a.role)
        ]
        if specialists:
            specialist_block = (
                "\n\n## Available specialist agents (from Agency catalog)\n"
                "When creating tasks, prefer assigning to a specialist role if one matches.\n"
                "Fall back to generic roles (backend, qa, etc.) if no specialist fits.\n\n" + "\n".join(specialists)
            )

    # Build rich task context via TaskContextBuilder
    rich_context = ""
    if context_builder is not None:
        try:
            rich_context = context_builder.build_context(tasks)
        except Exception as exc:
            logger.warning("TaskContextBuilder failed, skipping rich context: %s", exc)

    # Build template context for renderer
    context: dict[str, Any] = {
        "GOAL": tasks[0].title,
        "TASK_DESCRIPTION": task_block,
        "PROJECT_STATE": project_context,
        "AVAILABLE_ROLES": available_roles,
        "INSTRUCTIONS": instructions,
        "SPECIALISTS": specialist_block,
    }

    role_prompt = _resolve_role_prompt(
        role,
        tasks,
        context,
        templates_dir,
        catalog_system_prompt,
        agency_catalog,
    )

    lesson_context = _get_lesson_context(role, tasks, workdir)

    # Assemble final prompt as named sections for budget-aware compression
    from bernstein.core.section_dedup import deduplicate_section

    named_sections: list[tuple[str, str]] = [
        ("role", role_prompt),
        ("git_safety", deduplicate_section(_render_git_safety_protocol())),
    ]
    if specialist_block:
        named_sections.append(("specialists", specialist_block))
    # Consensus relay section (issue #4678): inject prior cycle decisions for
    # manager-role spawns only. read_file from the file store; omit the section
    # entirely when the store is absent, empty, or chain verification fails.
    if role == "manager":
        try:
            from bernstein.core.orchestration.consensus_relay import (
                MANAGER_RELAY_SECTION,
                spawn_section_for_workdir,
            )

            relay_block = spawn_section_for_workdir(workdir)
            if relay_block:
                named_sections.append((MANAGER_RELAY_SECTION, relay_block))
        except Exception as exc:
            # Never block a spawn because of relay problems - but a section
            # that silently stops appearing is indistinguishable from a store
            # that is simply empty, which is how this feature would die
            # unnoticed.
            logger.warning("Consensus relay section omitted from manager spawn: %s", exc)
    # KV-cache locality: lessons go AFTER the stable header (role,
    # git_safety, specialists) but BEFORE the variable goal/task body
    # so the cacheable prefix stays byte-stable across spawns.
    if _memory_auto_inject_enabled():
        memory_block = _render_memory_lessons_block(workdir)
        if memory_block:
            named_sections.append(("memory_lessons", memory_block))
    named_sections.append(("tasks", f"\n## Assigned tasks\n{task_block}"))
    # Artifact contract (#4539): surface the kind/path/criteria an
    # artifact-mode task is judged by. Empty for the git path, so a plain
    # coding task's prompt is unchanged.
    artifact_contract = render_artifact_contract(tasks)
    if artifact_contract:
        named_sections.append(("artifact contract", f"\n{artifact_contract}"))
    if lesson_context:
        named_sections.append(("lessons", f"\n{lesson_context}\n"))
    if rich_context:
        named_sections.append(("context", f"\n{rich_context}\n"))
    # Parent context inheritance: inject parent's context summary
    parent_ctx_parts = [t.parent_context for t in tasks if t.parent_context]
    if parent_ctx_parts:
        named_sections.append(
            (
                "parent_context",
                "\n## Parent context (inherited)\n"
                "This task was decomposed from a parent task. The parent agent gathered "
                "the following context:\n" + "\n".join(parent_ctx_parts) + "\n",
            )
        )
    predecessor_ctx = _render_predecessor_context(tasks, task_graph)
    if predecessor_ctx:
        named_sections.append(("predecessor", predecessor_ctx))
    # Only include bulletin when it has real content beyond whitespace/header
    if bulletin_summary and bulletin_summary.strip():
        named_sections.append(
            (
                "team awareness",
                deduplicate_section(
                    f"\n## Team awareness\n"
                    f"Other agents are working in parallel. Recent activity:\n{bulletin_summary}\n\n"
                    f"If you need to create a shared utility, check if it already exists first.\n"
                    f"If you define an API endpoint, use consistent naming with existing endpoints.\n",
                ),
            )
        )
    # Coordination mailbox (#2357): typed messages other workers addressed to
    # these tasks, rendered deterministically from the mailbox journal so two
    # adapter types receive byte-identical context.
    if mailbox_section and mailbox_section.strip():
        named_sections.append(("coordination mailbox", deduplicate_section(mailbox_section)))
    # File ownership warnings: tell agents which files are locked by others
    if file_ownership:
        # Exclude files owned by the current agent
        other_files = {fp: owner for fp, owner in file_ownership.items() if owner != session_id}
        if other_files:
            lines = ["\n## Files currently being edited by other agents (do NOT modify):"]
            for fpath, owner in sorted(other_files.items()):
                lines.append(f"- {fpath} (by {owner})")
            lines.append(
                "\nIf you need changes in these files, post a bulletin requesting the owning agent to make them.\n"
            )
            named_sections.append(("file ownership", "\n".join(lines)))
    # Team coordination: instruct agents to post discoveries and query peers
    if session_id:
        agent_id = session_id
        named_sections.append(
            (
                "team coordination",
                deduplicate_section(
                    "\n## Team coordination\n"
                    "When you create a new file, define an API, or discover something other agents should know:\n"
                    "```bash\n"
                    "curl -s -X POST http://127.0.0.1:8052/bulletin "
                    '-H "Content-Type: application/json" \\\n'
                    '  -d \'{"agent_id": "' + agent_id + '", "type": "finding", '
                    '"content": "<describe what you created or discovered>"}\'\n'
                    "```\n"
                    "Examples of what to post:\n"
                    "- Created a new module: `Created src/foo/bar.py with FooClass`\n"
                    "- Defined an API endpoint: `Added POST /tasks/{id}/retry`\n"
                    "- Found a bug or gotcha: `Config loader silently ignores missing keys`\n"
                    "\n### Direct channel (agent-to-agent queries)\n"
                    "To ask another agent a question (e.g. about a schema or interface they own):\n"
                    "```bash\n"
                    "curl -s -X POST http://127.0.0.1:8052/channel/query "
                    '-H "Content-Type: application/json" \\\n'
                    '  -d \'{"sender_agent": "' + agent_id + '", '
                    '"topic": "<short-topic>", '
                    '"content": "<your question>", '
                    '"target_role": "<role>"}\'\n'
                    "```\n"
                    "Check for questions addressed to you:\n"
                    "```bash\n"
                    "curl -s http://127.0.0.1:8052/channel/queries?agent_id=" + agent_id + "\n"
                    "```\n"
                    "Respond to a query:\n"
                    "```bash\n"
                    "curl -s -X POST http://127.0.0.1:8052/channel/<query_id>/respond "
                    '-H "Content-Type: application/json" \\\n'
                    '  -d \'{"responder_agent": "' + agent_id + '", '
                    '"content": "<your answer>"}\'\n'
                    "```\n",
                ),
            )
        )
    try:
        rec_engine = RecommendationEngine(workdir)
        rec_engine.build()
        rec_section = rec_engine.render_for_prompt(role, max_chars=2000)
        if rec_section:
            named_sections.append(("recommendations", f"\n{rec_section}\n"))
    except Exception as exc:
        logger.debug("Recommendation rendering failed: %s", exc)
    if project_context:
        named_sections.append(("project", deduplicate_section(f"\n## Project context\n{project_context}\n")))
    # Mirrors spawner_core._render_prompt's output-style section (see the
    # turn-budget note below for why both renderers carry the same blocks).
    try:
        from bernstein.core.config.output_styles import load_output_styles

        _style_prompt = load_output_styles(workdir).get_prompt()
    except Exception as exc:
        logger.debug("Output style resolution failed: %s", exc)
        _style_prompt = ""
    if _style_prompt:
        named_sections.append(("output style", deduplicate_section(f"\n## Output style\n{_style_prompt}\n")))
    named_sections.append(("instructions", deduplicate_section(f"\n## Instructions\n{instructions}\n")))
    if session_id:
        try:
            heartbeat_instructions = HeartbeatMonitor(workdir).inject_heartbeat_instructions(session_id)
            named_sections.append(
                (
                    "heartbeat",
                    deduplicate_section(
                        "\n## Heartbeat (background)\n"
                        "Run this in the background to report progress:\n"
                        f"```bash\n{heartbeat_instructions}\n```\n",
                    ),
                )
            )
        except Exception as exc:
            logger.debug("Heartbeat instructions unavailable: %s", exc)
    if session_id:
        named_sections.append(("signal", deduplicate_section(_render_signal_check(session_id))))

    if meta_messages:
        nudges_block = "\n## Operational nudges\n" + "\n".join(f"- {m}" for m in meta_messages) + "\n"
        named_sections.append(("meta nudges", nudges_block))

    # Turn-budget nudge (work/bernstein/m27-nudge-plan.md, Approach C
    # MINIMAL) - see the sibling implementation/rationale in
    # bernstein.core.agents.spawner_core._render_prompt, which is the
    # actually-live production render path; this module's _render_prompt
    # is kept in sync for test coverage (tests/unit/agents/test_spawn_prompt_pure.py)
    # and any future caller that switches to it.
    if max_turns is not None and max_turns > 0:
        halfway_turn = max(1, max_turns // 2)
        # Mirrors spawner_core._render_prompt: near_end is 3 turns before
        # the cap, never below/at halfway, and never past max_turns itself.
        near_end_turn = min(max_turns, max(halfway_turn + 1, max_turns - 3))
        turn_budget_block = (
            "\n## Turn budget\n"
            f"You have a hard budget of {max_turns} tool-use turns for this task.\n\n"
            f"- By turn {halfway_turn} (roughly halfway): if the core task is already "
            "done, STOP - write your final summary now. Do not spend remaining turns "
            "re-reading files you've already read or re-verifying work that already "
            "passed.\n"
            f"- By turn {near_end_turn} (near your limit): if you have not yet written "
            "any code/output, you are out of time for further exploration - write "
            "SOMETHING now, even a partial/best-effort change, rather than continuing "
            "to read.\n"
            "- On your FINAL turn: your last message must be plain text summarizing "
            "what you accomplished, what remains unfinished, and any risks. Do not "
            "attempt further tool calls.\n\n"
            "STOP CONDITIONS - if any of these are true, stop immediately and write "
            "your summary:\n"
            "- All requested changes are implemented and tests pass\n"
            "- You have verified your work is correct\n"
            "- You are re-reading files you already read with no new information to "
            "gain\n"
        )
        named_sections.append(("turn budget", turn_budget_block))
        logger.info(
            "Turn budget nudge injected for role=%s session=%s: max_turns=%d halfway=%d near_end=%d",
            role,
            session_id,
            max_turns,
            halfway_turn,
            near_end_turn,
        )
    else:
        logger.info(
            "Turn budget nudge skipped for role=%s session=%s: max_turns not available "
            "at prompt-build time (resolved value=%r)",
            role,
            session_id,
            max_turns,
        )

    # Strip empty/whitespace-only sections before compression
    named_sections = [(name, content) for name, content in named_sections if content and content.strip()]

    # Conditional context activation (T677): skip sections irrelevant to this task
    all_owned: list[str] = []
    for t in tasks:
        all_owned.extend(t.owned_files)
    # Use the broadest scope across the task batch
    scope_values = {"small": 0, "medium": 1, "large": 2}
    max_scope = max((scope_values.get(t.scope.value, 1) for t in tasks), default=1)
    scope_name = {0: "small", 1: "medium", 2: "large"}.get(max_scope, "medium")
    named_sections = filter_sections(
        named_sections,
        role=role,
        scope=scope_name,
        owned_files=all_owned,
        session_id=session_id,
    )

    # Log prompt stats for observability
    total_chars = sum(len(content) for _, content in named_sections)
    section_names = [name for name, _ in named_sections]
    logger.info(
        "Prompt for %s: %d chars, %d sections (%s)",
        role,
        total_chars,
        len(named_sections),
        ", ".join(section_names),
    )

    # Spawn-time prompt budget check (#4377): measure before the adapter is
    # invoked and warn (non-fatally) when the assembled prompt consumes more
    # than the configured fraction of the model's context window.
    try:
        from bernstein.core.agents.spawn_prompt_budget import check_spawn_prompt_budget

        _budget = check_spawn_prompt_budget(
            named_sections,
            model=model,
            session_id=session_id,
        )
        if _budget.over_budget:
            logger.warning(_budget.warning_message)
    except Exception as _budget_exc:
        logger.debug("Spawn prompt budget check failed: %s", _budget_exc)

    # Prompt token-usage breakdown (system prompt %, context %, user prompt %)
    if session_id:
        try:
            from bernstein.core.prompt_token_analysis import (
                analyse_prompt_sections,
                save_prompt_token_report,
            )

            _pt_report = analyse_prompt_sections(named_sections, session_id=session_id)
            # "token" here counts LLM context tokens, not credentials.
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
            logger.debug(
                "Prompt token breakdown for %s: sys=%d%% ctx=%d%% user=%d%% total=%d",
                session_id,
                round(_pt_report.system_prompt_pct),
                round(_pt_report.context_pct),
                round(_pt_report.user_prompt_pct),
                _pt_report.total_tokens,
            )
            save_prompt_token_report(_pt_report, workdir)
            if _pt_report.suggestions:
                for _sug in _pt_report.suggestions:
                    # "token" here is an LLM context-token suggestion, not a credential.
                    # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure  # noqa: E501
                    logger.info("Prompt token suggestion [%s]: %s", session_id, _sug)
        except Exception as _pt_exc:
            # "token" here is LLM context tokens, not credentials.
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
            logger.debug("Prompt token analysis failed: %s", _pt_exc)

    # Apply staged context collapse (T418): truncate → drop sections → strip metadata
    try:
        from bernstein.core.context_collapse import staged_context_collapse

        collapse_result = staged_context_collapse(named_sections)
        compressed = "".join(content for _, content in collapse_result.sections)
        if collapse_result.steps:
            total_freed = sum(s.tokens_freed for s in collapse_result.steps)
            # "token" here counts LLM context tokens, not credentials.
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
            logger.info(
                "Context collapsed: %d → %d tokens (%d freed, %d steps, %s)",
                collapse_result.original_tokens,
                collapse_result.compressed_tokens,
                total_freed,
                len(collapse_result.steps),
                ", ".join(f"{s.stage.value}({s.section_name})" for s in collapse_result.steps[:5]),
            )
        if not collapse_result.within_budget:
            logger.warning(
                "Context collapse still above budget: %d tokens (critical sections exceed budget)",
                collapse_result.compressed_tokens,
            )
        if not compressed:
            raise RuntimeError("Context collapse produced empty prompt")
        return compressed
    except Exception as exc:
        logger.debug(
            "Staged context collapse failed, falling back to PromptCompressor: %s",
            exc,
        )
        # Fallback: legacy lesson truncation + PromptCompressor
        from bernstein.core.context_compression import DEFAULT_CATEGORY_BUDGETS

        lesson_budget = DEFAULT_CATEGORY_BUDGETS.get("lessons", 5_000)
        if lesson_context and (len(lesson_context) // 4) > lesson_budget:
            logger.info("Truncating lessons: exceeded budget of %d tokens", lesson_budget)
            lesson_context = lesson_context[: lesson_budget * 4] + "..."

    # Apply budget-aware prompt compression
    try:
        from bernstein.core.context_compression import PromptCompressor

        compressor = PromptCompressor()
        compressed, original_tokens, compressed_tokens, dropped = compressor.compress_sections(named_sections)
        if dropped:
            reduction_pct = (1.0 - compressed_tokens / max(1, original_tokens)) * 100
            # "token" here counts LLM context tokens, not credentials.
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
            logger.info(
                "Prompt compressed: %d → %d tokens (%.0f%% reduction), dropped: %s",
                original_tokens,
                compressed_tokens,
                reduction_pct,
                dropped,
            )
        return compressed
    except Exception as exc:
        logger.debug("PromptCompressor failed, using uncompressed prompt: %s", exc)
        return "".join(content for _, content in named_sections)


def render_prompt(
    tasks: list[Task],
    templates_dir: Path,
    workdir: Path,
    agency_catalog: dict[str, AgencyAgent] | None = None,
    catalog_system_prompt: str | None = None,
    context_builder: TaskContextBuilder | None = None,
    session_id: str = "",
    bulletin_summary: str = "",
    task_graph: TaskGraph | None = None,
    meta_messages: list[str] | None = None,
    file_ownership: dict[str, str] | None = None,
    max_turns: int | None = None,
    mailbox_section: str = "",
    model: str = "",
) -> str:
    """Public wrapper for compatibility-safe prompt rendering.

    Records the rendered cacheable prefix on the prompt-cache locality
    tracker so that drift between consecutive same-role spawns surfaces
    on ``prompt_cache_drift_total{role,reason}`` and in the in-memory
    snapshot consumed by ``bernstein cache report``.
    """
    rendered = _render_prompt(
        tasks,
        templates_dir,
        workdir,
        agency_catalog=agency_catalog,
        catalog_system_prompt=catalog_system_prompt,
        context_builder=context_builder,
        session_id=session_id,
        bulletin_summary=bulletin_summary,
        task_graph=task_graph,
        meta_messages=meta_messages,
        file_ownership=file_ownership,
        max_turns=max_turns,
        mailbox_section=mailbox_section,
        model=model,
    )
    _observe_cache_locality(rendered, tasks)
    return rendered


def _observe_cache_locality(prompt: str, tasks: list[Task]) -> None:
    """Surface prefix drift on the locality tracker; never raises.

    Splits the rendered prompt at the documented dynamic-section
    boundary (see :func:`bernstein.core.tokens.prompt_caching.
    extract_system_prefix`) and feeds the static prefix into the
    per-role drift tracker.  Failures are swallowed so a tracker bug
    never blocks an agent spawn.
    """
    if not tasks:
        return
    role = (getattr(tasks[0], "role", "") or "unknown").strip().lower()
    try:
        # Local imports keep spawn_prompt.py free of an unconditional
        # dependency on tokens.prompt_caching, which would create an
        # import cycle in some Bernstein test fixtures.
        from bernstein.core.agents.prompt_cache_locality import observe_prefix
        from bernstein.core.tokens.prompt_caching import extract_system_prefix

        prefix, _suffix = extract_system_prefix(prompt)
        observe_prefix(role=role, prefix=prefix)
    except Exception:  # pragma: no cover - defensive, never block spawns
        logger.debug("prompt cache locality observe failed", exc_info=True)


def _render_fallback(
    role: str,
    templates_dir: Path,
    agency_catalog: dict[str, AgencyAgent] | None = None,
) -> str:
    """Fallback: read raw template, check agency catalog, or generate default.

    Args:
        role: Role name.
        templates_dir: Root of templates/roles/ directory.
        agency_catalog: Optional Agency agent catalog to check for roles
            not found in templates/roles/.

    Returns:
        Raw role prompt string without variable substitution.
    """
    template_path = templates_dir / role / "system_prompt.md"
    if template_path.exists():
        return template_path.read_text(encoding="utf-8")

    # Check agency catalog: look for an agent whose name or role matches.
    if agency_catalog:
        agent = agency_catalog.get(role)
        if agent is None:
            # Try matching by mapped role name.
            for a in agency_catalog.values():
                if a.role == role:
                    agent = a
                    break
        if agent and agent.prompt_body:
            logger.info("Using Agency agent '%s' for role '%s'", agent.name, role)
            return agent.prompt_body

    return f"You are a {role} specialist."


# ---------------------------------------------------------------------------
# Cache-safe params builder for forked agents
# ---------------------------------------------------------------------------

#: Stable prompt components that forked agents MUST preserve for cache reuse.
_CACHEABLE_PROTOCOL_PREFIX = (
    "## Agent protocol\n"
    "All agents follow the Bernstein agent protocol:\n"
    "- Work in an isolated git worktree on a branch named ``agent/<session_id>``.\n"
    "- Check signal files (WAKEUP, SHUTDOWN, COMMAND) every 60 seconds.\n"
    "- Report progress via heartbeat and mark tasks complete with `bernstein task complete`.\n"
    "- Never push to main; always create a PR.\n"
)


def build_cache_safe_params(
    role: str,
    templates_dir: Path,
    workdir: Path,
    *,
    task_block: str = "",
    specialist_block: str = "",
    session_id: str = "",
) -> CacheSafeParams:
    """Build cache-safe parameters for a forked agent.

    Computes hashes of stable prompt components so forked agents can
    validate cache key alignment before spawning.

    Stable fields (affect cache key):
    - role: The agent's role name
    - templates_hash: Hash of the templates directory
    - project_context_hash: Hash of .sdd/project.md
    - git_safety_protocol: The git safety rules
    - agent_protocol_prefix: Protocol prefix shared across all agents

    Variable fields (do NOT affect cache key):
    - task_descriptions: Task-specific content
    - specialist_descriptions: Specialist agent descriptions
    - session_id: Unique session identifier

    Args:
        role: Agent role name (e.g. "backend", "qa").
        templates_dir: Path to templates/roles/ directory.
        workdir: Project working directory.
        task_block: Task descriptions block for this fork.
        specialist_block: Available specialist agents descriptions.
        session_id: Agent session ID.

    Returns:
        A frozen ``CacheSafeParams`` with stable hashes computed.
    """
    import hashlib

    # Compute templates_hash: hash all role template files
    template_hash_data = hashlib.sha256()
    if templates_dir.is_dir():
        for tpl_file in sorted(templates_dir.rglob("*.md")):
            template_hash_data.update(tpl_file.read_bytes())
    templates_hash = template_hash_data.hexdigest()

    # Compute project context hash
    project_md = workdir / ".sdd" / "project.md"
    project_context_hash = hashlib.sha256(project_md.read_bytes() if project_md.is_file() else b"").hexdigest()

    return CacheSafeParams(
        role=role,
        templates_hash=templates_hash,
        project_context_hash=project_context_hash,
        git_safety_protocol=_render_git_safety_protocol(),
        agent_protocol_prefix=_CACHEABLE_PROTOCOL_PREFIX,
        task_descriptions=task_block,
        specialist_descriptions=specialist_block,
        session_id=session_id,
    )


# ---------------------------------------------------------------------------
# Meta-messages for orchestrator nudges (T567)
# ---------------------------------------------------------------------------

_META_MESSAGE_HEADER = "<!-- ORCHESTRATOR META -->"
_META_MESSAGE_FOOTER = "<!-- /ORCHESTRATOR META -->"


def build_meta_message(nudge: str, *, phase: str = "", policy: str = "") -> str:
    """Build an orchestrator meta-message envelope (T567).

    Meta-messages are injected into agent context as operational instructions
    (nudges, policy reminders, phase hints) without appearing as user/assistant
    content.  They are wrapped in HTML comment markers so they are invisible
    to the model's conversational context but parseable by tooling.

    Args:
        nudge: The nudge text to inject.
        phase: Optional phase hint (e.g. ``"retry"``, ``"compaction"``).
        policy: Optional policy reminder text.

    Returns:
        Formatted meta-message string.
    """
    parts = [_META_MESSAGE_HEADER]
    if phase:
        parts.append(f"phase: {phase}")
    if policy:
        parts.append(f"policy: {policy}")
    parts.extend((nudge, _META_MESSAGE_FOOTER))
    return "\n".join(parts)


def extract_meta_messages(prompt: str) -> list[str]:
    """Extract all meta-message blocks from a prompt string (T567).

    Args:
        prompt: Full prompt text.

    Returns:
        List of meta-message content strings (without envelope markers).
    """
    import re

    pattern = re.compile(
        re.escape(_META_MESSAGE_HEADER) + r"(.*?)" + re.escape(_META_MESSAGE_FOOTER),
        re.DOTALL,
    )
    return [m.group(1).strip() for m in pattern.finditer(prompt)]


# ---------------------------------------------------------------------------
# Git safety protocol prompts (T582)
# ---------------------------------------------------------------------------

GIT_SAFETY_PROTOCOL = """\
## Git Safety Protocol

You MUST follow these git safety rules at all times:
- Never use `git push --force` or `git push -f` on shared branches.
- Never use `--no-verify` to skip git hooks.
- Never commit secrets, API keys, or credentials.
- Always commit to your assigned worktree branch (`agent/{session_id}`).
- Never modify `.git/hooks` or git configuration files.
- Always verify staged changes with `git diff --cached` before committing.
"""


def inject_git_safety_protocol(prompt: str, session_id: str = "") -> str:
    """Inject the git safety protocol into an agent prompt (T582).

    Args:
        prompt: Base prompt text.
        session_id: Agent session ID for branch name substitution.

    Returns:
        Prompt with git safety protocol appended.
    """
    protocol = GIT_SAFETY_PROTOCOL.replace("{session_id}", session_id or "SESSION_ID")
    return f"{prompt}\n\n{protocol}"


# ---------------------------------------------------------------------------
# Shell command embedding in role templates (T588)
# ---------------------------------------------------------------------------

_SHELL_CMD_PATTERN = _re.compile(r"!`([^`]+)`")


def expand_shell_commands(template: str, *, timeout: int = 5, workdir: Path | None = None) -> str:
    """Expand ``!`command``` syntax in a template string (T588).

    Executes each shell command and replaces the marker with its stdout.
    Commands that fail or time out are replaced with an empty string and
    a warning comment.

    Args:
        template: Template text that may contain ``!`command``` markers.
        timeout: Per-command timeout in seconds.
        workdir: Working directory for command execution.

    Returns:
        Template with all shell command markers replaced.
    """

    def _run_cmd(match: _re.Match[str]) -> str:
        cmd = match.group(1).strip()
        try:
            result = _subprocess.run(
                _shlex.split(cmd),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                cwd=workdir,
            )
            if result.returncode == 0:
                return result.stdout.strip()
            logger.warning("Shell command in template failed (exit %d): %s", result.returncode, cmd)
            return f"<!-- shell command failed: {cmd} -->"
        except _subprocess.TimeoutExpired:
            logger.warning("Shell command in template timed out: %s", cmd)
            return f"<!-- shell command timed out: {cmd} -->"
        except Exception as exc:
            logger.warning("Shell command in template error: %s - %s", cmd, exc)
            return f"<!-- shell command error: {cmd} -->"

    return _SHELL_CMD_PATTERN.sub(_run_cmd, template)


# ---------------------------------------------------------------------------
# Fork subagent with byte-identical prefix for prompt cache sharing
# ---------------------------------------------------------------------------

#: Marker that separates the cacheable prefix from the per-fork directive.
_FORK_DIRECTIVE_MARKER = "\n## Assigned tasks\n"


def fork_from_agent(
    parent_prompt: str,
    directive: str,
    *,
    session_id: str = "",
) -> str:
    """Create a forked agent prompt that shares the parent's cache prefix.

    The parent's system prompt is split at the task assignment boundary.
    Everything before that boundary is preserved byte-for-byte so the
    forked agent achieves a prompt-cache hit on the parent's prefix.
    Only the final directive (task descriptions, instructions) varies.

    Args:
        parent_prompt: The full rendered prompt of the parent agent.
        directive: The new directive text to append after the shared prefix.
            This is typically a review instruction, quality gate check, or
            a different task assignment.
        session_id: Optional session ID to inject into signal-check blocks
            within the directive.

    Returns:
        A new prompt string where the prefix is byte-identical to the
        parent's prefix and only the directive section differs.
    """
    # Find the task assignment marker - everything before it is the
    # cacheable prefix shared with the parent.
    prefix = _extract_cache_prefix(parent_prompt)

    # Build the forked prompt: byte-identical prefix + new directive.
    forked = f"{prefix}\n\n## Fork directive\n{directive}\n"

    # Inject session-specific signal checks if a session_id is provided.
    if session_id:
        forked += _render_signal_check(session_id)

    return forked


_FORK_OUTPUT_MARKER = "\n\n## Fork directive\n"


def _extract_cache_prefix(prompt: str) -> str:
    """Extract the cacheable prefix from a prompt.

    Splits at the task assignment marker or fork directive marker,
    whichever comes first, so both parent and forked prompts yield
    the same prefix.
    """
    # Try both markers and use whichever appears first.
    positions = []
    task_pos = prompt.find(_FORK_DIRECTIVE_MARKER)
    if task_pos >= 0:
        positions.append(task_pos)
    fork_pos = prompt.find(_FORK_OUTPUT_MARKER)
    if fork_pos >= 0:
        positions.append(fork_pos)

    if positions:
        return prompt[: min(positions)]
    return prompt


def fork_cache_key(prompt: str) -> str:
    """Compute the cache key for the cacheable prefix of a prompt.

    Use this to verify that a forked prompt will achieve a cache hit
    against the parent: both should return the same key.

    Args:
        prompt: The full rendered prompt (parent or forked).

    Returns:
        SHA-256 hex digest of the cacheable prefix.
    """
    import hashlib

    prefix = _extract_cache_prefix(prompt)
    return hashlib.sha256(prefix.encode("utf-8")).hexdigest()


def render_spawn_prompt(
    session_id: str,
    task: Any,
    workdir: Path,
    agent_type: str = "claude",
) -> str:
    """Render the task-content block of a spawn prompt.

    Returns a string containing the session header, task fields, and git
    safety protocol - the three sections most susceptible to prompt injection.
    Task content is indented with four spaces so that any injected directives
    (e.g. ``System:``) appear as body text rather than structural headers.
    Intended for use by the pen-test suite to verify injection resistance.

    Args:
        session_id: The agent session identifier.
        task: A task-like object with ``id``, ``role``, ``title``, and
            ``description`` attributes.
        workdir: The working directory for the agent session.
        agent_type: The CLI agent type (e.g. "claude", "codex").

    Returns:
        A prompt string with task content safely embedded.
    """

    # Indent all user-supplied content by 4 spaces so structural directives
    # (e.g. "System:") cannot be injected as top-level prompt elements.
    def _indent(text: str) -> str:
        return "\n".join(f"    {line}" for line in text.splitlines())

    lines: list[str] = [
        f"## Session: {session_id}",
        f"Agent type: {agent_type}",
        f"Workdir: {workdir}",
        "",
        "## Task",
        f"ID: {getattr(task, 'id', '')}",
        f"Role: {getattr(task, 'role', '')}",
        "",
        "### Title",
        _indent(getattr(task, "title", "")),
        "",
        "### Description",
        _indent(getattr(task, "description", "")),
        "",
        _render_git_safety_protocol(),
    ]
    return "\n".join(lines)


def build_git_safety_protocol() -> str:
    """Return the git safety protocol block.

    Public alias for :func:`_render_git_safety_protocol`, used by the
    pen-test suite to verify that calling this function does not trigger
    any subprocess calls.

    Returns:
        Markdown block with git safety rules.
    """
    return _render_git_safety_protocol()
