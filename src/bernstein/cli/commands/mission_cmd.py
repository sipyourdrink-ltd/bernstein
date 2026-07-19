"""``bernstein mission`` - ledger-projected multi-day goals (#2509).

A mission is a declared decomposition of a goal into phases, each with a
verification gate and a budget envelope. Mission status is never stored: it is
a pure deterministic projection over the work-ledger chain plus the evidence
bundles the phase receipts reference (see
:mod:`bernstein.core.orchestration.missions`). This command group is the
operator surface over it:

* ``define``  - validate a mission spec and write the ``mission.defined``
  transition into a fresh work ledger.
* ``status``  - project the current mission status from the ledger and print
  it (with the ``mission_status_hash`` two hosts must agree on).
* ``verify``  - re-verify the chain end to end and prove every referenced
  evidence bundle is intact; a tampered entry, a deleted bundle, or a ledger
  that declares no mission (or more than one) fails.
* ``resume``  - rebuild mission state purely by replaying the ledger on any
  clone, reproducing the identical status hash after a restart or reimage.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import click

from bernstein.cli.helpers import console
from bernstein.core.orchestration.missions import (
    MISSION_UNVERIFIED,
    MissionProjection,
    MissionSpec,
    MissionSpecError,
    define_mission,
    mission_ledger_dir,
    project_mission_from_ledger,
)
from bernstein.core.persistence.work_ledger import (
    KIND_MISSION_DEFINED,
    LedgerError,
    LedgerReader,
    WorkLedger,
)

if TYPE_CHECKING:
    from bernstein.core.chat.bridge import BridgeProtocol
    from bernstein.core.orchestration.mission_digest import MissionDigest

# Exit codes shared across the group so operators (and the dashboard) can
# branch on the specific failure mode.
EXIT_OK = 0
EXIT_NO_MISSION = 1
EXIT_VERIFY_FAILED = 2
EXIT_BAD_SPEC = 3

_WORKDIR_OPTION = click.option(
    "--workdir",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help="Project root (defaults to current directory).",
)
_JSON_OPTION = click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Emit machine-readable JSON instead of the Rich summary.",
)


def _sdd_dir(workdir: Path | None) -> Path:
    return (workdir or Path.cwd()).resolve() / ".sdd"


def _workdir(workdir: Path | None) -> Path:
    return (workdir or Path.cwd()).resolve()


@click.group("mission")
def mission_group() -> None:
    """Ledger-projected missions: define, status, verify, resume."""


# ---------------------------------------------------------------------------
# define
# ---------------------------------------------------------------------------


@mission_group.command("define")
@click.argument("spec_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@_WORKDIR_OPTION
@_JSON_OPTION
def mission_define_cmd(spec_path: Path, workdir: Path | None, output_json: bool) -> None:
    """Define a mission from a JSON spec, writing ledger entries only.

    The spec is validated at the boundary; the goal text is bound into the
    ledger by digest, never verbatim. No status row is written -- status is
    projected on demand.

    \b
    Exit codes:
        0  mission defined
        3  the spec failed validation, or the mission is already defined
    """
    try:
        raw = json.loads(spec_path.read_text(encoding="utf-8"))
        spec = MissionSpec.from_dict(raw)
    except (json.JSONDecodeError, MissionSpecError, TypeError, ValueError) as exc:
        console.print(f"[red]Invalid mission spec:[/red] {exc}")
        raise SystemExit(EXIT_BAD_SPEC) from None

    ledger_dir = mission_ledger_dir(_sdd_dir(workdir), spec.mission_id)
    try:
        ledger = WorkLedger.open(ledger_dir)
        entry = define_mission(ledger=ledger, spec=spec)
        ledger.close()
    except MissionSpecError as exc:
        # A mission owns its ledger: redefining one would let the projection
        # and the evidence lookup read different specs from the same chain.
        console.print(f"[red]Refusing to define mission:[/red] {exc}")
        raise SystemExit(EXIT_BAD_SPEC) from None
    except LedgerError as exc:
        console.print(f"[red]Failed to write mission definition:[/red] {exc}")
        raise SystemExit(EXIT_VERIFY_FAILED) from None

    if output_json:
        console.print_json(
            json.dumps(
                {
                    "mission_id": spec.mission_id,
                    "spec_hash": spec.spec_hash(),
                    "phases": list(spec.phase_ids()),
                    "entry_hash": entry.entry_hash,
                }
            )
        )
    else:
        console.print(
            f"[green]Mission defined:[/green] {spec.mission_id} "
            f"({len(spec.phases)} phase(s)), spec {spec.spec_hash()[:16]}..."
        )
        console.print(f"[dim]Ledger:[/dim] {ledger_dir}")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def _require_mission(workdir: Path | None, mission_id: str) -> None:
    """Exit with :data:`EXIT_NO_MISSION` unless *mission_id* names a real mission.

    Missions share the ledger root with plain run ledgers, so the presence of a
    ledger directory proves nothing. A mission ledger declares exactly one
    ``mission.defined`` transition naming the requested id; anything else is
    not this mission, and reporting it as verified would let ``verify`` bless a
    directory that carries no mission at all.

    The subtlety is what "is not this mission" may rest on.
    :meth:`LedgerReader.entries` yields any parseable row without checking a
    single hash, so the declared id is an attacker-controlled claim. Refusing
    on the strength of it would turn a tampered ledger into a missing one --
    the loudest signal replaced by the quietest. So only a chain that
    *verifies* is allowed to report "no such mission"; anything torn falls
    through to the projection, which renders ``ledger_verified=false`` with
    ``overall=unverified`` so ``verify`` fails on integrity rather than
    absence. A ledger carrying more than one definition is ambiguous, not
    absent, and likewise falls through to the :data:`MISSION_UNVERIFIED`
    verdict the projection forces for it.

    The HTTP route's ``_require_mission`` applies the identical rule, so the
    two surfaces agree on every shape. The chain walk runs only on the failure
    path, so a healthy mission pays nothing for it.
    """
    ledger_dir = mission_ledger_dir(_sdd_dir(workdir), mission_id)
    reader = LedgerReader(ledger_dir)
    if not reader.exists():
        console.print(f"[red]No mission ledger for {mission_id!r}[/red] at {ledger_dir}")
        raise SystemExit(EXIT_NO_MISSION)

    defined = [entry for entry in reader.entries() if entry.kind == KIND_MISSION_DEFINED]
    if len(defined) == 1 and str(defined[0].payload.get("mission_id", "")) == mission_id:
        return
    if not reader.verify().ok or len(defined) > 1:
        return

    if not defined:
        console.print(f"[red]Not a mission ledger:[/red] {ledger_dir} declares no mission")
    else:
        declared = str(defined[0].payload.get("mission_id", ""))
        console.print(f"[red]Ledger at {ledger_dir} declares mission {declared!r}, not {mission_id!r}[/red]")
    raise SystemExit(EXIT_NO_MISSION)


def _render_status(proj: MissionProjection) -> None:
    from rich.panel import Panel
    from rich.table import Table

    status = proj.status
    console.print()
    console.print(
        Panel(
            f"[bold]Mission[/bold] [cyan]{status.mission_id}[/cyan]  overall: [bold]{status.overall}[/bold]",
            border_style="green" if proj.ledger_verified else "red",
            expand=False,
        )
    )
    table = Table(show_header=True, box=None, padding=(0, 2))
    table.add_column("Phase", style="cyan", no_wrap=True)
    table.add_column("State")
    table.add_column("Envelope")
    table.add_column("Budget", justify="right")
    table.add_column("Spend", justify="right")
    for phase in status.phases:
        table.add_row(
            phase.phase_id,
            phase.state,
            phase.envelope,
            f"${phase.budget_usd:.2f}",
            f"${phase.spend_usd:.2f}",
        )
    console.print(table)
    console.print(f"[dim]status hash:[/dim] {proj.status_hash}")
    console.print(
        f"[dim]ledger verified:[/dim] {proj.ledger_verified}  [dim]evidence verified:[/dim] {proj.evidence_verified}"
    )
    console.print()


@mission_group.command("status")
@click.argument("mission_id")
@_WORKDIR_OPTION
@_JSON_OPTION
def mission_status_cmd(mission_id: str, workdir: Path | None, output_json: bool) -> None:
    """Project and print the current mission status from the ledger.

    \b
    Exit codes:
        0  status projected
        1  no ledger declaring this mission id
        2  the ledger chain does not verify (tampered entry)
    """
    _require_mission(workdir, mission_id)
    projection = project_mission_from_ledger(
        sdd_dir=_sdd_dir(workdir), workdir=_workdir(workdir), mission_id=mission_id
    )
    if output_json:
        payload = {
            **projection.status.to_dict(),
            "mission_status_hash": projection.status_hash,
            "ledger_head": projection.ledger_head,
            "ledger_verified": projection.ledger_verified,
            "evidence_verified": projection.evidence_verified,
        }
        console.print_json(json.dumps(payload))
    else:
        _render_status(projection)


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


@mission_group.command("verify")
@click.argument("mission_id")
@_WORKDIR_OPTION
@_JSON_OPTION
def mission_verify_cmd(mission_id: str, workdir: Path | None, output_json: bool) -> None:
    """Verify the mission chain and every referenced evidence bundle.

    Fails when a ledger entry was tampered with (surfaced at its exact chain
    position) or when a referenced evidence bundle is missing or altered (the
    projection marks the phase unverified).

    \b
    Exit codes:
        0  chain + evidence verify; every passed phase is provable
        1  no ledger declaring this mission id
        2  verification failed (chain torn or evidence diverged)
    """
    _require_mission(workdir, mission_id)
    projection = project_mission_from_ledger(
        sdd_dir=_sdd_dir(workdir), workdir=_workdir(workdir), mission_id=mission_id
    )
    ok = projection.ledger_verified and projection.evidence_verified and projection.status.overall != MISSION_UNVERIFIED

    if output_json:
        console.print_json(
            json.dumps(
                {
                    "mission_id": mission_id,
                    "ok": ok,
                    "ledger_verified": projection.ledger_verified,
                    "evidence_verified": projection.evidence_verified,
                    "overall": projection.status.overall,
                    "mission_status_hash": projection.status_hash,
                }
            )
        )
    elif ok:
        console.print(
            f"[green]Mission verified:[/green] {mission_id}, "
            f"overall {projection.status.overall}, status {projection.status_hash[:16]}..."
        )
    else:
        console.print(f"[red]Mission verification failed for {mission_id!r}:[/red]")
        if not projection.ledger_verified:
            console.print("  [red]-[/red] the work-ledger chain does not verify (a ledger entry was tampered with)")
        if not projection.evidence_verified:
            unverified = [p.phase_id for p in projection.status.phases if p.state == "unverified"]
            console.print(f"  [red]-[/red] evidence diverged for phase(s): {', '.join(unverified) or 'unknown'}")
    if not ok:
        raise SystemExit(EXIT_VERIFY_FAILED)


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------


@mission_group.command("resume")
@click.argument("mission_id")
@_WORKDIR_OPTION
@_JSON_OPTION
def mission_resume_cmd(mission_id: str, workdir: Path | None, output_json: bool) -> None:
    """Rebuild mission state purely by replaying the ledger on this clone.

    A mission survives restart, reimage, and machine moves with no auxiliary
    state files: the status hash reproduced here is byte-identical to the one
    projected on the host that ran the mission, provided the ledger is a
    byte-identical copy.

    \b
    Exit codes:
        0  mission state rebuilt from the ledger
        1  no ledger declaring this mission id
        2  the ledger chain does not verify (tampered entry)
    """
    _require_mission(workdir, mission_id)
    projection = project_mission_from_ledger(
        sdd_dir=_sdd_dir(workdir), workdir=_workdir(workdir), mission_id=mission_id
    )
    if output_json:
        payload = {
            **projection.status.to_dict(),
            "mission_status_hash": projection.status_hash,
            "ledger_head": projection.ledger_head,
            "ledger_verified": projection.ledger_verified,
            "evidence_verified": projection.evidence_verified,
            "entry_count": projection.entry_count,
        }
        console.print_json(json.dumps(payload))
    else:
        _render_status(projection)
        console.print(
            f"[green]Resumed mission[/green] {mission_id} from {projection.entry_count} ledger entries "
            f"(status {projection.status_hash[:16]}...)"
        )


# ---------------------------------------------------------------------------
# digest: signed daily progress digest (#2510)
# ---------------------------------------------------------------------------


_FIRE_TIME_OPTION = click.option(
    "--fire-time",
    type=int,
    required=True,
    help="Integer Unix epoch of the canonical fire instant.",
)


def _build_digest(workdir: Path | None, mission_id: str, fire_time: int) -> tuple[MissionProjection, MissionDigest]:
    from bernstein.core.orchestration.mission_digest import build_mission_digest

    projection = project_mission_from_ledger(
        sdd_dir=_sdd_dir(workdir), workdir=_workdir(workdir), mission_id=mission_id
    )
    return projection, build_mission_digest(projection, fire_time=fire_time)


def _build_bridge(platform: str, token: str) -> BridgeProtocol:
    """Construct a chat driver for *platform*, mirroring ``chat serve`` conventions.

    Overridable in tests to inject an in-memory bridge without touching the
    network. Slack additionally needs its Socket Mode app token via
    ``BERNSTEIN_SLACK_APP_TOKEN``.
    """
    import os

    if platform == "slack":
        from bernstein.core.chat.drivers.slack import SlackBridge

        app_token = os.environ.get("BERNSTEIN_SLACK_APP_TOKEN", "")
        if not app_token:
            raise click.UsageError(
                "Slack requires the Socket Mode app token in $BERNSTEIN_SLACK_APP_TOKEN (plus the bot token)."
            )
        return SlackBridge(token=token, app_token=app_token)
    if platform == "discord":
        from bernstein.core.chat.drivers.discord import DiscordBridge

        return DiscordBridge(token=token)
    if platform == "telegram":
        from bernstein.core.chat.drivers.telegram import TelegramBridge

        return TelegramBridge(token=token)
    raise click.UsageError(f"unsupported platform {platform!r}: expected slack, discord, or telegram")


@mission_group.group("digest")
def digest_group() -> None:
    """Signed daily progress digests: show, send, verify."""


@digest_group.command("show")
@click.argument("mission_id")
@_FIRE_TIME_OPTION
@_WORKDIR_OPTION
@_JSON_OPTION
def mission_digest_show_cmd(mission_id: str, fire_time: int, workdir: Path | None, output_json: bool) -> None:
    """Compute and print the canonical digest for a fire instant (read-only).

    The digest is a pure fold over the ledger at ``fire_time``; it writes
    nothing to the chain. Two operators with the same ledger print the same
    ``digest_hash``.

    \b
    Exit codes:
        0  digest computed
        1  no ledger declaring this mission id
        2  the ledger chain does not verify (tampered entry)
    """
    from bernstein.core.orchestration.mission_digest import render_digest_message

    _require_mission(workdir, mission_id)
    _projection, digest = _build_digest(workdir, mission_id, fire_time)
    message = render_digest_message(digest)
    if output_json:
        payload = {
            **digest.to_dict(),
            "digest_hash": digest.digest_hash(),
            "receipt_id": digest.receipt_id(),
            "message": message,
        }
        console.print_json(json.dumps(payload))
    else:
        console.print(message)


@digest_group.command("send")
@click.argument("mission_id")
@_FIRE_TIME_OPTION
@click.option("--platform", type=click.Choice(["slack", "discord", "telegram"]), required=True)
@click.option("--thread", "thread_id", required=True, help="Chat thread / channel to post the digest to.")
@click.option("--token", default="", help="Bot token (falls back to the platform's env var).")
@click.option("--recurrence", default="", help="Canonical recurrence rule the fire was produced by.")
@_WORKDIR_OPTION
@_JSON_OPTION
def mission_digest_send_cmd(
    mission_id: str,
    fire_time: int,
    platform: str,
    thread_id: str,
    token: str,
    recurrence: str,
    workdir: Path | None,
    output_json: bool,
) -> None:
    """Record a digest receipt and post it to chat, idempotent per fire.

    Delivery is keyed on the digest receipt id, so re-running the same fire
    (including after a restart) records nothing new and posts nothing.

    \b
    Exit codes:
        0  digest posted, or already delivered (idempotent no-op)
        1  no ledger declaring this mission id
        2  the ledger chain does not verify (tampered entry)
    """
    import asyncio

    from bernstein.core.orchestration.mission_digest_delivery import run_digest_fire
    from bernstein.core.security.audit_chain import AuditChainStore

    _require_mission(workdir, mission_id)
    resolved_token = token or _platform_token(platform)
    bridge = _build_bridge(platform, resolved_token)
    chain = AuditChainStore(_sdd_dir(workdir) / "audit")

    result = asyncio.run(
        run_digest_fire(
            sdd_dir=_sdd_dir(workdir),
            workdir=_workdir(workdir),
            mission_id=mission_id,
            fire_time=fire_time,
            chain=chain,
            bridge=bridge,
            thread_id=thread_id,
            recurrence=recurrence,
        )
    )
    outcome = result.outcome
    if output_json:
        console.print_json(
            json.dumps(
                {
                    "mission_id": mission_id,
                    "digest_hash": result.digest.digest_hash(),
                    "receipt_id": outcome.receipt_id,
                    "posted": outcome.posted,
                    "message_id": outcome.message_id,
                    "reason": outcome.reason,
                    "recorded_receipt": result.recorded_receipt,
                    "fire_graph_hash": result.fire_graph_hash,
                }
            )
        )
    elif outcome.posted:
        console.print(
            f"[green]Digest posted[/green] to {platform}:{thread_id} "
            f"(digest {result.digest.digest_hash()[:16]}..., receipt {outcome.receipt_id})"
        )
    else:
        console.print(f"[yellow]Digest already delivered[/yellow] (receipt {outcome.receipt_id}); no double-post.")


def _platform_token(platform: str) -> str:
    import os

    env = {
        "slack": "BERNSTEIN_SLACK_BOT_TOKEN",
        "discord": "BERNSTEIN_DISCORD_TOKEN",
        "telegram": "BERNSTEIN_TELEGRAM_TOKEN",
    }[platform]
    token = os.environ.get(env, "")
    if not token:
        raise click.UsageError(f"{platform} requires a bot token via --token or ${env}.")
    return token


@digest_group.command("verify")
@click.argument("mission_id")
@_FIRE_TIME_OPTION
@click.option(
    "--message",
    "message_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Path to the posted chat message text to verify against the ledger.",
)
@_WORKDIR_OPTION
@_JSON_OPTION
def mission_digest_verify_cmd(
    mission_id: str,
    fire_time: int,
    message_path: Path,
    workdir: Path | None,
    output_json: bool,
) -> None:
    """Recompute the digest from the ledger and prove a posted message matches.

    Recomputes the canonical digest at ``fire_time`` from the ledger, then
    checks the posted message equals the digest's verbatim projection. An
    edited or truncated message is detected as a mismatch. When an audit chain
    is present the recorded digest receipt is cross-checked too: the chain must
    verify and carry a receipt binding this exact ``digest_hash``.

    \b
    Exit codes:
        0  the posted message matches the ledger-recomputed digest
        1  no ledger declaring this mission id
        2  mismatch (edited / truncated message, or a torn receipt)
    """
    from bernstein.core.orchestration.mission_digest import verify_message_matches
    from bernstein.core.security.audit_chain import EVENT_MISSION_DIGEST_RECEIPT, AuditChainStore

    _require_mission(workdir, mission_id)
    _projection, digest = _build_digest(workdir, mission_id, fire_time)
    posted = message_path.read_text(encoding="utf-8")
    result = verify_message_matches(posted, digest)

    # Optional receipt-side proof: the chain must verify and carry the receipt.
    audit_dir = _sdd_dir(workdir) / "audit"
    chain_verified: bool | None = None
    receipt_present: bool | None = None
    if audit_dir.exists():
        chain = AuditChainStore(audit_dir)
        chain_verified = chain.verify()[0]
        events = chain.query(event_type=EVENT_MISSION_DIGEST_RECEIPT)
        receipt_present = any(e.details.get("digest_hash") == digest.digest_hash() for e in events)

    ok = result.matches and (chain_verified is not False) and (receipt_present is not False)
    if output_json:
        console.print_json(
            json.dumps(
                {
                    "mission_id": mission_id,
                    "ok": ok,
                    "message_matches": result.matches,
                    "reason": result.reason,
                    "digest_hash": digest.digest_hash(),
                    "receipt_id": digest.receipt_id(),
                    "chain_verified": chain_verified,
                    "receipt_present": receipt_present,
                }
            )
        )
    elif ok:
        console.print(
            f"[green]Digest verified:[/green] posted message matches the ledger-recomputed digest "
            f"{digest.digest_hash()[:16]}..."
        )
    else:
        console.print(f"[red]Digest verification failed for {mission_id!r}:[/red] {result.reason}")
        if chain_verified is False:
            console.print("  [red]-[/red] the audit chain does not verify (a receipt was tampered with)")
        if receipt_present is False:
            console.print("  [red]-[/red] no chain receipt binds this digest hash")
    if not ok:
        raise SystemExit(EXIT_VERIFY_FAILED)


__all__ = [
    "EXIT_BAD_SPEC",
    "EXIT_NO_MISSION",
    "EXIT_OK",
    "EXIT_VERIFY_FAILED",
    "mission_group",
]
