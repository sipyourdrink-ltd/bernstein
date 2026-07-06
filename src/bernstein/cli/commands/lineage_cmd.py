"""``bernstein lineage`` -- per-artifact lineage trail commands.

Every agent file write emits a signed lineage record linking the output
(path + byte range + sha-256) back to its producer, the prompt SHA, the
model name, the cost, and the input artefacts. Schema v2 adds a
``regulatory_class`` field plus a customer-key Ed25519 signature
(RFC 8037 / EdDSA) for DORA, NIS2, and EU AI Act Article 12 evidence.

Two surfaces:

* ``bernstein lineage <file>:<line>`` (legacy positional form) -- walks
  the lineage chain back from a file/line to the producing agent. This
  invocation existed before the regulator-class extension and is kept
  for back-compat.
* ``bernstein lineage walk <file>:<line>`` -- explicit form of the
  above; preferred in scripts to avoid colliding with subcommand names.
* ``bernstein lineage export <run_id> --format <csv|jsonld|html>`` --
  produce a regulator-shaped artefact for an audit package.
* ``bernstein lineage verify <run_id>`` -- one-shot chain verification;
  exits 0 only when every record validates.

Operator guide: docs/compliance/lineage-export.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import click
from rich.table import Table

from bernstein.cli.commands.lineage_export_cmd import lineage_export_cmd
from bernstein.cli.commands.lineage_replay_cmd import lineage_replay_cmd
from bernstein.cli.commands.lineage_tracker_audit_cmd import tracker_audit_cmd
from bernstein.cli.commands.lineage_verify_cmd import lineage_verify_cmd
from bernstein.cli.helpers import console


def _parse_target(target: str) -> tuple[str, int | None]:
    """Split ``"path/to/file.py:42"`` into ``("path/to/file.py", 42)``.

    A bare path returns ``(path, None)`` -- the lookup then matches
    every record for the file regardless of line.
    """
    if ":" not in target:
        return target, None
    path, _, suffix = target.rpartition(":")
    if not path:
        return target, None
    try:
        line = int(suffix)
    except ValueError:
        return target, None
    return path, line


class _LineageGroup(click.Group):
    """Group that preserves the legacy ``bernstein lineage <file>:<line>`` form.

    Without this override, ``bernstein lineage src/foo.py:42`` would
    fail with ``No such command 'src/foo.py:42'``. We rewrite the
    args so click-internally invokes the ``walk`` subcommand whenever
    the first positional token is not a registered subcommand name.
    """

    def resolve_command(
        self,
        ctx: click.Context,
        args: list[str],
    ) -> tuple[str | None, click.Command | None, list[str]]:
        if args and not args[0].startswith("-") and args[0] not in self.commands:
            args = ["walk", *args]
        return super().resolve_command(ctx, args)


@click.group(name="lineage", cls=_LineageGroup, invoke_without_command=True)
@click.pass_context
def lineage_cmd(ctx: click.Context) -> None:
    """Per-artifact lineage trail (output -> producer + inputs).

    Records are signed with the customer-supplied Ed25519 key (RFC 8037).
    Use ``bernstein lineage verify`` in CI to fail any run whose chain
    breaks; cite: docs/compliance/lineage-export.md.

    \b
    Examples:
      bernstein lineage src/foo.py:42
      bernstein lineage walk src/foo.py:42
      bernstein lineage export <run_id> --format html --output /tmp/x.html
      bernstein lineage verify <run_id> --public-key /etc/customer-pub.pem
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@lineage_cmd.command(name="walk")
@click.argument("target", required=True)
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
@click.option(
    "--run",
    "run_id",
    default=None,
    help="Restrict to a single run id (default: all runs in the WAL directory).",
)
@click.option(
    "--limit",
    type=int,
    default=20,
    show_default=True,
    help="Maximum number of records to display.",
)
def walk_cmd(target: str, workdir: str, run_id: str | None, limit: int) -> None:
    """Walk the lineage chain backwards from ``<file>[:<line>]``."""
    from bernstein.core.persistence.lineage import LineageReader

    sdd_dir = Path(workdir).resolve() / ".sdd"
    if not sdd_dir.is_dir():
        console.print(f"[red]No .sdd directory at[/red] {sdd_dir}")
        raise SystemExit(1)

    path, line = _parse_target(target)

    records = LineageReader(sdd_dir).lookup(path, line, run_id=run_id)

    if not records:
        console.print(f"[yellow]No lineage records for[/yellow] {target}")
        return

    records = records[-limit:]

    where = f"{path}:{line}" if line is not None else path
    console.print()
    console.print(f"[bold]Lineage trail for[/bold] {where} ({len(records)} record(s))")
    console.print()

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Time", style="dim", no_wrap=True)
    table.add_column("Producer", no_wrap=True)
    table.add_column("Run", no_wrap=True)
    table.add_column("Prompt SHA", style="dim", no_wrap=True)
    table.add_column("Inputs", overflow="fold")
    table.add_column("Model", no_wrap=True)
    table.add_column("Tokens", justify="right")
    table.add_column("Cost USD", justify="right")
    table.add_column("Reg. class", no_wrap=True)
    table.add_column("Cust. sig", no_wrap=True)

    for record in records:
        ts = f"{record.timestamp:.0f}" if record.timestamp else "-"
        inputs_str = ", ".join(a.path for a in record.inputs) or "-"
        prompt_short = record.prompt_sha[:12] + "..." if record.prompt_sha else "-"
        sig_short = "yes" if record.customer_signature else "-"
        table.add_row(
            ts,
            record.producer.agent_id,
            record.producer.run_id,
            prompt_short,
            inputs_str,
            record.model or "-",
            str(record.tokens),
            f"{record.cost_usd:.4f}",
            record.regulatory_class or "-",
            sig_short,
        )

    console.print(table)
    console.print()


lineage_cmd.add_command(lineage_export_cmd, "export")
lineage_cmd.add_command(lineage_verify_cmd, "verify")
lineage_cmd.add_command(lineage_replay_cmd, "replay")
lineage_cmd.add_command(tracker_audit_cmd, "tracker-audit")


# ── ADR-009 lineage v1 subcommands ──────────────────────────────────────────


@lineage_cmd.command(name="gate")
@click.option(
    "--log",
    "log_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path(".sdd/lineage/log.jsonl"),
    show_default=True,
    help="Lineage log path (ADR-009 §4).",
)
@click.option(
    "--cards",
    "cards_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path(".sdd/agents"),
    show_default=True,
    help="Agent cards directory.",
)
@click.option(
    "--steward-allowlist",
    default=None,
    help="Comma-separated agent_ids permitted to write merge entries.",
)
@click.option(
    "--operator-secret-env",
    default="BERNSTEIN_OPERATOR_SECRET",
    show_default=True,
    help="Env var holding the HMAC operator secret (optional).",
)
@click.option("--output-json", is_flag=True, help="Emit JSON instead of human text.")
def gate_cmd(
    log_path: Path,
    cards_dir: Path,
    steward_allowlist: str | None,
    operator_secret_env: str,
    output_json: bool,
) -> None:
    """Run the lineage v1 CI gate. Exits 1 on failure."""
    import json
    import os
    import sys

    from bernstein.core.lineage.gate import check as gate_check

    if not log_path.exists():
        if output_json:
            click.echo(json.dumps({"ok": True, "failures": [], "skipped": "log missing"}))
        else:
            console.print(f"[yellow]Lineage gate:[/yellow] SKIP (no log at {log_path})")
        return

    allow: frozenset[str] | None = None
    if steward_allowlist:
        allow = frozenset(s.strip() for s in steward_allowlist.split(",") if s.strip())

    secret = os.environ.get(operator_secret_env)
    operator_secret = secret.encode("utf-8") if secret else None

    result = gate_check(
        log_path=log_path,
        agent_cards_dir=cards_dir,
        operator_secret=operator_secret,
        steward_allowlist=allow,
    )
    if output_json:
        click.echo(json.dumps({"ok": result.ok, "failures": result.failures}, indent=2))
    elif result.ok:
        console.print("[green]Lineage gate:[/green] PASS")
    else:
        console.print(f"[red]Lineage gate:[/red] FAIL ({len(result.failures)} issue(s))")
        for fail in result.failures:
            console.print(f"  - {fail}")
    if not result.ok:
        sys.exit(1)


@lineage_cmd.command(name="forks")
@click.option(
    "--log",
    "log_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path(".sdd/lineage/log.jsonl"),
    show_default=True,
)
@click.option("--output-json", is_flag=True, help="Emit JSON output.")
def forks_cmd(log_path: Path, output_json: bool) -> None:
    """Report all unresolved forks in the lineage log."""
    import json

    from bernstein.cli.commands._lineage_v1_helpers import read_entries
    from bernstein.core.lineage.tips import detect_forks

    if not log_path.exists():
        if output_json:
            click.echo(json.dumps([]))
        else:
            console.print(f"[yellow]No log at {log_path}[/yellow]")
        return

    entries = read_entries(log_path)
    forks = detect_forks(entries)
    if output_json:
        payload = [
            {
                "artefact_path": f.artefact_path,
                "parent_hash": f.parent_hash,
                "child_hashes": list(f.child_hashes),
            }
            for f in forks
        ]
        click.echo(json.dumps(payload, indent=2))
        return
    if not forks:
        console.print("[green]No forks.[/green]")
        return
    console.print(f"[red]{len(forks)} fork(s) detected:[/red]")
    for f in forks:
        console.print(
            f"  - {f.artefact_path} @ parent={f.parent_hash[:24]}... "
            f"children={[c[:24] + '...' for c in f.child_hashes]}"
        )


@lineage_cmd.command(name="chain")
@click.argument("artefact_path", required=True)
@click.option(
    "--log",
    "log_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path(".sdd/lineage/log.jsonl"),
    show_default=True,
)
@click.option(
    "--cards",
    "cards_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path(".sdd/agents"),
    show_default=True,
)
def chain_cmd(artefact_path: str, log_path: Path, cards_dir: Path) -> None:
    """Verify the chain for a single artefact (ADR-009 §5.3)."""
    import sys

    from bernstein.cli.commands._lineage_v1_helpers import read_entries
    from bernstein.core.lineage.gate import check as gate_check
    from bernstein.core.lineage.tips import compute_tips

    if not log_path.exists():
        console.print(f"[yellow]No log at {log_path}[/yellow]")
        return

    entries = [e for e in read_entries(log_path) if e.artefact_path == artefact_path]
    if not entries:
        console.print(f"[yellow]No entries for {artefact_path}[/yellow]")
        sys.exit(1)
    # Reuse the full gate, then narrow output to this artefact.
    result = gate_check(log_path=log_path, agent_cards_dir=cards_dir)
    tips = compute_tips(entries).get(artefact_path, {"open": [], "merged": []})
    relevant = [f for f in result.failures if artefact_path in f]
    if relevant:
        console.print(f"[red]chain FAIL ({len(relevant)}):[/red]")
        for f in relevant:
            console.print(f"  - {f}")
        sys.exit(1)
    console.print(f"[green]chain OK[/green] ({len(entries)} entry(ies))")
    console.print(f"  open tips: {tips['open']}")
    console.print(f"  merged:    {tips['merged']}")


@lineage_cmd.command(name="reindex")
@click.option(
    "--log",
    "log_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path(".sdd/lineage/log.jsonl"),
    show_default=True,
)
def reindex_cmd(log_path: Path) -> None:
    """Rebuild by-artefact + tips projections from log.jsonl (§4 invariant)."""
    from bernstein.cli.commands._lineage_v1_helpers import reindex

    if not log_path.exists():
        console.print(f"[yellow]No log at {log_path}[/yellow]")
        return
    written = reindex(log_path)
    console.print(f"[green]Reindexed:[/green] {written} projection(s) under {log_path.parent}")


@lineage_cmd.command(name="conflicts")
@click.option(
    "--artefact",
    "artefact_filter",
    default=None,
    help="Restrict listing to a single artefact path.",
)
@click.option(
    "--log",
    "log_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path(".sdd/lineage/log.jsonl"),
    show_default=True,
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON output.")
def conflicts_cmd(artefact_filter: str | None, log_path: Path, as_json: bool) -> None:
    """List unresolved lineage forks in a human-readable, side-by-side form.

    A fork appears whenever two-or-more agents write distinct content for the
    same artefact off the same parent hash. The listing surfaces the artefact
    path, the competing entry hashes, the sibling agent ids, and the timestamp
    of each candidate so the operator can pick a merge policy.
    """
    import json as _json

    from bernstein.cli.commands._lineage_conflict_helpers import build_conflict_views
    from bernstein.cli.commands._lineage_v1_helpers import read_entries

    if not log_path.exists():
        if as_json:
            click.echo(_json.dumps([]))
        else:
            console.print(f"[yellow]No log at {log_path}[/yellow]")
        return

    entries = read_entries(log_path)
    views = build_conflict_views(entries, artefact_filter)
    if as_json:
        click.echo(_json.dumps([v.to_dict() for v in views], indent=2, sort_keys=True))
        return

    if not views:
        if artefact_filter is not None:
            console.print(f"[green]No unresolved forks for[/green] {artefact_filter}")
        else:
            console.print("[green]No unresolved forks.[/green]")
        return

    console.print(f"[red]{len(views)} unresolved fork(s):[/red]")
    for v in views:
        table = Table(
            title=v.artefact_path,
            show_header=True,
            header_style="bold magenta",
            show_lines=False,
        )
        table.add_column("Candidate", style="dim", no_wrap=True)
        table.add_column("Agent", no_wrap=True)
        table.add_column("ts_ns", justify="right")
        table.add_column("Content hash", no_wrap=True)
        table.add_column("Entry hash", no_wrap=True)
        for c in v.candidates:
            table.add_row(
                "candidate",
                c.agent_id,
                str(c.ts_ns),
                c.content_hash[:24] + "...",
                c.entry_hash[:24] + "...",
            )
        console.print(table)
        console.print(f"  parent={v.parent_hash[:24]}...   char-count diff: {v.char_count_diff} byte(s)")


@lineage_cmd.command(name="resolve")
@click.argument("artefact_path", required=True)
@click.option(
    "--policy",
    "policy_name",
    required=True,
    help="Merge policy: human, first-writer, or agent:<id>.",
)
@click.option(
    "--reason",
    default="",
    help="Free-form operator note attached to the audit record.",
)
@click.option(
    "--diff",
    "show_diff",
    is_flag=True,
    help="Print a full unified diff of the competing entries (human policy).",
)
@click.option(
    "--yes",
    "auto_yes",
    is_flag=True,
    help="Skip the interactive prompt; pick the first candidate for human policy.",
)
@click.option(
    "--log",
    "log_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path(".sdd/lineage/log.jsonl"),
    show_default=True,
)
@click.option(
    "--sdd-dir",
    "sdd_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path(".sdd"),
    show_default=True,
    help="Project .sdd directory; merge-audit JSONL lands underneath it.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON output.")
def resolve_cmd(
    artefact_path: str,
    policy_name: str,
    reason: str,
    show_diff: bool,
    auto_yes: bool,
    log_path: Path,
    sdd_dir: Path,
    as_json: bool,
) -> None:
    """Resolve one unresolved fork using a named ``MergePolicy``.

    The ``human`` policy presents a side-by-side summary and (unless
    ``--yes`` is set) asks the operator which candidate wins. The
    ``first-writer`` and ``agent:<id>`` policies pick deterministically;
    they exit non-zero when no candidate satisfies the rule. Every
    successful resolution emits a ``lineage.merge_entry`` lifecycle hook
    so downstream auditors see the decision.
    """
    import json as _json
    import sys

    from bernstein.cli.commands._lineage_conflict_helpers import (
        FirstWriterPolicy,
        HumanPolicy,
        LineageConflict,
        build_conflict_views,
        format_unified_diff,
        index_entries,
        resolve_one,
        resolve_policy_name,
    )
    from bernstein.cli.commands._lineage_v1_helpers import read_entries
    from bernstein.core.lineage.audit import emit_lineage_merge_entry
    from bernstein.core.lineage.tips import detect_forks

    if not log_path.exists():
        console.print(f"[red]No log at {log_path}[/red]")
        sys.exit(1)

    entries = read_entries(log_path)
    forks = [f for f in detect_forks(entries) if f.artefact_path == artefact_path]
    if not forks:
        console.print(f"[yellow]No unresolved fork for[/yellow] {artefact_path}")
        sys.exit(1)
    fork = forks[0]
    by_hash = index_entries(entries)

    try:
        policy = resolve_policy_name(policy_name)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        sys.exit(2)

    # Human policy needs an interactive choice; raise on its own otherwise.
    if isinstance(policy, HumanPolicy):
        views = build_conflict_views(entries, artefact_path)
        if not views:
            console.print(f"[yellow]No unresolved fork for[/yellow] {artefact_path}")
            sys.exit(1)
        view = views[0]
        if show_diff:
            diff = format_unified_diff(by_hash, fork)
            if diff:
                console.print(diff)
        if auto_yes:
            winner_hash = view.candidates[0].entry_hash
        else:
            console.print(f"[bold]Resolve fork for[/bold] {artefact_path}")
            for idx, c in enumerate(view.candidates, start=1):
                console.print(
                    f"  [{idx}] agent={c.agent_id} ts_ns={c.ts_ns} "
                    f"entry={c.entry_hash[:24]}... content={c.content_hash[:24]}..."
                )
            choice = click.prompt(
                "Pick a candidate index",
                type=click.IntRange(1, len(view.candidates)),
            )
            winner_hash = view.candidates[choice - 1].entry_hash
        winner = by_hash[winner_hash]
    else:
        try:
            winner = resolve_one(fork, by_hash, policy)
        except LineageConflict as exc:
            console.print(f"[red]{exc}[/red]")
            sys.exit(1)
        # FirstWriterPolicy ties off-the-shelf; AgentPolicy already filtered.
        winner_hash = next(
            h for h in fork.child_hashes if by_hash[h].agent_id == winner.agent_id and by_hash[h].ts_ns == winner.ts_ns
        )
        # Type narrowing for the lint pass.
        assert isinstance(policy, (FirstWriterPolicy,)) or policy_name.startswith("agent:")

    payload = emit_lineage_merge_entry(
        artefact_path=artefact_path,
        policy=policy_name,
        winner_hash=winner_hash,
        candidate_hashes=list(fork.child_hashes),
        parent_hash=fork.parent_hash,
        reason=reason,
        sdd_dir=sdd_dir,
    )

    if as_json:
        click.echo(_json.dumps(payload.to_dict(), indent=2, sort_keys=True))
        return

    console.print(
        f"[green]Resolved[/green] {artefact_path}: policy={policy_name} "
        f"winner={winner_hash[:24]}... agent={winner.agent_id}"
    )
    if reason:
        console.print(f"  reason: {reason}")


@lineage_cmd.command(name="merge")
@click.argument("artefact_path", required=True)
@click.option(
    "--use-content",
    "use_content",
    required=True,
    help="Entry hash whose content_hash should win.",
)
@click.option(
    "--log",
    "log_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path(".sdd/lineage/log.jsonl"),
    show_default=True,
)
def merge_cmd(artefact_path: str, use_content: str, log_path: Path) -> None:
    """Manually resolve a lineage fork via operator-chosen content (§6.3)."""
    import sys

    from bernstein.cli.commands._lineage_v1_helpers import read_entries
    from bernstein.core.lineage.tips import detect_forks

    if not log_path.exists():
        console.print(f"[red]No log at {log_path}[/red]")
        sys.exit(1)
    entries = read_entries(log_path)
    relevant = [f for f in detect_forks(entries) if f.artefact_path == artefact_path]
    if not relevant:
        console.print(f"[yellow]No fork for {artefact_path}[/yellow]")
        return
    valid_winners = {h for f in relevant for h in f.child_hashes}
    if use_content not in valid_winners:
        console.print(
            f"[red]--use-content {use_content[:24]}... is not a candidate child for any fork on {artefact_path}[/red]"
        )
        sys.exit(1)
    console.print(
        "[green]Merge prepared.[/green] Steward signing happens in core; "
        f"run `bernstein lineage gate` after `LineageStore.append` writes the merge entry "
        f"with content from {use_content[:24]}..."
    )


# -- v2 two-layer store (issue #1249) --------------------------------------


@lineage_cmd.group(name="v2")
def v2_group() -> None:
    """Lineage v2 - two-layer storage (parent refs + detached children).

    Opt-in writer activated via ``BERNSTEIN_LINEAGE_V2=1`` or
    ``bernstein.yaml`` ``lineage.version: 2``. v1 stays the default.
    """


def _default_v2_root() -> Path:
    return Path(".sdd/lineage/v2")


def _load_v2_store(root: Path) -> LineageV2Store:
    from bernstein.core.lineage.v2_store import LineageV2Store

    return LineageV2Store(root)


if TYPE_CHECKING:  # pragma: no cover
    from bernstein.core.lineage.v2_store import LineageV2Store


@v2_group.command(name="show")
@click.argument("task_id", required=True)
@click.option(
    "--root",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Override the v2 store root (default: .sdd/lineage/v2).",
)
@click.option("--output-json", is_flag=True, help="Emit raw JSON instead of a table.")
def v2_show_cmd(task_id: str, root: Path | None, output_json: bool) -> None:
    """Reconstruct + print the full timeline for ``TASK_ID``."""
    import json as _json

    timeline = _load_v2_store(root or _default_v2_root()).replay(task_id)
    if output_json:
        from dataclasses import asdict

        payload = [{"parent": asdict(ref), "bodies": [asdict(b) for b in bodies]} for ref, bodies in timeline]
        click.echo(_json.dumps(payload, indent=2, sort_keys=True))
        return

    if not timeline:
        console.print(f"[yellow]No v2 records for task[/yellow] {task_id}")
        return

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Run", no_wrap=True)
    table.add_column("Call id", no_wrap=True)
    table.add_column("Summary", overflow="fold")
    table.add_column("Bodies", justify="right")
    table.add_column("child_sha (prefix)", no_wrap=True)
    for ref, bodies in timeline:
        table.add_row(
            ref.child_run_id,
            ref.parent_call_id,
            ref.summary,
            str(len(bodies)),
            ref.child_sha[:24] + "...",
        )
    console.print()
    console.print(f"[bold]Lineage v2 timeline for[/bold] {task_id} ({len(timeline)} ref(s))")
    console.print()
    console.print(table)


@v2_group.command(name="verify")
@click.option(
    "--root",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Override the v2 store root (default: .sdd/lineage/v2).",
)
@click.option("--output-json", is_flag=True, help="Emit JSON output.")
def v2_verify_cmd(root: Path | None, output_json: bool) -> None:
    """Validate the HMAC chains across both layers. Exits 1 on failure."""
    import json as _json
    import sys

    result = _load_v2_store(root or _default_v2_root()).verify()
    if output_json:
        click.echo(
            _json.dumps(
                {
                    "ok": result.ok,
                    "failures": result.failures,
                    "parent_count": result.parent_count,
                    "child_count": result.child_count,
                },
                indent=2,
            )
        )
    elif result.ok:
        console.print(f"[green]Lineage v2:[/green] OK ({result.parent_count} parent / {result.child_count} child)")
    else:
        console.print(f"[red]Lineage v2:[/red] FAIL ({len(result.failures)} issue(s))")
        for f in result.failures:
            console.print(f"  - {f}")
    if not result.ok:
        sys.exit(1)


@v2_group.command(name="export")
@click.argument("task_id", required=True)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["jsonl", "sigstore"], case_sensitive=False),
    default="jsonl",
    show_default=True,
)
@click.option(
    "--root",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Override the v2 store root (default: .sdd/lineage/v2).",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write to file instead of stdout.",
)
def v2_export_cmd(task_id: str, fmt: str, root: Path | None, output_path: Path | None) -> None:
    """Export the timeline for ``TASK_ID`` as JSONL or SLSA v0.3 attestations."""
    import json as _json

    store = _load_v2_store(root or _default_v2_root())
    if fmt.lower() == "jsonl":
        payload = store.export_jsonl(task_id)
    else:
        payload = _json.dumps(store.export_sigstore(task_id), indent=2, sort_keys=True)
    if output_path is None:
        click.echo(payload)
    else:
        output_path.write_text(payload, encoding="utf-8")
        console.print(f"[green]Wrote {len(payload)} bytes ->[/green] {output_path}")
