"""Verify CLI -- run receipts, ladder receipts, WAL integrity, determinism, memory, wheelhouse.

``bernstein verify`` is a group. Two run-receipt verbs (issue #2924) and a
ladder-receipt verb (issue #2927):

* ``bernstein verify run <run-id>`` -- build the signed ``run-receipt.json``
  binding the run's journal head, lineage-spine head, and (opt-in) an
  audit-chain range under one Ed25519-signed subject with an embedded
  public JWK.
* ``bernstein verify receipt <path> [--public-key PEM]`` -- verify a
  receipt **fully offline** from the file alone: no HMAC key, no ``.sdd/``.
  Exit codes: ``0`` OK, ``1`` empty/malformed input, ``2`` tamper detected
  (naming the first divergent journal step index).
* ``bernstein verify ladder <receipt-hash>`` -- re-derive a pre-merge
  verifier-ladder receipt: per-tier ``tier / config_hash / evidence_hash /
  verdict`` plus the composite claim, re-checked against the
  ``verifier-ladder`` lineage spine. Exit codes: ``0`` OK, ``1`` no readable
  receipt, ``2`` re-derivation or anchor mismatch.

The five legacy flag/positional modes are preserved verbatim under the
default ``legacy`` subcommand -- any invocation whose first token is not a
known subcommand routes there, so existing scripts keep their exact
behaviour and exit codes:

* ``bernstein verify <wheelhouse-path>`` -- verify air-gap wheelhouse
  manifest + signatures (cosign by default; GPG path supported).
* ``bernstein verify --wal-integrity <run-id>`` -- replay WAL hash chain
  for a run; non-zero exit on any mismatch.
* ``bernstein verify --determinism <run-id>`` -- compute execution
  fingerprint (decision-trace hash) so the run is reproducible.
* ``bernstein verify --memory-audit`` -- walk lesson-memory provenance
  for OWASP Agent Security Initiative ASI06 (Memory & Context Poisoning,
  2026); refuses to OK any unsigned write.
* ``bernstein verify --formal <task-id>`` -- spawn Z3 / Lean4 property
  checks against the task contract. The CLI surface is shipped; Z3 / Lean4
  binaries must be installed separately on PATH (no bundled extra).

One routing edge: a wheelhouse directory literally named ``run``,
``receipt``, ``ladder``, or ``legacy`` shadows the positional mode -- spell
it ``./run`` (or use ``bernstein verify legacy run``) in that case.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import click
from rich.panel import Panel
from rich.table import Table

from bernstein.cli.helpers import console

if TYPE_CHECKING:
    from bernstein.core.wal import WALEntryDigest

_GREEN_ZERO = "[green]0[/green]"

SDD_DIR = Path(".sdd")


class _DefaultSubcommandGroup(click.Group):
    """A group that routes to a default subcommand when the first token is not
    a known subcommand, so ``verify --wal-integrity ...`` and
    ``verify <wheelhouse-path>`` still reach the legacy flag modes.

    Same pattern as the compliance ``pack`` group
    (:mod:`bernstein.cli.commands.compliance_cmd`).
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        self._default_cmd = kwargs.pop("default_cmd", None)
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        default = self._default_cmd
        if isinstance(default, str):
            if not args:
                args = [default]
            elif args[0] not in self.commands and args[0] not in ("--help", "-h"):
                args = [default, *args]
        return super().parse_args(ctx, args)


@click.group("verify", cls=_DefaultSubcommandGroup, default_cmd="legacy")
def verify_cmd() -> None:
    """Verify run receipts, ladder receipts, WAL integrity, determinism, memory, or a wheelhouse.

    \b
      bernstein verify run <run-id>               Build the signed run receipt
      bernstein verify receipt <path>             Verify a receipt offline (0/1/2)
      bernstein verify ladder <receipt-hash>      Re-derive a verifier-ladder receipt (0/1/2)
      bernstein verify <wheelhouse-path>          Verify air-gap wheelhouse signatures
      bernstein verify --wal-integrity <run-id>   Validate hash chain
      bernstein verify --determinism  <run-id>    Show execution fingerprint
      bernstein verify --memory-audit             Audit lesson memory provenance
      bernstein verify --formal <task-id>         Run Z3/Lean4 property checks

    Flag and positional modes route to the ``legacy`` subcommand unchanged.
    """


@verify_cmd.command("legacy")
@click.argument(
    "wheelhouse_path",
    required=False,
    default=None,
    type=click.Path(exists=False, file_okay=False, dir_okay=True, path_type=Path),
)
@click.option(
    "--wal-integrity",
    "wal_run_id",
    default=None,
    metavar="RUN_ID",
    help="Verify WAL hash chain integrity for a run.",
)
@click.option(
    "--determinism",
    "determinism_run_id",
    default=None,
    metavar="RUN_ID",
    help="Compute and display execution fingerprint for a run.",
)
@click.option(
    "--expect",
    "expect_fingerprint",
    default=None,
    metavar="FINGERPRINT",
    help="Gate --determinism: exit non-zero unless the run's fingerprint equals this value.",
)
@click.option(
    "--baseline",
    "baseline_run_id",
    default=None,
    metavar="RUN_ID",
    help="Gate --determinism: exit non-zero unless the run reproduces this baseline run's fingerprint.",
)
@click.option(
    "--memory-audit",
    "memory_audit",
    is_flag=True,
    default=False,
    help="Audit lesson memory provenance chain (OWASP ASI06 2026).",
)
@click.option(
    "--formal",
    "formal_task_id",
    default=None,
    metavar="TASK_ID",
    help="Run Z3/Lean4 formal property checks for a completed task.",
)
@click.option(
    "--ca-pubkey",
    "ca_pubkey",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Public key (PEM) for wheelhouse signature verification. Defaults to the bundled release key.",
)
@click.option(
    "--require-signatures/--no-require-signatures",
    "require_signatures",
    default=False,
    help="When set, wheelhouse verify exits non-zero if any signature is missing.",
)
@click.option(
    "--require-customer-sig/--no-require-customer-sig",
    "require_customer_sig",
    default=False,
    help="When set, wheelhouse verify exits non-zero unless MANIFEST.customer.sig "
    "is present and validates against .bernstein/trust/customer-keys/.",
)
@click.option(
    "--customer-trust-dir",
    "customer_trust_dir",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help="Override the customer-key trust directory.",
)
@click.option(
    "--sigstore/--no-sigstore",
    "sigstore",
    default=False,
    help="Additively verify Sigstore build-provenance attestations "
    "(`actions/attest-build-provenance`) for every wheel via `gh attestation verify`. "
    "Default behaviour is unchanged when this flag is off.",
)
@click.option(
    "--sigstore-owner",
    "sigstore_owner",
    default=None,
    metavar="OWNER",
    help="GitHub owner whose attestations are accepted. Defaults to the project owner.",
)
@click.option(
    "--sigstore-repo",
    "sigstore_repo",
    default=None,
    metavar="OWNER/REPO",
    help="Optional repo to pin attestations to.",
)
@click.option(
    "--sigstore-offline/--no-sigstore-offline",
    "sigstore_offline",
    default=False,
    help="Verify against a local .sigstore bundle next to each artefact (or in --sigstore-bundle-dir). "
    "Air-gap-friendly path.",
)
@click.option(
    "--sigstore-bundle-dir",
    "sigstore_bundle_dir",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help="Directory of pre-downloaded .sigstore bundles for offline verification.",
)
@click.option(
    "--require-sigstore/--no-require-sigstore",
    "require_sigstore",
    default=False,
    help="Promote a missing attestation to a hard failure. Implies --sigstore.",
)
def verify_legacy_cmd(
    wheelhouse_path: Path | None,
    wal_run_id: str | None,
    determinism_run_id: str | None,
    expect_fingerprint: str | None,
    baseline_run_id: str | None,
    memory_audit: bool,
    formal_task_id: str | None,
    ca_pubkey: Path | None,
    require_signatures: bool,
    require_customer_sig: bool,
    customer_trust_dir: Path | None,
    sigstore: bool,
    sigstore_owner: str | None,
    sigstore_repo: str | None,
    sigstore_offline: bool,
    sigstore_bundle_dir: Path | None,
    require_sigstore: bool,
) -> None:
    """Verify WAL integrity, execution determinism, memory provenance, formal properties, or a wheelhouse.

    The default subcommand: ``bernstein verify <args>`` routes here whenever
    the first token is not ``run`` / ``receipt`` / ``ladder`` / ``legacy``,
    so every pre-group invocation keeps its exact behaviour and exit codes.

    \b
      bernstein verify <wheelhouse-path>          Verify air-gap wheelhouse signatures
      bernstein verify --wal-integrity <run-id>   Validate hash chain
      bernstein verify --determinism  <run-id>    Show execution fingerprint
      bernstein verify --determinism  <run-id> --expect <fp>     Gate on a recorded fingerprint
      bernstein verify --determinism  <run-b> --baseline <run-a> Gate that run-b reproduces run-a
      bernstein verify --memory-audit             Audit lesson memory provenance
      bernstein verify --formal <task-id>         Run Z3/Lean4 property checks
    """
    # --expect / --baseline only have meaning as a gate on --determinism, and
    # they are mutually exclusive (an explicit digest XOR a baseline run). Reject
    # ambiguous combinations rather than silently picking one.
    if expect_fingerprint is not None and baseline_run_id is not None:
        raise click.UsageError("--expect and --baseline are mutually exclusive; pass only one.")
    if (expect_fingerprint is not None or baseline_run_id is not None) and determinism_run_id is None:
        raise click.UsageError("--expect/--baseline require --determinism <run-id>.")

    if wheelhouse_path is wal_run_id is determinism_run_id is formal_task_id is None and not memory_audit:
        console.print(
            "[dim]Use <wheelhouse-path>, --wal-integrity <run-id>, --determinism <run-id>, "
            "--memory-audit, or --formal <task-id>.[/dim]"
        )
        console.print("[dim]WAL files are stored in .sdd/runtime/wal/<run-id>.wal.jsonl[/dim]")
        return

    exit_code = 0

    if wheelhouse_path is not None:
        exit_code |= _verify_wheelhouse(
            wheelhouse_path,
            ca_pubkey=ca_pubkey,
            require_signatures=require_signatures,
            require_customer_sig=require_customer_sig,
            customer_trust_dir=customer_trust_dir,
            sigstore=sigstore or require_sigstore,
            sigstore_owner=sigstore_owner,
            sigstore_repo=sigstore_repo,
            sigstore_offline=sigstore_offline,
            sigstore_bundle_dir=sigstore_bundle_dir,
            require_sigstore=require_sigstore,
        )

    if wal_run_id is not None:
        exit_code |= _verify_wal_integrity(wal_run_id)

    if determinism_run_id is not None:
        exit_code |= _verify_determinism(
            determinism_run_id,
            expect=expect_fingerprint,
            baseline_run_id=baseline_run_id,
        )

    if memory_audit:
        exit_code |= _verify_memory_provenance()

    if formal_task_id is not None:
        exit_code |= _verify_formal(formal_task_id)

    raise SystemExit(exit_code)


def _verify_wheelhouse(
    wheelhouse_path: Path,
    *,
    ca_pubkey: Path | None,
    require_signatures: bool,
    require_customer_sig: bool = False,
    customer_trust_dir: Path | None = None,
    sigstore: bool = False,
    sigstore_owner: str | None = None,
    sigstore_repo: str | None = None,
    sigstore_offline: bool = False,
    sigstore_bundle_dir: Path | None = None,
    require_sigstore: bool = False,
) -> int:
    """Verify an air-gap wheelhouse's MANIFEST.json and per-wheel signatures.

    Returns 0 if every wheel matches its sha256 in the manifest and (when
    signature files are present or required) every signature validates
    against ``ca_pubkey``. Returns 1 on the first mismatch with a clear
    message naming the offending wheel. When ``require_customer_sig`` is
    True, also requires the two-key chain (org + customer Ed25519
    countersignature) to validate before returning success.

    When ``sigstore`` is True, the function additionally runs
    ``gh attestation verify`` against every wheel after the cosign /
    GPG / PEM-key path completes. Sigstore can only escalate the exit
    code -- existing pass paths stay green when no Sigstore attestation
    is found and ``require_sigstore`` is off (graceful skip).
    """
    import hashlib
    import json
    from typing import Any, cast

    console.print()

    if not wheelhouse_path.exists() or not wheelhouse_path.is_dir():
        console.print(
            Panel(
                f"[bold red]Wheelhouse not found:[/bold red] {wheelhouse_path}",
                border_style="red",
                expand=False,
            )
        )
        return 1

    manifest_path = wheelhouse_path / "MANIFEST.json"
    if not manifest_path.exists():
        console.print(
            Panel(
                f"[bold red]Missing MANIFEST.json in:[/bold red] {wheelhouse_path}",
                border_style="red",
                expand=False,
            )
        )
        return 1

    try:
        manifest = cast("dict[str, Any]", json.loads(manifest_path.read_text()))
    except json.JSONDecodeError as exc:
        console.print(
            Panel(
                f"[bold red]Malformed MANIFEST.json:[/bold red] {exc}",
                border_style="red",
                expand=False,
            )
        )
        return 1

    wheels_raw_any: Any = manifest.get("wheels") or []
    if not isinstance(wheels_raw_any, list) or not wheels_raw_any:
        console.print(
            Panel(
                "[bold red]MANIFEST.json contains no wheels[/bold red]",
                border_style="red",
                expand=False,
            )
        )
        return 1
    wheels: list[dict[str, Any]] = [
        cast("dict[str, Any]", e) for e in cast("list[Any]", wheels_raw_any) if isinstance(e, dict)
    ]

    from bernstein.core.distribution.verifier import _is_safe_wheel_name

    failures: list[str] = []
    verified = 0
    signed = 0
    for entry in wheels:
        name_raw = entry.get("name")
        expected_sha_raw = entry.get("sha256")
        name = str(name_raw) if isinstance(name_raw, str) else ""
        expected_sha = str(expected_sha_raw) if isinstance(expected_sha_raw, str) else ""
        if not name or not expected_sha:
            failures.append(f"manifest entry malformed: {entry!r}")
            continue
        if not _is_safe_wheel_name(name):
            failures.append(f"unsafe wheel name in manifest: {name!r}")
            continue
        wheel_path = wheelhouse_path / name
        if not wheel_path.exists():
            failures.append(f"missing wheel: {name}")
            continue
        if wheel_path.is_symlink():
            failures.append(f"symlink wheel rejected: {name}")
            continue
        h = hashlib.sha256()
        with wheel_path.open("rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
        actual = h.hexdigest()
        if actual != expected_sha:
            failures.append(f"sha256 mismatch: {name} (expected {expected_sha[:12]}..., got {actual[:12]}...)")
            continue
        verified += 1
        sig_path = wheel_path.with_suffix(wheel_path.suffix + ".sig")
        if sig_path.exists():
            signed += 1
            if ca_pubkey is not None and not _verify_blob_signature(wheel_path, sig_path, ca_pubkey):
                failures.append(f"signature invalid: {name}")
        elif require_signatures:
            failures.append(f"missing signature: {name}")

    # Two-key chain: org signature must pass first, then the optional
    # customer countersignature is validated against the trust store.
    from bernstein.core.distribution.customer_countersign import (
        verify_customer_signature,
    )

    customer_outcome = verify_customer_signature(
        wheelhouse_path,
        trust_dir=customer_trust_dir,
    )
    if not customer_outcome.present and require_customer_sig:
        failures.append("missing customer signature: MANIFEST.customer.sig")
    elif customer_outcome.present and customer_outcome.valid is False:
        failures.append(f"customer signature invalid: {customer_outcome.error}")
    elif customer_outcome.present and customer_outcome.valid is None and require_customer_sig:
        failures.append(f"customer signature unverified: {customer_outcome.error}")

    if failures:
        console.print(
            Panel(
                "[bold red]Wheelhouse Verify: FAILED[/bold red]",
                border_style="red",
                expand=False,
            )
        )
        for err in failures:
            console.print(f"  [red]![/red] {err}")
        console.print()
        return 1

    console.print(
        Panel(
            "[bold green]Wheelhouse Verify: PASSED[/bold green]",
            border_style="green",
            expand=False,
        )
    )
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Key", style="dim", no_wrap=True, min_width=18)
    table.add_column("Value")
    table.add_row("Path", str(wheelhouse_path))
    table.add_row("Wheels verified", str(verified))
    table.add_row("Signatures present", str(signed))
    table.add_row("CA pubkey", str(ca_pubkey) if ca_pubkey else "(none - checksum only)")
    if customer_outcome.valid is True:
        table.add_row("Customer sig", f"ok (org={customer_outcome.matched_org})")
    elif customer_outcome.present:
        table.add_row("Customer sig", "present, unverified")
    else:
        table.add_row("Customer sig", "(absent)")
    console.print(table)
    console.print()

    if sigstore:
        rc_sigstore = _verify_sigstore_attestations(
            wheelhouse_path,
            owner=sigstore_owner,
            repo=sigstore_repo,
            offline=sigstore_offline,
            bundle_dir=sigstore_bundle_dir,
            require_attestation=require_sigstore,
        )
        if rc_sigstore != 0:
            return rc_sigstore

    return 0


def _verify_sigstore_attestations(
    wheelhouse_path: Path,
    *,
    owner: str | None,
    repo: str | None,
    offline: bool,
    bundle_dir: Path | None,
    require_attestation: bool,
) -> int:
    """Run ``gh attestation verify`` against every wheel in *wheelhouse_path*.

    Returns 0 on pass / advisory-skip, 1 on any hard failure (or any
    skip when ``require_attestation`` is True).
    """
    from bernstein.core.distribution import (
        SIGSTORE_DEFAULT_OWNER,
        SigstoreAttestationVerifier,
        verify_artefacts_with_sigstore,
    )

    wheels = sorted(wheelhouse_path.glob("*.whl"))
    verifier = SigstoreAttestationVerifier(
        owner=owner or SIGSTORE_DEFAULT_OWNER,
        repo=repo,
        offline=offline,
        bundle_dir=bundle_dir,
    )
    report = verify_artefacts_with_sigstore(
        wheels,
        verifier=verifier,
        require_attestation=require_attestation,
    )

    console.print()
    if not report.verifier_available:
        console.print(
            Panel(
                "[bold yellow]Sigstore Verify: SKIPPED[/bold yellow]",
                border_style="yellow",
                expand=False,
            )
        )
        console.print("  [yellow]![/yellow] gh CLI not on PATH -- install GitHub CLI to opt in")
        for fail in report.failures:
            console.print(f"  [red]![/red] {fail}")
        console.print()
        return 1 if report.failures else 0

    if report.ok is True:
        console.print(
            Panel(
                "[bold green]Sigstore Verify: PASSED[/bold green]",
                border_style="green",
                expand=False,
            )
        )
    elif report.ok is False:
        console.print(
            Panel(
                "[bold red]Sigstore Verify: FAILED[/bold red]",
                border_style="red",
                expand=False,
            )
        )
        for fail in report.failures:
            console.print(f"  [red]![/red] {fail}")
    else:
        console.print(
            Panel(
                "[bold yellow]Sigstore Verify: ADVISORY[/bold yellow]",
                border_style="yellow",
                expand=False,
            )
        )
        for skip in report.skips:
            console.print(f"  [dim]-[/dim] {skip}")

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Key", style="dim", no_wrap=True, min_width=22)
    table.add_column("Value")
    table.add_row("Owner", verifier.owner)
    table.add_row("Artefacts attested", str(report.passes))
    table.add_row("Failures", str(len(report.failures)))
    table.add_row("Skipped", str(len(report.skips)))
    console.print(table)
    console.print()
    return 1 if report.ok is False else 0


def _verify_blob_signature(blob: Path, sig: Path, pubkey: Path) -> bool:
    """Verify a detached signature using the cryptography library.

    Supports raw Ed25519 / ECDSA signatures over the blob bytes. Falls
    back to RSA-PSS when the public key is RSA. Returns False on any
    error so callers treat malformed signatures as failure.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

    try:
        pem = pubkey.read_bytes()
        public_key = serialization.load_pem_public_key(pem)
        sig_bytes = sig.read_bytes()
        blob_bytes = blob.read_bytes()
        if isinstance(public_key, ed25519.Ed25519PublicKey):
            public_key.verify(sig_bytes, blob_bytes)
            return True
        if isinstance(public_key, ec.EllipticCurvePublicKey):
            public_key.verify(sig_bytes, blob_bytes, ec.ECDSA(hashes.SHA256()))
            return True
        if isinstance(public_key, rsa.RSAPublicKey):
            public_key.verify(
                sig_bytes,
                blob_bytes,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
                hashes.SHA256(),
            )
            return True
    except (InvalidSignature, ValueError, TypeError, OSError):
        return False
    return False


def _verify_wal_integrity(run_id: str) -> int:
    """Verify the WAL hash chain for *run_id*. Returns 0 on success, 1 on failure."""
    from bernstein.core.wal import WALReader

    reader = WALReader(run_id=run_id, sdd_dir=SDD_DIR)

    console.print()
    try:
        is_valid, errors = reader.verify_chain()
    except FileNotFoundError:
        console.print(
            Panel(
                f"[bold red]WAL file not found for run:[/bold red] {run_id}",
                border_style="red",
                expand=False,
            )
        )
        console.print(f"[dim]Expected: {SDD_DIR}/runtime/wal/{run_id}.wal.jsonl[/dim]")
        console.print()
        return 1

    if is_valid:
        # Count entries for display
        entry_count = sum(1 for _ in reader.iter_entries())
        console.print(
            Panel(
                "[bold green]WAL Integrity: PASSED[/bold green]",
                border_style="green",
                expand=False,
            )
        )
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Key", style="dim", no_wrap=True, min_width=14)
        table.add_column("Value")
        table.add_row("Run ID", run_id)
        table.add_row("Entries", str(entry_count))
        table.add_row("Chain", "intact")
        console.print(table)
    else:
        console.print(
            Panel(
                "[bold red]WAL Integrity: FAILED[/bold red]",
                border_style="red",
                expand=False,
            )
        )
        for err in errors:
            console.print(f"  [red]![/red] {err}")

    console.print()
    return 0 if is_valid else 1


def _verify_determinism(
    run_id: str,
    *,
    expect: str | None = None,
    baseline_run_id: str | None = None,
) -> int:
    """Compute the execution fingerprint for *run_id*, optionally gating on it.

    Three modes, selected by the optional arguments:

    * Bare (``expect is None and baseline_run_id is None``): print the
      fingerprint table and return 0. Output and exit code are unchanged
      from the original observe-only behaviour.
    * ``expect`` set: compare the run's fingerprint to the expected digest
      in constant time. Return 0 on match, 2 on mismatch (printing both
      digests).
    * ``baseline_run_id`` set: compare the run's fingerprint to a second
      run's. Return 0 on match, 2 on mismatch -- and on mismatch, name the
      first diverging WAL entry from the shared hash chain.

    A missing WAL (for either run) returns 1 with the existing
    "WAL file not found" message.

    Scope: a green gate proves the two WAL *decision traces* matched, not
    that on-disk artefacts are byte-identical.
    """
    from bernstein.core.wal import ExecutionFingerprint, WALReader

    reader = WALReader(run_id=run_id, sdd_dir=SDD_DIR)

    console.print()
    try:
        fp = ExecutionFingerprint.from_wal(reader)
    except FileNotFoundError:
        return _wal_not_found(run_id)

    fingerprint = fp.compute()

    if expect is None is baseline_run_id:
        # Bare mode: observe-only, byte-identical to the original surface.
        # The entry count is only displayed in this branch, so the second WAL
        # scan stays scoped here rather than running for every gated call.
        entry_count = sum(1 for _ in WALReader(run_id=run_id, sdd_dir=SDD_DIR).iter_entries())
        console.print(
            Panel(
                "[bold]Execution Determinism Fingerprint[/bold]",
                border_style="blue",
                expand=False,
            )
        )
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Key", style="dim", no_wrap=True, min_width=14)
        table.add_column("Value")
        table.add_row("Run ID", run_id)
        table.add_row("Entries", str(entry_count))
        table.add_row("Fingerprint", fingerprint)
        console.print(table)
        console.print("\n  [dim]Two runs with the same fingerprint made identical decisions in identical order.[/dim]")
        console.print()
        return 0

    if baseline_run_id is not None:
        return _gate_against_baseline(run_id, fingerprint, baseline_run_id)
    if expect is not None:
        return _gate_against_expected(run_id, fingerprint, expect)
    # Unreachable: bare mode returned above, and the caller guarantees at most
    # one of expect/baseline is set. Belt-and-suspenders for the type checker.
    return 0


def _wal_not_found(run_id: str) -> int:
    """Print the canonical 'WAL file not found' panel and return 1."""
    console.print(
        Panel(
            f"[bold red]WAL file not found for run:[/bold red] {run_id}",
            border_style="red",
            expand=False,
        )
    )
    console.print(f"[dim]Expected: {SDD_DIR}/runtime/wal/{run_id}.wal.jsonl[/dim]")
    console.print()
    return 1


# A gate proves the decision trace matched -- not that artefacts on disk are
# identical. Surfaced verbatim under every gate result so a green check is not
# mistaken for full on-disk reproducibility.
_GATE_SCOPE_NOTE = (
    "  [dim]Scope: this proves the WAL decision trace matched, not that on-disk artefacts are identical.[/dim]"
)


def _digest_lines(label_actual: str, actual: str, label_other: str, other: str) -> None:
    """Print two digests on their own full-width lines (never truncated)."""
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Key", style="dim", no_wrap=True, min_width=14)
    table.add_column("Value", no_wrap=True, overflow="fold")
    table.add_row(label_actual, actual)
    table.add_row(label_other, other)
    console.print(table)


def _gate_against_expected(run_id: str, fingerprint: str, expect: str) -> int:
    """Compare *fingerprint* to *expect* in constant time. Return 0 / 2."""
    import hmac

    matched = hmac.compare_digest(fingerprint, expect)

    if matched:
        console.print(
            Panel(
                "[bold green]Determinism Gate: PASSED[/bold green]",
                border_style="green",
                expand=False,
            )
        )
        _digest_lines("Run ID", run_id, "Fingerprint", fingerprint)
        console.print(f"\n{_GATE_SCOPE_NOTE}")
        console.print()
        return 0

    console.print(
        Panel(
            "[bold red]Determinism Gate: FAILED[/bold red]",
            border_style="red",
            expand=False,
        )
    )
    _digest_lines("Expected", expect, "Actual", fingerprint)
    console.print(f"\n{_GATE_SCOPE_NOTE}")
    console.print()
    return 2


def _gate_against_baseline(run_id: str, fingerprint: str, baseline_run_id: str) -> int:
    """Compare *run_id* to *baseline_run_id*. Return 0 / 2 (1 if baseline WAL missing).

    On mismatch, name the first WAL entry at which the two decision traces
    diverged, derived from the existing hash-chain order via per-entry
    cumulative digests.
    """
    import hmac

    from bernstein.core.wal import ExecutionFingerprint, WALReader, first_divergence

    baseline_reader = WALReader(run_id=baseline_run_id, sdd_dir=SDD_DIR)
    try:
        baseline_fp = ExecutionFingerprint.from_wal(baseline_reader).compute()
    except FileNotFoundError:
        return _wal_not_found(baseline_run_id)

    if hmac.compare_digest(fingerprint, baseline_fp):
        console.print(
            Panel(
                "[bold green]Determinism Gate: PASSED[/bold green]",
                border_style="green",
                expand=False,
            )
        )
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Key", style="dim", no_wrap=True, min_width=14)
        table.add_column("Value", no_wrap=True, overflow="fold")
        table.add_row("Run", run_id)
        table.add_row("Baseline", baseline_run_id)
        table.add_row("Fingerprint", fingerprint)
        console.print(table)
        console.print(f"\n{_GATE_SCOPE_NOTE}")
        console.print()
        return 0

    # Mismatch: locate the first diverging WAL entry from the chain order.
    run_digests = ExecutionFingerprint.entry_digests(WALReader(run_id=run_id, sdd_dir=SDD_DIR))
    baseline_digests = ExecutionFingerprint.entry_digests(WALReader(run_id=baseline_run_id, sdd_dir=SDD_DIR))
    idx = first_divergence(baseline_digests, run_digests)

    console.print(
        Panel(
            "[bold red]Determinism Gate: FAILED[/bold red]",
            border_style="red",
            expand=False,
        )
    )
    _digest_lines("Baseline", baseline_fp, "Actual", fingerprint)
    if idx is not None:
        console.print()
        _print_divergence(idx, baseline_run_id, baseline_digests, run_id, run_digests)
    console.print(f"\n{_GATE_SCOPE_NOTE}")
    console.print()
    return 2


def _print_divergence(
    idx: int,
    baseline_run_id: str,
    baseline_digests: list[WALEntryDigest],
    run_id: str,
    run_digests: list[WALEntryDigest],
) -> None:
    """Render the first diverging WAL entry (index + seq + decision type)."""

    def _describe(digests: list[WALEntryDigest]) -> str:
        if idx < len(digests):
            d = digests[idx]
            return f"seq {d.seq} ({d.decision_type})"
        return "(no entry -- run ended earlier)"

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Key", style="dim", no_wrap=True, min_width=20)
    table.add_column("Value")
    table.add_row("First divergence at", f"WAL entry index {idx}")
    table.add_row(f"Baseline {baseline_run_id}", _describe(baseline_digests))
    table.add_row(f"Run {run_id}", _describe(run_digests))
    console.print(table)


def _verify_memory_provenance() -> int:
    """Audit the lesson memory provenance chain and convention receipts. Returns 0 on clean, 1 on failure."""
    from bernstein.core.knowledge.conventions import verify_conventions_audit
    from bernstein.core.memory_integrity import audit_provenance, verify_chain

    lessons_path = SDD_DIR / "memory" / "lessons.jsonl"
    conventions_dir = SDD_DIR / "conventions" / "receipts"
    has_lessons = lessons_path.exists()
    has_conventions = conventions_dir.exists() and any(conventions_dir.glob("*.json"))
    console.print()

    if not has_lessons and not has_conventions:
        console.print(
            Panel(
                "[dim]No lesson memory found: nothing to audit.[/dim]",
                border_style="dim",
                expand=False,
            )
        )
        console.print()
        return 0

    chain_result = verify_chain(lessons_path) if has_lessons else None
    conv_result = verify_conventions_audit(SDD_DIR)

    lessons_valid = chain_result.valid if chain_result is not None else True
    conv_valid = conv_result.valid
    all_valid = lessons_valid and conv_valid

    if all_valid:
        console.print(
            Panel(
                "[bold green]Memory Provenance: CLEAN[/bold green]",
                border_style="green",
                expand=False,
            )
        )
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Key", style="dim", no_wrap=True, min_width=20)
        table.add_column("Value")
        if chain_result is not None:
            table.add_row("Entries verified", str(chain_result.entries_checked))
        if conv_result.receipts_checked > 0:
            table.add_row("Conventions verified", str(conv_result.receipts_checked))
        table.add_row("Chain", "intact")
        table.add_row("Tampering", "none detected")
        console.print(table)
    else:
        console.print(
            Panel(
                "[bold red]Memory Provenance: VIOLATION DETECTED[/bold red]",
                border_style="red",
                expand=False,
            )
        )
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Key", style="dim", no_wrap=True, min_width=20)
        table.add_column("Value")
        if chain_result is not None:
            table.add_row("Entries checked", str(chain_result.entries_checked))
            table.add_row("First broken at", f"line {chain_result.broken_at}" if chain_result.broken_at > 0 else "N/A")
        if conv_result.receipts_checked > 0:
            table.add_row("Conventions checked", str(conv_result.receipts_checked))
        console.print(table)
        console.print()
        if chain_result is not None:
            for err in chain_result.errors:
                console.print(f"  [red]![/red] {err}")
        for err in conv_result.errors:
            console.print(f"  [red]![/red] {err}")

    # Show provenance trail summary if lessons exist
    if has_lessons:
        trail = audit_provenance(lessons_path)
        if trail:
            tampered = [e for e in trail if not e.hash_valid]
            mispositioned = [e for e in trail if not e.chain_position_valid]
            console.print()
            table2 = Table(show_header=False, box=None, padding=(0, 2))
            table2.add_column("Key", style="dim", no_wrap=True, min_width=20)
            table2.add_column("Value")
            table2.add_row("Total entries", str(len(trail)))
            table2.add_row(
                "Hash-tampered",
                f"[red]{len(tampered)}[/red]" if tampered else _GREEN_ZERO,
            )
            table2.add_row(
                "Chain-mispositioned",
                f"[red]{len(mispositioned)}[/red]" if mispositioned else _GREEN_ZERO,
            )
            console.print(table2)

    console.print()
    return 0 if all_valid else 1


def _verify_formal(task_id: str) -> int:
    """Run Z3/Lean4 formal property checks for *task_id*. Returns 0 on pass, 1 on failure."""
    import httpx

    from bernstein.cli.helpers import SERVER_URL
    from bernstein.core.formal_verification import load_formal_verification_config, run_formal_verification
    from bernstein.core.models import Task

    workdir = Path.cwd()
    console.print()

    # Load formal_verification config from bernstein.yaml
    fv_config = load_formal_verification_config(workdir)
    if fv_config is None:
        console.print(
            Panel(
                "[dim]No formal_verification section in bernstein.yaml: nothing to verify.[/dim]",
                border_style="dim",
                expand=False,
            )
        )
        console.print()
        return 0

    if not fv_config.enabled:
        console.print(
            Panel("[dim]Formal verification is disabled (enabled: false).[/dim]", border_style="dim", expand=False)
        )
        console.print()
        return 0

    if not fv_config.properties:
        console.print(
            Panel("[dim]No properties defined in formal_verification section.[/dim]", border_style="dim", expand=False)
        )
        console.print()
        return 0

    # Fetch task from server
    task: Task | None = None
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{SERVER_URL}/tasks/{task_id}")
            resp.raise_for_status()
            task = Task.from_dict(resp.json())
    except Exception as exc:
        console.print(
            Panel(
                f"[bold red]Could not fetch task {task_id!r}:[/bold red] {exc}",
                border_style="red",
                expand=False,
            )
        )
        console.print(f"[dim]Is the Bernstein server running? ({SERVER_URL})[/dim]")
        console.print()
        return 1

    # Run formal verification
    fv_result = run_formal_verification(task, workdir, fv_config)

    if fv_result.passed:
        console.print(
            Panel(
                "[bold green]Formal Verification: PASSED[/bold green]",
                border_style="green",
                expand=False,
            )
        )
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Key", style="dim", no_wrap=True, min_width=22)
        table.add_column("Value")
        table.add_row("Task ID", task_id)
        table.add_row("Task", task.title[:60])
        table.add_row("Properties checked", str(fv_result.properties_checked))
        table.add_row("Violations", _GREEN_ZERO)
        console.print(table)
    else:
        console.print(
            Panel(
                "[bold red]Formal Verification: FAILED[/bold red]",
                border_style="red",
                expand=False,
            )
        )
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Key", style="dim", no_wrap=True, min_width=22)
        table.add_column("Value")
        table.add_row("Task ID", task_id)
        table.add_row("Task", task.title[:60])
        table.add_row("Properties checked", str(fv_result.properties_checked))
        table.add_row("Violations", f"[red]{len(fv_result.violations)}[/red]")
        console.print(table)
        console.print()
        for violation in fv_result.violations:
            console.print(f"  [red]✗[/red] [bold]{violation.property_name}[/bold] ({violation.checker})")
            console.print(f"    [dim]{violation.detail}[/dim]")
            if violation.counterexample and violation.counterexample != "(timeout)":
                console.print(f"    [yellow]Counterexample:[/yellow] {violation.counterexample[:200]}")

    console.print()
    return 0 if fv_result.passed else 1


# ---------------------------------------------------------------------------
# Run receipts (issue #2924)
# ---------------------------------------------------------------------------


@verify_cmd.command("run")
@click.argument("run_id", required=True)
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
@click.option(
    "--signing-key-path",
    "signing_key_path",
    default=None,
    type=click.Path(dir_okay=False, exists=True, resolve_path=True),
    help=(
        "Path to the Ed25519 private key (PEM PKCS#8 or raw 32-byte) that signs "
        "the receipt. Reuses the lineage / audit-receipt KMS key material "
        "(see src/bernstein/core/security/lineage_kms.py). Mutually exclusive "
        "with --signing-env-var. Falls back to $BERNSTEIN_RUN_RECEIPT_SIGNING_KEY_PATH "
        "/ $BERNSTEIN_RUN_RECEIPT_SIGNING_ENV_VAR when neither flag is given."
    ),
)
@click.option(
    "--signing-env-var",
    "signing_env_var",
    default=None,
    help="Env var carrying a PEM Ed25519 private key. Mutually exclusive with --signing-key-path.",
)
@click.option("--signing-key-id", "signing_key_id", default=None, help="Operator-stable JWK 'kid' for the receipt key.")
@click.option(
    "--include-audit-range",
    is_flag=True,
    default=False,
    help=(
        "Also embed a re-chained audit-chain slice (needs the operator audit "
        "HMAC key at build time only, plus --audit-since/--audit-until)."
    ),
)
@click.option("--audit-since", "audit_since", default=None, help="ISO-8601 inclusive lower bound of the audit window.")
@click.option("--audit-until", "audit_until", default=None, help="ISO-8601 exclusive upper bound of the audit window.")
@click.option(
    "--output",
    "-o",
    "output_path",
    default=None,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Receipt destination (defaults to .sdd/runs/<run-id>/run-receipt.json).",
)
def verify_run_cmd(
    run_id: str,
    workdir: str,
    signing_key_path: str | None,
    signing_env_var: str | None,
    signing_key_id: str | None,
    include_audit_range: bool,
    audit_since: str | None,
    audit_until: str | None,
    output_path: Path | None,
) -> None:
    """Build the signed run receipt for RUN_ID.

    Binds the run's journal head (replay identity), lineage-spine head
    (artifact provenance), and optionally an audit-chain range under one
    Ed25519-signed subject with the public key embedded as a JWK. The
    resulting ``run-receipt.json`` verifies fully offline with
    ``bernstein verify receipt`` -- no HMAC key, no live ``.sdd/``.
    """
    from bernstein.core.persistence.lineage_signer import LineageSignerError
    from bernstein.core.replay.journal import JournalPathError
    from bernstein.core.replay.run_receipt import (
        RunReceiptError,
        build_run_receipt,
        resolve_kms_adapter_from_env,
    )
    from bernstein.core.security.lineage_kms import EnvBasedKMSAdapter, FileBasedKMSAdapter

    if signing_key_path and signing_env_var:
        console.print("[red]--signing-key-path and --signing-env-var are mutually exclusive.[/red]")
        raise SystemExit(2)

    try:
        if signing_key_path:
            kms_adapter: object | None = FileBasedKMSAdapter(Path(signing_key_path), kid=signing_key_id)
        elif signing_env_var:
            kms_adapter = EnvBasedKMSAdapter(signing_env_var, kid=signing_key_id)
        else:
            kms_adapter = resolve_kms_adapter_from_env()
    except (LineageSignerError, OSError, ValueError) as exc:
        console.print(f"[red]Failed to load receipt signing key: {exc}[/red]")
        raise SystemExit(1) from None
    if kms_adapter is None:
        console.print(
            "[red]No signing key configured.[/red] Provide --signing-key-path or "
            "--signing-env-var (or set $BERNSTEIN_RUN_RECEIPT_SIGNING_KEY_PATH / "
            "$BERNSTEIN_RUN_RECEIPT_SIGNING_ENV_VAR)."
        )
        raise SystemExit(2)

    audit_hmac_key: bytes | None = None
    if include_audit_range:
        if not audit_since or not audit_until:
            console.print("[red]--include-audit-range requires --audit-since and --audit-until.[/red]")
            raise SystemExit(2)
        from bernstein.core.security.audit import load_or_create_audit_key

        try:
            audit_hmac_key = load_or_create_audit_key()
        except OSError as exc:  # pragma: no cover - filesystem race
            console.print(f"[red]Failed to load audit key: {exc}[/red]")
            raise SystemExit(1) from None

    sdd_dir = Path(workdir).resolve() / ".sdd"
    try:
        receipt = build_run_receipt(
            run_id,
            sdd_dir,
            kms_adapter,
            include_audit_range=include_audit_range,
            audit_hmac_key=audit_hmac_key,
            audit_since=audit_since,
            audit_until=audit_until,
            output_path=output_path,
        )
    except (RunReceiptError, JournalPathError, LineageSignerError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from None

    console.print()
    console.print(Panel("[bold green]Run Receipt: WRITTEN[/bold green]", border_style="green", expand=False))
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Key", style="dim", no_wrap=True, min_width=18)
    table.add_column("Value")
    table.add_row("Run ID", receipt.run_id)
    table.add_row("Journal head", receipt.journal_head[:16] + "…")
    table.add_row("Spine head", (receipt.spine_head[:16] + "…") if receipt.spine_head else "(no spine entries)")
    if receipt.audit_head_sha256 is not None:
        table.add_row("Audit range head", receipt.audit_head_sha256[:16] + "…")
    table.add_row("Receipt SHA-256", receipt.sha256[:16] + "…")
    if receipt.receipt_path is not None:
        table.add_row("Receipt", str(receipt.receipt_path))
    console.print(table)
    console.print("\n  [dim]Verify offline with: bernstein verify receipt <path> [--public-key PEM][/dim]")
    console.print()
    raise SystemExit(0)


@verify_cmd.command("receipt")
@click.argument("receipt_path", type=click.Path(path_type=Path))
@click.option(
    "--public-key",
    "public_key_path",
    default=None,
    type=click.Path(dir_okay=False, path_type=Path),
    help=(
        "Optional trusted Ed25519 public key (PEM) to pin. The key embedded "
        "in the receipt must match it; default is trust-on-first-use of the "
        "embedded JWK."
    ),
)
def verify_receipt_cmd(receipt_path: Path, public_key_path: Path | None) -> None:
    """Verify a run receipt offline from RECEIPT_PATH.

    Recomputes the journal head, the spine head, and (when present) the
    audit-range head from the bytes embedded in the receipt, rebuilds the
    signed subject from those recomputed values, and checks the Ed25519
    signature. No HMAC key and no ``.sdd/`` are read.

    What a pass proves depends on the key source, and the verdict says
    which check ran. Without ``--public-key`` the signature is checked
    against the key EMBEDDED in the receipt (trust-on-first-use), which
    proves the file is internally consistent -- integrity only, not who
    signed it: a forger controlling the whole file could re-sign with
    their own key. With ``--public-key`` the embedded key must match the
    pinned out-of-band key, which additionally proves provenance.

    \b
    Exit codes:
      0  receipt verifies
      1  empty or malformed input (unreadable file, missing ranges/fields)
      2  tamper detected (the first divergent journal step index is named)
    """
    from bernstein.core.replay.run_receipt import verify_run_receipt

    try:
        receipt_bytes = receipt_path.read_bytes()
    except OSError as exc:
        console.print(f"[red]Cannot read receipt:[/red] {exc}")
        raise SystemExit(1) from None

    public_key_pem: bytes | None = None
    if public_key_path is not None:
        try:
            public_key_pem = public_key_path.read_bytes()
        except OSError as exc:
            console.print(f"[red]Cannot read --public-key:[/red] {exc}")
            raise SystemExit(1) from None

    result = verify_run_receipt(receipt_bytes, public_key_pem=public_key_pem)

    console.print()
    console.print(
        f"[bold]Run receipt[/bold] run={result.run_id or '(unknown)'} "
        f"journal_events={result.journal_events} spine_entries={result.spine_entries}"
    )
    if result.ok:
        if public_key_pem is not None:
            console.print(
                "[green]OK (provenance: pinned key)[/green] -- every head recomputes from the "
                "embedded ranges and the signature verifies against the pinned public key."
            )
        else:
            console.print(
                "[green]OK (integrity-only: embedded key)[/green] -- every head recomputes from "
                "the embedded ranges and the signature matches the key embedded in the receipt "
                "(trust-on-first-use)."
            )
            console.print(
                "  [dim]This proves the file is internally consistent, not who signed it: a forger "
                "controlling the whole file could re-sign with their own key. Pass --public-key "
                "with the operator's key for provenance.[/dim]"
            )
        raise SystemExit(0)
    if result.status == "malformed":
        console.print("[yellow]MALFORMED[/yellow] -- the receipt cannot be checked:")
        for err in result.errors:
            console.print(f"  - {err}")
        raise SystemExit(1)
    console.print("[red]TAMPER DETECTED[/red]:")
    for err in result.errors:
        console.print(f"  - {err}")
    if result.divergent_step is not None:
        console.print(f"  [red]first divergent journal step: {result.divergent_step}[/red]")
    raise SystemExit(2)


# ---------------------------------------------------------------------------
# Verifier-ladder receipts (issue #2927)
# ---------------------------------------------------------------------------


@verify_cmd.command("ladder")
@click.argument("receipt_hash", required=True)
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
def verify_ladder_cmd(receipt_hash: str, workdir: str) -> None:
    """Re-derive and verify the verifier-ladder receipt RECEIPT_HASH.

    The receipt is never trusted: the stored body is re-hashed, the
    composite ``merge_eligible`` claim is re-derived from the stored tier
    verdicts, and every tier's spine anchor is re-checked against the
    ``verifier-ladder`` lineage spine's content hashes. Prints per-tier
    ``tier / config_hash / evidence_hash / verdict`` plus the composite
    result.

    \b
    Exit codes:
      0  receipt verifies and its composite claim is entailed
      1  no readable receipt for the hash
      2  re-derivation or spine-anchor mismatch (tamper)
    """
    from bernstein.core.quality.verifier_ladder import verify_ladder_receipt
    from bernstein.core.security.audit import load_or_create_audit_key

    root = Path(workdir).resolve()
    try:
        hmac_key = load_or_create_audit_key()
    except OSError as exc:
        console.print(f"[red]Failed to load audit key: {exc}[/red]")
        raise SystemExit(1) from None

    result = verify_ladder_receipt(
        workdir=root,
        lineage_root=root / ".sdd" / "lineage",
        hmac_key=hmac_key,
        receipt_hash=receipt_hash,
    )

    console.print()
    receipt = result.receipt
    if receipt is not None:
        table = Table(show_header=True, box=None, padding=(0, 2))
        table.add_column("Tier", style="bold", no_wrap=True, min_width=13)
        table.add_column("Verdict", no_wrap=True)
        table.add_column("Config hash", no_wrap=True)
        table.add_column("Evidence hash", no_wrap=True)
        for record in receipt.records:
            verdict_style = {"pass": "green", "fail": "red", "skip": "yellow"}.get(record.verdict, "red")
            table.add_row(
                record.tier.value,
                f"[{verdict_style}]{record.verdict}[/{verdict_style}]",
                record.config_hash[:16] + "…",
                record.evidence_hash[:16] + "…",
            )
        console.print(table)
        console.print()
        eligible = "[green]yes[/green]" if receipt.merge_eligible else "[red]no[/red]"
        console.print(f"  Task: {receipt.task_id}    Merge eligible (stored): {eligible}")

    if result.ok:
        console.print(
            Panel(
                "[bold green]Verifier Ladder: VERIFIED[/bold green]",
                border_style="green",
                expand=False,
            )
        )
        console.print(
            "  [dim]Composite claim re-derived from the stored tier verdicts; every tier's "
            "spine anchor seals exactly its recorded evidence.[/dim]"
        )
        console.print()
        raise SystemExit(0)

    if result.status == "missing":
        console.print(
            Panel(
                "[bold red]Verifier Ladder: NOT FOUND[/bold red]",
                border_style="red",
                expand=False,
            )
        )
        console.print(f"  [red]![/red] {result.reason}")
        console.print()
        raise SystemExit(1)

    console.print(
        Panel(
            "[bold red]Verifier Ladder: FAILED[/bold red]",
            border_style="red",
            expand=False,
        )
    )
    console.print(f"  [red]![/red] {result.reason}")
    console.print()
    raise SystemExit(2)
