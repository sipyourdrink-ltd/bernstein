"""``bernstein context``: chain-anchored worker context capsules (#2545).

A runtime context capsule is a content-addressed, Ed25519-signed record of what
a worker was given: task id, run id, params hash, worktree, role, budget
envelope remaining, dependency state, and the audit chain head at spawn (plus
the intent capsule hash when one exists). Its hash is recorded in the spawn
record, the run journal, and the audit chain::

    bernstein context show     <task-id>
    bernstein context verify   <task-id>
    bernstein context manifest <task-id>

``show`` prints the operator projection of the capsule. ``verify`` recomputes
the capsule offline from the on-disk bytes and checks its hash against the
``context.capsule`` audit-chain entry and the ``context.capsule_recorded``
journal event at the recorded chain position; a tampered capsule, a reordered
journal, or a mock-layer fixture fails.

``manifest`` derives the content-addressed context manifest for a task's
declared path set (#3366): every declared file addressed by the hash of its
bytes, and every path that does not resolve recorded ``unmanifested`` with its
reason code. Nothing anchors the digest in a run record yet, so the command is
a read of the working tree, not of the chain.

``bernstein context segment-prompt`` (#3455) is a separate, offline debug
utility: it digests the role/task/mailbox/resume blocks the orchestrator
authors into named segments plus one ordered segment-list digest. It reads
only the files passed on the command line and writes nothing -- anchoring a
segment digest in a real run is later scope for #3455.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import click

from bernstein.cli.helpers import console
from bernstein.core.security.audit import load_or_create_audit_key

if TYPE_CHECKING:
    from bernstein.core.tasks.models import Task


def _sdd_dir(workdir: Path) -> Path:
    return workdir / ".sdd"


def _load_task(workdir: Path, task_id: str) -> Task | None:
    """Return the persisted task for *task_id*, or None when there is no such task."""
    from bernstein.core.tasks.task_store import TaskStore

    sdd_dir = _sdd_dir(workdir)
    store = TaskStore(
        jsonl_path=sdd_dir / "runtime" / "tasks.jsonl",
        archive_path=sdd_dir / "archive" / "tasks.jsonl",
    )
    store.replay_jsonl()
    return store.get_task(task_id)


def _chain(workdir: Path):
    from bernstein.core.security.audit_chain import AuditChainStore

    return AuditChainStore(_sdd_dir(workdir) / "audit", key=load_or_create_audit_key())


@click.group("context")
def context_group() -> None:
    """Show and verify chain-anchored worker context capsules.

    \b
      bernstein context show     <task-id>
      bernstein context verify   <task-id>
      bernstein context manifest <task-id>
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
            "journal_identity": result.journal_identity,
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
        console.print("[green]OK[/green] -- the capsule re-derives from its signed record and parsed journal.")
        console.print(f"Whole-journal identity: {result.journal_identity}")
        raise SystemExit(0)
    if result.capsule is None:
        console.print(f"[yellow]NO CAPSULE[/yellow] -- {result.reason}")
        raise SystemExit(1)
    if result.is_mock:
        console.print(f"[red]MOCK[/red] -- {result.reason}")
    else:
        console.print(f"[red]MISMATCH[/red] -- {result.reason}")
    raise SystemExit(2)


@context_group.command("manifest")
@click.argument("task_id")
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/; declared paths resolve under it.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON.")
def context_manifest_cmd(task_id: str, workdir: str, as_json: bool) -> None:
    """Derive the content-addressed context manifest for a task.

    Each declared path on the task (``owned_files``) is content-addressed by the
    hash of its bytes; a path that does not resolve keeps its position and
    records ``unmanifested`` with a reason code, so absence is explicit.

    Exit codes: 0 = derived, 1 = no such task.
    """
    from bernstein.core.agents.context_manifest import derive_context_manifest

    root = Path(workdir).resolve()
    task = _load_task(root, task_id)
    if task is None:
        console.print(f"[yellow]NO TASK[/yellow] -- no task {task_id} under {root / '.sdd'}")
        raise SystemExit(1)

    manifest = derive_context_manifest(repo_root=root, declared_paths=task.owned_files)
    if as_json:
        payload = {
            "task_id": task_id,
            "manifest_digest": manifest.manifest_digest(),
            "entry_count": len(manifest.entries),
            "unmanifested_count": len(manifest.unmanifested),
            **manifest.to_dict(),
        }
        console.print_json(json.dumps(payload))
        return

    console.print()
    console.print("[bold]Context manifest[/bold]")
    console.print(f"  task_id          {task_id}")
    console.print(f"  manifest_digest  {manifest.manifest_digest()}")
    console.print(f"  entries          {len(manifest.entries)}")
    console.print(f"  unmanifested     {len(manifest.unmanifested)}")
    if not manifest.entries:
        console.print("  [yellow](the task declares no paths -- nothing was manifested)[/yellow]")
        return
    console.print()
    for index, entry in enumerate(manifest.entries):
        if entry.unmanifested:
            console.print(f"  {index:>3}  [yellow]unmanifested[/yellow] ({entry.reason})  {entry.path}")
        else:
            console.print(f"  {index:>3}  {entry.digest}  {entry.path}")


def _read_block(path: str | None) -> str:
    if path is None:
        return ""
    return Path(path).read_text(encoding="utf-8")


@context_group.command("segment-prompt")
@click.option(
    "--role-file", type=click.Path(exists=True, dir_okay=False), default=None, help="File with the role block."
)
@click.option(
    "--task-file", type=click.Path(exists=True, dir_okay=False), default=None, help="File with the task block."
)
@click.option(
    "--mailbox-file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="File with the coordination-mailbox block.",
)
@click.option(
    "--resume-file", type=click.Path(exists=True, dir_okay=False), default=None, help="File with the resume block."
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON.")
def context_segment_prompt_cmd(
    role_file: str | None,
    task_file: str | None,
    mailbox_file: str | None,
    resume_file: str | None,
    as_json: bool,
) -> None:
    """Digest prompt blocks into named segments (#3455 step 1, offline debug utility).

    Reads each of the role/task/mailbox/resume blocks from a file (an omitted
    block is treated as empty, not skipped) and prints each segment's name and
    ``sha256:`` digest, plus the ordered segment-list digest. Pure and
    offline: this command reads only the files given on the command line and
    writes nothing to ``.sdd/`` or any other run state -- anchoring a segment
    digest in a real run is later scope for #3455.
    """
    from bernstein.core.agents.prompt_segments import segment_prompt, segments_digest

    segments = segment_prompt(
        role_block=_read_block(role_file),
        task_block=_read_block(task_file),
        mailbox_block=_read_block(mailbox_file),
        resume_block=_read_block(resume_file),
    )
    list_digest = segments_digest(segments)

    if as_json:
        payload = {
            "segments": [{"name": s.name, "digest": s.digest} for s in segments],
            "segments_digest": list_digest,
        }
        console.print_json(json.dumps(payload))
        return

    console.print()
    console.print("[bold]Prompt segments[/bold]")
    for segment in segments:
        console.print(f"  {segment.name:<8} {segment.digest}")
    console.print(f"  {'digest':<8} {list_digest}")


__all__ = ["context_group"]
