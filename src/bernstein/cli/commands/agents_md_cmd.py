"""``bernstein agents-md`` -- canonical AGENTS.md generator + cross-CLI sync.

Subcommands:

* ``bernstein agents-md generate`` -- print canonical AGENTS.md to stdout.
* ``bernstein agents-md write [--target T]`` -- write one target's files.
  ``T`` ∈ ``canonical | cursor | claude | aider | goose``. Default:
  ``canonical``.
* ``bernstein agents-md sync`` -- write *all* target formats; the
  killer-feature command.
* ``bernstein agents-md verify [--target T]`` -- exit non-zero when any
  on-disk file diverges from the generated content. CI-friendly.
* ``bernstein agents-md diff [--target T]`` -- print a human-readable
  unified diff between disk and generated; exit 0 either way.

Design follows ``bernstein.cli.commands.lineage_cmd``: click ``@group``
with subcommands declared inline (small ones) or attached at module
bottom (heavier sibling files). All heavy imports are lazy inside command
bodies so the top-level CLI startup stays fast.
"""

from __future__ import annotations

import difflib
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from bernstein.core.knowledge.agents_md_bridge import BridgeOutput, Target
    from bernstein.core.knowledge.agents_md_generator import AgentsMdSection


_TARGET_CHOICES = ("canonical", "cursor", "claude", "aider", "goose")

#: Exit code when a git checkout's default branch cannot be resolved. The
#: generated context files are committed verbatim, so writing a wrong or
#: guessed branch name is worse than failing loudly (issue #4578).
_EXIT_UNRESOLVED_DEFAULT_BRANCH = 3


# ---------------------------------------------------------------------------
# Group
# ---------------------------------------------------------------------------


@click.group(name="agents-md", invoke_without_command=True)
@click.pass_context
def agents_md_cmd(ctx: click.Context) -> None:
    """Canonical AGENTS.md generator with cross-CLI rewrite.

    Follows the canonical agents.md spec ("AGENTS.md is just standard
    Markdown" -- agents.md/) and the AAIF AGENTS.md project profile. Derives
    one canonical IR from the repo, then renders to five target shapes:
    canonical AGENTS.md, Cursor ``.cursor/rules/*.mdc``, Claude
    ``CLAUDE.md``, Aider ``CONVENTIONS.md``, and Goose ``.goosehints``.

    \b
    Examples:
      bernstein agents-md generate            # print canonical AGENTS.md
      bernstein agents-md sync                # write all 5 target files
      bernstein agents-md write --target cursor
      bernstein agents-md verify              # exit 1 if any file is stale
      bernstein agents-md diff --target claude

    Cite: agents.md/ (canonical spec), aaif.io/projects/agents-md/ (AAIF profile).
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------


@agents_md_cmd.command(name="generate")
@click.option(
    "--workdir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path.cwd,
    show_default="cwd",
    help="Repository root.",
)
@click.option(
    "--target",
    type=click.Choice(_TARGET_CHOICES),
    default="canonical",
    show_default=True,
    help="Which target's content to print.",
)
@click.option(
    "--repo-name",
    default=None,
    help="Display name for the H1. Defaults to the workdir basename.",
)
@click.option(
    "--default-branch",
    default=None,
    help="Explicit default branch for the git-workflow section; overrides "
    "git probes. Useful for CI clones that never fetch origin/HEAD.",
)
def agents_md_generate(workdir: Path, target: str, repo_name: str | None, default_branch: str | None) -> None:
    """Print one target's content to stdout. No file is written."""
    sections, module_map_page, name = _generate_sections(workdir, repo_name, default_branch)
    output = _render_target(sections, target, name, module_map_page)
    # Each target's BridgeOutput has 1+ files; print them concatenated with
    # a clear separator so the operator can see the structure.
    files = list(output.files.items())
    if len(files) == 1:
        click.echo(files[0][1])
        return
    for relpath, content in files:
        click.echo(f"--- {relpath} ---")
        click.echo(content)


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


@agents_md_cmd.command(name="write")
@click.option(
    "--workdir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path.cwd,
    show_default="cwd",
    help="Repository root.",
)
@click.option(
    "--target",
    type=click.Choice(_TARGET_CHOICES),
    default="canonical",
    show_default=True,
    help="Which target's files to write.",
)
@click.option(
    "--repo-name",
    default=None,
    help="Display name for the H1. Defaults to the workdir basename.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be written without touching disk.",
)
@click.option(
    "--default-branch",
    default=None,
    help="Explicit default branch for the git-workflow section; overrides "
    "git probes. Useful for CI clones that never fetch origin/HEAD.",
)
def agents_md_write(
    workdir: Path, target: str, repo_name: str | None, dry_run: bool, default_branch: str | None
) -> None:
    """Write one target's files to disk."""
    sections, module_map_page, name = _generate_sections(workdir, repo_name, default_branch)
    output = _render_target(sections, target, name, module_map_page)
    written = _write_output(output, workdir, dry_run=dry_run)
    if dry_run:
        click.echo(f"[dry-run] {len(output.files)} file(s) would be written under {workdir}")
        for rel in output.files:
            click.echo(f"  · {rel}")
    else:
        click.echo(f"Wrote {written} file(s) under {workdir}")


# ---------------------------------------------------------------------------
# sync - the killer-feature command
# ---------------------------------------------------------------------------


@agents_md_cmd.command(name="sync")
@click.option(
    "--workdir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path.cwd,
    show_default="cwd",
    help="Repository root.",
)
@click.option(
    "--repo-name",
    default=None,
    help="Display name for the H1. Defaults to the workdir basename.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be written without touching disk.",
)
@click.option(
    "--default-branch",
    default=None,
    help="Explicit default branch for the git-workflow section; overrides "
    "git probes. Useful for CI clones that never fetch origin/HEAD.",
)
def agents_md_sync(workdir: Path, repo_name: str | None, dry_run: bool, default_branch: str | None) -> None:
    """Write all target formats so all five files agree."""
    from bernstein.core.knowledge.agents_md_bridge import render_all

    sections, module_map_page, name = _generate_sections(workdir, repo_name, default_branch)
    outputs = render_all(sections, repo_name=name, module_map_page=module_map_page)
    written_total = 0
    planned_total = 0
    for target, output in outputs.items():
        written_total += _write_output(output, workdir, dry_run=dry_run)
        planned_total += len(output.files)
        if dry_run:
            for rel in output.files:
                click.echo(f"[dry-run] would write {rel}  (target={target})")
        else:
            for rel in output.files:
                click.echo(f"  · {rel}  ({target})")
    if dry_run:
        click.echo(f"[dry-run] {planned_total} file(s) across {len(outputs)} target(s) would be synced")
    else:
        click.echo(f"Synced {written_total} file(s) across {len(outputs)} target(s) under {workdir}")


# ---------------------------------------------------------------------------
# verify - CI-friendly drift detector
# ---------------------------------------------------------------------------


@agents_md_cmd.command(name="verify")
@click.option(
    "--workdir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path.cwd,
    show_default="cwd",
    help="Repository root.",
)
@click.option(
    "--target",
    type=click.Choice([*_TARGET_CHOICES, "all"]),
    default="all",
    show_default=True,
    help="Which target(s) to verify against on-disk content.",
)
@click.option(
    "--repo-name",
    default=None,
    help="Display name for the H1. Defaults to the workdir basename.",
)
@click.option(
    "--default-branch",
    default=None,
    help="Explicit default branch for the git-workflow section; overrides "
    "git probes. Useful for CI clones that never fetch origin/HEAD.",
)
def agents_md_verify(workdir: Path, target: str, repo_name: str | None, default_branch: str | None) -> None:
    """Exit 1 if any on-disk file diverges from the generated content.

    Designed for CI gating::

        bernstein agents-md verify || (echo 'AGENTS.md drift; run sync' && exit 1)
    """
    from bernstein.core.knowledge.agents_md_bridge import (
        ALL_TARGETS,
        render,
    )

    sections, module_map_page, name = _generate_sections(workdir, repo_name, default_branch)
    targets: tuple[Target, ...] = ALL_TARGETS if target == "all" else (target,)  # type: ignore[assignment]

    drift_count = 0
    checked_count = 0
    for t in targets:
        output = render(sections, t, repo_name=name, module_map_page=module_map_page)
        for rel, expected in output.files.items():
            checked_count += 1
            on_disk = workdir / rel
            if not on_disk.is_file():
                click.echo(f"MISSING  {rel}  (target={t})")
                drift_count += 1
                continue
            actual = on_disk.read_text(encoding="utf-8")
            # Compare normalised: ignore trailing whitespace + final newline
            # variance. Renderers emit a single trailing "\n", but editors,
            # git autocrlf, and platform tooling can introduce stray trailing
            # whitespace that is not semantically a drift.
            if actual.rstrip() != expected.rstrip():
                click.echo(f"DRIFT    {rel}  (target={t})")
                a_norm, e_norm = actual.rstrip(), expected.rstrip()
                for i, (a, e) in enumerate(zip(a_norm, e_norm, strict=False)):
                    if a != e:
                        ctx_a = a_norm[max(0, i - 20) : i + 20]
                        ctx_e = e_norm[max(0, i - 20) : i + 20]
                        click.echo(
                            f"         first diff at offset {i}: actual={ctx_a!r} expected={ctx_e!r}",
                            err=True,
                        )
                        break
                else:
                    click.echo(
                        f"         length diff: actual={len(a_norm)} expected={len(e_norm)}",
                        err=True,
                    )
                drift_count += 1
    if drift_count:
        click.echo(
            f"\n{drift_count} file(s) drift. Run `bernstein agents-md sync` to fix.",
            err=True,
        )
        sys.exit(1)
    click.echo(f"OK       all {checked_count} file(s) in sync")


# ---------------------------------------------------------------------------
# diff - informational, no exit-code drama
# ---------------------------------------------------------------------------


@agents_md_cmd.command(name="diff")
@click.option(
    "--workdir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path.cwd,
    show_default="cwd",
    help="Repository root.",
)
@click.option(
    "--target",
    type=click.Choice([*_TARGET_CHOICES, "all"]),
    default="all",
    show_default=True,
    help="Which target(s) to diff.",
)
@click.option(
    "--repo-name",
    default=None,
    help="Display name for the H1. Defaults to the workdir basename.",
)
@click.option(
    "--default-branch",
    default=None,
    help="Explicit default branch for the git-workflow section; overrides "
    "git probes. Useful for CI clones that never fetch origin/HEAD.",
)
def agents_md_diff(workdir: Path, target: str, repo_name: str | None, default_branch: str | None) -> None:
    """Print unified diff between on-disk and generated for each target file."""
    from bernstein.core.knowledge.agents_md_bridge import (
        ALL_TARGETS,
        render,
    )

    sections, module_map_page, name = _generate_sections(workdir, repo_name, default_branch)
    targets: tuple[Target, ...] = ALL_TARGETS if target == "all" else (target,)  # type: ignore[assignment]

    any_diff = False
    for t in targets:
        output = render(sections, t, repo_name=name, module_map_page=module_map_page)
        for rel, expected in output.files.items():
            on_disk = workdir / rel
            actual = on_disk.read_text(encoding="utf-8") if on_disk.is_file() else ""
            if actual == expected:
                continue
            any_diff = True
            click.echo(f"\n# {rel}  (target={t})\n")
            for line in difflib.unified_diff(
                actual.splitlines(keepends=True),
                expected.splitlines(keepends=True),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
                n=3,
            ):
                click.echo(line, nl=False)
    if not any_diff:
        click.echo("No drift across selected target(s).")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _generate_sections(
    workdir: Path, repo_name: str | None, default_branch: str | None = None
) -> tuple[list[AgentsMdSection], str | None, str]:
    """Run the generator + return ``(sections, module_map_page, repo_name)``.

    ``module_map_page`` is the full per-file table for ``docs/sdd/module-map.md``
    (#4142) - computed alongside ``sections`` here, once, since both read the
    same repo tree and every command below needs both. ``None`` when there is
    nothing to document, matching :func:`render_module_map_page`'s own
    not-applicable case; callers pass it straight through to the bridge,
    which omits the file rather than writing an empty page.

    ``repo_name`` resolution order:

    1. Explicit ``--repo-name`` flag (when provided).
    2. ``[project] name`` from ``pyproject.toml`` if present (Python repos).
    3. ``"name"`` from ``package.json`` if present (JS repos).
    4. Basename of ``workdir`` as last-resort fallback.

    The pyproject/package fallbacks make the H1 sensible when running from
    a worktree whose directory name is auto-generated (e.g.
    ``.claude/worktrees/agent-abc123``) instead of the project's real name.
    """
    from bernstein.core.knowledge.agents_md_generator import (
        DefaultBranchUnresolvedError,
        GenerateOptions,
        generate,
        render_module_map_page,
    )

    resolved = workdir.resolve()
    try:
        sections = generate(resolved, GenerateOptions(default_branch=default_branch))
    except DefaultBranchUnresolvedError as exc:
        click.echo(
            f"error: {exc}",
            err=True,
        )
        sys.exit(_EXIT_UNRESOLVED_DEFAULT_BRANCH)
    if not sections:
        click.echo(f"No content derived from {workdir}: is this a repository?", err=True)
        sys.exit(2)
    module_map_page = render_module_map_page(resolved)
    name = repo_name or _infer_repo_name(resolved)
    return sections, module_map_page, name


def _infer_repo_name(repo_path: Path) -> str:
    """Best-effort project name inference from manifest files.

    Returns the project's canonical name when we can read a Python or JS
    manifest, otherwise the directory basename.
    """
    py_name = _read_pyproject_name(repo_path / "pyproject.toml")
    if py_name:
        return py_name
    js_name = _read_package_json_name(repo_path / "package.json")
    if js_name:
        return js_name
    return repo_path.name


def _read_pyproject_name(pyproj: Path) -> str | None:
    """Return ``[project] name`` from a ``pyproject.toml`` if readable."""
    if not pyproj.is_file():
        return None
    try:
        import tomllib  # py311+
    except ImportError:  # pragma: no cover - handled at runtime
        return None
    try:
        data: dict[str, object] = tomllib.loads(pyproj.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    project_obj: object = data.get("project")
    if not isinstance(project_obj, dict):
        return None
    return _coerce_name_field(project_obj)  # type: ignore[arg-type]


def _read_package_json_name(package_json: Path) -> str | None:
    """Return ``"name"`` from a ``package.json`` if readable."""
    if not package_json.is_file():
        return None
    import json

    try:
        data: object = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return _coerce_name_field(data)  # type: ignore[arg-type]


def _coerce_name_field(mapping: dict[object, object]) -> str | None:
    """Pull ``mapping["name"]`` and return it only when it's a usable string.

    Centralised so both manifest readers stay simple and pyright doesn't
    have to chase ``Unknown`` types through nested narrowing - the
    ``# type: ignore`` at the call sites is the trade-off for keeping JSON
    parsing typed-as-``object`` until the runtime check narrows it.
    """
    name: object = mapping.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def _render_target(
    sections: list[AgentsMdSection], target: str, repo_name: str, module_map_page: str | None
) -> BridgeOutput:
    """Single-target render bridge."""
    from bernstein.core.knowledge.agents_md_bridge import render

    return render(sections, target, repo_name=repo_name, module_map_page=module_map_page)  # type: ignore[arg-type]


def _write_output(output: BridgeOutput, repo_root: Path, *, dry_run: bool) -> int:
    """Write one ``BridgeOutput`` under ``repo_root``. Returns count written."""
    if dry_run:
        return 0
    written = 0
    for rel, content in output.files.items():
        path = repo_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written += 1
    return written


__all__ = ["agents_md_cmd"]
