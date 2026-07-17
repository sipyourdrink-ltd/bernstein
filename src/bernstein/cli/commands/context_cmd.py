"""``bernstein context``: chain-anchored worker context capsules (#2545).

A runtime context capsule is a content-addressed, Ed25519-signed record of what
a worker was given: task id, run id, params hash, worktree, role, budget
envelope remaining, dependency state, and the audit chain head at spawn (plus
the intent capsule hash when one exists). Its hash is recorded in the spawn
record, the run journal, and the audit chain::

    bernstein context show   <task-id>
    bernstein context verify <task-id>

``show`` prints the operator projection of the capsule. ``verify`` recomputes
the capsule offline from the on-disk bytes and checks its hash against the
``context.capsule`` audit-chain entry and the ``context.capsule_recorded``
journal event at the recorded chain position; a tampered capsule, a reordered
journal, or a mock-layer fixture fails.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from bernstein.cli.helpers import console


def _load_hmac_key() -> bytes:
    from bernstein.core.security.audit import load_or_create_audit_key

    return load_or_create_audit_key()


def _sdd_dir(workdir: Path) -> Path:
    return workdir / ".sdd"


def _chain(workdir: Path):
    from bernstein.core.security.audit_chain import AuditChainStore

    return AuditChainStore(_sdd_dir(workdir) / "audit", key=_load_hmac_key())


@click.group("context")
def context_group() -> None:
    """Show and verify chain-anchored worker context capsules.

    \b
      bernstein context show   <task-id>
      bernstein context verify <task-id>
    """


@context_group.command("show")
@click.argument("task_id")
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON.")
def context_show_cmd(task_id: str, workdir: str, as_json: bool) -> None:
    """Print the operator projection of a context capsule.

    Exit codes: 0 = found, 1 = no capsule.
    """
    from bernstein.core.agents.context_capsule import project_capsule, read_capsule_record

    root = Path(workdir).resolve()
    signed = read_capsule_record(_sdd_dir(root), task_id)
    if signed is None:
        console.print(f"[yellow]NO CAPSULE[/yellow] -- no context capsule for task {task_id}")
        raise SystemExit(1)

    view = project_capsule(signed)
    if as_json:
        console.print_json(json.dumps(view))
        return

    console.print()
    console.print("[bold]Context capsule[/bold]")
    console.print(f"  task_id                 {view['task_id']}")
    console.print(f"  run_id                  {view['run_id']}")
    console.print(f"  capsule_hash            {view['capsule_hash']}")
    console.print(f"  params_hash             {view['params_hash'] or '(none)'}")
    console.print(f"  role                    {view['role']}")
    console.print(f"  worktree_path           {view['worktree_path']}")
    console.print(f"  budget_remaining_tokens {view['budget_remaining_tokens']}")
    console.print(f"  budget_remaining_usd    {view['budget_remaining_usd_micros'] / 1_000_000:.6f}")
    console.print(f"  audit_chain_head        {view['audit_chain_head']}")
    console.print(f"  intent_capsule_hash     {view['intent_capsule_hash'] or '(none)'}")
    if view["is_mock"]:
        console.print("  [yellow]mock                    true (never verifies as real)[/yellow]")


@context_group.command("verify")
@click.argument("task_id")
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON.")
def context_verify_cmd(task_id: str, workdir: str, as_json: bool) -> None:
    """Recompute a context capsule offline from journal + chain.

    Checks the capsule hash against the audit chain and the run journal's Merkle
    chain at the recorded position. Exit codes: 0 = verified, 1 = no capsule,
    2 = mismatch / mock / tamper.
    """
    from bernstein.core.agents.context_capsule import verify_context_capsule

    root = Path(workdir).resolve()
    result = verify_context_capsule(sdd_dir=_sdd_dir(root), chain=_chain(root), task_id=task_id)

    if as_json:
        payload = {
            "ok": result.ok,
            "reason": result.reason,
            "is_mock": result.is_mock,
            "signature_ok": result.signature_ok,
            "chain_ok": result.chain_ok,
            "journal_ok": result.journal_ok,
        }
        console.print_json(json.dumps(payload))
        if result.ok:
            raise SystemExit(0)
        if result.capsule is None:
            raise SystemExit(1)
        raise SystemExit(2)

    console.print()
    console.print(f"[bold]Context verify[/bold] task={task_id}")
    if result.ok:
        console.print("[green]OK[/green] -- the capsule re-derives byte-identically from the chain and journal.")
        raise SystemExit(0)
    if result.capsule is None:
        console.print(f"[yellow]NO CAPSULE[/yellow] -- {result.reason}")
        raise SystemExit(1)
    if result.is_mock:
        console.print(f"[red]MOCK[/red] -- {result.reason}")
    else:
        console.print(f"[red]MISMATCH[/red] -- {result.reason}")
    raise SystemExit(2)


__all__ = ["context_group"]
