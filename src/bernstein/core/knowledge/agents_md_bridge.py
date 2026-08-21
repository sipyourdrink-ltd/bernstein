"""Cross-CLI bridge - translate canonical AGENTS.md sections into target formats.

The companion of :mod:`bernstein.core.knowledge.agents_md_generator`. Given
the same ``list[AgentsMdSection]`` IR, this module emits the on-disk shape
each major coding-agent CLI actually reads in 2026:

==============  =============================================================
Target          Canonical filename(s)
==============  =============================================================
Cursor          ``.cursor/rules/<key>.mdc`` (current; YAML frontmatter +
                markdown body); legacy ``.cursorrules`` deliberately not
                emitted because Cursor's docs no longer document it.
Claude Code     ``CLAUDE.md`` at repo root; freeform markdown ≤ 200 lines
                target. Subdirectory ``CLAUDE.md`` files are out of scope -
                AGENTS.md is the central source.
Aider           ``CONVENTIONS.md`` at repo root + ``.aider.conf.yml`` patch
                with ``read: CONVENTIONS.md``. Without the conf-file entry
                the convention file is dead, so the bridge always emits
                both.
Goose           ``.goosehints`` at repo root. Plaintext (markdown renders
                fine, no required structure).
Canonical       ``AGENTS.md`` at repo root, plus ``docs/sdd/module-map.md``
                (the full per-file module map; ``AGENTS.md``'s own
                "Module map" section carries only a compact index and
                links here - see #4142).
==============  =============================================================

Design rules:

* The canonical IR (``AgentsMdSection``) is the *only* source of truth,
  with one narrow exception: the canonical target's
  ``docs/sdd/module-map.md`` is threaded in as a precomputed string
  (``render``'s ``module_map_page`` parameter) rather than derived from
  ``sections``, because it needs a second filesystem read the IR doesn't
  carry. Every other file in every other target is a pure function of
  ``sections`` alone.
* No target invents new content beyond that one exception. If a target's
  format requires a field the IR doesn't carry (e.g. Cursor's
  ``description`` frontmatter), it is derived from the section's existing
  fields (title, kind), never invented.
* All renderers return *relative* paths under the supplied repo root so
  the CLI's writer/verifier can reason about the destination uniformly.

References per target:

* Cursor rules - https://cursor.com/docs/context/rules
* Claude Code memory - https://code.claude.com/docs/en/memory
* Aider conventions - https://aider.chat/docs/usage/conventions.html
* Aider conf.yml - https://aider.chat/docs/config/aider_conf.html
* Goose hints - https://github.com/block/goose/blob/main/.goosehints
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from bernstein.core.knowledge.agents_md_generator import (
    MODULE_MAP_PAGE,
    AgentsMdSection,
    render_canonical,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public dataclass - one render's worth of files for one target
# ---------------------------------------------------------------------------


Target = Literal["canonical", "cursor", "claude", "aider", "goose"]
"""Closed set of bridge target names. Used by the CLI's ``--target`` flag."""

ALL_TARGETS: tuple[Target, ...] = ("canonical", "cursor", "claude", "aider", "goose")


@dataclass(frozen=True)
class BridgeOutput:
    """The set of relative paths and content a target render produces.

    Attributes:
        target: Which target this output is for.
        files: ``{relative_path: file_content_str}``. Paths are
            forward-slash strings and always relative to the repo root.
            Use :meth:`absolute_paths` if you need ``Path`` objects bound
            to a specific repo.
    """

    target: Target
    files: dict[str, str]

    def absolute_paths(self, repo_root: Path) -> dict[Path, str]:
        return {(repo_root / p).resolve(): content for p, content in self.files.items()}


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def render(
    sections: list[AgentsMdSection],
    target: Target,
    *,
    repo_name: str | None = None,
    module_map_page: str | None = None,
) -> BridgeOutput:
    """Translate ``sections`` into the on-disk shape for ``target``.

    Args:
        sections: Output of :func:`bernstein.core.knowledge.agents_md_generator.generate`.
        target: One of ``ALL_TARGETS``.
        repo_name: Display name woven into the canonical H1.
        module_map_page: Output of
            :func:`bernstein.core.knowledge.agents_md_generator.render_module_map_page`,
            when the caller has it. Written alongside the canonical target
            only (``docs/sdd/module-map.md`` is one repo-wide reference
            page, not a per-target mirror - see #4142). Threaded in as a
            string rather than computed here from a repo path: this module
            renders from ``sections`` alone everywhere else, and re-reading
            the filesystem inside a "translate this IR" function would be
            the one exception. ``None`` omits the page, matching
            :func:`render_module_map_page`'s own not-applicable case.

    Returns:
        :class:`BridgeOutput` ready for the CLI to write or diff.
    """
    if target == "canonical":
        return _render_canonical(sections, repo_name=repo_name, module_map_page=module_map_page)
    if target == "cursor":
        return _render_cursor(sections)
    if target == "claude":
        return _render_claude(sections, repo_name=repo_name)
    if target == "aider":
        return _render_aider(sections, repo_name=repo_name)
    if target == "goose":
        return _render_goose(sections, repo_name=repo_name)
    raise ValueError(f"Unknown render target: {target!r}")


def render_all(
    sections: list[AgentsMdSection],
    *,
    repo_name: str | None = None,
    module_map_page: str | None = None,
) -> dict[Target, BridgeOutput]:
    """Render every target in one call. Returns a target-keyed dict."""
    return {t: render(sections, t, repo_name=repo_name, module_map_page=module_map_page) for t in ALL_TARGETS}


# ---------------------------------------------------------------------------
# Canonical (AGENTS.md) - same content, packaged as a BridgeOutput
# ---------------------------------------------------------------------------


def _render_canonical(
    sections: list[AgentsMdSection],
    *,
    repo_name: str | None,
    module_map_page: str | None = None,
) -> BridgeOutput:
    body = render_canonical(sections, repo_name=repo_name)
    files = {"AGENTS.md": body}
    if module_map_page is not None:
        files[MODULE_MAP_PAGE] = module_map_page
    return BridgeOutput(target="canonical", files=files)


# ---------------------------------------------------------------------------
# Cursor - .cursor/rules/<key>.mdc with YAML frontmatter
# ---------------------------------------------------------------------------


def _render_cursor(sections: list[AgentsMdSection]) -> BridgeOutput:
    """Per-section MDC files under ``.cursor/rules/``.

    Frontmatter fields used (per https://cursor.com/docs/context/rules):

    * ``description`` - derived from section ``title``. Cursor agent uses
      this to decide relevance for "Apply Intelligently" sessions.
    * ``globs`` - ``section.target_globs`` joined with commas. When empty,
      the field is omitted (Cursor treats absent ``globs`` correctly).
    * ``alwaysApply`` - ``section.always_apply``. When ``True`` and
      ``globs`` is also set, Cursor ignores ``globs`` (per their docs);
      that's intentional - we want one rule injected every session.
    """
    files: dict[str, str] = {}
    for sec in sections:
        relpath = f".cursor/rules/{sec.key}.mdc"
        files[relpath] = _cursor_mdc_for(sec)
    return BridgeOutput(target="cursor", files=files)


_CURSOR_AUTOGEN_MARKER = (
    "<!-- AUTO-GENERATED by `bernstein agents-md sync` - DO NOT edit by hand. Source: AGENTS.md. -->"
)


def _cursor_mdc_for(sec: AgentsMdSection) -> str:
    """Build one MDC file's content (frontmatter + autogen marker + body).

    The HTML-comment marker sits between the YAML frontmatter and the body
    so Cursor's frontmatter parser is unaffected and reviewers see the
    "do not edit" hint immediately.
    """
    fm: list[str] = ["---", f"description: {_safe_one_line(sec.title)}"]
    if sec.target_globs:
        fm.append(f"globs: {','.join(sec.target_globs)}")
    fm.extend((f"alwaysApply: {'true' if sec.always_apply else 'false'}", "---"))
    return "\n".join(fm) + "\n\n" + _CURSOR_AUTOGEN_MARKER + "\n\n" + sec.body.rstrip() + "\n"


# ---------------------------------------------------------------------------
# Claude Code - single CLAUDE.md at repo root
# ---------------------------------------------------------------------------


_CLAUDE_LINE_BUDGET = 200
"""Anthropic's documented soft cap. We don't hard-truncate; we warn instead."""


def _render_claude(sections: list[AgentsMdSection], *, repo_name: str | None) -> BridgeOutput:
    """Single ``CLAUDE.md`` at repo root.

    Sections preserve their headings. We deliberately do NOT use the
    ``@AGENTS.md`` import shortcut to avoid coupling to file-system layout
    on the consuming side; teams that want both files can either keep
    them in sync (the whole point of this bridge) or symlink one to the
    other.
    """
    name = repo_name or "Project"
    parts: list[str] = [
        f"# {name} - CLAUDE.md\n",
        "<!-- AUTO-GENERATED by `bernstein agents-md sync` - DO NOT edit by hand. Source: AGENTS.md. -->\n",
    ]
    for sec in sections:
        parts.append(f"\n## {sec.title}\n\n{sec.body.rstrip()}\n")
    body = "".join(parts).rstrip() + "\n"

    line_count = body.count("\n")
    if line_count > _CLAUDE_LINE_BUDGET:
        logger.info(
            "CLAUDE.md is %d lines (Anthropic's soft cap is %d). "
            "Consider trimming overlay sections under .sdd/agents-md/.",
            line_count,
            _CLAUDE_LINE_BUDGET,
        )
    return BridgeOutput(target="claude", files={"CLAUDE.md": body})


# ---------------------------------------------------------------------------
# Aider - CONVENTIONS.md at repo root + .aider.conf.yml patch
# ---------------------------------------------------------------------------


def _render_aider(sections: list[AgentsMdSection], *, repo_name: str | None) -> BridgeOutput:
    """Aider's instructions are a two-file convention.

    1. ``CONVENTIONS.md`` - markdown content (anything Aider should know).
    2. ``.aider.conf.yml`` - config that makes Aider actually load the
       conventions file. Without ``read: CONVENTIONS.md`` the conventions
       file is dead. We always patch (or create) the conf file so the
       sync is self-consistent.
    """
    name = repo_name or "Project"
    parts: list[str] = [
        f"# {name} - CONVENTIONS.md\n",
        "<!-- AUTO-GENERATED by `bernstein agents-md sync` - DO NOT edit by hand. Source: AGENTS.md. -->\n",
    ]
    for sec in sections:
        parts.append(f"\n## {sec.title}\n\n{sec.body.rstrip()}\n")
    conv = "".join(parts).rstrip() + "\n"

    # The .aider.conf.yml is intentionally minimal - we only own the
    # `read:` line. If the user has more config (model, edit-format, …)
    # they can add it; the writer in agents_md_cmd.py merges, not
    # overwrites.
    conf = "# AUTO-GENERATED by `bernstein agents-md sync` - managed line: `read:`.\nread: CONVENTIONS.md\n"

    return BridgeOutput(
        target="aider",
        files={
            "CONVENTIONS.md": conv,
            ".aider.conf.yml": conf,
        },
    )


# ---------------------------------------------------------------------------
# Goose - .goosehints at repo root, plaintext
# ---------------------------------------------------------------------------


def _render_goose(sections: list[AgentsMdSection], *, repo_name: str | None) -> BridgeOutput:
    """Goose reads ``.goosehints`` as plaintext.

    Markdown renders fine and Goose's own repo uses prose + bullets, so we
    keep the section structure. Headers go from ``##`` to plain ``[Title]``
    style + double newline so plaintext-mode tools also parse the section
    breaks visually.
    """
    name = repo_name or "Project"
    parts: list[str] = [
        f"# {name} - Goose hints",
        "AUTO-GENERATED by `bernstein agents-md sync` - DO NOT edit by hand. Source: AGENTS.md.",
        "",
    ]
    for sec in sections:
        parts.extend((f"## {sec.title}", "", sec.body.rstrip(), ""))
    body = "\n".join(parts).rstrip() + "\n"
    return BridgeOutput(target="goose", files={".goosehints": body})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_one_line(s: str) -> str:
    """Collapse to one line, no leading/trailing whitespace, no newlines.

    Used for YAML scalar fields that must not break out of a single line
    (``description:`` etc.). YAML quoting is not strictly necessary for
    the values we emit (titles like ``Module map``, ``Build & test``), but
    we strip control characters defensively.
    """
    return " ".join(s.split())


__all__ = [
    "ALL_TARGETS",
    "BridgeOutput",
    "Target",
    "render",
    "render_all",
]
