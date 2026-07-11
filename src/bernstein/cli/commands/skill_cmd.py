"""``bernstein skill``: usage-provenance surface for installed skills.

Issue #2301. Complements the catalog install/lockfile surface with the
usage-attestation layer:

    bernstein skill provenance <skill>   # verified runs + artifacts a skill fed
    bernstein skill verify <skill>       # recompute install receipt + usage links

``<skill>`` is either a catalog entry id (resolved to its installed content
digest via ``skills.lock``) or a raw content digest. Provenance counts only
runs whose journal head still verifies; the count is recomputed from those
heads on every call, never read from a stored counter.
"""

from __future__ import annotations

from pathlib import Path

import click

from bernstein.cli.helpers import console


def _load_hmac_key() -> bytes:
    from bernstein.core.security.audit import load_or_create_audit_key

    return load_or_create_audit_key()


def _lineage_root(workdir: Path) -> Path:
    return workdir / ".sdd" / "lineage"


def _resolve_skill_hash(workdir: Path, skill: str) -> tuple[str, str | None]:
    """Return ``(skill_hash, manifest_hash)`` for ``skill``.

    ``skill`` may be a catalog entry id or a raw content digest. When it
    matches a lockfile row, the row's ``content_digest`` and
    ``manifest_sha256`` are returned; otherwise the input is treated as a
    raw content digest with no known manifest hash.
    """
    from bernstein.core.skills.catalog.lockfile import CATALOG_LOCK_FILENAME, read_state

    state = read_state(workdir / CATALOG_LOCK_FILENAME)
    row = state.find_catalog(skill)
    if row is not None:
        return row.content_digest, row.manifest_sha256
    return skill, None


@click.group("skill")
def skill_group() -> None:
    """Inspect the usage provenance of installed skills.

    \b
      bernstein skill provenance code-review   # verified runs + artifacts
      bernstein skill verify code-review        # recompute install receipt
    """


@skill_group.command("provenance")
@click.argument("skill", required=True)
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
def skill_provenance_cmd(skill: str, workdir: str) -> None:
    """Print the recomputable usage-provenance graph for *skill*.

    Exit codes: 0 = graph rendered (may be empty), 1 = bad input.
    """
    from rich.table import Table

    from bernstein.core.skills.provenance import provenance_graph

    root = Path(workdir).resolve()
    skill_hash, _ = _resolve_skill_hash(root, skill)

    graph = provenance_graph(
        workdir=root,
        lineage_root=_lineage_root(root),
        hmac_key=_load_hmac_key(),
        skill_hash=skill_hash,
    )

    console.print()
    console.print(
        f"[bold]Skill provenance[/bold] skill={skill} hash={skill_hash[:16]} verified_runs={graph.verified_run_count}"
    )
    if not graph.runs:
        console.print("[dim]No recorded usage for this skill.[/dim]")
        return

    table = Table(header_style="bold cyan")
    table.add_column("Run")
    table.add_column("Journal head")
    table.add_column("Verified")
    table.add_column("Reason")
    for run in graph.runs:
        table.add_row(
            run.run_id,
            run.journal_head[:16],
            "[green]yes[/green]" if run.verified else "[red]no[/red]",
            run.reason or "-",
        )
    console.print(table)


@skill_group.command("verify")
@click.argument("skill", required=True)
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
def skill_verify_cmd(skill: str, workdir: str) -> None:
    """Recompute *skill*'s install receipt and usage links.

    Detects a manifest-hash drift between the receipt and the currently
    installed content (AC5).

    Exit codes: 0 = verified, 1 = bad input / no receipt, 2 = mismatch.
    """
    from bernstein.core.skills.provenance import verify_install

    root = Path(workdir).resolve()
    skill_hash, manifest_hash = _resolve_skill_hash(root, skill)
    if manifest_hash is None:
        console.print(
            f"[red]No lockfile row for[/red] {skill!r}; cannot recompute the "
            "installed manifest hash. Pass a catalog entry id installed via "
            "`bernstein skills catalog install`."
        )
        raise SystemExit(1)

    result = verify_install(
        workdir=root,
        lineage_root=_lineage_root(root),
        hmac_key=_load_hmac_key(),
        skill_hash=skill_hash,
        installed_manifest_hash=manifest_hash,
    )
    console.print()
    console.print(f"[bold]Skill verify[/bold] skill={skill} hash={skill_hash[:16]}")
    if result.ok:
        console.print("[green]OK[/green] -- install receipt anchored, manifest hash matches.")
        raise SystemExit(0)
    if result.receipt is None:
        console.print(f"[yellow]NO RECEIPT[/yellow] -- {result.reason}")
        raise SystemExit(1)
    console.print(f"[red]MISMATCH[/red] -- {result.reason}")
    raise SystemExit(2)
