"""Response-cache inspection and maintenance commands."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import click
from rich.table import Table

from bernstein.cli.helpers import console
from bernstein.core.persistence.action_cache import (
    ActionRecord,
    open_cache,
)
from bernstein.core.persistence.cache_eviction import (
    cache_dir,
    open_ledger,
    open_tombstones,
    write_recall_report,
)
from bernstein.core.persistence.cache_policy import CachePolicy
from bernstein.core.semantic_cache import ResponseCacheManager, SemanticCacheEntry


def _entry_to_dict(entry: SemanticCacheEntry) -> dict[str, Any]:
    """Convert a cache entry into a JSON-safe summary payload."""
    return {
        "source_task_id": entry.source_task_id,
        "verified": entry.verified,
        "git_diff_lines": entry.git_diff_lines,
        "hit_count": entry.hit_count,
        "created_at": entry.created_at,
        "last_used_at": entry.last_used_at,
        "response": entry.response,
        "key_text": entry.key_text,
    }


def _format_age(ts: float | None) -> str:
    """Return a short age label for a cache timestamp."""
    if ts is None:
        return "never"
    age_s = max(0, int(time.time() - ts))
    if age_s < 60:
        return f"{age_s}s"
    if age_s < 3600:
        return f"{age_s // 60}m"
    if age_s < 86_400:
        return f"{age_s // 3600}h"
    return f"{age_s // 86_400}d"


@click.group("cache")
def cache_group() -> None:
    """Inspect and manage the response cache."""


@cache_group.command("list")
@click.option(
    "--workdir",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path(),
    show_default=True,
    help="Project root containing .sdd/caching/response_cache.jsonl.",
)
@click.option("--limit", type=int, default=25, show_default=True, help="Maximum number of entries to show.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
def list_cache_entries(workdir: Path, limit: int, as_json: bool) -> None:
    """List cached task-result entries."""
    entries = ResponseCacheManager(workdir.resolve()).list_entries()
    if limit > 0:
        entries = entries[:limit]

    if as_json:
        click.echo(json.dumps({"entries": [_entry_to_dict(entry) for entry in entries]}, indent=2))
        return

    if not entries:
        console.print("[dim]No response-cache entries found.[/dim]")
        return

    table = Table(title="Response Cache", header_style="bold cyan")
    table.add_column("Task", style="dim", min_width=12)
    table.add_column("Verified", justify="center")
    table.add_column("Diff", justify="right")
    table.add_column("Hits", justify="right")
    table.add_column("Age", justify="right")
    table.add_column("Summary", min_width=30)

    for entry in entries:
        table.add_row(
            entry.source_task_id or "-",
            "yes" if entry.verified else "no",
            str(entry.git_diff_lines),
            str(entry.hit_count),
            _format_age(entry.last_used_at or entry.created_at),
            entry.response[:120],
        )
    console.print(table)


@cache_group.command("inspect")
@click.argument("task_id")
@click.option(
    "--workdir",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path(),
    show_default=True,
    help="Project root containing .sdd/caching/response_cache.jsonl.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
def inspect_cache_entry(task_id: str, workdir: Path, as_json: bool) -> None:
    """Inspect the cached result produced by a specific task."""
    entry = ResponseCacheManager(workdir.resolve()).inspect_task(task_id)
    if entry is None:
        if as_json:
            click.echo(json.dumps({"error": f"No cache entry found for task {task_id}"}, indent=2))
        else:
            console.print(f"[red]No cache entry found for task {task_id}.[/red]")
        raise SystemExit(1)

    payload = _entry_to_dict(entry)
    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return

    console.print(f"[bold]Task:[/bold] {task_id}")
    console.print(f"[bold]Verified:[/bold] {'yes' if entry.verified else 'no'}")
    console.print(f"[bold]Diff lines:[/bold] {entry.git_diff_lines}")
    console.print(f"[bold]Hits:[/bold] {entry.hit_count}")
    console.print(f"[bold]Created:[/bold] {_format_age(entry.created_at)} ago")
    console.print(f"[bold]Last used:[/bold] {_format_age(entry.last_used_at)} ago")
    console.print(f"[bold]Key:[/bold] {entry.key_text}")
    console.print(f"[bold]Response:[/bold] {entry.response}")


@cache_group.command("clear")
@click.option(
    "--workdir",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path(),
    show_default=True,
    help="Project root containing .sdd/caching/response_cache.jsonl.",
)
@click.option(
    "--unverified",
    "unverified_only",
    is_flag=True,
    default=False,
    help="Remove only unverified cache entries.",
)
@click.option("--yes", is_flag=True, default=False, help="Skip confirmation prompt.")
def clear_cache_entries(workdir: Path, unverified_only: bool, yes: bool) -> None:
    """Clear response-cache entries."""
    target = "unverified entries" if unverified_only else "all entries"
    if not yes and not click.confirm(f"Delete {target} from the response cache?"):
        raise SystemExit(1)

    removed = ResponseCacheManager(workdir.resolve()).clear(unverified_only=unverified_only)
    console.print(f"[green]Removed {removed} response-cache entr{'y' if removed == 1 else 'ies'}.[/green]")


# ---------------------------------------------------------------------------
# Cache-policy engine: surgical transitive eviction + policy inspection (#2551).
# ---------------------------------------------------------------------------


@cache_group.command("evict")
@click.argument("key")
@click.option("--reason", required=True, help="Machine-readable revocation reason (e.g. 'pr_reverted').")
@click.option(
    "--workdir",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path(),
    show_default=True,
    help="Project root containing .sdd/caching/policy/.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output the recall set as JSON.")
def evict_cache_key(key: str, reason: str, workdir: Path, as_json: bool) -> None:
    """Surgically evict a cache KEY and everything derived from it.

    \b
    Tombstones the key and every entry reachable over ``served_from`` lineage
    edges, prints the recall set of consuming runs, emits a ``cache.eviction``
    audit-chain event, and writes a recall report under
    ``.sdd/caching/policy/``. A tombstoned key can never serve again, even when
    its drift verdict is fresh.
    """
    root = workdir.resolve()
    ledger = open_ledger(root)
    tombstones = open_tombstones(root)
    recall = tombstones.evict(key, reason, ledger=ledger, ts=int(time.time()))

    report_path = cache_dir(root) / f"recall-{key[:16]}.json"
    write_recall_report(report_path, recall)

    # Chain-attest the eviction. Best-effort: a missing audit key must not fail
    # the operator's revocation, which is already durable in the tombstone log.
    try:
        from bernstein.core.security.audit import load_or_create_audit_key
        from bernstein.core.security.audit_chain import AuditChainStore, record_cache_eviction

        chain = AuditChainStore(root / ".sdd" / "audit", key=load_or_create_audit_key())
        record_cache_eviction(
            chain=chain,
            cache_key=key,
            reason=reason,
            tombstoned_count=len(recall.tombstoned),
            recall_count=len(recall.consumers),
        )
    except Exception as exc:  # eviction durability must not depend on the audit key
        console.print(f"[yellow]warning: eviction not chain-attested ({exc}); tombstone still applied[/yellow]")

    if as_json:
        click.echo(json.dumps(recall.to_dict(), indent=2))
        return

    consumers = ", ".join(recall.consumers) or "(none)"
    console.print(f"[green]Evicted {len(recall.tombstoned)} key(s) for reason '{reason}'.[/green]")
    console.print(f"[bold]Root key:[/bold] {key}")
    console.print(f"[bold]Tombstoned:[/bold] {', '.join(recall.tombstoned) or '(none)'}")
    console.print(f"[bold]Recall set ({len(recall.consumers)} run(s)):[/bold] {consumers}")
    console.print(f"[dim]Recall report: {report_path}[/dim]")


@cache_group.command("policy")
@click.option(
    "--file",
    "policy_file",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    required=True,
    help="JSON file declaring a CachePolicy.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output the resolved policy as JSON.")
def show_cache_policy(policy_file: Path, as_json: bool) -> None:
    """Resolve a cache policy file and print its canonical form and hash.

    \b
    Loads the JSON policy declaration, validates it, and prints the canonical
    policy plus its ``sha256`` policy hash - the value bound into every cache
    hit, miss, dedup, and eviction audit event for tasks under this policy.
    """
    policy = CachePolicy.from_dict(json.loads(policy_file.read_text(encoding="utf-8")))
    if as_json:
        click.echo(json.dumps({"policy": policy.to_dict(), "policy_hash": policy.policy_hash()}, indent=2))
        return
    console.print(f"[bold]Policy hash:[/bold] {policy.policy_hash()}")
    console.print(f"[bold]Ingredients:[/bold] {', '.join(policy.ingredients) or '(mandatory only)'}")
    console.print(f"[bold]Expiry mode:[/bold] {policy.expiry_mode} (drift window={policy.drift_window})")
    console.print(f"[bold]TTL seconds:[/bold] {policy.ttl_seconds}")
    console.print(f"[bold]Verified only:[/bold] {policy.verified_only}")
    console.print(f"[bold]World facing:[/bold] {policy.world_facing}")
    console.print(f"[bold]Store scope:[/bold] {policy.store_scope}")


# ---------------------------------------------------------------------------
# Action-cache subgroup: deterministic replay of recorded LLM/tool actions.
# Storage layer is bernstein.core.persistence.fingerprint.MemoStore - we
# only inspect/replay the typed records on top of it.
# ---------------------------------------------------------------------------


@cache_group.group("action")
def action_cache_subgroup() -> None:
    """Inspect / replay the action-level LLM cache (cache action ...)."""


@action_cache_subgroup.command("stats")
@click.option(
    "--workdir",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path(),
    show_default=True,
    help="Project root containing .sdd/runtime/action_cache/.",
)
def action_cache_stats(workdir: Path) -> None:
    """Show on-disk size and entry count for the action cache."""
    cache = open_cache(workdir.resolve(), mode="off")
    entries = cache.store._iter_entries()
    total = sum(size for _, size, _ in entries)
    console.print(f"[bold]Action cache:[/bold] {len(entries)} record(s), {total / 1024 / 1024:.2f} MiB on disk")
    console.print(f"[dim]Root: {cache.store.root}[/dim]")


@action_cache_subgroup.command("replay")
@click.argument("run_id")
@click.option(
    "--workdir",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path(),
    show_default=True,
    help="Project root containing .sdd/runtime/action_cache/.",
)
@click.option(
    "--as-json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the replay report as JSON.",
)
def action_cache_replay(run_id: str, workdir: Path, as_json: bool) -> None:
    """Re-execute a recorded run against the action cache.

    \b
    Walks ``.sdd/runtime/action_cache/`` for records tagged with
    ``run_id`` and reports hits/misses + drift between any duplicate
    keys.  In ``replay`` mode no live LLM call is issued, so the cost
    of running this command is $0.

    \b
      bernstein cache action replay 20240315-143022
    """
    cache = open_cache(workdir.resolve(), mode="replay", run_id=run_id)
    found: list[ActionRecord] = []
    for path, _size, _atime in cache.store._iter_entries():
        try:
            import pickle

            raw = pickle.loads(path.read_bytes())
        except (OSError, pickle.UnpicklingError, EOFError):
            continue
        if isinstance(raw, ActionRecord) and raw.run_id == run_id:
            found.append(raw)

    if as_json:
        click.echo(
            json.dumps(
                {
                    "run_id": run_id,
                    "records": [json.loads(r.to_json()) for r in found],
                    "count": len(found),
                },
                indent=2,
            )
        )
        return

    if not found:
        console.print(f"[yellow]No action-cache records found for run_id={run_id}.[/yellow]")
        return

    table = Table(title=f"Action cache replay: {run_id}", header_style="bold cyan")
    table.add_column("Model", style="dim")
    table.add_column("Tool")
    table.add_column("Tokens", justify="right")
    table.add_column("Cost", justify="right")
    table.add_column("Output (head)", min_width=40)
    for rec in found:
        total_tok = rec.tokens.prompt_tokens + rec.tokens.completion_tokens
        table.add_row(
            rec.model_id,
            rec.tool_name or "(llm)",
            str(total_tok),
            f"${rec.cost_usd:.4f}",
            rec.output_text[:80].replace("\n", " "),
        )
    console.print(table)
    console.print(f"[green]Replayed {len(found)} action(s) for run {run_id} at $0 live cost.[/green]")
