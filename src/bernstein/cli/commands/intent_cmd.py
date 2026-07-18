"""``bernstein intent``: intent capsules with deterministic drift verification.

Issue #2514. An intent capsule is the approved goal compiled into a canonical,
signed chain entry listing the allowed action classes, file-scope globs,
permitted adapters, egress classes, a cost-envelope reference, and an expiry.
The drift monitor maps observed journal events to action classes and compares
them against the capsule; the conformance verdict is a pure function of
``(journal, capsule)``:

    bernstein intent show   <task-id>
    bernstein intent verify <task-id>

``show`` prints the operator projection of the capsule (never the free-text
goal, only its digest). ``verify`` recomputes conformance offline: it checks the
capsule hash against the audit chain, resolves the governed run from the signed
audit entry, walks that run journal's Merkle chain, and maps observed action
classes against the capsule. A tampered capsule, a repointed capsule record, a
missing capsule-bound anchor, or a reordered journal fails; a drifted run
reports the divergence.

Both commands are read-only. ``verify`` in particular never creates audit key
material: a minted key cannot authenticate an existing chain, so doing so would
turn a missing-key setup error into a false tamper report.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from bernstein.cli.helpers import console


def _sdd_dir(workdir: Path) -> Path:
    return workdir / ".sdd"


def _read_only_chain(workdir: Path):
    """Open the audit chain for reading, never minting key material.

    ``verify`` only reads. Creating a key here would produce one that cannot
    authenticate the existing chain, so every entry would fail its HMAC check
    and a plain missing-key setup error would be reported as tampering.
    """
    from bernstein.core.security.audit import load_audit_key
    from bernstein.core.security.audit_chain import AuditChainStore

    return AuditChainStore(_sdd_dir(workdir) / "audit", key=load_audit_key())


def _exit_code(result) -> int:
    """Map a verify result onto a distinct exit code per state.

    A conformant-but-unsealed run gets its own code rather than sharing one
    with either success or drift: an operator has to be able to tell "clean and
    attested" from "clean so far, completeness unknown" from "drifted".
    """
    from bernstein.core.security.intent_capsule import SEAL_SEALED

    if result.ok:
        return 0
    if result.capsule is None:
        return 1
    if result.conformant and result.seal_state != SEAL_SEALED:
        return 4
    return 2


@click.group("intent")
def intent_group() -> None:
    """Show and verify intent capsules and their drift conformance.

    \b
      bernstein intent show   <task-id>
      bernstein intent verify <task-id>
    """


@intent_group.command("show")
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
def intent_show_cmd(task_id: str, workdir: str, as_json: bool) -> None:
    """Print the operator projection of an intent capsule.

    Exit codes: 0 = found, 1 = no capsule.
    """
    from bernstein.core.security.intent_capsule import (
        allowed_action_classes_hash,
        capsule_hash,
        read_capsule_binding,
    )

    root = Path(workdir).resolve()
    capsule, run_id = read_capsule_binding(_sdd_dir(root), task_id)
    if capsule is None:
        console.print(f"[yellow]NO CAPSULE[/yellow] -- no intent capsule for task {task_id}")
        raise SystemExit(1)

    view = {
        "task_id": capsule.task_id,
        "plan_id": capsule.plan_id,
        "run_id": run_id,
        "capsule_hash": capsule_hash(capsule),
        "goal_digest": capsule.goal_digest,
        "allowed_action_classes": list(capsule.allowed_action_classes),
        "allowed_action_classes_hash": allowed_action_classes_hash(capsule),
        "file_scope_globs": list(capsule.file_scope_globs),
        "permitted_adapters": list(capsule.permitted_adapters),
        "egress_classes": list(capsule.egress_classes),
        "cost_envelope_ref": capsule.cost_envelope_ref,
        "expiry_ts": capsule.expiry_ts,
    }
    if as_json:
        console.print_json(json.dumps(view))
        return

    console.print()
    console.print("[bold]Intent capsule[/bold]")
    console.print(f"  task_id                 {capsule.task_id}")
    console.print(f"  plan_id                 {capsule.plan_id}")
    console.print(f"  run_id                  {run_id}")
    console.print(f"  capsule_hash            {capsule_hash(capsule)}")
    console.print(f"  goal_digest             {capsule.goal_digest}")
    console.print(f"  allowed_action_classes  {', '.join(capsule.allowed_action_classes)}")
    console.print(f"  file_scope_globs        {', '.join(capsule.file_scope_globs)}")
    console.print(f"  permitted_adapters      {', '.join(capsule.permitted_adapters)}")
    console.print(f"  egress_classes          {', '.join(capsule.egress_classes) or '(none)'}")
    console.print(f"  cost_envelope_ref       {capsule.cost_envelope_ref}")
    console.print(f"  expiry_ts               {capsule.expiry_ts}")


@intent_group.command("verify")
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
def intent_verify_cmd(task_id: str, workdir: str, as_json: bool) -> None:
    """Recompute conformance offline from journal + capsule.

    Checks the capsule hash against the audit chain, resolves the run from the
    signed audit entry, walks the run journal's Merkle chain, and maps observed
    action classes against the capsule. Read-only: it never writes to the chain
    and never creates an audit key.

    Exit codes: 0 = sealed and conformant, 1 = no capsule, 2 = drift or tamper,
    3 = cannot verify (no audit key), 4 = unsealed (no drift found, but the run
    has not committed its end so completeness is not attested).
    """
    from bernstein.core.security.audit import AuditKeyMissingError
    from bernstein.core.security.intent_capsule import (
        project_conformance_verdict,
        verify_intent_conformance,
    )

    root = Path(workdir).resolve()
    try:
        chain = _read_only_chain(root)
    except AuditKeyMissingError as exc:
        if as_json:
            console.print_json(json.dumps({"ok": False, "error": "audit_key_missing", "reason": str(exc)}))
        else:
            console.print()
            console.print(f"[yellow]CANNOT VERIFY[/yellow] -- {exc}")
        raise SystemExit(3) from exc

    result = verify_intent_conformance(
        sdd_dir=_sdd_dir(root),
        chain=chain,
        task_id=task_id,
    )

    if as_json:
        payload = {
            "ok": result.ok,
            "conformant": result.conformant,
            "seal_state": result.seal_state,
            "reason": result.reason,
            "run_id": result.run_id,
            "verdict": project_conformance_verdict(result.verdict) if result.verdict else None,
        }
        console.print_json(json.dumps(payload))
        raise SystemExit(_exit_code(result))

    console.print()
    console.print(f"[bold]Intent verify[/bold] task={task_id}")
    if result.ok:
        console.print("[green]OK[/green] -- the run stayed inside the approved capsule (sealed).")
        raise SystemExit(0)
    if result.capsule is None:
        console.print(f"[yellow]NO CAPSULE[/yellow] -- {result.reason}")
        raise SystemExit(1)
    if result.verdict is not None and not result.verdict.conformant:
        classes = ", ".join(sorted({d.action_class for d in result.verdict.divergences}))
        console.print(f"[red]DRIFT[/red] -- {result.reason}")
        console.print(f"  divergent action classes: {classes}")
        console.print(f"  verdict_hash: {result.verdict.verdict_hash}")
        console.print(f"  seal state: {result.seal_state}")
    elif result.conformant:
        console.print(f"[yellow]UNSEALED[/yellow] -- {result.reason}")
    else:
        console.print(f"[red]MISMATCH[/red] -- {result.reason}")
    raise SystemExit(_exit_code(result))


__all__ = ["intent_group"]
