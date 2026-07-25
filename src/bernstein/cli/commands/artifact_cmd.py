"""``bernstein artifact``: verify, inspect and score outputs by artifact key.

Two commands answer a *task* question:

    bernstein artifact verify <task_id>

re-derives a non-coding task's canonical artifact hash from the stored bytes,
ties it to the signed entry, and runs the lineage gate (Ed25519 signature +
operator HMAC chain + parent integrity), so "the agent produced this exact
artifact" is a cryptographic check rather than a trust exercise (issue #2608).

The rest answer an *artifact* question, keyed by canonical artifact URI rather
than by the task that happened to produce it (issue #2559)::

    bernstein artifact list
    bernstein artifact log <uri>
    bernstein artifact health <uri>

``log`` is the attribution surface: which agent identity, running which model,
produced the current tip of this PR / package / deployment, read off the chain.
``health`` is the rolled-up verdict -- chain integrity, a single current set of
bytes, evidence, cadence -- recomputed offline from ``.sdd`` state alone. Its
``--json`` output is produced by the same function the server route calls, so
the CLI and the dashboard cannot disagree about an artifact.

This is the singular ``artifact`` group; the plural ``artifacts`` group lists
agent-posted, journal-anchored task artifacts (issue #2553) and is unrelated.
"""

from __future__ import annotations

import os
from pathlib import Path

import click

from bernstein.cli.helpers import console

#: Exit code for a red / unverifiable verdict. Matches ``artifact verify`` so a
#: script can treat any non-zero from this group as "do not trust this output".
_EXIT_BAD = 2


@click.group("artifact")
def artifact_group() -> None:
    """Verify, inspect and score outputs by artifact key.

    \b
      bernstein artifact verify <task_id>
      bernstein artifact list
      bernstein artifact log <uri>
      bernstein artifact health <uri>
    """


@artifact_group.command("verify")
@click.argument("task_id")
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True, path_type=Path),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
@click.option(
    "--operator-secret-env",
    default="BERNSTEIN_OPERATOR_SECRET",
    show_default=True,
    help="Env var holding the operator HMAC secret. Falls back to the audit key.",
)
@click.option("--output-json", is_flag=True, help="Emit a JSON verdict instead of human text.")
def artifact_verify_cmd(task_id: str, workdir: Path, operator_secret_env: str, output_json: bool) -> None:
    """Re-derive TASK_ID's canonical hash and verify its signed lineage record.

    Exit codes: 0 = verified, 2 = tampered / missing / unverifiable.
    """
    import json
    import sys

    from bernstein.core.lineage.artifact_record import ARTIFACT_SINK_RELPATH, verify_artifact

    sdd = workdir.resolve() / ".sdd"
    sink_root = workdir.resolve() / ARTIFACT_SINK_RELPATH
    log_path = sdd / "lineage" / "log.jsonl"
    cards_dir = sdd / "agents"

    operator_secret = _resolve_operator_secret(operator_secret_env)

    result = verify_artifact(
        task_id=task_id,
        sink_root=sink_root,
        log_path=log_path,
        cards_dir=cards_dir,
        operator_secret=operator_secret,
    )

    if output_json:
        click.echo(json.dumps(_json_verdict(result)))
    elif result.ok:
        console.print(f"[green]VERIFIED[/green] task={task_id}")
        console.print(f"  content_hash  {result.content_hash}")
        console.print(f"  entry_hash    {result.entry_hash}")
        _render_figures(result)
    else:
        console.print(f"[red]TAMPERED[/red] task={task_id}")
        for failure in result.failures:
            console.print(f"  - {failure}")
        _render_figures(result)

    if not result.ok:
        sys.exit(2)


_workdir_option = click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True, path_type=Path),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)


def _spine_hmac_key() -> bytes:
    """Return the audit-chain key the lineage spine tags entries with."""
    from bernstein.core.security.audit import load_or_create_audit_key

    return load_or_create_audit_key()


@artifact_group.command("list")
@_workdir_option
@click.option("--output-json", is_flag=True, help="Emit JSON instead of human text.")
def artifact_list_cmd(workdir: Path, output_json: bool) -> None:
    """List every artifact key the local lineage spines carry."""
    import json

    from bernstein.core.lineage.artifact_health import list_artifact_keys

    counts = list_artifact_keys(workdir.resolve())
    ordered = sorted(counts.items())

    if output_json:
        click.echo(json.dumps({"artifacts": [{"productions": n, "uri": u} for u, n in ordered]}, sort_keys=True))
        return
    if not ordered:
        console.print("[yellow]no artifacts recorded[/yellow]")
        return
    for uri, count in ordered:
        console.print(f"  {count:>4}  {uri}")


@artifact_group.command("log")
@click.argument("uri")
@_workdir_option
@click.option("--limit", type=int, default=0, show_default=True, help="Max records (0 = all).")
@click.option("--output-json", is_flag=True, help="Emit JSON instead of human text.")
def artifact_log_cmd(uri: str, workdir: Path, limit: int, output_json: bool) -> None:
    """Show who produced URI, newest first, with the entry hash that proves it.

    The first record is the current tip: the agent identity and model behind the
    bytes that are live right now. ``verified`` is recomputed per entry, so a
    tampered row is named here instead of being averaged away.

    Recorded *attempts* are listed after the productions: tasks that declared
    this artifact and did not deliver it. An empty production list next to a
    populated attempt list is the answer to "did anything try?", which used to
    require reading run directories by hand.
    """
    from bernstein.core.lineage.artifact_health import artifact_attempts, artifact_log, artifact_log_json

    key = _spine_hmac_key()
    records = artifact_log(workdir.resolve(), uri, hmac_key=key, limit=limit)
    attempts = artifact_attempts(workdir.resolve(), uri, hmac_key=key, limit=limit)

    if output_json:
        click.echo(artifact_log_json(records, uri=uri, attempts=attempts))
        return
    if not records and not attempts:
        console.print(f"[yellow]nothing recorded for[/yellow] {uri}")
        return
    console.print(f"[bold]{uri}[/bold]")
    if not records:
        console.print("  [yellow]no productions recorded[/yellow]")
    for i, record in enumerate(records):
        marker = "[green]OK[/green]" if record.verified else "[red]TAMPERED[/red]"
        label = "tip" if i == 0 else "   "
        console.print(f"  {label} {marker} {record.entry_hash}")
        console.print(f"        actor={record.actor or '-'} model={record.model or '-'} run={record.run_id}")
        console.print(f"        content={record.content_hash} step={record.step_id or '-'}")
    for attempt in attempts:
        marker = "[green]OK[/green]" if attempt.verified else "[red]TAMPERED[/red]"
        console.print(f"  [yellow]attempt[/yellow] {marker} {attempt.entry_hash}")
        console.print(f"        task={attempt.task_id or '-'} outcome={attempt.outcome or '-'}")
        console.print(f"        actor={attempt.actor or '-'} model={attempt.model or '-'} run={attempt.run_id}")


@artifact_group.command("health")
@click.argument("uri")
@_workdir_option
@click.option(
    "--at",
    type=int,
    default=None,
    help="Evaluation instant, in the unit the spine timestamps use. Defaults to the wall clock. "
    "Pin it to reproduce a verdict byte-for-byte.",
)
@click.option(
    "--cadence-seconds",
    type=int,
    default=None,
    help="Declared refresh cadence. Omit when the artifact declares none; the cadence leg then reports "
    "not_applicable rather than failing.",
)
@click.option("--output-json", is_flag=True, help="Emit the canonical JSON verdict instead of human text.")
def artifact_health_cmd(
    uri: str,
    workdir: Path,
    at: int | None,
    cadence_seconds: int | None,
    output_json: bool,
) -> None:
    """Recompute URI's health verdict offline from local .sdd state.

    Exit codes: 0 = green or amber, 2 = red.

    The verdict is a pure function of the collected state and the evaluation
    instant, and the JSON comes from the same function the server route calls,
    so a verdict recomputed here equals the one the dashboard shows for the
    same state and instant.
    """
    import json
    import sys
    import time

    from bernstein.core.lineage.artifact_health import RED, artifact_health_json

    instant = at if at is not None else int(time.time())
    payload = artifact_health_json(
        workdir.resolve(),
        uri,
        hmac_key=_spine_hmac_key(),
        at=instant,
        cadence_seconds=cadence_seconds,
    )
    verdict = json.loads(payload)

    if output_json:
        click.echo(payload)
    else:
        _render_health(verdict)

    if verdict["verdict"] == RED:
        sys.exit(_EXIT_BAD)


def _render_health(verdict: dict) -> None:
    """Render a verdict document as human text."""
    colour = {"green": "green", "amber": "yellow", "red": "red"}.get(str(verdict["verdict"]), "white")
    console.print(f"[{colour}]{str(verdict['verdict']).upper()}[/{colour}] {verdict['uri']}")
    tip = verdict.get("tip") or {}
    if tip.get("entry_hash"):
        console.print(f"  tip     {tip['entry_hash']}")
        console.print(f"  actor   {tip.get('actor') or '-'}  model {tip.get('model') or '-'}")
    for leg in verdict.get("legs", []):
        status = str(leg["status"])
        mark = {
            "pass": "[green]pass[/green]",
            "fail": "[red]fail[/red]",
            "stale": "[yellow]stale[/yellow]",
        }.get(status, f"[dim]{status}[/dim]")
        console.print(f"  {mark:<24} {leg['name']}: {leg['detail']}")


def _json_verdict(result: object) -> dict:
    """Assemble the JSON verdict, including the per-figure provenance section."""
    payload = {
        "task_id": result.task_id,  # type: ignore[attr-defined]
        "ok": result.ok,  # type: ignore[attr-defined]
        "content_hash": result.content_hash,  # type: ignore[attr-defined]
        "entry_hash": result.entry_hash,  # type: ignore[attr-defined]
        "failures": result.failures,  # type: ignore[attr-defined]
    }
    figures = getattr(result, "figures", None)
    if figures is not None:
        payload["figures"] = {
            "ok": figures.ok,
            "provenances": [
                {"label": p.label, "value": p.value, "ok": p.ok, "statement": p.statement} for p in figures.provenances
            ],
            "unanchored": [
                {"surface": u.surface, "category": u.category, "line": u.line, "col": u.col} for u in figures.unanchored
            ],
        }
    return payload


def _render_figures(result: object) -> None:
    """Render the per-figure provenance statement below the verdict (issue #2888)."""
    figures = getattr(result, "figures", None)
    if figures is None or not figures.has_figures:
        return
    console.print("  figures:")
    for p in figures.provenances:
        marker = "[green]OK[/green]" if p.ok else "[red]FAIL[/red]"
        label = p.label or p.value
        console.print(f"    {marker} {label} ({p.value}) - {p.statement}")
    for u in figures.unanchored:
        console.print(f"    [red]UNANCHORED[/red] {u.surface} ({u.category}) at line {u.line}, col {u.col}")


def _resolve_operator_secret(env_var: str) -> bytes | None:
    """Resolve the operator HMAC secret: env var first, then the audit key.

    Returns ``None`` only when neither source yields a key, in which case the
    HMAC leg of verification is skipped (signature + chain are still enforced).
    """
    secret = os.environ.get(env_var)
    if secret:
        return secret.encode("utf-8")
    try:
        from bernstein.core.security.audit import load_or_create_audit_key

        return load_or_create_audit_key()
    except Exception:  # pragma: no cover - defensive: never block verification on key IO
        return None


__all__ = ["artifact_group"]
