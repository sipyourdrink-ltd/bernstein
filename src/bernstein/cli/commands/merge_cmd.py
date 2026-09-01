"""``merge`` command group - pick best agent solution and verify merge admission receipts.

``bernstein merge pick``: pick the best agent solution and merge it into main.
Used after comparing parallel branches (A/B testing different models on the
same task) to select a winner.

``bernstein merge verify``: verify offline that a merge SHA carries a valid
merge-admission receipt, following the same exit-code contract as
``bernstein review-receipt verify``: 0 = verified / 1 = no receipt / 2 = mismatch.

Back-compat: ``merge`` used to be a single command taking ``--pick``/``--base``/
etc. directly, before ``pick``/``verify`` became subcommands. Those options are
still declared on the ``merge`` group itself, so ``bernstein merge --base main
--pick 2`` (no subcommand) keeps working, running the same code as
``bernstein merge pick --base main --pick 2``. See ``merge_cmd`` below.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click

from bernstein.cli.diff_cmd import (
    _branch_exists,
    _find_agent_by_session,
    _find_agent_for_task,
    _load_agents,
    _run_git,
    resolve_diff,
)
from bernstein.cli.helpers import console

# ------------------------------------------------------------------
# Helpers (shared between subcommands)
# ------------------------------------------------------------------


def _resolve_agent_branch(
    identifier: str, root: Path, agents: list[dict[str, Any]]
) -> tuple[str | None, dict[str, Any] | None]:
    """Resolve an identifier (task ID or session ID) to a branch name."""
    agent = _find_agent_for_task(identifier, agents) or _find_agent_by_session(identifier, agents)
    if agent is None:
        return None, None

    session_id = agent.get("id", "")
    branch = f"agent/{session_id}"

    worktree_path = root / ".sdd" / "worktrees" / session_id
    if worktree_path.exists() and (worktree_path / ".git").exists():
        return branch, agent
    if _branch_exists(branch, root):
        return branch, agent

    return None, agent


def _current_branch(root: Path) -> str:
    """Get the current branch name."""
    return _run_git(["rev-parse", "--abbrev-ref", "HEAD"], root)


def _verify_merge_result(root: Path, merge_msg: str, switched: bool, original_branch: str) -> None:
    """Verify merge succeeded or handle conflicts."""
    _run_git(["rev-parse", "HEAD"], root)
    merge_log = _run_git(["log", "-1", "--oneline"], root)

    is_merge_success = merge_msg[:20] in merge_log or "Merge" in merge_log
    if is_merge_success:
        console.print(f"[green]Merged successfully:[/green] {merge_log}")
        return

    status = _run_git(["status", "--porcelain"], root)
    has_conflicts = any(line[:2] in ("UU", "AA", "DD") for line in status.splitlines() if len(line) >= 2)
    if has_conflicts:
        console.print("[red]Merge conflicts detected![/red]")
        console.print("[dim]Resolve conflicts manually, then commit.[/dim]")
        _run_git(["merge", "--abort"], root)
        if switched:
            _run_git(["checkout", original_branch], root)
        raise SystemExit(1)

    console.print(f"[green]Merge completed:[/green] {merge_log}")


def _merge_pick_impl(
    pick_id: str,
    base: str,
    workdir: str,
    no_ff: bool,
    message: str | None,
    dry_run: bool,
    reject_others: tuple[str, ...],
) -> None:
    """Pick the best agent solution and merge it -- the shared body.

    Both entry points call this and only this: the ``merge`` group's own
    callback (legacy ``bernstein merge --pick ...`` with no subcommand) and
    ``merge_pick_cmd`` (``bernstein merge pick --pick ...``). Keep the actual
    logic here so the two invocations can never drift apart.
    """
    root = Path(workdir).resolve()
    agents = _load_agents(root)

    branch, agent = _resolve_agent_branch(pick_id, root, agents)

    if branch is None:
        if agent:
            console.print(
                f"[yellow]Agent found for [bold]{pick_id}[/bold] but no branch/worktree exists.[/yellow]\n"
                "[dim]The branch may have been cleaned up or already merged.[/dim]"
            )
        else:
            console.print(f"[red]No agent or branch found for:[/red] [bold]{pick_id}[/bold]")
        raise SystemExit(1)

    session_id = agent.get("id", "") if agent else pick_id
    role = agent.get("role", "") if agent else ""
    model = agent.get("model", "") if agent else ""

    console.print(f"[bold]Picking:[/bold] [cyan]{branch}[/cyan]  [dim]role={role}, model={model}[/dim]")

    resolved = resolve_diff(pick_id, root, agents, base)
    if resolved.stat_text:
        console.print("\n[bold]Changes to merge:[/bold]")
        console.print(resolved.stat_text)
        console.print()

    if dry_run:
        console.print("[yellow]--dry-run: no changes made.[/yellow]")

        if reject_others:
            console.print("\n[bold]Would reject (delete branches):[/bold]")
            for rej_id in reject_others:
                rej_branch, _ = _resolve_agent_branch(rej_id, root, agents)
                status = f"[cyan]{rej_branch}[/cyan]" if rej_branch else "[dim]not found[/dim]"
                console.print(f"  {rej_id} -> {status}")
        return

    current = _current_branch(root)
    switched = False

    if current != base:
        console.print(f"[dim]Switching to {base}...[/dim]")
        result = _run_git(["checkout", base], root)
        if not result and "error" in _run_git(["checkout", base], root).lower():
            console.print(f"[red]Failed to checkout {base}.[/red]")
            raise SystemExit(1)
        switched = True

    merge_msg = message or f"Merge {branch}: pick best solution (session {session_id})"
    merge_args = ["merge"]
    if no_ff:
        merge_args.append("--no-ff")
    merge_args.extend(["-m", merge_msg, branch])

    _run_git(merge_args, root)
    _verify_merge_result(root, merge_msg, switched, current)

    if reject_others:
        console.print()
        for rej_id in reject_others:
            rej_branch, _ = _resolve_agent_branch(rej_id, root, agents)
            if rej_branch:
                del_result = _run_git(["branch", "-D", rej_branch], root)
                if del_result:
                    console.print(f"[dim]Deleted rejected branch:[/dim] [red]{rej_branch}[/red]")
                else:
                    console.print(f"[yellow]Could not delete:[/yellow] {rej_branch}")
            else:
                console.print(f"[dim]No branch found for rejected agent:[/dim] {rej_id}")


# ------------------------------------------------------------------
# CLI group
# ------------------------------------------------------------------


@click.group("merge", invoke_without_command=True, no_args_is_help=False)
@click.option(
    "--pick",
    "pick_id",
    type=str,
    default=None,
    metavar="AGENT",
    help="Task ID or session ID of the agent whose solution to merge. "
    "Legacy form: passing this directly to `bernstein merge` (no subcommand) "
    "runs `merge pick`.",
)
@click.option("--base", default="main", show_default=True, help="Target branch to merge into.")
@click.option(
    "--workdir",
    default=".",
    show_default=True,
    type=click.Path(),
    help="Project root (parent of .sdd/).",
)
@click.option("--no-ff", "no_ff", is_flag=True, default=True, show_default=True, help="Use --no-ff merge.")
@click.option("--message", "-m", default=None, help="Custom merge commit message.")
@click.option("--dry-run", is_flag=True, default=False, help="Show what would be merged without merging.")
@click.option(
    "--reject",
    "reject_others",
    multiple=True,
    metavar="AGENT",
    help="Also delete branches of rejected agents (repeatable).",
)
@click.pass_context
def merge_cmd(
    ctx: click.Context,
    pick_id: str | None,
    base: str,
    workdir: str,
    no_ff: bool,
    message: str | None,
    dry_run: bool,
    reject_others: tuple[str, ...],
) -> None:
    """Merge management: pick best agent solution and verify merge admission receipts.

    \b
      bernstein merge pick   --pick <task-id>   # merge agent work into main
      bernstein merge verify --sha <sha>         # offline-verify merge receipt

    \b
    Back-compat: the options above also work directly on ``bernstein merge``
    with no subcommand (``bernstein merge --base main --pick 2``), reaching
    the same code as ``bernstein merge pick``. Scripts written before ``pick``
    and ``verify`` became subcommands keep working unchanged; new scripts
    should prefer ``bernstein merge pick`` explicitly.
    """
    if ctx.invoked_subcommand is not None:
        return
    if pick_id is None:
        click.echo(ctx.get_help())
        return
    _merge_pick_impl(
        pick_id=pick_id,
        base=base,
        workdir=workdir,
        no_ff=no_ff,
        message=message,
        dry_run=dry_run,
        reject_others=reject_others,
    )


# ------------------------------------------------------------------
# pick subcommand
# ------------------------------------------------------------------


@merge_cmd.command("pick")
@click.option(
    "--pick",
    "pick_id",
    type=str,
    required=True,
    metavar="AGENT",
    help="Task ID or session ID of the agent whose solution to merge.",
)
@click.option("--base", default="main", show_default=True, help="Target branch to merge into.")
@click.option(
    "--workdir",
    default=".",
    show_default=True,
    type=click.Path(),
    help="Project root (parent of .sdd/).",
)
@click.option("--no-ff", "no_ff", is_flag=True, default=True, show_default=True, help="Use --no-ff merge.")
@click.option("--message", "-m", default=None, help="Custom merge commit message.")
@click.option("--dry-run", is_flag=True, default=False, help="Show what would be merged without merging.")
@click.option(
    "--reject",
    "reject_others",
    multiple=True,
    metavar="AGENT",
    help="Also delete branches of rejected agents (repeatable).",
)
def merge_pick_cmd(
    pick_id: str,
    base: str,
    workdir: str,
    no_ff: bool,
    message: str | None,
    dry_run: bool,
    reject_others: tuple[str, ...],
) -> None:
    """Pick the best agent solution and merge it.

    After comparing parallel branches with ``bernstein diff --compare``,
    use this command to merge the winning solution into the target branch.

    \b
    Examples:
      bernstein merge pick --pick backend-abc123           # merge agent's work
      bernstein merge pick --pick task-id-prefix           # resolve by task ID
      bernstein merge pick --pick agent1 --reject agent2   # merge one, delete other
      bernstein merge pick --pick agent1 --dry-run         # preview only
    """
    _merge_pick_impl(
        pick_id=pick_id,
        base=base,
        workdir=workdir,
        no_ff=no_ff,
        message=message,
        dry_run=dry_run,
        reject_others=reject_others,
    )


# ------------------------------------------------------------------
# verify subcommand
# ------------------------------------------------------------------


@merge_cmd.command("verify")
@click.option(
    "--sha",
    "head_sha",
    required=True,
    help="Commit SHA to verify the merge receipt for.",
)
@click.option(
    "--workdir",
    "-w",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False, exists=True),
    help="Project root containing .sdd/.",
)
def merge_verify_cmd(head_sha: str, workdir: str) -> None:
    """Prove offline that a merge SHA carries a valid merge-admission receipt.

    Recomputes the spine anchor over the receipt's canonical binding bytes,
    checks the Ed25519 signature, and verifies the merge spine.

    Exit codes:
        0 = verified
        1 = no receipt / bad input
        2 = mismatch (tamper)

    This follows the same exit-code contract as ``bernstein review-receipt verify``.
    """
    from bernstein.core.quality.merge_receipt import verify_merge_receipt
    from bernstein.core.security.audit import load_or_create_audit_key

    root = Path(workdir).resolve()

    hmac_key = load_or_create_audit_key()
    lineage_root = root / ".sdd" / "lineage"

    result = verify_merge_receipt(
        workdir=root,
        lineage_root=lineage_root,
        hmac_key=hmac_key,
        head_sha=head_sha,
    )

    console.print()
    console.print(f"[bold]Merge receipt verify[/bold] sha={head_sha}")

    if result.ok:
        console.print(f"  decision  {result.decision}")
        console.print(f"  authority {result.authority}")
        if result.receipt and result.receipt.review_receipt_id:
            console.print(f"  review_receipt_id {result.receipt.review_receipt_id}")
        console.print("[green]OK[/green] -- merge receipt verified, spine anchored.")
        raise SystemExit(0)

    receipt = result.receipt
    if receipt is None:
        console.print(f"[yellow]NO RECEIPT[/yellow] -- {result.reason}")
        raise SystemExit(1)

    console.print(f"[red]MISMATCH[/red] -- {result.reason}")
    raise SystemExit(2)
