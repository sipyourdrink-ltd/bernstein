"""CLI helpers for the per-step replay surface (#1799, #2605).

The top-level ``bernstein replay`` command is implemented in
``advanced_cmd.py`` as a ``nargs=-1`` pseudo-group so the legacy
``bernstein replay <run_id>`` shape keeps working. This module hosts
the *new* verbs added by #1799 and #2605:

* ``bernstein replay <agent_id>`` (interactive view + chain verification)
* ``bernstein replay export <agent_id>`` (portable, offline-verifiable receipt)
* ``bernstein replay publish <agent_id>`` (explicit opt-in, redacted receipt)
* ``bernstein replay verify <receipt_path>`` (offline verifier helper)
* ``bernstein replay debug <run>`` / ``<left> <right>`` (forensic time-travel
  debugger: recompute-mismatch localization, two-run path diff, ``--fork-from``
  reproduction, offline-verifiable debug receipt)

The dispatch functions are called from the existing ``replay`` command
in ``advanced_cmd.py``; keeping them in a separate module makes them
testable in isolation without standing up the whole CLI graph.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from bernstein.core.persistence.journal import (
    JournalReader,
    agent_journal_dir,
)
from bernstein.core.persistence.journal_diff import diff_journals
from bernstein.core.persistence.journal_export import (
    ReceiptError,
    export_receipt,
    verify_receipt,
)
from bernstein.core.persistence.journal_publish import (
    PublishError,
    RedactionPolicy,
    publish_receipt,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

#: Default cap on the number of projection rows rendered by ``replay debug``.
#: Matches the ``limit`` pattern in :func:`replay_agent_view`; ``--full``
#: lifts the cap for an unbounded projection.
_DEFAULT_DEBUG_LIMIT = 200


def _resolve_agent_dir(sdd_dir: Path, agent_id: str) -> Path:
    """Return the agent journal directory under *sdd_dir*."""
    return agent_journal_dir(sdd_dir, agent_id)


def replay_agent_view(
    agent_id: str,
    sdd_dir: Path,
    *,
    as_json: bool = False,
    limit: int | None = None,
) -> int:
    """Render the interactive per-step view for *agent_id*.

    Per AC #3 the view verifies the on-disk chain matches the recorded
    head *before* any rendering, so an operator never sees a tampered
    chain laid out as if it were intact.

    Returns the process exit code (0 on success, non-zero on chain
    verification failure).
    """
    from bernstein.cli.helpers import console

    agent_dir = _resolve_agent_dir(sdd_dir, agent_id)
    if not agent_dir.exists():
        console.print(f"[red]No journal for agent[/red] {agent_id} (looked at {agent_dir})")
        return 2

    reader = JournalReader(agent_dir)
    head_entry = reader.head()
    if head_entry is None:
        console.print(f"[yellow]Journal for {agent_id} is empty[/yellow]")
        return 0

    verification = reader.verify(expected_head=head_entry.step_hash)
    if not verification.ok:
        console.print(f"[red]Chain verification failed for {agent_id}:[/red]")
        for err in verification.errors:
            console.print(f"  - {err}")
        return 1

    entries = list(reader.entries())
    if limit is not None:
        entries = entries[:limit]

    if as_json:
        click_payload = {
            "agent_id": agent_id,
            "head_hash": verification.head_hash,
            "steps": verification.steps,
            "entries": [e.to_dict() for e in entries],
        }
        console.print_json(json.dumps(click_payload, default=str))
        return 0

    from rich.table import Table

    console.print(f"[bold]Replay for[/bold] [cyan]{agent_id}[/cyan]")
    console.print(f"[dim]head_hash:[/dim] [bold]{verification.head_hash}[/bold]")
    console.print(f"[dim]steps:[/dim] {verification.steps}")

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("seq", style="dim", width=4)
    table.add_column("step_hash", width=18)
    table.add_column("prev_hash", width=18)
    table.add_column("model")
    table.add_column("tool_call")
    for entry in entries:
        tool_label = ""
        if entry.tool_call is not None:
            tool_label = str(entry.tool_call.get("name", "<tool>")) if isinstance(entry.tool_call, dict) else "<tool>"
        table.add_row(
            str(entry.seq),
            f"{entry.step_hash[:16]}...",
            f"{entry.prev_hash[:16]}...",
            entry.model or "-",
            tool_label,
        )
    console.print(table)
    console.print("[dim]Use 'bernstein replay export <agent_id>' to produce an offline-verifiable receipt.[/dim]")
    return 0


def replay_export(
    agent_id: str,
    sdd_dir: Path,
    output: Path,
    *,
    signer_key_path: Path | None = None,
) -> int:
    """Build a portable receipt and write it to *output*. Returns exit code."""
    from bernstein.cli.helpers import console

    agent_dir = _resolve_agent_dir(sdd_dir, agent_id)
    if not agent_dir.exists():
        console.print(f"[red]No journal for agent[/red] {agent_id}")
        return 2

    signer = None
    if signer_key_path is not None:
        from bernstein.core.persistence.lineage_signer import (
            Ed25519FileKeySigner,
            LineageSignerError,
        )

        try:
            signer = Ed25519FileKeySigner.from_path(signer_key_path)
        except LineageSignerError as exc:
            console.print(f"[red]Cannot load signing key:[/red] {exc}")
            return 2

    try:
        result = export_receipt(
            agent_dir,
            output,
            agent_id=agent_id,
            signer=signer,
        )
    except ReceiptError as exc:
        console.print(f"[red]Export failed:[/red] {exc}")
        return 1

    console.print(f"[green]Exported receipt[/green] -> {result.path}")
    console.print(f"  head_hash: {result.head_hash}")
    console.print(f"  steps:     {result.steps}")
    console.print(f"  signed:    {result.signed}")
    return 0


def replay_publish(
    agent_id: str,
    sdd_dir: Path,
    output: Path,
    *,
    opt_in: bool,
    signer_key_path: Path | None = None,
) -> int:
    """Publish a privacy-redacted receipt. Returns exit code."""
    from bernstein.cli.helpers import console

    if not opt_in:
        console.print(
            "[red]Refusing to publish:[/red] pass --yes-i-want-to-publish "
            "to confirm. Local-only is the default; publish is the only "
            "path that writes outside .sdd/runtime/."
        )
        return 2

    agent_dir = _resolve_agent_dir(sdd_dir, agent_id)
    if not agent_dir.exists():
        console.print(f"[red]No journal for agent[/red] {agent_id}")
        return 2

    signer = None
    if signer_key_path is not None:
        from bernstein.core.persistence.lineage_signer import (
            Ed25519FileKeySigner,
            LineageSignerError,
        )

        try:
            signer = Ed25519FileKeySigner.from_path(signer_key_path)
        except LineageSignerError as exc:
            console.print(f"[red]Cannot load signing key:[/red] {exc}")
            return 2

    try:
        result = publish_receipt(
            agent_dir,
            output,
            agent_id=agent_id,
            policy=RedactionPolicy.default(),
            opt_in=True,
            signer=signer,
        )
    except PublishError as exc:
        console.print(f"[red]Publish failed:[/red] {exc}")
        return 1

    console.print(f"[green]Published redacted receipt[/green] -> {result.path}")
    console.print(f"  original_head:  {result.original_head_hash}")
    console.print(f"  redacted_head:  {result.head_hash}")
    console.print(f"  steps:          {result.steps}")
    console.print(f"  signed:         {result.signed}")
    return 0


def replay_verify(
    receipt_path: Path,
    *,
    expected_head: str | None,
    public_key_path: Path | None,
) -> int:
    """Verify a receipt tarball offline. Returns exit code."""
    from bernstein.cli.helpers import console

    if not receipt_path.exists():
        console.print(f"[red]Receipt not found:[/red] {receipt_path}")
        return 2

    verifier = None
    if public_key_path is not None:
        from bernstein.core.persistence.lineage_signer import (
            Ed25519PublicKeyVerifier,
            LineageSignerError,
        )

        try:
            verifier = Ed25519PublicKeyVerifier.from_path(public_key_path)
        except LineageSignerError as exc:
            console.print(f"[red]Cannot load public key:[/red] {exc}")
            return 2

    try:
        result = verify_receipt(
            receipt_path,
            expected_head=expected_head,
            verifier=verifier,
        )
    except ReceiptError as exc:
        console.print(f"[red]Receipt malformed:[/red] {exc}")
        return 1

    if result.ok:
        console.print(f"[green]Receipt verified:[/green] head={result.head_hash} steps={result.steps}")
        return 0

    console.print(f"[red]Receipt failed verification ({len(result.errors)} error(s)):[/red]")
    for err in result.errors:
        console.print(f"  - {err}")
    return 1


def replay_diff_journals(
    left_agent_id: str,
    right_agent_id: str,
    sdd_dir: Path,
    *,
    as_json: bool = False,
) -> int:
    """Walk two agent journals side-by-side and surface the first divergence.

    Per AC #5 the orchestrator never silently accepts a divergent replay;
    this command is the operator-facing surface for that check.
    """
    from bernstein.cli.helpers import console

    left_dir = _resolve_agent_dir(sdd_dir, left_agent_id)
    right_dir = _resolve_agent_dir(sdd_dir, right_agent_id)
    if not left_dir.exists() or not right_dir.exists():
        console.print("[red]One or both journals are missing.[/red]")
        return 2

    divergence = diff_journals(left_dir, right_dir)
    if divergence is None:
        if as_json:
            console.print_json(json.dumps({"diverged": False}))
        else:
            console.print("[green]No divergence; chains match end-to-end.[/green]")
        return 0

    if as_json:
        payload = {
            "diverged": True,
            "seq": divergence.seq,
            "fields_changed": list(divergence.fields_changed),
            "left_values": divergence.left_values,
            "right_values": divergence.right_values,
            "reason": divergence.reason,
        }
        console.print_json(json.dumps(payload, default=str))
        return 1

    console.print(f"[yellow]Divergence at step {divergence.seq}[/yellow]: {divergence.reason}")
    for field in divergence.fields_changed:
        left = divergence.left_values.get(field)
        right = divergence.right_values.get(field)
        console.print(f"  [bold]{field}[/bold]:")
        console.print(f"    left:  {left!r}")
        console.print(f"    right: {right!r}")
    return 1


# ---------------------------------------------------------------------------
# replay debug - forensic time-travel debugger (#2605)
# ---------------------------------------------------------------------------


def replay_debug(
    args: list[str],
    sdd_dir: Path,
    *,
    as_json: bool = False,
    fork_from: int | None = None,
    jump_to_failure: bool = False,
    sign_key_path: Path | None = None,
    full: bool = False,
    limit: int | None = None,
    repo_root: Path | None = None,
) -> int:
    """Dispatch the forensic ``replay debug`` verb. Returns the exit code.

    ``[run]`` runs the single-chain walk (optionally forking with
    ``fork_from``); ``[left, right]`` runs the two-run path diff. The surface
    is forensic: it freezes the recorded chain and proves where it diverged;
    it never re-executes anything. The debug receipt is the deliverable and
    verifies offline via the ``replay verify`` path.
    """
    from bernstein.cli.helpers import console

    if len(args) == 1:
        return _debug_single_run(
            args[0],
            sdd_dir,
            as_json=as_json,
            fork_from=fork_from,
            jump_to_failure=jump_to_failure,
            sign_key_path=sign_key_path,
            full=full,
            limit=limit,
            repo_root=repo_root,
        )
    if len(args) == 2:
        if fork_from is not None:
            console.print("[red]--fork-from applies to single-run debug only.[/red]")
            return 2
        return _debug_two_run(
            args[0],
            args[1],
            sdd_dir,
            as_json=as_json,
            jump_to_failure=jump_to_failure,
            full=full,
            limit=limit,
        )

    console.print(
        "[red]Usage:[/red] bernstein replay debug <RUN> [--fork-from N] [--sign KEY] "
        "OR bernstein replay debug <LEFT> <RIGHT>"
    )
    return 2


def _load_signer(sign_key_path: Path | None):  # type: ignore[no-untyped-def]
    """Return an ``Ed25519FileKeySigner`` for *sign_key_path*, or ``None``.

    Reuses the same signer the export path uses so a signed debug receipt is
    indistinguishable from a signed export receipt. Raises the signer's own
    ``LineageSignerError`` on a bad key so the caller can fail fast.
    """
    if sign_key_path is None:
        return None
    from bernstein.core.persistence.lineage_signer import Ed25519FileKeySigner

    return Ed25519FileKeySigner.from_path(sign_key_path)


def _debug_single_run(
    run: str,
    sdd_dir: Path,
    *,
    as_json: bool,
    fork_from: int | None,
    jump_to_failure: bool,
    sign_key_path: Path | None,
    full: bool,
    limit: int | None,
    repo_root: Path | None,
) -> int:
    """Single-chain forensic walk + optional fork + debug receipt."""
    from bernstein.cli.helpers import console
    from bernstein.core.replay.debug import walk_and_verify

    agent_dir = _resolve_agent_dir(sdd_dir, run)
    if not agent_dir.exists():
        console.print(f"[red]No journal for run[/red] {run} (looked at {agent_dir})")
        return 2

    reader = JournalReader(agent_dir)
    head_entry = reader.head()
    if head_entry is None:
        console.print(f"[yellow]Journal for {run} is empty.[/yellow]")
        return 0

    # AC1: verify the chain head before emitting any success output; a tampered
    # chain is refused (non-zero exit). AC2: the refusal still localises the
    # first divergent step so a flaky re-run becomes a reproducible bug report.
    verification = reader.verify(expected_head=head_entry.step_hash)
    if not verification.ok:
        mismatch = next(iter(walk_and_verify(reader)), None)
        return _emit_single_run_tamper(
            run,
            mismatch,
            verification.errors,
            as_json=as_json,
            jump_to_failure=jump_to_failure,
        )

    # Healthy chain: optional fork-and-reproduce, then the debug receipt.
    fork_info: dict[str, Any] | None = None
    if fork_from is not None:
        fork_info = _debug_fork(run, from_step=fork_from, repo_root=repo_root)
        if fork_info is None:
            return 1

    try:
        signer = _load_signer(sign_key_path)
    except Exception as exc:
        console.print(f"[red]Cannot load signing key:[/red] {exc}")
        return 1

    receipt_path = sdd_dir / "runtime" / "receipts" / f"{run}.debug.tar"
    try:
        result = export_receipt(agent_dir, receipt_path, agent_id=run, signer=signer)
    except ReceiptError as exc:
        console.print(f"[red]Debug receipt export failed:[/red] {exc}")
        return 1

    return _emit_single_run_ok(
        run,
        reader,
        verification,
        result,
        fork_info,
        as_json=as_json,
        jump_to_failure=jump_to_failure,
        full=full,
        limit=limit,
    )


def _emit_single_run_tamper(
    run: str,
    mismatch: Any,
    errors: list[str],
    *,
    as_json: bool,
    jump_to_failure: bool,
) -> int:
    """Report the localised tamper and refuse the chain (exit 1)."""
    from bernstein.cli.helpers import console

    payload: dict[str, Any] = {
        "mode": "single-run",
        "run": run,
        "verified": False,
        "errors": errors,
    }
    if mismatch is not None:
        payload["seq"] = mismatch.seq
        payload["first_divergent_field"] = mismatch.first_divergent_field
        payload["expected_hash"] = mismatch.expected_hash
        payload["actual_hash"] = mismatch.actual_hash
    if jump_to_failure:
        payload["jump_to_seq"] = mismatch.seq if mismatch is not None else None

    if as_json:
        console.print_json(json.dumps(payload, default=str))
        return 1

    console.print(f"[red]Chain refused for {run}: tampered / does not verify.[/red]")
    if mismatch is not None:
        console.print(
            f"[yellow]Divergence at step {mismatch.seq}[/yellow] (field: [bold]{mismatch.first_divergent_field}[/bold])"
        )
        console.print(f"[dim]expected:[/dim] {mismatch.expected_hash}")
        console.print(f"[dim]actual:  [/dim] {mismatch.actual_hash}")
    for err in errors:
        console.print(f"  - {err}")
    return 1


def _emit_single_run_ok(
    run: str,
    reader: JournalReader,
    verification: Any,
    result: Any,
    fork_info: dict[str, Any] | None,
    *,
    as_json: bool,
    jump_to_failure: bool,
    full: bool,
    limit: int | None,
) -> int:
    """Emit the healthy-chain debug receipt + bounded step projection."""
    from bernstein.cli.helpers import console

    cap = None if full else (limit if limit is not None else _DEFAULT_DEBUG_LIMIT)
    projection: list[dict[str, Any]] = []
    for entry in reader.entries():
        if cap is not None and len(projection) >= cap:
            break
        tool_name = ""
        if isinstance(entry.tool_call, dict):
            tool_name = str(entry.tool_call.get("name", ""))
        projection.append(
            {
                "seq": entry.seq,
                "step_hash": entry.step_hash,
                "prev_hash": entry.prev_hash,
                "model": entry.model,
                "tool_call": tool_name,
            }
        )

    payload: dict[str, Any] = {
        "mode": "single-run",
        "run": run,
        "verified": True,
        "head_hash": verification.head_hash,
        "steps": verification.steps,
        "receipt": {
            "path": str(result.path),
            "head_hash": result.head_hash,
            "steps": result.steps,
            "signed": result.signed,
        },
        "projection": projection,
    }
    if fork_info is not None:
        payload["fork"] = fork_info
    if jump_to_failure:
        # A healthy chain has no failure to jump to.
        payload["jump_to_seq"] = None

    if as_json:
        console.print_json(json.dumps(payload, default=str))
        return 0

    console.print(f"[bold]Replay debug for[/bold] [cyan]{run}[/cyan]")
    console.print(f"[dim]head_hash:[/dim] [bold]{verification.head_hash}[/bold]")
    console.print(f"[dim]steps:[/dim] {verification.steps}")
    console.print(f"[green]Debug receipt[/green] -> {result.path} (signed={result.signed})")
    console.print("[dim]Verify offline with:[/dim] bernstein replay verify " + str(result.path))
    if fork_info is not None:
        console.print(
            f"[cyan]Forked[/cyan] at step {fork_info['from_step']} -> "
            f"{fork_info['fork_worktree']} (anchor {fork_info['parent_step_hash'][:16]}...)"
        )
    return 0


def _debug_fork(run: str, *, from_step: int, repo_root: Path | None) -> dict[str, Any] | None:
    """Fork-and-reproduce at ``from_step`` via ``fork_session``.

    ``fork_session`` fails fast on an out-of-range seq with no side effects
    on disk, so a bad step never leaves a dangling worktree. Returns the fork
    lineage (recording ``parent_step_hash`` as the reproduction anchor), or
    ``None`` on failure.
    """
    from bernstein.cli.helpers import console
    from bernstein.core.sessions.fork import SessionForkError, fork_session

    try:
        fork = fork_session(parent_session_id=run, repo_root=repo_root, from_step=from_step)
    except SessionForkError as exc:
        console.print(f"[red]Fork failed:[/red] {exc}")
        return None

    return {
        "from_step": fork.from_step,
        "parent_step_hash": fork.parent_step_hash,
        "fork_session_id": fork.fork_session_id,
        "fork_branch": fork.fork_branch,
        "fork_worktree": str(fork.fork_worktree),
    }


def _debug_two_run(
    left: str,
    right: str,
    sdd_dir: Path,
    *,
    as_json: bool,
    jump_to_failure: bool,
    full: bool,
    limit: int | None,
) -> int:
    """Two-run time-travel path diff; writes a content-addressed artifact."""
    from bernstein.cli.helpers import console
    from bernstein.core.replay.debug import two_run_path_diff

    left_dir = _resolve_agent_dir(sdd_dir, left)
    right_dir = _resolve_agent_dir(sdd_dir, right)
    if not left_dir.exists() or not right_dir.exists():
        console.print("[red]One or both journals are missing.[/red]")
        return 2

    path_diff = two_run_path_diff(left_dir, right_dir)

    # The content-addressed artifact is the source of truth: canonical bytes,
    # chain content only, so two invocations (or two operators) match exactly.
    artifact = path_diff.to_dict()
    artifact_bytes = (json.dumps(artifact, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    artifact_dir = sdd_dir / "runtime" / "debug"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / f"{left}__{right}.pathdiff.json"
    artifact_path.write_bytes(artifact_bytes)

    # Presentation extras (display cap, jump anchor) live outside the
    # content-addressed body so they never perturb ``diff_hash``.
    cap = None if full else (limit if limit is not None else _DEFAULT_DEBUG_LIMIT)
    display_steps = path_diff.steps if cap is None else path_diff.steps[:cap]
    envelope: dict[str, Any] = {
        "mode": "two-run",
        "left": left,
        "right": right,
        "artifact_path": str(artifact_path),
        "diff_hash": path_diff.diff_hash,
        "diverged": path_diff.diverged,
        "divergence": artifact["divergence"],
        "steps": display_steps,
    }
    if jump_to_failure:
        envelope["jump_to_seq"] = path_diff.divergence.seq if path_diff.divergence is not None else None

    if as_json:
        console.print_json(json.dumps(envelope, default=str))
        return 1 if path_diff.diverged else 0

    if not path_diff.diverged:
        console.print("[green]No divergence; chains match end-to-end.[/green]")
        console.print(f"[dim]diff artifact:[/dim] {artifact_path} ({path_diff.diff_hash[:16]}...)")
        return 0

    divergence = path_diff.divergence
    assert divergence is not None  # diverged implies a divergence
    console.print(f"[yellow]Divergence at step {divergence.seq}[/yellow]: {divergence.reason}")
    for name in divergence.fields_changed:
        console.print(f"  [bold]{name}[/bold]:")
        console.print(f"    left:  {divergence.left_values.get(name)!r}")
        console.print(f"    right: {divergence.right_values.get(name)!r}")
    console.print(f"[dim]diff artifact:[/dim] {artifact_path} ([bold]{path_diff.diff_hash}[/bold])")
    return 1


__all__ = [
    "replay_agent_view",
    "replay_debug",
    "replay_diff_journals",
    "replay_export",
    "replay_publish",
    "replay_verify",
]
