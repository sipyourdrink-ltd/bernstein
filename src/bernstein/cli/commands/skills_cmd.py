"""``bernstein skills``: list / show / verify skill packs (oai-004).

Layered customisation (issue 1624) is wired in via the
``--layered`` / ``--per-layer`` options on ``list`` and ``show``: when
set, the CLI consults :mod:`bernstein.core.skills.layered` instead of
the in-package skill loader. The two paths intentionally coexist so
existing operators keep their plugin-source view while teams adopting
the BASE/TEAM/USER layout get the layered view on demand.
"""

from __future__ import annotations

import contextlib
import json
import signal
import time
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from bernstein.core.skills.loader import SkillLoader

from bernstein import get_templates_dir
from bernstein.cli.helpers import console
from bernstein.core.skills.lifecycle import (
    SKILLS_TOML_FILENAME,
    InstallScope,
    SkillLifecycleError,
    SkillsTomlError,
    init_skill,
    install_local,
    remove_skill,
    sync_skills,
)
from bernstein.core.skills.lint import LintSeverity, lint_skill


@click.group("skills")
def skills_group() -> None:
    """List and inspect progressive-disclosure skill packs.

    \b
      bernstein skills list           # compact overview
      bernstein skills list --layered # show layered (base/team/user) view
      bernstein skills show backend   # print SKILL.md body
      bernstein skills show backend --reference python-conventions.md
      bernstein skills show backend --per-layer  # merged + per-layer diff
    """


@skills_group.command("list")
@click.option(
    "--no-plugins",
    "no_plugins",
    is_flag=True,
    default=False,
    help="Skip third-party ``bernstein.skill_sources`` plugins.",
)
@click.option(
    "--layered",
    "layered",
    is_flag=True,
    default=False,
    help="List skills from the BASE/TEAM/USER layers, showing layer-of-origin.",
)
def skills_list(no_plugins: bool, layered: bool) -> None:
    """List every discoverable skill with a one-line description."""
    from rich.table import Table

    if layered:
        _skills_list_layered()
        return

    from bernstein.core.planning.role_resolver import get_loader

    templates_root = get_templates_dir(Path.cwd())
    templates_roles_dir = templates_root / "roles"
    try:
        loader = get_loader(templates_roles_dir, include_plugins=not no_plugins)
    except Exception as exc:
        console.print(f"[red]Failed to load skill index:[/red] {exc}")
        raise SystemExit(1) from exc

    skills = loader.list_all()
    if not skills:
        console.print(f"[dim]No skill packs found. Expected at {templates_root / 'skills'}[/dim]")
        return

    table = Table(
        title="Skill packs",
        show_lines=False,
        header_style="bold cyan",
    )
    table.add_column("NAME", style="dim", min_width=14)
    table.add_column("DESCRIPTION", min_width=50)
    table.add_column("REFS", justify="right", min_width=4)
    table.add_column("SCRIPTS", justify="right", min_width=6)
    table.add_column("SOURCE", min_width=8)

    for skill in skills:
        description = skill.description.strip().replace("\n", " ")
        if len(description) > 100:
            description = description[:97] + "..."
        table.add_row(
            skill.name,
            description,
            str(len(skill.references)),
            str(len(skill.scripts)),
            skill.source_name,
        )

    console.print(table)
    console.print(f"\n[dim]{len(skills)} skill(s) total[/dim]")


@skills_group.command("show")
@click.argument("name")
@click.option("--reference", "reference", help="Reference filename to load.")
@click.option("--script", "script", help="Script filename to load.")
@click.option(
    "--per-layer",
    "per_layer",
    is_flag=True,
    default=False,
    help="Print the merged skill plus a per-layer diff (BASE/TEAM/USER).",
)
def skills_show(name: str, reference: str | None, script: str | None, per_layer: bool) -> None:
    """Print the SKILL.md body for a skill (optionally a reference/script)."""
    if per_layer:
        _skills_show_layered(name)
        return

    from bernstein.core.skills.load_skill_tool import load_skill

    templates_root = get_templates_dir(Path.cwd())
    templates_roles_dir = templates_root / "roles"
    result = load_skill(
        name=name,
        reference=reference,
        script=script,
        templates_roles_dir=templates_roles_dir,
    )
    if result.error:
        console.print(f"[red]{result.error}[/red]")
        raise SystemExit(1)

    if reference is not None and result.reference_content is not None:
        console.print(result.reference_content)
        return
    if script is not None and result.script_content is not None:
        console.print(result.script_content)
        return

    console.print(result.body)
    if result.available_references:
        console.print("\n[dim]references: " + ", ".join(result.available_references) + "[/dim]")
    if result.available_scripts:
        console.print("[dim]scripts: " + ", ".join(result.available_scripts) + "[/dim]")


# ---------------------------------------------------------------------------
# Layered (issue 1624) helpers
# ---------------------------------------------------------------------------


def _skills_list_layered() -> None:
    """Render the layered (BASE/TEAM/USER) skill table."""
    from rich.table import Table

    from bernstein.core.skills.layered import LayeredSkillPaths, list_skills

    paths = LayeredSkillPaths.defaults()
    entries = list_skills(paths=paths)

    if not entries:
        console.print(
            "[dim]No layered skills found. Expected one of:\n"
            f"  base: {paths.base}\n  team: {paths.team}\n  user: {paths.user}[/dim]"
        )
        return

    table = Table(
        title="Skills (layered view)",
        show_lines=False,
        header_style="bold cyan",
    )
    table.add_column("NAME", style="dim", min_width=14)
    table.add_column("BASE", justify="center", min_width=4)
    table.add_column("TEAM", justify="center", min_width=4)
    table.add_column("USER", justify="center", min_width=4)
    table.add_column("ORIGIN", min_width=12)

    for name, layers in entries:
        labels = [layer.label for layer in layers]
        table.add_row(
            name,
            "x" if "base" in labels else "",
            "x" if "team" in labels else "",
            "x" if "user" in labels else "",
            "+".join(labels),
        )

    console.print(table)
    console.print(f"\n[dim]{len(entries)} skill(s) total[/dim]")


def _skills_show_layered(name: str) -> None:
    """Render the merged skill plus a per-layer diff."""
    from bernstein.core.skills.layered import (
        LayeredSkillPaths,
        SkillNotFoundError,
        load_skill,
        per_layer_view,
    )

    paths = LayeredSkillPaths.defaults()
    try:
        merged = load_skill(name, paths=paths)
    except SkillNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from exc

    console.print(f"[bold cyan]merged skill: {merged.name}[/bold cyan]")
    console.print("[dim]layers present: " + ", ".join(layer.label for layer in merged.layers_present) + "[/dim]")
    console.print(json.dumps(merged.as_dict(), indent=2, sort_keys=True))

    fragments = per_layer_view(name, paths=paths)
    for layer in sorted(fragments, key=lambda layer: layer.value):
        console.print(f"\n[bold]layer: {layer.label}[/bold] ([dim]{paths.for_layer(layer)}[/dim])")
        console.print(json.dumps(fragments[layer], indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# Lifecycle commands (issue 1720, track 1)
# ---------------------------------------------------------------------------


def _parse_scope(scope_str: str) -> InstallScope:
    """Coerce the ``--scope`` CLI flag into an :class:`InstallScope`."""
    try:
        return InstallScope(scope_str)
    except ValueError as exc:
        raise click.BadParameter(f"unknown scope {scope_str!r}; expected project or user") from exc


@skills_group.command("init")
@click.argument("name")
@click.option(
    "--scope",
    "scope",
    type=click.Choice(["project", "user"]),
    default="project",
    help="Scaffold under the project or user skill directory.",
)
@click.option(
    "--description",
    "description",
    default=None,
    help="Description to write into SKILL.md.",
)
def skills_init(name: str, scope: str, description: str | None) -> None:
    """Create a deterministic local skill scaffold."""
    install_scope = _parse_scope(scope)
    try:
        result = init_skill(
            name,
            scope=install_scope,
            workdir=Path.cwd(),
            description=description,
        )
    except SkillLifecycleError as exc:
        console.print(f"[red]init failed:[/red] {exc}")
        raise SystemExit(1) from exc
    console.print(f"[green]initialized[/green] {result.name} -> {result.install_dir}")


@skills_group.command("install")
@click.argument("source", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--scope",
    "scope",
    type=click.Choice(["project", "user"]),
    default="project",
    help="Install scope: project (.bernstein/skills/) or user (~/.bernstein/skills/).",
)
@click.option(
    "--name",
    "override_name",
    default=None,
    help="Override the auto-detected skill name (uses source filename otherwise).",
)
@click.option(
    "--strict",
    "strict",
    is_flag=True,
    default=False,
    help="Fail install when skill lint reports ERROR findings.",
)
@click.option(
    "--accept-risk",
    "accept_risk",
    is_flag=True,
    default=False,
    help="Allow installs that require explicit risk acceptance.",
)
def skills_install(source: Path, scope: str, override_name: str | None, strict: bool, accept_risk: bool) -> None:
    """Install a skill from a local path.

    \b
      bernstein skills install ./templates/skills/bernstein-test-runner.md
      bernstein skills install ./my-skill-dir --scope user
    """
    install_scope = _parse_scope(scope)
    try:
        result = install_local(
            source.resolve(),
            scope=install_scope,
            workdir=Path.cwd(),
            override_name=override_name,
            strict_lint=strict,
            accept_risk=accept_risk,
        )
    except SkillLifecycleError as exc:
        console.print(f"[red]install failed:[/red] {exc}")
        raise SystemExit(1) from exc
    console.print(
        f"[green]installed[/green] {result.name} -> {result.install_dir} (digest {result.digest.digest[:12]}...)",
    )


@skills_group.command("remove")
@click.argument("name")
@click.option(
    "--scope",
    "scope",
    type=click.Choice(["project", "user"]),
    default="project",
    help="Remove from project or user scope.",
)
def skills_remove(name: str, scope: str) -> None:
    """Remove a previously installed skill."""
    install_scope = _parse_scope(scope)
    removed = remove_skill(name, scope=install_scope, workdir=Path.cwd())
    if removed:
        console.print(f"[green]removed[/green] {name} from {scope} scope")
    else:
        console.print(f"[yellow]not installed[/yellow] {name} in {scope} scope")
        raise SystemExit(1)


@skills_group.command("sync")
@click.option(
    "--manifest",
    "manifest",
    type=click.Path(path_type=Path),
    default=None,
    help=f"Path to {SKILLS_TOML_FILENAME}. Defaults to <cwd>/{SKILLS_TOML_FILENAME}.",
)
@click.option(
    "--scope",
    "scope",
    type=click.Choice(["project", "user"]),
    default="project",
    help="Where to install declared skills.",
)
@click.option(
    "--strict",
    "strict",
    is_flag=True,
    default=False,
    help="Fail sync when skill lint reports ERROR findings.",
)
@click.option(
    "--accept-risk",
    "accept_risk",
    is_flag=True,
    default=False,
    help="Allow installs that require explicit risk acceptance.",
)
def skills_sync(manifest: Path | None, scope: str, strict: bool, accept_risk: bool) -> None:
    """Install every skill declared in ``bernstein-skills.toml``.

    Re-runs are idempotent: skills whose digest already matches are
    skipped. The lock file (``skills.lock``) is rewritten beside the
    manifest on every successful run.
    """
    toml_path = manifest if manifest is not None else (Path.cwd() / SKILLS_TOML_FILENAME)
    install_scope = _parse_scope(scope)
    try:
        outcomes = sync_skills(
            toml_path.resolve(),
            scope=install_scope,
            workdir=Path.cwd(),
            strict_lint=strict,
            accept_risk=accept_risk,
        )
    except (SkillsTomlError, SkillLifecycleError) as exc:
        console.print(f"[red]sync failed:[/red] {exc}")
        raise SystemExit(1) from exc

    if not outcomes:
        console.print("[dim]no skills declared in manifest[/dim]")
        return
    for outcome in outcomes:
        marker = {
            "installed": "[green]+ installed[/green]",
            "updated": "[yellow]~ updated[/yellow]",
            "unchanged": "[dim]= unchanged[/dim]",
        }.get(outcome.action, outcome.action)
        console.print(f"{marker} {outcome.name} ({outcome.digest.digest[:12]}...)")


@skills_group.command("lint")
@click.argument("names", nargs=-1)
@click.option(
    "--scope",
    "scope",
    type=click.Choice(["project", "user"]),
    default="project",
    help="Lint installed skills from project or user scope.",
)
def skills_lint(names: tuple[str, ...], scope: str) -> None:
    """Advisory lint: validate frontmatter, references, sensitive patterns.

    Lint is non-blocking in this release. Exit code is 0 even when
    errors are reported; the operator decides whether to act on them.
    """
    from bernstein.core.skills.lifecycle import scope_root

    install_scope = _parse_scope(scope)
    root = scope_root(install_scope, workdir=Path.cwd())
    if not root.is_dir():
        console.print(f"[dim]no installed skills under {root}[/dim]")
        return

    targets: list[Path] = [root / name for name in names] if names else sorted(p for p in root.iterdir() if p.is_dir())

    all_findings = 0
    for target in targets:
        if not target.is_dir():
            console.print(f"[red]missing[/red] {target.name} (not installed)")
            all_findings += 1
            continue
        findings = lint_skill(target)
        if not findings:
            console.print(f"[green]ok[/green] {target.name}")
            continue
        all_findings += len(findings)
        console.print(f"[bold]{target.name}[/bold]")
        for finding in findings:
            tag = "[red]error[/red]" if finding.severity is LintSeverity.ERROR else "[yellow]warn[/yellow]"
            console.print(f"  {tag} {finding.code}: {finding.message}")

    if all_findings:
        console.print(f"\n[dim]{all_findings} finding(s) total (advisory only)[/dim]")


@skills_group.command("test")
@click.argument("suite", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--skills-root",
    "skills_root",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Skill root to test. Defaults to <cwd>/.bernstein/skills.",
)
def skills_test(suite: Path, skills_root: Path | None) -> None:
    """Run a deterministic trigger-set suite without model calls."""
    from bernstein.core.skills.authoring import SkillAuthoringError, run_trigger_suite

    root = skills_root if skills_root is not None else Path.cwd() / ".bernstein" / "skills"
    try:
        result = run_trigger_suite(root.resolve(), suite.resolve())
    except SkillAuthoringError as exc:
        console.print(f"[red]test failed:[/red] {exc}")
        raise SystemExit(1) from exc

    for case_result in result.cases:
        if case_result.passed:
            console.print(f"[green]ok[/green] {case_result.case.name}")
            continue
        details: list[str] = []
        if case_result.missing:
            details.append("missing: " + ", ".join(case_result.missing))
        if case_result.unexpected:
            details.append("unexpected: " + ", ".join(case_result.unexpected))
        console.print(f"[red]fail[/red] {case_result.case.name} ({'; '.join(details)})")

    total = len(result.cases)
    console.print(f"\n{result.passed_count} passed / {total} case(s)")
    if not result.passed:
        raise SystemExit(1)


@skills_group.command("diff")
@click.argument("left", type=click.Path(exists=True, path_type=Path))
@click.argument("right", type=click.Path(exists=True, path_type=Path))
def skills_diff(left: Path, right: Path) -> None:
    """Compare two skill directories using canonical manifest/body digests."""
    from bernstein.core.skills.authoring import diff_skill_dirs
    from bernstein.core.skills.lifecycle import SkillLifecycleError

    try:
        result = diff_skill_dirs(left.resolve(), right.resolve())
    except SkillLifecycleError as exc:
        console.print(f"[red]diff failed:[/red] {exc}")
        raise SystemExit(1) from exc

    if not result.changed:
        console.print(f"[green]unchanged[/green] digest {result.left_digest.digest}")
        return

    console.print("[yellow]changed[/yellow] " + ", ".join(result.changed_sections))
    console.print(f"left:  {result.left_digest.digest}")
    console.print(f"right: {result.right_digest.digest}")
    raise SystemExit(1)


@skills_group.command("bench")
@click.argument("suite", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--skills-root",
    "skills_root",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Skill root to bench. Defaults to <cwd>/.bernstein/skills.",
)
@click.option(
    "--iterations",
    "iterations",
    type=click.IntRange(min=1),
    default=10,
    show_default=True,
    help="Number of deterministic trigger-set iterations.",
)
def skills_bench(suite: Path, skills_root: Path | None, iterations: int) -> None:
    """Benchmark a deterministic trigger-set suite without model calls."""
    from bernstein.core.skills.authoring import SkillAuthoringError, bench_trigger_suite

    root = skills_root if skills_root is not None else Path.cwd() / ".bernstein" / "skills"
    try:
        result = bench_trigger_suite(root.resolve(), suite.resolve(), iterations=iterations)
    except SkillAuthoringError as exc:
        console.print(f"[red]bench failed:[/red] {exc}")
        raise SystemExit(1) from exc

    console.print(
        f"{result.iterations} iteration(s), {result.elapsed_seconds:.6f}s, "
        f"{result.suite.passed_count}/{len(result.suite.cases)} case(s) passing"
    )
    if not result.suite.passed:
        raise SystemExit(1)


@skills_group.command("helpfulness")
def skills_helpfulness() -> None:
    """Rebuild the local skill helpfulness report."""
    from bernstein.core.skills.helpfulness import build_helpfulness_report, write_helpfulness_report

    report = build_helpfulness_report(Path.cwd())
    path = write_helpfulness_report(Path.cwd(), report=report)
    console.print(f"[green]wrote[/green] {path}")
    if not report.skills:
        console.print("[dim]no matched skill activations[/dim]")
        return
    ranked = sorted(report.skills.values(), key=lambda item: (-item.posterior_mean, item.skill))
    for item in ranked[:10]:
        console.print(
            f"{item.skill}: {item.posterior_mean:.2f} "
            f"({item.successes}/{item.observations} successful, {item.failures} failed)"
        )
    if report.unmatched_activations:
        console.print(f"[dim]{report.unmatched_activations} activation(s) had no task outcome yet[/dim]")


@skills_group.command("bisect")
@click.argument("task_id")
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    default=False,
    help="Print the bisect plan as JSON.",
)
def skills_bisect(task_id: str, json_output: bool) -> None:
    """Build a local skill-activation bisect plan for a task."""
    from bernstein.core.skills.bisect import SkillBisectError, build_skill_bisect_plan

    try:
        plan = build_skill_bisect_plan(Path.cwd(), task_id)
    except SkillBisectError as exc:
        console.print(f"[red]bisect failed:[/red] {exc}")
        raise SystemExit(1) from exc

    payload = plan.as_payload()
    if json_output:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    console.print(f"[bold]task[/bold] {plan.task_id}: {plan.outcome}; {plan.candidate_count} candidate skill(s)")
    console.print("[bold]next probe[/bold]")
    console.print("disable: " + (", ".join(plan.next_probe.disable) or "(none)"))
    console.print("keep: " + (", ".join(plan.next_probe.keep) or "(none)"))
    for candidate in plan.candidates:
        console.print(
            f"- {candidate.skill} ({candidate.trigger_source or 'unknown'}, role={candidate.role or 'unknown'})"
        )


@skills_group.command("watch")
@click.argument(
    "path",
    type=click.Path(path_type=Path),
    required=False,
)
def skills_watch(path: Path | None) -> None:
    """Hot-reload the skill index on filesystem change.

    Watches the project's ``.bernstein/skills/`` directory by default.
    Pass an explicit path to watch a different root (e.g. an in-tree
    ``templates/skills/``). Press Ctrl-C to stop.
    """
    from bernstein.core.skills.watcher import start_skill_watcher

    watch_path = (path or Path.cwd() / ".bernstein" / "skills").resolve()
    console.print(f"[cyan]watching[/cyan] {watch_path}")

    reload_count = 0

    def on_reload(loader: SkillLoader) -> None:
        nonlocal reload_count
        del loader
        reload_count += 1
        console.print(f"[green]reloaded[/green] index (event #{reload_count})")

    handle = start_skill_watcher(watch_path, on_reload)
    stopped = False

    def _shutdown(_signum: int, _frame: object) -> None:
        nonlocal stopped
        stopped = True

    # Save the previous SIGINT handler so subsequent in-process CLI calls
    # (and tests that run the watch command in-process) see the original
    # disposition restored when this command returns.
    previous_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, _shutdown)
    try:
        while not stopped:
            time.sleep(0.2)
    finally:
        # ``getsignal`` may return a value (e.g. an opaque C handler) that
        # ``signal.signal`` cannot accept; leaving the current handler in
        # place is preferable to crashing the cleanup path.
        with contextlib.suppress(TypeError, ValueError):
            signal.signal(signal.SIGINT, previous_sigint)
        handle.stop()
        console.print("[dim]watcher stopped[/dim]")


from bernstein.cli.commands.skill_cmd import skill_provenance_cmd, skill_verify_cmd  # noqa: E402

skills_group.add_command(skill_provenance_cmd, "provenance")
skills_group.add_command(skill_verify_cmd, "verify")
