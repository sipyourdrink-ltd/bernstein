"""Canonical AGENTS.md generator - single source of truth for agent context.

Teams running more than one CLI coding agent (Cursor + Claude Code + Codex
is the common 2026 stack) end up maintaining three to five near-identical
context files (``AGENTS.md``, ``CLAUDE.md``, ``.cursor/rules/*.mdc``,
``CONVENTIONS.md`` + ``.aider.conf.yml``, ``.goosehints``) that drift over
weeks. The test command in one file diverges from another, conventions
disagree, and nobody notices until an agent picks the stale one.

This module derives a *canonical* list of :class:`AgentsMdSection` records
from the repository - module docstrings, git history, ``pyproject.toml``,
shipped role templates, optionally curated overlays under
``.sdd/agents-md/`` - and renders the full ``AGENTS.md``. The companion
:mod:`bernstein.core.knowledge.agents_md_bridge` then translates that
canonical IR into the Cursor / Claude Code / Aider / Goose target formats
without inventing new conventions.

Architecture
------------

1. :func:`generate` walks the repo and returns
   ``list[AgentsMdSection]`` ordered for canonical render.
2. Each section is built by a *pure* ``_build_*`` function that takes the
   repo path and reads the primitives it needs. Pure means: no side
   effects, deterministic, side-input goes through the parameter list.
3. :func:`render_canonical` joins the sections with stable spacing and
   the AAIF / agents.md plain-markdown convention (no frontmatter).
4. The ``conventions`` and ``custom`` sections read overlay files from
   ``.sdd/agents-md/`` when present so curated prose can live next to
   the auto-derived sections in one source of truth.

The generator is deliberately *not* an opinionated framework - it follows
the canonical https://agents.md/ guidance ("AGENTS.md is just standard
Markdown") and the community-converged section set surfaced in the GitHub
``how-to-write-a-great-agents-md`` post analysis of 2,500+ repos.

References
----------

* AGENTS.md canonical site - https://agents.md/
* AAIF AGENTS.md project page - https://aaif.io/projects/agents-md/
* Agentic AI readthedocs mirror - https://agentic-ai.readthedocs.io/en/latest/Standards/agents-md/
* GitHub blog write-up - https://github.blog/ai-and-ml/github-copilot/how-to-write-a-great-agents-md-lessons-from-over-2500-repositories/
* OpenAI Codex AGENTS.md guide - https://developers.openai.com/codex/guides/agents-md
"""

from __future__ import annotations

import ast
import logging
import re
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public dataclass - canonical IR for one AGENTS.md section
# ---------------------------------------------------------------------------


SectionKind = Literal[
    "overview",
    "module-map",
    "build-test",
    "setup",
    "architecture",
    "conventions",
    "git-workflow",
    "roles",
    "custom",
]
"""The closed set of canonical section kinds.

``custom`` covers plugin-contributed and ``.sdd/agents-md/*.md`` overlay
sections. Any other ``kind`` value is a programming error.
"""


@dataclass(frozen=True)
class AgentsMdSection:
    """One canonical section of the agent-context document.

    Sections are deliberately small structurally - just an identifier, a
    heading, and a markdown body. The complexity of "what to write" lives
    in the section builders below, not in the dataclass.

    Attributes:
        key: Stable identifier suitable for filenames and frontmatter
            (e.g. ``module-map``). Lowercase kebab-case.
        title: Display heading rendered as ``## {title}`` in canonical output.
        body: Markdown body. May contain code fences, tables, paragraphs.
            Trailing newline is normalised by the renderer.
        kind: Closed-set classifier used by the bridge to pick the right
            translation strategy per target.
        target_globs: For path-scoped sections (e.g. ``src/bernstein/cli/``
            conventions), the globs that select files where the section
            applies. Used by Cursor MDC ``globs:`` and Claude Code rules.
            Empty tuple = applies project-wide.
        always_apply: Cursor-specific frontmatter hint. ``True`` → injected
            every session. ``False`` → only auto-attached when ``target_globs``
            match. Ignored by other targets.
    """

    key: str
    title: str
    body: str
    kind: SectionKind
    target_globs: tuple[str, ...] = ()
    always_apply: bool = True


# ---------------------------------------------------------------------------
# Configuration - known packages + ordering hints (mirrors gen_agents_md.py)
# ---------------------------------------------------------------------------

_INIT_PY = "__init__.py"

PACKAGE_META: dict[str, str] = {
    "core": "orchestration engine",
    "adapters": "CLI agent adapters",
    "agents": "agent catalog & discovery",
    "cli": "Click CLI",
    "evolution": "self-evolution engine",
    "eval": "evaluation harness",
    "plugins": "plugin system (pluggy)",
    "tui": "Textual TUI",
    "github_app": "GitHub App integration",
    "mcp": "MCP server",
    "benchmark": "SWE-bench",
}

# Packages whose ``__init__.py`` is a re-export shim and brings no
# additional information to the table.
_SKIP_INIT: frozenset[str] = frozenset(PACKAGE_META.keys())

_SKIP_FILES: frozenset[str] = frozenset({"__pycache__", "__main__.py"})

# Curated ordering for ``core/`` so the most-load-bearing modules appear
# first regardless of alphabetical order.
_CORE_PINNED_ORDER: tuple[str, ...] = (
    "models.py",
    "server.py",
    "orchestrator.py",
    "tick_pipeline.py",
    "task_lifecycle.py",
    "agent_lifecycle.py",
    "spawner.py",
    "router.py",
    "janitor.py",
    "context.py",
)

# Files that conceptually belong to one row even though they live in
# separate modules (e.g. backend-split storage).
_MULTI_FILE_ROWS: dict[str, tuple[str, ...]] = {
    "store.py": ("store.py", "store_redis.py", "store_postgres.py"),
}
_SKIP_IN_MULTI: frozenset[str] = frozenset({"store_redis.py", "store_postgres.py"})

# Documented non-package directories. Hand-curated; no auto-derivation.
_NON_PACKAGE_DIRS: tuple[tuple[str, str], ...] = (
    ("templates/roles/", "Jinja2 role prompts (manager, backend, qa, security, devops, etc.)"),
    ("templates/prompts/", "Prompt templates (judge.md, etc.) - bundled into wheel"),
    (".sdd/", "All runtime state (never commit `.sdd/runtime/`)"),
    (".sdd/backlog/open/", "YAML task specs waiting to be picked up"),
    (".sdd/backlog/claimed/", "Tasks currently being worked"),
    (".sdd/backlog/closed/", "Completed/cancelled tasks"),
    (".sdd/runtime/", "PIDs, logs, session state, signal files"),
    (".sdd/metrics/", "JSONL metric records"),
    (".sdd/traces/", "JSONL agent traces"),
    (".sdd/agents/catalog.json", "Registered agent catalog"),
    ("tests/unit/", "Fast unit tests (no network)"),
    ("tests/integration/", "Integration tests (require running server)"),
    ("scripts/run_tests.py", "Per-file isolated test runner"),
)

# Overlay directory for curated section content the user wants to keep
# under version control next to the auto-derived data.
_OVERLAY_DIR = ".sdd/agents-md"


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GenerateOptions:
    """Knobs that control what :func:`generate` produces.

    Attributes:
        include_module_map: When ``False``, skip the deterministic module-map
            section. Useful for repos that don't have a Python ``src/``
            layout where the table would mislead.
        include_git_workflow: When ``False``, skip git-derived workflow data.
            Useful when running outside a git working tree.
        max_module_map_lines: Soft cap on rows per package table; when a
            package would render more rows than this it is summarised.
            ``0`` disables the cap.
        overlay_dir: Path (relative to repo root) where curated overlay
            files live. Each ``.md`` in this directory becomes a section.
    """

    include_module_map: bool = True
    include_git_workflow: bool = True
    max_module_map_lines: int = 0
    overlay_dir: str = _OVERLAY_DIR


def generate(repo_path: Path, options: GenerateOptions | None = None) -> list[AgentsMdSection]:
    """Walk ``repo_path`` and produce the canonical ordered section list.

    The order is fixed: overview → module-map → build-test → setup →
    architecture → conventions → git-workflow → roles → custom overlays.
    A section that has no content (e.g. no ``pyproject.toml`` for
    build-test) is *omitted* rather than emitted empty - readers expect
    every section heading to carry information.

    Args:
        repo_path: Repository root. Must exist; need not be a git checkout
            unless ``options.include_git_workflow`` is ``True``.
        options: See :class:`GenerateOptions`. Defaults are sensible for a
            Python project with a ``src/bernstein/`` layout.

    Returns:
        Ordered list of :class:`AgentsMdSection`. Empty when ``repo_path``
        is missing.
    """
    if not repo_path.exists():
        return []
    opts = options or GenerateOptions()

    builders: list[tuple[str, object]] = [
        ("overview", _build_overview(repo_path)),
        ("module-map", _build_module_map(repo_path, opts) if opts.include_module_map else None),
        ("build-test", _build_build_test(repo_path)),
        ("setup", _build_setup(repo_path)),
        ("architecture", _build_architecture(repo_path)),
        ("conventions", _build_conventions(repo_path, opts)),
        ("git-workflow", _build_git_workflow(repo_path) if opts.include_git_workflow else None),
        ("roles", _build_roles(repo_path)),
        ("documentation-duty", _build_documentation_duty()),
    ]

    sections = [sec for _key, sec in builders if sec is not None]

    sections.extend(_build_overlay_sections(repo_path, opts))
    return sections


# ---------------------------------------------------------------------------
# Canonical render
# ---------------------------------------------------------------------------


_CANONICAL_PREAMBLE = (
    "<!-- AUTO-GENERATED by `bernstein agents-md sync` - DO NOT edit by hand. "
    "Curated content lives under `.sdd/agents-md/`. -->\n"
)


def render_canonical(sections: list[AgentsMdSection], *, repo_name: str | None = None) -> str:
    """Render the full canonical AGENTS.md.

    Pure markdown, no frontmatter, no Bernstein-specific schema header.
    Mirrors the canonical https://agents.md/ guidance.

    Args:
        sections: Ordered list as returned by :func:`generate`.
        repo_name: Optional name to use in the H1. When ``None``, uses
            "Project" as a neutral placeholder.

    Returns:
        Complete markdown text ending in a single trailing newline.
    """
    name = repo_name or "Project"
    parts: list[str] = [f"# {name} - AGENTS.md\n", _CANONICAL_PREAMBLE]
    for sec in sections:
        parts.append(f"\n## {sec.title}\n\n{sec.body.rstrip()}\n")
    text = "".join(parts).rstrip() + "\n"
    return text


# ---------------------------------------------------------------------------
# Section builders - each is pure, returns AgentsMdSection | None
# ---------------------------------------------------------------------------


def _build_overview(repo_path: Path) -> AgentsMdSection | None:
    """Pull the first paragraph of README.{md,rst,txt} (or ``.sdd/project.md``)."""
    candidates = [
        repo_path / "README.md",
        repo_path / "README.rst",
        repo_path / "README.txt",
        repo_path / ".sdd" / "project.md",
    ]
    for p in candidates:
        if not p.is_file():
            continue
        body = _first_paragraph(p) or _first_n_lines(p, 8)
        if body:
            return AgentsMdSection(
                key="overview",
                title="Overview",
                body=body,
                kind="overview",
                always_apply=True,
            )
    return None


def _build_module_map(repo_path: Path, opts: GenerateOptions) -> AgentsMdSection | None:
    """Port of ``scripts/gen_agents_md.py`` - Python-package docstring table.

    Returns ``None`` when ``src/bernstein/`` (or equivalent) is absent.
    """
    src_root = repo_path / "src" / "bernstein"
    if not src_root.is_dir():
        return None

    blocks: list[str] = []
    for pkg, meta in PACKAGE_META.items():
        pkg_dir = src_root / pkg
        if not pkg_dir.is_dir():
            continue
        rows = _collect_package_rows(pkg_dir, pkg)
        if not rows:
            continue
        if opts.max_module_map_lines > 0 and len(rows) > opts.max_module_map_lines:
            extra = len(rows) - opts.max_module_map_lines
            rows = rows[: opts.max_module_map_lines]
            rows.append((f"_… +{extra} more_", "_truncated_"))
        blocks.append(f"### `src/bernstein/{pkg}/` - {meta}\n\n{_render_two_column_table(rows, 'File')}")

    if not blocks:
        return None

    blocks.append(
        "### Key non-package directories\n\n"
        + _render_two_column_table([(f"`{p}`", purpose) for p, purpose in _NON_PACKAGE_DIRS], "Path")
    )
    body = "\n\n".join(blocks)
    return AgentsMdSection(
        key="module-map",
        title="Module map",
        body=body,
        kind="module-map",
        always_apply=False,
        target_globs=("src/**", "tests/**"),
    )


def _build_build_test(repo_path: Path) -> AgentsMdSection | None:
    """Distil run-and-test commands from pyproject.toml, Makefile, package.json.

    The output is intentionally thin: a fenced code block with the most
    likely *one* command for each role (install / test / lint / type-check
    / build), or several when the project clearly distinguishes (e.g. uv
    plus pip fallbacks). Avoids dumping every script entry.

    Two derivations are load-bearing and keyed off files in the tree rather
    than a static guess, so the rendered command never drifts from how the
    repo is actually built:

    * **uv detection.** A ``uv.lock`` at the root means the project is
      uv-managed even when ``pyproject.toml`` carries no ``[tool.uv]``
      table (hatchling-built repos are the common case). Prior versions
      only inspected the pyproject text and silently fell back to
      ``pip install`` / bare ``pytest`` for these repos, contradicting the
      ``uv run`` lint/type-check lines emitted right below.
    * **Isolated test runner.** When the repo ships ``scripts/run_tests.py``
      that command is emitted instead of a bare ``pytest``. A bare
      ``pytest`` over the whole suite retains every collected test object
      for the session, which on a large suite grows into tens of GB; the
      per-file runner (the command CI shards actually invoke) caps memory
      at one file's worth. Emitting the safe command here keeps the
      contributor-facing guidance consistent with the role prompts under
      ``templates/roles/`` and with CI.
    """
    pairs: list[tuple[str, str]] = []

    pyproj = repo_path / "pyproject.toml"
    if pyproj.is_file():
        text = pyproj.read_text(encoding="utf-8", errors="replace")
        uses_uv = (repo_path / "uv.lock").is_file() or "[tool.uv]" in text or "uv" in text.split("\n", 1)[0].lower()
        run = "uv run " if uses_uv else ""
        pairs.append(("uv sync", "install + lock") if uses_uv else ("pip install -e .[dev]", "install"))
        if (repo_path / "scripts" / "run_tests.py").is_file():
            pairs.append((f"{run}python scripts/run_tests.py", "tests (isolated per-file runner)"))
        else:
            pairs.append((f"{run}pytest", "tests"))
        if "ruff" in text:
            pairs.extend(((f"{run}ruff check .", "lint"), (f"{run}ruff format .", "format")))
        if "mypy" in text or "pyright" in text:
            pairs.append((f"{run}mypy src", "type-check"))

    makefile = repo_path / "Makefile"
    if makefile.is_file():
        targets = _parse_make_targets(makefile)
        for t in ("test", "lint", "build", "install"):
            if t in targets:
                pairs.append((f"make {t}", ""))

    package_json = repo_path / "package.json"
    if package_json.is_file():
        scripts = _parse_package_json_scripts(package_json)
        for s in ("build", "test", "lint", "dev"):
            if s in scripts:
                pairs.append((f"npm run {s}", ""))

    if not pairs:
        return None
    seen: set[str] = set()
    deduped: list[tuple[str, str]] = []
    for cmd, note in pairs:
        if cmd not in seen:
            seen.add(cmd)
            deduped.append((cmd, note))
    width = max((len(cmd) for cmd, note in deduped if note), default=0)
    lines = [f"{cmd:<{width}}  # {note}" if note else cmd for cmd, note in deduped]
    body = "```\n" + "\n".join(lines) + "\n```"
    return AgentsMdSection(
        key="build-test",
        title="Build & test",
        body=body,
        kind="build-test",
        always_apply=True,
    )


def _build_setup(repo_path: Path) -> AgentsMdSection | None:
    """One-paragraph setup block, derived from the most authoritative installer.

    Prefers ``uv`` when ``uv.lock`` is present, otherwise ``pip``,
    otherwise ``npm`` for JS-only repos.
    """
    if (repo_path / "uv.lock").exists():
        body = (
            "1. `uv sync` to install + lock the project.\n"
            "2. `uv run python -m bernstein --help` (or your equivalent entry point).\n"
            "3. See [Build & test](#build--test) for the recurring commands."
        )
    elif (repo_path / "pyproject.toml").exists():
        body = (
            "1. `pip install -e .[dev]` in a fresh venv.\n"
            "2. `python -m <module> --help` for the entry point.\n"
            "3. See [Build & test](#build--test) for the recurring commands."
        )
    elif (repo_path / "package.json").exists():
        body = (
            "1. `npm ci` (or `npm install` for first-pass).\n"
            "2. `npm run dev` to start; `npm run build` to package.\n"
            "3. See [Build & test](#build--test) for the recurring commands."
        )
    else:
        return None
    return AgentsMdSection(
        key="setup",
        title="Setup",
        body=body,
        kind="setup",
        always_apply=True,
    )


def _build_architecture(repo_path: Path) -> AgentsMdSection | None:
    """Surface entry points from ``[project.scripts]`` plus any back-compat
    redirect map declared in ``src/<pkg>/core/__init__.py``.

    Skipped silently when no entry points are declared. We deliberately
    don't pull every callable out of ast_symbol_graph - for AGENTS.md the
    operator wants ~5 starting points, not the full call graph.

    The redirect-map detection covers a load-bearing audit invariant
    : when a project ships a ``sys.meta_path`` finder for
    legacy import paths, the AGENTS.md surface must point readers at
    the real mechanism so the documentation never drifts back to
    "shim file lives at <name>.py" claims that aren't true.
    """
    pyproj = repo_path / "pyproject.toml"
    if not pyproj.is_file():
        return None
    scripts = _parse_pyproject_scripts(pyproj)
    if not scripts:
        return None
    rows = [(f"`{name}`", target) for name, target in scripts.items()]
    body = "Top-level entry points exposed by the package:\n\n" + _render_two_column_table(rows, "Command")

    redirect_note = _detect_back_compat_redirect_map(repo_path)
    if redirect_note:
        body = body + "\n\n" + redirect_note

    return AgentsMdSection(
        key="architecture",
        title="Architecture (entry points)",
        body=body,
        kind="architecture",
        always_apply=True,
    )


def _detect_back_compat_redirect_map(repo_path: Path) -> str | None:
    """Return a markdown note when ``core/__init__.py`` declares a back-
    compat redirect map.

    Looks for the canonical pair ``_REDIRECT_MAP`` + ``_CoreRedirectFinder``
    inside any ``src/*/core/__init__.py`` file. Both names must be present
    for the section to fire - finding only one is ambiguous (could be
    a renamed-but-not-replaced alias) and we'd rather emit nothing than
    misdescribe.
    """
    src_dir = repo_path / "src"
    if not src_dir.is_dir():
        return None
    for pkg_dir in sorted(src_dir.iterdir()):
        if not pkg_dir.is_dir():
            continue
        init_path = pkg_dir / "core" / "__init__.py"
        if not init_path.is_file():
            continue
        try:
            text = init_path.read_text(encoding="utf-8")
        except OSError:
            continue
        if "_REDIRECT_MAP" not in text or "_CoreRedirectFinder" not in text:
            continue
        rel = init_path.relative_to(repo_path).as_posix()
        return (
            "**Back-compat aliases.** Legacy import paths (e.g. "
            f"`{pkg_dir.name}.core.orchestrator`) are served by a "
            "`sys.meta_path` finder, not by physical shim files. The mechanism "
            f"lives in `{rel}` as `_CoreRedirectFinder` driven by the "
            "`_REDIRECT_MAP` dict - add new aliases there rather than creating "
            "shim modules at the old path."
        )
    return None


def _build_conventions(repo_path: Path, opts: GenerateOptions) -> AgentsMdSection | None:
    """Read ``<overlay_dir>/conventions.md`` if present.

    This is the single curated section the operator owns. The generator
    does not invent style rules from a static cheat sheet - that would
    rot the moment the project's actual style drifts.
    """
    overlay = repo_path / opts.overlay_dir / "conventions.md"
    if not overlay.is_file():
        return None
    body = overlay.read_text(encoding="utf-8").strip()
    if not body:
        return None
    return AgentsMdSection(
        key="conventions",
        title="Coding conventions",
        body=body,
        kind="conventions",
        always_apply=True,
    )


def _build_git_workflow(repo_path: Path) -> AgentsMdSection | None:
    """Default branch line, deterministic across environments.

    The previous version included ``git_context.hot_files`` output in
    the section body. That made the section non-deterministic across
    shallow CI clones (`actions/checkout` defaults to depth 1) and full
    local checkouts -- the same repo at the same SHA would render
    different "hot files" tables in CI vs on a developer machine,
    causing ``bernstein agents-md verify`` to flag drift even after a
    fresh local sync. Returns ``None`` outside a git working tree.
    """
    default_branch = _git_default_branch(repo_path)
    if default_branch is None:
        return None
    return AgentsMdSection(
        key="git-workflow",
        title="Git workflow",
        body=f"Default branch: `{default_branch}`.",
        kind="git-workflow",
        always_apply=True,
    )


def _build_roles(repo_path: Path) -> AgentsMdSection | None:
    """List the role names shipped under ``templates/roles/``.

    The body is intentionally a single sentence + bullet list. The actual
    role prompts are too long to inline and are loaded by the orchestrator
    at runtime; AGENTS.md just declares what exists.
    """
    roles_dir = repo_path / "templates" / "roles"
    if not roles_dir.is_dir():
        return None
    role_names = sorted(d.name for d in roles_dir.iterdir() if d.is_dir() and (d / "system_prompt.md").is_file())
    if not role_names:
        return None
    body = (
        "Bernstein ships agent role prompts under `templates/roles/`. "
        "The orchestrator loads them at task-spawn time; you don't write "
        "to them manually.\n\n" + "\n".join(f"- `{r}`" for r in role_names)
    )
    return AgentsMdSection(
        key="roles",
        title="Agent roles",
        body=body,
        kind="roles",
        always_apply=False,
        target_globs=("templates/roles/**",),
    )


def _build_documentation_duty() -> AgentsMdSection:
    """Project-wide rule: every feature PR ships docs in the same PR.

    Emitted unconditionally so the rule is present in every target file
    that the bridge produces. Kept short by design so it never crowds out
    the auto-derived sections above it.
    """
    body = (
        "Every PR that adds or changes a feature MUST update docs in the same PR:\n\n"
        "- User-visible behaviour: update the relevant `README.md` section.\n"
        "- Operator workflows: update `docs/operations/<area>.md`.\n"
        "- Public API surface: regenerate `docs/api/` schemas.\n"
        "- Architecture or new module: update `docs/sdd/` and run "
        "`bernstein agents-md sync` so AGENTS.md, CLAUDE.md, `.goosehints`, "
        "`CONVENTIONS.md`, and `.cursor/rules/*.mdc` stay aligned.\n"
        "- New test layer: also update `docs/contributing/testing.md`.\n\n"
        "PRs without the matching docs change will be sent back. "
        "Docs and code ship together."
    )
    return AgentsMdSection(
        key="documentation-duty",
        title="Documentation duty",
        body=body,
        kind="custom",
        always_apply=True,
    )


def _build_overlay_sections(repo_path: Path, opts: GenerateOptions) -> list[AgentsMdSection]:
    """Read every ``.md`` under ``<overlay_dir>/`` except ``conventions.md``.

    Each file becomes one section. Filename (without extension) becomes
    the section ``key`` and the H1 inside the file becomes the title;
    when the file has no H1, the kebab-cased filename is used.
    """
    overlay_dir = repo_path / opts.overlay_dir
    if not overlay_dir.is_dir():
        return []
    out: list[AgentsMdSection] = []
    for path in sorted(overlay_dir.glob("*.md")):
        if path.name == "conventions.md":
            continue  # already consumed by _build_conventions
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            continue
        title, body = _split_title_and_body(text, fallback=path.stem.replace("-", " ").title())
        out.append(
            AgentsMdSection(
                key=path.stem,
                title=title,
                body=body,
                kind="custom",
                always_apply=False,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Helpers - small, well-tested
# ---------------------------------------------------------------------------


def _first_docstring_line(path: Path) -> str:
    """Return the first non-empty line of a Python module's docstring."""
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (SyntaxError, UnicodeDecodeError, OSError):
        return ""
    doc = ast.get_docstring(tree)
    if not doc:
        return ""
    for line in doc.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped.rstrip(".")
    return ""


def _collect_package_rows(pkg_dir: Path, package: str) -> list[tuple[str, str]]:
    """Collect ``(display_name, description)`` rows for one top-level package.

    Mirrors ``scripts/gen_agents_md.py`` so the existing AGENTS.md output
    is preserved byte-for-byte where this section is concerned.
    """
    rows: list[tuple[str, str]] = []
    if package == "core":
        pinned = [f for f in _CORE_PINNED_ORDER if (pkg_dir / f).exists()]
        rest = sorted(
            f.name
            for f in pkg_dir.iterdir()
            if f.is_file()
            and f.suffix == ".py"
            and f.name not in pinned
            and f.name not in _SKIP_FILES
            and f.name != _INIT_PY
        )
        files = [pkg_dir / name for name in (pinned + rest)]
    else:
        files = sorted(f for f in pkg_dir.iterdir() if f.is_file() and f.suffix == ".py" and f.name not in _SKIP_FILES)

    for py_file in files:
        fname = py_file.name
        if fname == _INIT_PY and package in _SKIP_INIT:
            continue
        if fname in _SKIP_IN_MULTI:
            continue
        if fname in _MULTI_FILE_ROWS:
            display = " / ".join(f"`{f}`" for f in _MULTI_FILE_ROWS[fname])
            rows.append((display, _first_docstring_line(py_file)))
            continue
        rows.append((f"`{fname}`", _first_docstring_line(py_file)))

    rows.extend(_collect_subpackage_rows(pkg_dir))
    return rows


_SUBPACKAGE_FILE_LIST_CAP = 6
"""Soft cap on enumerated module names per sub-package row.

A sub-package with 30+ modules dumped inline turns a useful module-map into
a wall of names. When the sub-package's own ``__init__.py`` already has a
docstring, the file list is omitted entirely - the docstring is the
authoritative description. When there's no docstring we list at most this
many module stems and append ``+N more`` for the rest.
"""


def _collect_subpackage_rows(pkg_dir: Path) -> list[tuple[str, str]]:
    """Sub-package directories surface as one row each.

    The description is sourced in priority order:

    1. Sub-package ``__init__.py`` module docstring (authoritative).
    2. A capped list of module stems (when there's no docstring) so the
       reader still sees what lives inside without a 50-name wall.
    3. A neutral ``"<name>/ sub-package"`` placeholder (last resort).

    Skips dunder/underscore-prefixed dirs (``__pycache__``, ``_internal``).
    """
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for subdir in sorted(pkg_dir.iterdir()):
        if not subdir.is_dir() or subdir.name.startswith("_") or subdir.name in seen:
            continue
        seen.add(subdir.name)
        init = subdir / _INIT_PY
        docstring = _first_docstring_line(init) if init.exists() else ""
        py_names = sorted(f.stem for f in subdir.glob("*.py") if not f.name.startswith("_"))
        rows.append((f"`{subdir.name}/`", _summarise_subpackage(subdir.name, docstring, py_names)))
    return rows


def _summarise_subpackage(name: str, docstring: str, py_names: list[str]) -> str:
    """Compose the ``Purpose`` cell content for one sub-package row.

    The docstring wins outright when present; the file list is informative
    only when there's no docstring, and even then capped to keep the table
    scannable.
    """
    if docstring:
        return docstring
    if py_names:
        if len(py_names) <= _SUBPACKAGE_FILE_LIST_CAP:
            return f"Sub-package: {', '.join(py_names)}"
        head = ", ".join(py_names[:_SUBPACKAGE_FILE_LIST_CAP])
        return f"Sub-package: {head} (+{len(py_names) - _SUBPACKAGE_FILE_LIST_CAP} more)"
    return f"{name}/ sub-package"


def _render_two_column_table(
    rows: list[tuple[str, str]],
    left_header: str,
    *,
    right_header: str = "Purpose",
) -> str:
    """Render a two-column markdown table with right-padded left column."""
    if not rows:
        return ""
    col1_w = max((len(r[0]) for r in rows), default=0)
    col1_w = max(col1_w, len(left_header))
    lines = [
        f"| {left_header:<{col1_w}} | {right_header} |",
        f"|{'-' * (col1_w + 2)}|{'-' * (len(right_header) + 2)}|",
    ]
    for left, right in rows:
        lines.append(f"| {left:<{col1_w}} | {right} |")
    return "\n".join(lines)


_README_SKIP_PREFIXES = (
    "#",  # any-level markdown heading
    "[![",  # shields.io / badge link
    "> ",  # blockquote
    "![",  # inline image
    "<!--",  # html comment
    "<",  # raw HTML - divs, picture, source, br, etc. README chrome
    "---",  # horizontal rule
    "===",  # setext underline
)


def _first_paragraph(path: Path) -> str:
    """Return the first prose paragraph from ``path``.

    A paragraph is everything between the first non-skipped, non-blank
    line and the next blank line. Skips markdown headings, badge links,
    blockquotes, inline images, HTML comments, raw HTML chrome (``<div>``,
    ``<picture>``, etc.) and rule separators so the output reads as the
    actual prose introduction rather than the README's decoration.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    paragraph: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line:
            if paragraph:
                break
            continue
        stripped = line.lstrip()
        if any(stripped.startswith(prefix) for prefix in _README_SKIP_PREFIXES):
            if paragraph:
                break
            continue
        if _looks_like_nav_strip(stripped):
            if paragraph:
                break
            continue
        paragraph.append(line)
    return "\n".join(paragraph).strip()


def _looks_like_nav_strip(line: str) -> bool:
    """Detect language-switchers and horizontal link menus.

    Heuristic: 3+ markdown link tokens on a single line is almost always a
    nav strip rather than prose. Real README first paragraphs typically
    carry 0-2 links of context, not a menu. Catches both pipe-separated
    (``[a]() | [b]()``) and middot-separated (``[a]() · [b]()``) and HTML
    entity (``[a]() &middot; [b]()``) variants without per-separator
    enumeration.
    """
    return len(re.findall(r"\[[^\]]+\]\([^)]+\)", line)) >= 3


def _first_n_lines(path: Path, n: int) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8").splitlines()[:n]).strip()
    except OSError:
        return ""


def _parse_make_targets(makefile: Path) -> set[str]:
    """Return target names declared in a Makefile, ignoring ``.PHONY`` etc."""
    out: set[str] = set()
    try:
        text = makefile.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z0-9_-]+)\s*:", line)
        if m:
            target = m.group(1)
            if not target.startswith("."):
                out.add(target)
    return out


def _parse_package_json_scripts(package_json: Path) -> set[str]:
    """Return the keys of the top-level ``scripts`` block in package.json."""
    import json

    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    scripts = data.get("scripts", {})
    return set(scripts.keys()) if isinstance(scripts, dict) else set()


def _parse_pyproject_scripts(pyproj: Path) -> dict[str, str]:
    """Return ``{name: target}`` for ``[project.scripts]`` entries.

    Falls back to an empty dict on parse error or missing section.
    Uses tomllib to avoid a third-party dep.
    """
    try:
        import tomllib  # py311+
    except ImportError:  # pragma: no cover - handled at runtime
        return {}
    try:
        data = tomllib.loads(pyproj.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    scripts = data.get("project", {}).get("scripts", {})
    return {str(k): str(v) for k, v in scripts.items()} if isinstance(scripts, dict) else {}


def _git_default_branch(repo_path: Path) -> str | None:
    """Return the default branch (``main``/``master``/...) or ``None``.

    Resolution order is deliberate so that the answer is *deterministic
    across environments* - local checkouts, CI shallow clones, detached
    HEADs, and worktrees must all agree:

    1. ``git symbolic-ref refs/remotes/origin/HEAD`` - set when ``git
       remote set-head origin -a`` ran. Authoritative on developer
       machines; usually absent on ``actions/checkout`` runners.
    2. ``git rev-parse --verify main`` then ``master`` - recognises the
       conventional default-branch names. Works even in shallow clones
       and detached HEAD. This step is what keeps CI render output
       byte-stable against a freshly committed local sync.
    3. ``git rev-parse --abbrev-ref HEAD`` - last resort. Returns the
       current branch name when neither ``main`` nor ``master`` exists.
       Skipped when HEAD is detached (returns the literal ``HEAD``)
       because that would inject the PR-branch name into AGENTS.md.
    """
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            ref = result.stdout.strip()
            if ref.startswith("refs/remotes/origin/"):
                return ref.removeprefix("refs/remotes/origin/")
        # Try both local refs (``main``) and remote-tracking refs
        # (``refs/remotes/origin/main``). actions/checkout only fetches the
        # PR branch and origin tip refs; it does not create a local ``main``
        # branch, so ``git rev-parse --verify main`` returns 128 there.
        # ``refs/remotes/origin/main`` is what CI actually has.
        for candidate in (
            "main",
            "master",
            "refs/remotes/origin/main",
            "refs/remotes/origin/master",
        ):
            result = subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet", candidate],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return candidate.removeprefix("refs/remotes/origin/")
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            branch = result.stdout.strip()
            if branch and branch != "HEAD":
                return branch
    except (subprocess.TimeoutExpired, OSError):
        return None
    # Final fallback - only if we're inside a git checkout. ``main`` is
    # the modern default; older repos that adopted ``master`` will have
    # been resolved earlier in the chain. Keeps the rendered section
    # deterministic across shallow CI clones, worktrees, and detached
    # heads where every prior probe came up empty.
    if (repo_path / ".git").exists():
        return "main"
    return None


def _split_title_and_body(text: str, *, fallback: str) -> tuple[str, str]:
    """Split a markdown blob into (title, body) by reading its first H1.

    When no H1 is present, ``fallback`` is used and the entire text becomes
    the body.
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("# "):
            title = stripped[2:].strip() or fallback
            body = "\n".join(lines[i + 1 :]).strip()
            return title, body
    return fallback, text.strip()


__all__ = [
    "PACKAGE_META",
    "AgentsMdSection",
    "GenerateOptions",
    "SectionKind",
    "generate",
    "render_canonical",
]
