"""Provenance-verified self-update surface (issue #2942).

``bernstein self`` groups the update lifecycle:

* ``bernstein self check-update``  Verify a signed release feed offline and
  seal a chain-anchored advisory. Never runs without an explicit request or
  the ``BERNSTEIN_UPDATE_CHECK`` opt-in; refused outright under the air-gap
  profile unless pointed at a locally mirrored feed.
* ``bernstein self update``        Refuse while a run is active, re-verify the
  wheel hash against the advisory before pip runs, receipt the result.
* ``bernstein self pin`` / ``unpin``  Signed version pin the updater will not
  cross without ``--override-pin``.
* ``bernstein self rollback``      Return to the previous *receipted* version,
  its wheel re-verified against the cached signed feed.

``bernstein self-update`` remains as a compatibility alias that dispatches
into the same verified flow.

Nothing in this module recommends or installs a version it has not first
verified: the trust root is required, a feed that fails its signature yields
no candidate at all, and the wheel hash is re-checked on disk immediately
before install.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any, NoReturn, cast
from urllib.parse import urlparse

import click
from rich.panel import Panel
from rich.table import Table

from bernstein.cli.helpers import (
    SDD_PID_SERVER,
    SDD_PID_SPAWNER,
    SDD_PID_WATCHDOG,
    console,
    is_alive,
    read_pid,
)
from bernstein.core.distribution.update_advisory import (
    ENV_RELEASE_FEED,
    ReleaseFeed,
    ReleaseFeedError,
    VersionPin,
    build_install_receipt,
    build_update_advisory,
    cache_is_fresh,
    fetch_release_feed,
    install_identity_pems,
    load_cached_advisory,
    load_cached_feed,
    load_release_feed,
    load_trust_root,
    pin_blocks,
    previous_receipted_version,
    read_version_pin,
    receipt_sha256,
    resolve_check_permission,
    seal_advisory,
    store_cached_advisory,
    store_cached_feed,
    store_receipt,
    trust_root_fingerprint,
    verify_advisory_document,
    verify_release_feed_document,
    verify_wheel_against_advisory,
    write_version_pin,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PACKAGE_NAME: str = "bernstein"

#: Exit code when the command cannot produce a *verified* answer. Distinct
#: from "verified, and you are behind" (0): an operator scripting around this
#: must be able to tell "could not verify" from "nothing to do".
EXIT_UNVERIFIED: int = 1

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
_FEED_OPTION = click.option(
    "--feed",
    "feed_ref",
    default=None,
    help=f"Signed release feed: a local mirrored file or an https URL (default ${ENV_RELEASE_FEED}).",
)
_TRUST_ROOT_OPTION = click.option(
    "--trust-root",
    "trust_root",
    default=None,
    type=click.Path(dir_okay=False, path_type=Path),
    help="SPKI PEM of the release signing identity (default $BERNSTEIN_RELEASE_TRUST_ROOT).",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _root(workdir: Path | None) -> Path:
    return (workdir or Path.cwd()).resolve()


def _get_installed_version() -> str:
    """Return the currently installed Bernstein version, or ``"unknown"``."""
    try:
        return _pkg_version(_PACKAGE_NAME)
    except PackageNotFoundError:
        return "unknown"


def _fail(message: str) -> NoReturn:
    """Print an operator-facing refusal and exit non-zero."""
    console.print(f"[bold red]Refusing:[/bold red] {message}")
    raise SystemExit(EXIT_UNVERIFIED)


def _active_run_blockers(root: Path) -> list[str]:
    """Return reasons this process must not swap the orchestrator right now.

    A deterministic orchestrator must not replace its own coordination code
    under a live workflow: the run's journal would record one version and the
    remaining steps would execute under another, which breaks replay before it
    breaks anything visible. Three independent signals are consulted, because
    "a run is active" is not a single flag in this codebase:

    * a detached run-service supervisor whose pid is alive;
    * a run whose ledger projection still has in-flight or scheduled tasks and
      has not been closed;
    * a live legacy server / spawner / watchdog pid file.
    """
    blockers: list[str] = []
    try:
        from bernstein.core.run_service import RunService, supervisor_status
        from bernstein.core.run_service.paths import list_run_ids

        service = RunService(root)
        for run_id in list_run_ids(root / ".sdd"):
            status = supervisor_status(root, run_id)
            if status.running:
                blockers.append(f"detached run {run_id} is live (supervisor pid {status.pid})")
                continue
            state = service.project(run_id)
            pending = len(state.in_flight_tasks) + len(state.scheduled_tasks)
            if pending and not state.run_closed:
                blockers.append(f"run {run_id} still has {pending} task(s) in flight or scheduled")
    except Exception as exc:  # an unreadable run store must fail closed
        blockers.append(f"could not establish run state ({type(exc).__name__}); refusing to update blind")

    for label, relative in (
        ("orchestration server", SDD_PID_SERVER),
        ("spawner", SDD_PID_SPAWNER),
        ("watchdog", SDD_PID_WATCHDOG),
    ):
        pid = read_pid(str(root / relative))
        if pid is not None and is_alive(pid):
            blockers.append(f"{label} is running (pid {pid})")
    return blockers


def _chain_anchor(root: Path) -> str:
    """Return the current audit-chain head, or ``""`` when unavailable."""
    try:
        from bernstein.core.security.audit_chain import AuditChainStore

        return AuditChainStore(root / ".sdd" / "audit").prev_chain_digest
    except Exception:  # an unreadable chain must not crash the check
        return ""


def _resolve_feed_document(
    feed_ref: str | None,
    *,
    explicit_request: bool,
) -> tuple[dict[str, Any], str, bool]:
    """Load the release feed document, honouring the offline posture.

    A local (mirrored) feed is always permitted -- reading a file is not
    egress, and it is the only path an air-gapped install has. A remote feed
    goes through :func:`resolve_check_permission` first and through the live
    egress policy second.

    Returns:
        ``(document, source_label, offline_profile)``.
    """
    import os

    reference = (feed_ref or os.environ.get(ENV_RELEASE_FEED, "")).strip()
    permission = resolve_check_permission(explicit_request=explicit_request)
    if not reference:
        _fail(
            f"no release feed configured; set ${ENV_RELEASE_FEED} to a mirrored feed file "
            "or the project's signed feed URL (see docs/operations/updates.md)",
        )

    if urlparse(reference).scheme in {"http", "https"}:
        if not permission.allowed:
            _fail(f"{permission.reason}; point --feed at a locally mirrored signed feed instead")
        try:
            return fetch_release_feed(reference), reference, permission.offline_profile
        except ReleaseFeedError as exc:
            _fail(str(exc))
        except Exception as exc:  # egress denial is a refusal, not a traceback
            _fail(f"release feed fetch blocked: {exc}")

    try:
        return load_release_feed(Path(reference).expanduser()), reference, permission.offline_profile
    except ReleaseFeedError as exc:
        _fail(str(exc))


def _verified_feed(
    feed_ref: str | None,
    trust_root: Path | None,
    *,
    explicit_request: bool,
) -> tuple[ReleaseFeed, dict[str, Any], str, str, bool]:
    """Load and verify a release feed. Refuses rather than returning unverified.

    Returns:
        ``(feed, document, trust_root_pem, source_label, offline_profile)``.
    """
    pem, pem_source = load_trust_root(trust_root)
    if not pem.strip():
        _fail(
            "no release trust root installed; provenance cannot be verified. "
            "Install the project signing identity at ~/.bernstein/release-trust-root.pem "
            "or pass --trust-root (see docs/operations/updates.md)",
        )
    document, source, offline = _resolve_feed_document(feed_ref, explicit_request=explicit_request)
    verification = verify_release_feed_document(document, trust_root_pem=pem)
    if not verification.ok or verification.feed is None:
        _fail(f"{verification.reason} (trust root: {pem_source})")
    return verification.feed, document, pem, source, offline


def _pip(args: list[str]) -> tuple[bool, str]:
    """Run pip in a subprocess; return ``(ok, stderr)``."""
    result = subprocess.run(
        [sys.executable, "-m", "pip", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return result.returncode == 0, (result.stderr or "").strip()


def _download_wheel(version: str, dest: Path) -> Path | None:
    """Download the wheel for *version* into *dest* without installing it.

    Downloading before installing is what makes the hash check meaningful:
    ``pip install <spec>`` would resolve and install in one step, leaving no
    artefact to verify against the advisory beforehand.
    """
    ok, stderr = _pip(
        [
            "download",
            f"{_PACKAGE_NAME}=={version}",
            "--no-deps",
            "--only-binary=:all:",
            "--dest",
            str(dest),
            "--quiet",
        ],
    )
    if not ok:
        console.print(f"[red]pip download failed:[/red]\n{stderr}")
        return None
    wheels = sorted(dest.glob("*.whl"))
    return wheels[0] if wheels else None


def _emit_receipt(
    root: Path,
    *,
    direction: str,
    from_version: str,
    to_version: str,
    wheel_sha256: str,
    key_fingerprint: str,
    advisory_hash: str,
    attestation_ok: bool | None,
) -> tuple[str, bool]:
    """Seal, store, and chain-anchor an install/rollback receipt.

    Returns:
        ``(receipt_sha256, anchored)``. The receipt is written to disk even
        when the chain append fails, so the operator never loses the record of
        what was installed.
    """
    receipt = build_install_receipt(
        from_version=from_version,
        to_version=to_version,
        wheel_sha256=wheel_sha256,
        provenance_key_fingerprint=key_fingerprint,
        advisory_sha256_value=advisory_hash,
        direction=direction,
        chain_anchor=_chain_anchor(root),
        attestation_ok=attestation_ok,
    )
    digest = receipt_sha256(receipt)
    store_receipt(receipt)
    anchored = False
    try:
        from bernstein.core.security.audit_chain import AuditChainStore, record_self_update_receipt

        record_self_update_receipt(
            chain=AuditChainStore(root / ".sdd" / "audit"),
            receipt_sha256=digest,
            direction=direction,
            from_version=from_version,
            to_version=to_version,
            wheel_sha256=wheel_sha256,
            provenance_key_fingerprint=key_fingerprint,
            advisory_sha256=advisory_hash,
            attestation_verified=attestation_ok,
        )
        anchored = True
    except Exception as exc:  # the install already happened; never crash after it
        console.print(
            f"[yellow]Could not anchor the install receipt into the audit chain: {type(exc).__name__}[/yellow]",
        )
    return digest, anchored


def _render_advisory(preimage: dict[str, Any], *, digest: str, anchored: bool) -> None:
    """Render a sealed advisory as an operator-readable table."""
    delta = cast("dict[str, Any]", preimage.get("surface_delta") or {})
    counts = cast("dict[str, int]", delta.get("counts") or {})
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Label", style="dim", no_wrap=True, min_width=22)
    table.add_column("Value")
    table.add_row("Installed", str(preimage.get("installed_version")))
    candidate = preimage.get("candidate_version")
    table.add_row("Candidate", str(candidate) if candidate else "[green]up to date[/green]")
    if candidate:
        table.add_row("Wheel sha256", str(preimage.get("candidate_wheel_sha256")))
        headline = str(delta.get("highest") or "feature")
        table.add_row(
            "Surface delta",
            f"{delta.get('total', 0)} release(s); highest surface: [bold]{headline}[/bold]",
        )
        table.add_row(
            "By surface",
            ", ".join(f"{name}={counts.get(name, 0)}" for name in sorted(counts)) or "[dim]none[/dim]",
        )
    table.add_row("Provenance", "verified" if preimage.get("provenance_verified") else "[yellow]none[/yellow]")
    table.add_row("Trust root", str(preimage.get("trust_root_fingerprint")))
    table.add_row("Feed sha256", str(preimage.get("feed_sha256")))
    table.add_row("Advisory sha256", digest)
    table.add_row("Chain anchor", str(preimage.get("checked_at_chain_anchor") or "[yellow]none[/yellow]"))
    table.add_row("Anchored", "yes" if anchored else "no")
    pin = preimage.get("pinned_version")
    if pin:
        table.add_row("Version pin", str(pin))
    console.print(table)


# ---------------------------------------------------------------------------
# self check-update
# ---------------------------------------------------------------------------


@click.group("self")
def self_group() -> None:
    """Provenance-verified update lifecycle: check, update, pin, rollback."""


@self_group.command("check-update")
@_FEED_OPTION
@_TRUST_ROOT_OPTION
@click.option(
    "--verify",
    "verify_path",
    default=None,
    type=click.Path(dir_okay=False, exists=True, path_type=Path),
    help="Recompute a sealed advisory offline and exit. Makes no network call.",
)
@click.option(
    "--cached",
    "cached_only",
    is_flag=True,
    default=False,
    help="Show the cached advisory without checking anything (never touches the network).",
)
@_WORKDIR_OPTION
@_JSON_OPTION
def check_update_cmd(
    feed_ref: str | None,
    trust_root: Path | None,
    verify_path: Path | None,
    cached_only: bool,
    workdir: Path | None,
    output_json: bool,
) -> None:
    """Verify a signed release feed and seal a chain-anchored advisory.

    \b
      bernstein self check-update                     Check and seal an advisory
      bernstein self check-update --cached            Show the last advisory only
      bernstein self check-update --verify a.json     Recompute one offline
    """
    if verify_path is not None:
        _run_verify(verify_path, output_json=output_json)
        return

    if cached_only:
        cached = load_cached_advisory()
        if cached is None:
            console.print("[dim]No cached update advisory. Run `bernstein self check-update`.[/dim]")
            raise SystemExit(EXIT_UNVERIFIED)
        _run_verify_document(cached, output_json=output_json)
        return

    root = _root(workdir)
    feed, document, pem, source, offline = _verified_feed(feed_ref, trust_root, explicit_request=True)
    pin, _pin_reason = read_version_pin()
    advisory = build_update_advisory(
        feed,
        installed_version=_get_installed_version(),
        chain_anchor=_chain_anchor(root),
        trust_root_pem=pem,
        pinned_version=pin.version if pin else None,
        offline_profile=offline,
    )
    private_pem, public_pem = install_identity_pems(root)
    sealed = seal_advisory(advisory, private_key_pem=private_pem, public_key_pem=public_pem)
    store_cached_advisory(sealed)
    store_cached_feed(document)

    anchored = False
    try:
        from bernstein.core.security.audit_chain import AuditChainStore, record_update_advisory

        record_update_advisory(
            chain=AuditChainStore(root / ".sdd" / "audit"),
            advisory_sha256=str(sealed["advisory_sha256"]),
            installed_version=advisory.installed_version,
            candidate_version=advisory.candidate_version,
            candidate_wheel_sha256=advisory.candidate_wheel_sha256,
            provenance_verified=advisory.provenance_verified,
            surface_delta=advisory.surface_delta.to_dict(),
            feed_sha256=advisory.feed_sha256,
            trust_root_fingerprint=advisory.trust_root_fingerprint,
        )
        anchored = True
    except Exception as exc:  # an audit write must never break the check
        console.print(f"[yellow]Could not anchor the advisory into the audit chain: {type(exc).__name__}[/yellow]")

    if output_json:
        console.print_json(json.dumps({"advisory": sealed, "anchored": anchored, "feed_source": source}))
        return
    console.print(f"[dim]Release feed verified against the configured trust root ({source}).[/dim]")
    _render_advisory(sealed["advisory"], digest=str(sealed["advisory_sha256"]), anchored=anchored)
    if advisory.candidate_version:
        console.print(f"\nRun [bold]bernstein self update[/bold] to install {advisory.candidate_version}.")


def _run_verify(path: Path, *, output_json: bool) -> None:
    try:
        document: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fail(f"cannot read advisory {path}: {exc}")
        return
    _run_verify_document(document, output_json=output_json)


def _run_verify_document(document: Any, *, output_json: bool) -> None:
    result = verify_advisory_document(document)
    if output_json:
        console.print_json(
            json.dumps(
                {
                    "ok": result.ok,
                    "reason": result.reason,
                    "content_hash_ok": result.content_hash_ok,
                    "signature_ok": result.signature_ok,
                    "chain_anchor": result.chain_anchor,
                    "advisory": result.advisory,
                },
            ),
        )
    elif result.ok and result.advisory is not None:
        console.print("[bold green]Advisory verified offline.[/bold green]")
        _render_advisory(result.advisory, digest=str(document.get("advisory_sha256")), anchored=True)
    else:
        console.print(
            Panel(
                f"[red]{result.reason}[/red]",
                title="Advisory verification failed",
                border_style="red",
                expand=False,
            ),
        )
    if not result.ok:
        raise SystemExit(EXIT_UNVERIFIED)


# ---------------------------------------------------------------------------
# self update
# ---------------------------------------------------------------------------


def _install_verified(
    root: Path,
    *,
    target_version: str,
    advisory_preimage: dict[str, Any],
    advisory_hash: str,
    direction: str,
    require_attestation: bool,
) -> None:
    """Download, verify, install, and receipt one version change."""
    current = _get_installed_version()
    with tempfile.TemporaryDirectory(prefix="bernstein-update-") as tmp:
        staging = Path(tmp)
        wheel = _download_wheel(target_version, staging)
        if wheel is None:
            _fail(f"could not obtain a wheel for {_PACKAGE_NAME}=={target_version}")
            return
        verdict = verify_wheel_against_advisory(
            wheel,
            advisory=advisory_preimage,
            require_attestation=require_attestation,
        )
        if not verdict.ok:
            _fail(verdict.reason)
            return
        console.print(f"[dim]{verdict.reason}[/dim]")
        console.print(f"[cyan]Installing {wheel.name}…[/cyan]")
        ok, stderr = _pip(["install", str(wheel), "--quiet"])
        if not ok:
            console.print(f"[red]pip error:[/red]\n{stderr}")
            raise SystemExit(EXIT_UNVERIFIED)

    digest, anchored = _emit_receipt(
        root,
        direction=direction,
        from_version=current,
        to_version=target_version,
        wheel_sha256=verdict.actual_sha256,
        key_fingerprint=str(advisory_preimage.get("trust_root_fingerprint", "")),
        advisory_hash=advisory_hash,
        attestation_ok=verdict.attestation_ok,
    )
    verb = "Rolled back to" if direction == "rollback" else "Upgraded to"
    console.print(f"[bold green]{verb} {_PACKAGE_NAME} {target_version}[/bold green]")
    console.print(f"[dim]Receipt {digest}{' (anchored)' if anchored else ' (not anchored)'}[/dim]")
    console.print("[dim]Restart your shell or run `bernstein --version` to confirm.[/dim]")


@self_group.command("update")
@_FEED_OPTION
@_TRUST_ROOT_OPTION
@click.option("--yes", "-y", "auto_yes", is_flag=True, default=False, help="Skip the confirmation prompt.")
@click.option(
    "--require-attestation",
    is_flag=True,
    default=False,
    help="Refuse to install unless the Sigstore build-provenance attestation verifies.",
)
@click.option(
    "--override-pin",
    is_flag=True,
    default=False,
    help="Cross an installed version pin (records the override in the receipt chain).",
)
@_WORKDIR_OPTION
def update_cmd(
    feed_ref: str | None,
    trust_root: Path | None,
    auto_yes: bool,
    require_attestation: bool,
    override_pin: bool,
    workdir: Path | None,
) -> None:
    """Install the provenance-verified candidate, never mid-run.

    Refuses while a run is active or tasks are pending, re-verifies the wheel
    hash against the advisory before pip runs, and receipts the result.
    """
    root = _root(workdir)
    blockers = _active_run_blockers(root)
    if blockers:
        console.print(
            Panel(
                "\n".join(f"- {line}" for line in blockers)
                + "\n\n[dim]Finish or stop the run, then re-run `bernstein self update`.[/dim]",
                title="Refusing to self-update: work is in flight",
                border_style="red",
                expand=False,
            ),
        )
        raise SystemExit(EXIT_UNVERIFIED)

    installed = _get_installed_version()
    feed, document, pem, source, offline = _verified_feed(feed_ref, trust_root, explicit_request=True)
    pin, pin_reason = read_version_pin()
    advisory = build_update_advisory(
        feed,
        installed_version=installed,
        chain_anchor=_chain_anchor(root),
        trust_root_pem=pem,
        pinned_version=pin.version if pin and not override_pin else None,
        offline_profile=offline,
    )
    if advisory.candidate_version is None:
        console.print(f"[green]You're up to date ({installed}).[/green]")
        if pin is not None:
            console.print(f"[dim]{pin_reason}[/dim]")
        return
    if pin is not None and pin_blocks(pin, advisory.candidate_version) and not override_pin:
        _fail(
            f"version pin {pin.version} blocks {advisory.candidate_version}; re-run with --override-pin to cross it",
        )

    private_pem, public_pem = install_identity_pems(root)
    sealed = seal_advisory(advisory, private_key_pem=private_pem, public_key_pem=public_pem)
    store_cached_advisory(sealed)
    store_cached_feed(document)
    console.print(f"[dim]Release feed verified against the configured trust root ({source}).[/dim]")
    _render_advisory(sealed["advisory"], digest=str(sealed["advisory_sha256"]), anchored=False)

    if not auto_yes and not click.confirm(f"\nUpgrade {_PACKAGE_NAME} {installed} → {advisory.candidate_version}?"):
        console.print("[dim]Update cancelled.[/dim]")
        return

    _install_verified(
        root,
        target_version=advisory.candidate_version,
        advisory_preimage=dict(sealed["advisory"]),
        advisory_hash=str(sealed["advisory_sha256"]),
        direction="install",
        require_attestation=require_attestation,
    )


# ---------------------------------------------------------------------------
# self pin / unpin
# ---------------------------------------------------------------------------


@self_group.command("pin")
@click.argument("version")
@click.option("--reason", default="", help="Why this version is standardised on.")
@_WORKDIR_OPTION
def pin_cmd(version: str, reason: str, workdir: Path | None) -> None:
    """Pin the installed version; `self update` will not cross it."""
    import datetime as dt

    root = _root(workdir)
    private_pem, public_pem = install_identity_pems(root)
    stamped = dt.datetime.now(tz=dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    path = write_version_pin(
        VersionPin(version=version, pinned_at=stamped, reason=reason),
        private_key_pem=private_pem,
        public_key_pem=public_pem,
    )
    console.print(f"[bold green]Pinned {_PACKAGE_NAME} to {version}[/bold green]")
    console.print(f"[dim]Signed pin written to {path}[/dim]")


@self_group.command("unpin")
def unpin_cmd() -> None:
    """Remove the version pin."""
    from bernstein.core.distribution.update_advisory import pin_path

    path = pin_path()
    if not path.exists():
        console.print("[dim]No version pin installed.[/dim]")
        return
    path.unlink()
    console.print(f"[bold green]Removed the version pin[/bold green] [dim]({path})[/dim]")


# ---------------------------------------------------------------------------
# self rollback
# ---------------------------------------------------------------------------


@self_group.command("rollback")
@_TRUST_ROOT_OPTION
@click.option("--yes", "-y", "auto_yes", is_flag=True, default=False, help="Skip the confirmation prompt.")
@_WORKDIR_OPTION
def rollback_cmd(trust_root: Path | None, auto_yes: bool, workdir: Path | None) -> None:
    """Return to the previous receipted version, re-verifying its wheel.

    The target comes from the receipted install history, and its wheel hash
    comes from the cached signed feed re-verified against the trust root -- so
    a rollback is provenance-checked exactly like a forward install, and needs
    no network access.
    """
    root = _root(workdir)
    blockers = _active_run_blockers(root)
    if blockers:
        console.print(
            Panel(
                "\n".join(f"- {line}" for line in blockers),
                title="Refusing to roll back: work is in flight",
                border_style="red",
                expand=False,
            ),
        )
        raise SystemExit(EXIT_UNVERIFIED)

    target = previous_receipted_version()
    if target is None:
        _fail("no receipted predecessor to roll back to; `bernstein self update` records one on every install")
        return

    pem, pem_source = load_trust_root(trust_root)
    if not pem.strip():
        _fail("no release trust root installed; cannot verify the rollback target's provenance")
    cached = load_cached_feed()
    if cached is None:
        _fail("no cached signed release feed; run `bernstein self check-update` before rolling back")
        return
    verification = verify_release_feed_document(cached, trust_root_pem=pem)
    if not verification.ok or verification.feed is None:
        _fail(f"cached release feed no longer verifies: {verification.reason} (trust root: {pem_source})")
        return
    entry = verification.feed.entry_for(target)
    if entry is None:
        _fail(f"the verified release feed carries no entry for {target}; refusing to install an unverified wheel")
        return

    current = _get_installed_version()
    console.print(f"[dim]Rolling back:[/dim] {current} → {target}")
    if not auto_yes and not click.confirm(f"Roll {_PACKAGE_NAME} back to {target}?"):
        console.print("[dim]Rollback cancelled.[/dim]")
        return

    # The rollback target's provenance comes from the same two facts a forward
    # install pins: the wheel hash the verified feed names, and the trust root
    # that vouched for that feed.
    preimage = {
        "candidate_wheel_sha256": entry.wheel_sha256,
        "trust_root_fingerprint": trust_root_fingerprint(pem),
        "feed_sha256": verification.feed.body_sha256(),
    }
    _install_verified(
        root,
        target_version=target,
        advisory_preimage=preimage,
        advisory_hash="",
        direction="rollback",
        require_attestation=False,
    )


# ---------------------------------------------------------------------------
# Compatibility alias: bernstein self-update
# ---------------------------------------------------------------------------


@click.command("self-update")
@click.option("--check", "check_only", is_flag=True, default=False, help="Check only; do not install.")
@click.option("--rollback", "rollback", is_flag=True, default=False, help="Revert to the previous receipted version.")
@click.option("--yes", "-y", "auto_yes", is_flag=True, default=False, help="Skip confirmation prompt.")
@click.pass_context
def self_update_cmd(ctx: click.Context, check_only: bool, rollback: bool, auto_yes: bool) -> None:
    """Compatibility alias for the `bernstein self` update lifecycle.

    \b
      bernstein self-update             same as `bernstein self update`
      bernstein self-update --check     same as `bernstein self check-update`
      bernstein self-update --rollback  same as `bernstein self rollback`
    """
    if rollback:
        ctx.invoke(rollback_cmd, trust_root=None, auto_yes=auto_yes, workdir=None)
        return
    if check_only:
        ctx.invoke(
            check_update_cmd,
            feed_ref=None,
            trust_root=None,
            verify_path=None,
            cached_only=False,
            workdir=None,
            output_json=False,
        )
        return
    ctx.invoke(
        update_cmd,
        feed_ref=None,
        trust_root=None,
        auto_yes=auto_yes,
        require_attestation=False,
        override_pin=False,
        workdir=None,
    )


def cached_advisory_summary() -> dict[str, Any] | None:
    """Return a compact view of the cached advisory for ``doctor`` / banners.

    Purely local. This function is the only update surface a passive command
    may call, and it cannot reach the network by construction: it reads the
    cache file and re-verifies it, nothing else.
    """
    document = load_cached_advisory()
    if document is None:
        return None
    result = verify_advisory_document(document)
    if not result.ok or result.advisory is None:
        return {"verified": False, "reason": result.reason}
    preimage = result.advisory
    delta = cast("dict[str, Any]", preimage.get("surface_delta") or {})
    return {
        "verified": True,
        "fresh": cache_is_fresh(document),
        "installed_version": preimage.get("installed_version"),
        "candidate_version": preimage.get("candidate_version"),
        "surface": delta.get("highest"),
        "releases_behind": delta.get("total", 0),
        "checked_at": preimage.get("checked_at"),
        "advisory_sha256": document.get("advisory_sha256"),
    }
