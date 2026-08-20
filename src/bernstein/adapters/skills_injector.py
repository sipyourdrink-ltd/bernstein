"""Inject per-task Claude Code skills into the worktree before spawn.

Claude Code's skill system (``.claude/skills/*.md``) provides context-triggered
capabilities.  Skills have frontmatter declaring when they should activate and
markdown content with instructions.  The model loads relevant skills on-demand
based on conversation context, and re-injects them after context compaction.

Bernstein writes role-specific skills into the worktree's ``.claude/skills/``
directory before spawning an agent so that:

- Orchestration protocols (completion, signal-check) survive context compaction
- Prompt size is reduced by 30-40% - boilerplate moves to skills loaded only
  when relevant
- Skills compose cleanly: a backend agent automatically gets the test-runner
  skill, a commit skill, and orchestration protocol skills

Template substitution uses simple ``{{PLACEHOLDER}}`` tokens (no Jinja2
dependency) so skills can be rendered without external libraries.
"""

from __future__ import annotations

import hashlib
import logging
import shlex
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypedDict, cast

import yaml

from bernstein.core.security.path_containment import (
    PathContainmentError,
    contained_subpath,
)
from bernstein.core.skills.activation_log import (
    ActivationRecord,
    log_activation,
)
from bernstein.core.skills.routing import auto_route_enabled, select_auto_route_templates
from bernstein.core.skills.sanitizer import sanitize_skill_body
from bernstein.core.skills.selection_rules import (
    SELECTION_RULES_FILENAME,
    load_selection_rules,
    resolve_rule_templates,
)

if TYPE_CHECKING:
    from bernstein.core.skills.routing import RoutableTask

    class Task(RoutableTask, Protocol):
        """Task fields used by the skill injector."""

        id: str


class _FrontmatterSchema(TypedDict, total=False):
    """Subset of Claude-Code skill frontmatter the injector reads.

    The loose-YAML parse may surface extra keys; only the ones the
    activation log needs are typed here.
    """

    version: str


_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Role → skill template mapping
# Always-injected (every role): completion protocol + signal check
# Role-specific: test runner for backend/qa, commit protocol for backend/docs
# ---------------------------------------------------------------------------

#: Skills always injected regardless of role.
_ALWAYS_INJECT: list[str] = [
    "bernstein-completion-protocol.md",
    "bernstein-signal-check.md",
]

#: Additional skills injected per role.
ROLE_SKILL_MAP: dict[str, list[str]] = {
    "backend": [
        "bernstein-test-runner.md",
        "bernstein-commit-protocol.md",
    ],
    "qa": [
        "bernstein-test-runner.md",
    ],
    "docs": [
        "bernstein-commit-protocol.md",
    ],
    "security": [],
}


def _resolve_git_exclude_path(workdir: Path) -> Path | None:
    """Resolve the ``info/exclude`` file for ``workdir``'s git repo.

    Uses ``git rev-parse --git-path info/exclude`` rather than assuming
    ``workdir/.git/info/exclude`` exists, because inside a ``git worktree``
    ``.git`` is a *file* containing a ``gitdir: <path>`` pointer to the real
    gitdir (typically under the main checkout's ``.git/worktrees/<name>/``),
    not a directory. ``git rev-parse --git-path`` resolves this correctly in
    both the plain-repo and worktree cases.

    Returns ``None`` (and logs at debug level) if ``workdir`` is not inside a
    git repository or the ``git`` binary is unavailable - exclusion is a
    best-effort hardening measure and must never block agent spawn.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-path", "info/exclude"],
            cwd=workdir,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _logger.debug("Could not resolve git info/exclude for %s: %s", workdir, exc)
        return None

    exclude_relpath = result.stdout.strip()
    if not exclude_relpath:
        _logger.debug("git rev-parse --git-path info/exclude returned empty for %s", workdir)
        return None

    exclude_path = Path(exclude_relpath)
    if not exclude_path.is_absolute():
        exclude_path = workdir / exclude_path
    return exclude_path


def _exclude_injected_paths(workdir: Path, relative_paths: list[str]) -> None:
    """Idempotently append ``relative_paths`` to ``workdir``'s ``info/exclude``.

    Injected skill files must remain readable by Claude Code (which reads
    the working tree directly) but must never be staged or committed by an
    agent's broad ``git add`` - two agents' worktrees render different
    session-specific content into the same filenames, so committing them
    causes a merge conflict on every worker merge back to the shared work
    branch. Writing to ``info/exclude`` (rather than ``.gitignore``) keeps
    this local-only and out of the tracked tree entirely.
    """
    exclude_path = _resolve_git_exclude_path(workdir)
    if exclude_path is None:
        _logger.debug(
            "Skipping git exclude registration for %s - not a git repo or git unavailable",
            workdir,
        )
        return

    try:
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    except OSError as exc:
        _logger.debug("Failed to read git exclude file %s: %s", exclude_path, exc)
        return

    existing_lines = set(existing.splitlines())
    new_lines = [path for path in relative_paths if path not in existing_lines]
    if not new_lines:
        _logger.debug("All injected paths already excluded in %s", exclude_path)
        return

    try:
        with exclude_path.open("a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            for path in new_lines:
                fh.write(f"{path}\n")
                _logger.debug("Excluded injected skill from git: %s -> %s", path, exclude_path)
    except OSError as exc:
        _logger.debug("Failed to append to git exclude file %s: %s", exclude_path, exc)
        return

    _logger.debug(
        "Registered %d injected skill path(s) in git exclude file %s",
        len(new_lines),
        exclude_path,
    )


def render_skill_template(
    content: str,
    *,
    session_id: str = "",
    tasks: list[Task] | None = None,
) -> str:
    """Render a skill template by substituting ``{{PLACEHOLDER}}`` tokens.

    Supported placeholders:

    - ``{{SESSION_ID}}``: agent session identifier
    - ``{{COMPLETE_CMDS}}``: ``bernstein task complete`` commands to mark all
      tasks complete
    - ``{{TASK_IDS}}``: space-separated task ID list

    Args:
        content: Raw skill template content.
        session_id: Agent session identifier.
        tasks: Tasks assigned to this agent.  Used to generate completion commands.

    Returns:
        Rendered skill content with placeholders substituted.
    """
    task_list = tasks or []

    # Build per-task completion commands using the first-class `bernstein
    # task complete` CLI front door (issue #3035). It resolves the
    # task-server URL and the session's auth token itself at runtime -
    # `server_post` -> `auth_headers()` / `resolve_server_url()` - unlike a
    # raw curl POST, which carried no Authorization header (a 401 under the
    # default auth-enabled config) and hardcoded `http://127.0.0.1:8052`
    # (wrong on any node that isn't the task server itself).
    complete_cmds_parts: list[str] = []
    for task in task_list:
        summary = shlex.quote(f"Completed: {task.title}")
        cmd = f"```bash\nbernstein task complete {shlex.quote(task.id)} --summary {summary}\n```"
        complete_cmds_parts.append(cmd)
    complete_cmds = (
        "\n\n".join(complete_cmds_parts)
        if complete_cmds_parts
        else ("```bash\n# No task IDs available - check with the orchestrator\n```")
    )

    task_ids = " ".join(t.id for t in task_list)

    result = content
    result = result.replace("{{SESSION_ID}}", session_id)
    result = result.replace("{{COMPLETE_CMDS}}", complete_cmds)
    result = result.replace("{{TASK_IDS}}", task_ids)
    return result


def inject_skills(
    workdir: Path,
    role: str,
    tasks: list[Task],
    session_id: str,
    templates_dir: Path,
) -> list[dict[str, str]]:
    """Write role-specific Claude Code skills into the worktree.

    Copies skills from ``templates/skills/`` to ``workdir/.claude/skills/``,
    rendering ``{{PLACEHOLDER}}`` tokens with task-specific data.

    Always injects orchestration protocol skills (completion, signal-check).
    Additional skills are injected based on the role via :data:`ROLE_SKILL_MAP`.

    Args:
        workdir: Working directory for the agent (worktree root).
        role: Agent role (e.g. ``"backend"``, ``"qa"``, ``"security"``).
        tasks: Tasks assigned to the agent.
        session_id: Agent session identifier, embedded in signal-check paths.
        templates_dir: Path to ``templates/roles/`` directory.  Skills are
            resolved from the sibling ``../skills/`` directory.
    """
    skills_source_dir = templates_dir.parent / "skills"
    if not skills_source_dir.is_dir():
        _logger.debug(
            "Skills templates directory not found: %s - skipping injection",
            skills_source_dir,
        )
        return []

    skills_dest_dir = workdir / ".claude" / "skills"
    skills_dest_dir.mkdir(parents=True, exist_ok=True)

    templates_to_inject = list(dict.fromkeys(_ALWAYS_INJECT + ROLE_SKILL_MAP.get(role, [])))
    trigger_by_template = {template_name: "role-binding" for template_name in templates_to_inject}

    # Declarative selection rules (issue #3383): a corpus-immune rule layer
    # between role binding and the opt-in TF-IDF auto-route. Existence is a
    # single cheap stat - when the table is absent the loader is never
    # invoked and behaviour is byte-identical to a rule-less install.
    if (skills_source_dir / SELECTION_RULES_FILENAME).is_file():
        rules = load_selection_rules(skills_source_dir)
        for rule_template in resolve_rule_templates(rules, tasks):
            if rule_template in trigger_by_template:
                # Role binding wins for templates selected by both layers.
                continue
            templates_to_inject.append(rule_template)
            trigger_by_template[rule_template] = "rule"

    if auto_route_enabled():
        for candidate in select_auto_route_templates(
            skills_source_dir,
            tasks,
            excluded_templates=templates_to_inject,
        ):
            templates_to_inject.append(candidate.template_name)
            trigger_by_template[candidate.template_name] = "auto-route"

    # Catalog-installed skill packs (.bernstein/skills/<name>/). The lockfile,
    # not a directory listing, decides which skills are installed here: workdir
    # is the per-task worktree, so its contents are branch contents, and a file
    # dropped under .bernstein/skills/ without an install would otherwise be
    # rendered in front of the agent. It is also the anchor the revocation gate
    # below iterates, so an id the lockfile never names is one that gate could
    # never match.
    catalog_skills_dir = workdir / ".bernstein" / "skills"
    catalog_skill_ids = _installed_catalog_skill_ids(workdir)
    for skill_id in catalog_skill_ids:
        template_name = f"{skill_id}.md"
        if template_name in trigger_by_template:
            continue
        if _resolve_catalog_skill(catalog_skills_dir, skill_id) is None:
            continue
        templates_to_inject.append(template_name)
        trigger_by_template[template_name] = "catalog"

    # Kill switch: refuse to inject any skill a signed revocation covers, and
    # record a chain-anchored refusal receipt for every revoked install
    # (issue #2527). Best-effort: a bad catalog cache must never wedge a spawn.
    revoked_ids = _revoked_skill_ids(workdir)

    written_relpaths: list[str] = []
    audit_records: list[dict[str, str]] = []
    for template_name in templates_to_inject:
        if template_name.rsplit(".", 1)[0] in revoked_ids:
            _logger.warning("Refusing to inject revoked skill %s (signed revocation)", template_name)
            audit_records.append(
                {
                    "template_name": template_name,
                    "version": "",
                    "pre_render_digest": "",
                    "rendered_digest": "",
                    "trigger_source": trigger_by_template.get(template_name, "unknown"),
                    "source": "injected",
                    "status": "refused",
                }
            )
            continue

        # Bundled role templates in templates/skills/ take precedence over catalog-installed skills
        source_path = skills_source_dir / template_name
        source_name = "templates/skills"
        if not source_path.exists():
            skill_stem = template_name.rsplit(".", 1)[0]
            if skill_stem in catalog_skill_ids:
                catalog_path = _resolve_catalog_skill(catalog_skills_dir, skill_stem)
                if catalog_path is not None:
                    source_path = catalog_path
                    source_name = ".bernstein/skills"

        if not source_path.exists():
            _logger.debug("Skill template not found: %s - skipping", source_path)
            audit_records.append(
                {
                    "template_name": template_name,
                    "version": "",
                    "pre_render_digest": "",
                    "rendered_digest": "",
                    "trigger_source": trigger_by_template.get(template_name, "unknown"),
                    "source": "injected",
                    "status": "missing",
                }
            )
            continue

        try:
            raw = source_path.read_text(encoding="utf-8")
        except OSError as exc:
            _logger.debug("Failed to read skill template %s: %s", source_path, exc)
            audit_records.append(
                {
                    "template_name": template_name,
                    "version": "",
                    "pre_render_digest": "",
                    "rendered_digest": "",
                    "trigger_source": trigger_by_template.get(template_name, "unknown"),
                    "source": "injected",
                    "status": "read_failed",
                }
            )
            continue

        # Strip invisible Unicode Tag codepoints (U+E0000-U+E007F, Cf, U+FFF9-
        # U+FFFB) before render-and-write so a poisoned third-party template
        # cannot smuggle hidden instructions into ``.claude/skills/*.md``. The
        # sanitizer is on by default; the hidden ``--unsafe-allow-unicode-tags``
        # CLI flag disables it for incident-reproduction scenarios.
        sanitized = sanitize_skill_body(
            raw,
            skill_name=template_name,
            origin=str(source_path),
            source_name=source_name,
        )

        pre_render_digest: str = hashlib.blake2b(sanitized.encode("utf-8")).hexdigest()
        rendered: str = render_skill_template(sanitized, session_id=session_id, tasks=tasks)
        rendered_digest: str = hashlib.blake2b(rendered.encode("utf-8")).hexdigest()

        dest_path = skills_dest_dir / template_name
        try:
            dest_path.write_text(rendered, encoding="utf-8")
            _logger.debug("Injected skill: %s -> %s", template_name, dest_path)
            written_relpaths.append(str(dest_path.relative_to(workdir)))
        except OSError as exc:
            _logger.debug("Failed to write skill %s: %s", dest_path, exc)
            audit_records.append(
                {
                    "template_name": template_name,
                    "version": "",
                    "pre_render_digest": pre_render_digest,
                    "rendered_digest": rendered_digest,
                    "trigger_source": trigger_by_template.get(template_name, "unknown"),
                    "source": "injected",
                    "status": "write_failed",
                }
            )
            continue

        # Activation log: best-effort, opt-out via env var. We compute a
        # short BLAKE2b digest over the sanitised (pre-render) body so
        # the log line refers to the source skill rather than the
        # rendered-with-task-ids variant. ``version`` is best-effort
        # pulled from frontmatter; missing values stay as empty strings.
        skill_name = template_name.rsplit(".", 1)[0]
        try:
            version = _extract_skill_version(sanitized)
        except Exception:
            _logger.debug("Failed to extract skill version for %s", template_name, exc_info=True)
            version = ""
        audit_records.append(
            {
                "template_name": template_name,
                "version": version,
                "pre_render_digest": pre_render_digest,
                "rendered_digest": rendered_digest,
                "trigger_source": trigger_by_template.get(template_name, "unknown"),
                "status": "injected",
                "source": "injected",
            }
        )
        try:
            digest: str = hashlib.blake2b(sanitized.encode("utf-8"), digest_size=16).hexdigest()
        except Exception:
            _logger.debug("Failed to compute activation log digest for %s", template_name, exc_info=True)
            digest = ""
        try:
            for task in tasks:
                log_activation(
                    ActivationRecord(
                        skill=skill_name,
                        role=role,
                        task_id=task.id,
                        trigger_source=trigger_by_template[template_name],
                        version=version,
                        digest=digest,
                    ),
                    workdir=workdir,
                )
            if not tasks:
                log_activation(
                    ActivationRecord(
                        skill=skill_name,
                        role=role,
                        task_id="",
                        trigger_source=trigger_by_template[template_name],
                        version=version,
                        digest=digest,
                    ),
                    workdir=workdir,
                )
        except Exception:
            # Never let the activation log block a spawn.
            _logger.debug("activation log append failed for %s", template_name, exc_info=True)

    if written_relpaths:
        _exclude_injected_paths(workdir, written_relpaths)
    return audit_records


def _installed_catalog_skill_ids(workdir: Path) -> list[str]:
    """Return catalog-installed skill ids for *workdir*, in lockfile order.

    The lockfile is the authority on what is installed, which matters because
    *workdir* is the per-task worktree and therefore holds branch-controlled
    files. It is also the same set :func:`_revoked_skill_ids` iterates, so
    reading from it keeps injection and revocation talking about the same
    skills. Best-effort, like the revocation path: any failure resolves to an
    empty list so a spawn is never wedged by a missing or unreadable lockfile.
    """
    try:
        from bernstein.core.skills.catalog.enforcement import installed_catalog_skills

        return list(dict.fromkeys(skill_id for skill_id, _version in installed_catalog_skills(workdir)))
    except Exception:
        _logger.debug("catalog lockfile unreadable under %s", workdir, exc_info=True)
        return []


def _resolve_catalog_skill(catalog_skills_dir: Path, skill_id: str) -> Path | None:
    """Return the file backing catalog skill *skill_id*, or ``None``.

    Every candidate is joined through the containment barrier rather than
    composed by hand, so an ordinary-looking child that is itself a symlink out
    of the worktree is refused before anything reads it. The returned path is
    the resolved one, which is the only value proven to be inside the base.

    Args:
        catalog_skills_dir: The ``.bernstein/skills`` directory to resolve under.
        skill_id: Skill id as recorded in the catalog lockfile.

    Returns:
        The containment-checked path to the skill body, or ``None`` when no
        candidate exists or every candidate escapes the base.
    """
    for candidate in (f"{skill_id}.md", f"{skill_id}/SKILL.md", f"{skill_id}/{skill_id}.md"):
        try:
            resolved = contained_subpath(catalog_skills_dir, candidate, label="catalog skill")
        except PathContainmentError:
            _logger.warning("Refusing catalog skill outside %s: %s", catalog_skills_dir, candidate)
            continue
        if resolved.is_file():
            return resolved
    return None


def _revoked_skill_ids(workdir: Path) -> set[str]:
    """Return the set of catalog-installed skill ids under a signed revocation.

    Best-effort and side-effecting: it records a chain-anchored spawn-side
    refusal receipt for every revoked install. Any failure (no catalog cache,
    unreadable lockfile) resolves to an empty set so a spawn is never blocked
    by the enforcement path itself.
    """
    try:
        from bernstein.core.skills.catalog.enforcement import enforce_spawn_revocations

        refused = enforce_spawn_revocations(workdir)
        return {item.skill_id for item in refused}
    except Exception:
        _logger.debug("revocation enforcement skipped for %s", workdir, exc_info=True)
        return set()


def _extract_skill_version(content: str) -> str:
    """Pull ``version`` from the YAML frontmatter, defaulting to empty.

    The injector handles Claude-Code-shaped skills whose frontmatter may
    not match :class:`SkillManifest` strictly; we do a loose YAML parse
    rather than running it through Pydantic so non-strict templates
    still produce an activation record.
    """
    if not content.startswith("---"):
        return ""
    lines = content.splitlines()
    fence_count = 0
    front_lines: list[str] = []
    for line in lines:
        if line.rstrip() == "---":
            fence_count += 1
            if fence_count == 2:
                break
            continue
        if fence_count == 1:
            front_lines.append(line)
    if fence_count < 2:
        return ""
    try:
        data = yaml.safe_load("\n".join(front_lines))
    except yaml.YAMLError:
        return ""
    if isinstance(data, dict):
        typed = cast(_FrontmatterSchema, data)
        version = typed.get("version")
        if isinstance(version, str):
            return version
    return ""
