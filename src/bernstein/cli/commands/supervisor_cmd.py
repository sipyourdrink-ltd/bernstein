"""CLI command group: ``bernstein supervisor`` - operator stall surface.

Two subcommands wire together the supervisor primitives:

* ``bernstein supervisor status [--json]`` - aggregates the stalled
  manager, watchdog, heartbeat, and respawn-budget data sources into a
  single table (or JSON document) keyed by worker.
* ``bernstein supervisor escalate <session_id> --reason "..."`` -
  records an escalation event in the audit chain, persists a signed
  escalation receipt, and fires the ``worker.escalated`` lifecycle
  event so external notifiers can route the alert.

The command never rewrites detection logic; every classification comes
from the existing source modules. See
:mod:`bernstein.core.orchestration.supervisor_aggregator` and
:mod:`bernstein.core.orchestration.supervisor_receipt` for the
underlying primitives.
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from bernstein.core.defaults import AGENT
from bernstein.core.lifecycle.hooks import LifecycleContext, LifecycleEvent
from bernstein.core.orchestration.supervisor_aggregator import (
    SupervisorSnapshot,
    aggregator_snapshot,
    snapshot_to_dict,
)
from bernstein.core.orchestration.supervisor_receipt import (
    EscalationReceipt,
    IdentityTokens,
    StallReason,
    assemble_receipt,
    receipt_to_dict,
    sign_receipt,
)
from bernstein.core.security.audit import AuditLog

logger = logging.getLogger(__name__)


SUPERVISOR_RECEIPT_DIR = ".sdd/runtime/supervisor/receipts"
SUPERVISOR_AUDIT_DIR = ".sdd/audit"
INSTALL_SIGNING_KEY_ENV = "BERNSTEIN_SUPERVISOR_SIGNING_KEY"
DEFAULT_INSTALL_SIGNING_KEY = ".sdd/runtime/supervisor/install.key"


# ---------------------------------------------------------------------------
# Status command
# ---------------------------------------------------------------------------


@click.group("supervisor")
def supervisor_group() -> None:
    """Operator supervisor surface for stuck and parked workers.

    \b
    Examples:
      bernstein supervisor status
      bernstein supervisor status --json
      bernstein supervisor escalate sess-123 --reason "wedged on auth"
    """


@supervisor_group.command("status")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit machine-readable JSON instead of a table.",
)
@click.option(
    "--workdir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Override the workspace root (defaults to cwd).",
)
def supervisor_status(as_json: bool, workdir: Path | None) -> None:
    """List every live worker with stall classification and recommended action."""
    root = workdir or Path.cwd()
    snapshot = aggregator_snapshot(
        root,
        heartbeat_stale_s=AGENT.heartbeat_stale_s,
    )

    if as_json:
        click.echo(json.dumps(snapshot_to_dict(snapshot), sort_keys=True, indent=2))
        return

    console = Console()
    _render_status_table(console, snapshot)


def _render_status_table(console: Console, snapshot: SupervisorSnapshot) -> None:
    """Render a Rich table view of the snapshot for interactive operators."""
    if not snapshot.workers:
        console.print("[dim]No live workers tracked under .sdd/runtime/.[/dim]")
        return

    table = Table(title="Bernstein supervisor", show_header=True, header_style="bold")
    table.add_column("worker")
    table.add_column("role")
    table.add_column("task")
    table.add_column("heartbeat", justify="right")
    table.add_column("status")
    table.add_column("stall")
    table.add_column("recommend")
    table.add_column("budget", justify="right")
    for row in snapshot.workers:
        hb = "-"
        if row.last_heartbeat_age_s is not None:
            hb = f"{int(row.last_heartbeat_age_s)}s"
        stuck_label = "STUCK" if row.is_stuck else "ok"
        table.add_row(
            str(row.worker_id),
            row.role or "-",
            row.task_id or "-",
            hb,
            stuck_label,
            row.stall_reason.value if row.is_stuck else "-",
            row.recommended_action.value,
            str(row.respawn_budget_remaining),
        )
    console.print(table)
    summary = f"{snapshot.stuck_count} stuck" + (
        f", oldest {int(snapshot.oldest_stall_age_s)}s" if snapshot.oldest_stall_age_s else ""
    )
    console.print(f"[dim]{summary}[/dim]")


# ---------------------------------------------------------------------------
# Escalate command
# ---------------------------------------------------------------------------


@supervisor_group.command("escalate")
@click.argument("session_id", required=True)
@click.option(
    "--reason",
    required=True,
    help="Operator-supplied reason string recorded in the audit chain.",
)
@click.option(
    "--workdir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Override the workspace root (defaults to cwd).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit machine-readable JSON for the persisted receipt.",
)
def supervisor_escalate(
    session_id: str,
    reason: str,
    workdir: Path | None,
    as_json: bool,
) -> None:
    """Record an explicit operator escalation in the audit chain.

    Writes a signed escalation receipt under
    ``.sdd/runtime/supervisor/receipts/`` and fires the
    ``worker.escalated`` lifecycle event so external notifiers route the
    alert.
    """
    reason = reason.strip()
    if not reason:
        raise click.ClickException("--reason must be a non-empty string")
    root = workdir or Path.cwd()
    snapshot = aggregator_snapshot(root, heartbeat_stale_s=AGENT.heartbeat_stale_s)
    row = next((w for w in snapshot.workers if w.session_id == session_id), None)
    if row is None:
        raise click.ClickException(
            f"session {session_id!r} not tracked in the supervisor snapshot",
        )

    # Load (or generate) the install Ed25519 signing key.
    signing_key_path = _resolve_signing_key_path(root)
    signing_key = _load_or_create_install_key(signing_key_path)

    from bernstein.core.security.audit_dsse import keyid_from_public_key

    keyid = keyid_from_public_key(signing_key.public_key())

    identity = IdentityTokens(
        install_rev=_load_install_rev(root),
        keyid=keyid,
        run_id=_load_run_id(root),
    )

    # Assemble the receipt from the aggregator row's failure slice.
    from bernstein.core.orchestration.supervisor_aggregator import (
        load_recent_failures,
    )

    failures = load_recent_failures(root, session_id)
    audit_entries: list[dict[str, Any]] = [
        {
            "event_type": str(rec.get("kind", "")),
            "session_id": session_id,
            "details": rec,
        }
        for rec in failures
    ]
    # Append the operator escalation as the trailing entry so the receipt
    # captures the operator's intent even when no prior diagnostic exists.
    audit_entries.append(
        {
            "event_type": "supervisor.escalate",
            "session_id": session_id,
            "details": {"reason": reason},
        }
    )

    prev_digest = _read_chain_anchor(root)
    receipt = assemble_receipt(
        worker_id=row.worker_id,
        worktree_id=row.worktree_id,
        session_id=session_id,
        stall_reason=row.stall_reason if row.is_stuck else StallReason.UNKNOWN,
        audit_entries=audit_entries,
        identity=identity,
        prev_chain_digest=prev_digest,
        respawn_budget_remaining=row.respawn_budget_remaining,
        details={
            "operator_reason": reason,
            "respawn_budget_remaining": row.respawn_budget_remaining,
        },
    )
    signed = sign_receipt(receipt, signing_key=signing_key)
    receipt_path = _persist_receipt(root, signed)

    # Append an escalation entry to the audit log so any verifier can
    # walk the existing HMAC chain back to this receipt.
    _append_escalation_audit(
        root,
        session_id=session_id,
        worker_id=row.worker_id,
        receipt_path=receipt_path,
        reason=reason,
        receipt_digest=signed.payload_digest,
    )

    # Fire the lifecycle event so external notifiers (Slack/Discord/etc.)
    # can route the escalation.
    _fire_worker_escalated_event(
        root,
        session_id=session_id,
        worker_id=row.worker_id,
        receipt_path=receipt_path,
        receipt=signed,
        reason=reason,
    )

    payload = {
        "session_id": session_id,
        "worker_id": row.worker_id,
        "receipt_path": str(receipt_path),
        "receipt_digest": signed.payload_digest,
        "recommended_action": signed.recommended_action.value,
        "stall_reason": signed.stall_reason.value,
    }
    if as_json:
        click.echo(json.dumps(payload, sort_keys=True, indent=2))
        return
    click.echo(
        f"escalated {session_id} - receipt {receipt_path} (recommend {signed.recommended_action.value})",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_signing_key_path(root: Path) -> Path:
    """Resolve the install signing-key path.

    Precedence:

    1. ``$BERNSTEIN_SUPERVISOR_SIGNING_KEY`` (explicit override).
    2. ``<workdir>/.sdd/runtime/supervisor/install.key``.
    """
    override = os.environ.get(INSTALL_SIGNING_KEY_ENV)
    if override:
        return Path(override).expanduser()
    return root / DEFAULT_INSTALL_SIGNING_KEY


def _load_or_create_install_key(path: Path) -> Any:
    """Load or generate the install Ed25519 signing key at ``path``.

    Reuses an existing 32-byte seed when the file is present; generates
    a fresh keypair otherwise and persists the raw seed with mode 0600.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if path.exists():
        try:
            # Read the seed exactly as written: the create path below persists
            # the raw 32-byte private seed with no trailing delimiter, so the
            # bytes on disk ARE the key. Do NOT strip(): a random Ed25519 seed
            # ends (or starts) with an ASCII-whitespace byte (0x09, 0x0a, 0x0b,
            # 0x0c, 0x0d, 0x20) ~4.7% of the time, and strip() would silently
            # drop it, corrupting a valid key into a "not 32 raw bytes" error.
            raw = path.read_bytes()
        except OSError as exc:
            raise click.ClickException(f"cannot read signing key {path}: {exc}") from exc
        if len(raw) != 32:
            raise click.ClickException(
                f"install signing key {path} is not 32 raw bytes; refusing to use it",
            )
        return Ed25519PrivateKey.from_private_bytes(raw)

    path.parent.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        path.parent.chmod(0o700)
    priv = Ed25519PrivateKey.generate()
    raw_bytes = priv.private_bytes_raw()
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, raw_bytes)
    finally:
        os.close(fd)
    path.chmod(0o600)
    return priv


def _load_install_rev(_root: Path) -> str:
    """Return the install fingerprint (best-effort).

    Empty string when the identity module is unavailable or raises;
    failures are logged so a missing fingerprint never silently turns
    into a tampering false negative during a postmortem.
    """
    try:
        from bernstein.core.identity.install_rev import get_install_rev
    except ImportError:
        logger.warning("install_rev module unavailable - receipt identity tokens will be empty")
        return ""
    try:
        return get_install_rev()
    except Exception:  # pragma: no cover - defensive
        logger.exception("install_rev lookup failed during escalation; using empty fingerprint")
        return ""


def _load_run_id(root: Path) -> str:
    """Return the current run id, if recorded under ``.sdd/runtime/``."""
    run_id_path = root / ".sdd" / "runtime" / "run_id"
    if not run_id_path.exists():
        return ""
    try:
        return run_id_path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _read_chain_anchor(root: Path) -> str:
    """Return the most recent audit-chain HMAC, or the genesis sentinel.

    Reads :class:`AuditLog` to recover the chain tail without writing.
    Returns ``"0" * 64`` (the genesis sentinel) only when no audit log
    directory exists. A directory that exists but is unreadable is a
    structural failure - we refuse to silently reset the chain anchor
    because doing so would let a fresh receipt skip the previous chain
    head and break the tamper-evidence guarantee.
    """
    audit_dir = root / SUPERVISOR_AUDIT_DIR
    if not audit_dir.exists():
        return "0" * 64
    try:
        log = AuditLog(audit_dir=audit_dir)
    except Exception as exc:  # pragma: no cover - audit setup failures
        logger.error("Failed to load audit log at %s", audit_dir, exc_info=True)
        raise click.ClickException(
            f"cannot read audit chain anchor from {audit_dir}: {exc}",
        ) from exc
    # ``AuditLog._prev_hmac`` is the recovered chain tail.
    return getattr(log, "_prev_hmac", "0" * 64)


def _persist_receipt(root: Path, receipt: EscalationReceipt) -> Path:
    """Write the receipt to ``.sdd/runtime/supervisor/receipts/<digest>.json``.

    Filenames use nanosecond timestamps to avoid collisions when the
    operator escalates the same session twice in the same second. The
    open is exclusive (``"x"``) so a colliding filename surfaces as an
    explicit ``FileExistsError`` rather than silently overwriting a
    prior receipt.
    """
    dest_dir = root / SUPERVISOR_RECEIPT_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{time.time_ns()}-{receipt.session_id}-{receipt.payload_digest[:12]}.json"
    path = dest_dir / fname
    with path.open("x", encoding="utf-8") as fh:
        fh.write(json.dumps(receipt_to_dict(receipt), sort_keys=True, indent=2))
    return path


def _append_escalation_audit(
    root: Path,
    *,
    session_id: str,
    worker_id: str,
    receipt_path: Path,
    reason: str,
    receipt_digest: str,
) -> None:
    """Append a ``supervisor.escalated`` event to the audit log."""
    audit_dir = root / SUPERVISOR_AUDIT_DIR
    try:
        audit_dir.mkdir(parents=True, exist_ok=True)
        log = AuditLog(audit_dir=audit_dir)
        log.log(
            event_type="supervisor.escalated",
            actor="operator",
            resource_type="session",
            resource_id=session_id,
            details={
                "worker_id": worker_id,
                "reason": reason,
                "receipt_path": str(receipt_path),
                "receipt_digest": receipt_digest,
            },
        )
    except Exception:  # pragma: no cover - never block the CLI on audit IO
        logger.debug("Failed to append supervisor.escalated audit entry", exc_info=True)


def _fire_worker_escalated_event(
    root: Path,
    *,
    session_id: str,
    worker_id: str,
    receipt_path: Path,
    receipt: EscalationReceipt,
    reason: str,
) -> None:
    """Fire the ``worker.escalated`` lifecycle event for external notifiers."""
    try:
        from bernstein.core.lifecycle import hooks as _hooks_mod
    except ImportError:  # pragma: no cover - lifecycle always present
        return
    registry = getattr(_hooks_mod, "GLOBAL_REGISTRY", None)
    if registry is None:
        return
    ctx = LifecycleContext(
        event=LifecycleEvent.WORKER_ESCALATED,
        session_id=session_id,
        workdir=root,
        data={
            "worker_id": worker_id,
            "reason": reason,
            "receipt_path": str(receipt_path),
            "recommended_action": receipt.recommended_action.value,
            "stall_reason": receipt.stall_reason.value,
        },
    )
    try:
        registry.run(LifecycleEvent.WORKER_ESCALATED, ctx)
    except Exception:  # pragma: no cover - notifier failures must not block
        logger.debug("worker.escalated lifecycle emit failed", exc_info=True)
