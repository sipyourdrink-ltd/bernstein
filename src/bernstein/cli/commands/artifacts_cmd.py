"""``bernstein artifacts``: agent-posted task artifacts (issue #2553).

A worker in the middle of a run can attach the substance of its work to its own
task: a markdown report, a comparison table, the URL of a deployed preview. Each
artifact is content-addressed in the evidence store, sealed into the lineage
spine, and appended to the task's Merkle-chained journal, so the record IS the
receipt: strip the anchors and it is just bytes; anchored, it recomputes offline
from the stored blob alone.

    bernstein artifacts list <task>          List every posted version + verify state.
    bernstein artifacts show <task> <key>    Render a key's latest version + history.

``bernstein audit verify`` runs the same integrity check across every artifact,
so a tampered blob is detected exactly like a tampered chain entry.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from bernstein.cli.helpers import console


def _load_hmac_key() -> bytes:
    from bernstein.core.security.audit import load_or_create_audit_key

    return load_or_create_audit_key()


@click.group("artifacts")
@click.pass_context
def artifacts_group(ctx: click.Context) -> None:
    """[Deprecated] List and render agent-posted task artifacts (use 'bernstein artifact').

    \b
      bernstein artifacts list <task>
      bernstein artifacts show <task> <key>
    """
    if ctx.invoked_subcommand is not None:
        click.echo(
            "WARNING: 'bernstein artifacts' is deprecated and will be removed in v4.0.0 (#3138): "
            "use 'bernstein artifact' instead.",
            err=True,
        )


@artifacts_group.command("list")
@click.argument("task")
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
@click.option("--output-json", is_flag=True, help="Emit JSON instead of human text.")
def artifacts_list_cmd(task: str, workdir: str, output_json: bool) -> None:
    """List every artifact version posted against TASK, with verify state.

    Exit code 0 when artifacts exist, 1 when there are none, 2 when tampering
    hid every row. ``--output-json`` emits one stable document on stdout for all
    three, so a caller reads the verdict without parsing the table:
    ``{"artifacts": [...], "reason": <str|null>, "task": ..., "verified": bool}``.
    A row that fails its blob or spine check leaves the exit code at 0 -- the
    listing rendered -- and reports ``verified: false``, exactly as the table
    marks that row tampered.
    """
    from rich.table import Table

    from bernstein.core.evidence.run_artifacts import read_artifact_rows, verify_run_artifacts

    sdd = Path(workdir).resolve() / ".sdd"
    # Read WITHOUT the fail-closed filter so a tampered journal renders as
    # tampered, not as "no artifacts". Verdicts drive the displayed state.
    records = read_artifact_rows(sdd, task, verify=False)
    verdict_list = verify_run_artifacts(sdd, task, hmac_key=_load_hmac_key())
    verdicts = {(r.key, r.version): r for r in verdict_list}

    def _emit_json(rows: list[dict[str, object]], *, verified: bool, reason: str | None) -> None:
        click.echo(json.dumps({"artifacts": rows, "reason": reason, "task": task, "verified": verified}))

    if not records:
        tampered = [v for v in verdict_list if not v.ok]
        if tampered:
            if output_json:
                _emit_json([], verified=False, reason=tampered[0].reason)
            else:
                console.print(f"[red]TAMPERED[/red] task={task} -- {tampered[0].reason}")
            raise SystemExit(2)
        if output_json:
            _emit_json([], verified=True, reason=None)
        else:
            console.print(f"[yellow]No artifacts found for task[/yellow] {task}")
        raise SystemExit(1)

    if output_json:
        rows: list[dict[str, object]] = []
        failures: list[str] = []
        for record in records:
            verdict = verdicts.get((record.key, record.version))
            ok = verdict.ok if verdict is not None else False
            if not ok:
                failures.append((verdict.reason if verdict is not None else None) or "no verification result")
            rows.append(
                {
                    "content_hash": record.content_hash,
                    "journal_index": record.journal_index,
                    "key": record.key,
                    "link_kind": record.link_kind,
                    "type": record.artifact_type,
                    "verified": ok,
                    "version": record.version,
                }
            )
        _emit_json(rows, verified=not failures, reason=failures[0] if failures else None)
        return

    console.print()
    console.print(f"[bold]Artifacts[/bold] task={task} ({len(records)} version(s))")
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Key", style="bold")
    table.add_column("Ver", justify="right")
    table.add_column("Type")
    table.add_column("Idx", justify="right")
    table.add_column("Verify")
    table.add_column("Content hash", style="dim")
    for record in records:
        verdict = verdicts.get((record.key, record.version))
        ok = verdict.ok if verdict is not None else False
        state = "[green]ok[/green]" if ok else "[red]tampered[/red]"
        kind = record.artifact_type + (f":{record.link_kind}" if record.link_kind else "")
        table.add_row(
            record.key,
            str(record.version),
            kind,
            str(record.journal_index),
            state,
            record.content_hash.split(":", 1)[-1][:16] + "...",
        )
    console.print(table)
    console.print("\n[dim]Verify offline:[/dim] bernstein audit verify\n")


@artifacts_group.command("show")
@click.argument("task")
@click.argument("key")
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
def artifacts_show_cmd(task: str, key: str, workdir: str) -> None:
    """Render KEY's latest version for TASK, plus its version history.

    A version whose stored blob fails its hash check renders as *tampered*, not
    as content. Exit codes: 0 = rendered, 1 = no such artifact, 2 = tampered.
    """
    from bernstein.core.evidence.bundle import EvidenceStore
    from bernstein.core.evidence.run_artifacts import read_artifact_rows, verify_run_artifacts
    from bernstein.core.lineage.spine import content_hash_of

    sdd = Path(workdir).resolve() / ".sdd"
    # Unverified read so a broken journal reaches the tampered (exit 2) path
    # instead of masquerading as a missing artifact.
    verdict_list = verify_run_artifacts(sdd, task, hmac_key=_load_hmac_key())
    verdicts = {(r.key, r.version): r for r in verdict_list}
    records = [r for r in read_artifact_rows(sdd, task, verify=False) if r.key == key]
    if not records:
        tampered = [v for v in verdict_list if not v.ok]
        if tampered:
            console.print(f"[red]TAMPERED[/red] task={task} -- {tampered[0].reason}")
            raise SystemExit(2)
        console.print(f"[yellow]No artifact[/yellow] key={key} for task {task}")
        raise SystemExit(1)
    latest = records[-1]
    verdict = verdicts.get((latest.key, latest.version))
    ok = verdict.ok if verdict is not None else False

    console.print()
    console.print(f"[bold]Artifact[/bold] task={task} key={key} version={latest.version}")
    console.print(f"  type            {latest.artifact_type}")
    console.print(f"  content_hash    {latest.content_hash}")
    console.print(f"  spine_entry     {latest.spine_entry_hash}")
    console.print(f"  journal_index   {latest.journal_index}")
    if latest.prev_version_hash:
        console.print(f"  prev_version    {latest.prev_version_hash}")

    if not ok:
        reason = verdict.reason if verdict is not None else "no verification result"
        console.print(f"\n[red]TAMPERED[/red] -- {reason}")
        console.print("[red]Content withheld: the stored blob does not recompute from the seal.[/red]\n")
        raise SystemExit(2)

    blob = EvidenceStore(sdd / "evidence").get(latest.content_hash)
    console.print("\n[bold]Content[/bold]")
    if blob is not None and content_hash_of(blob) == latest.content_hash:
        console.print(json.loads(blob.decode("utf-8")))

    if len(records) > 1:
        console.print("\n[bold]Version history[/bold]")
        for record in records:
            marker = " (latest)" if record.version == latest.version else ""
            console.print(f"  v{record.version}{marker}  idx={record.journal_index}  {record.content_hash}")
    console.print()


__all__ = ["artifacts_group"]
