"""``bernstein adapters verify`` - re-derive and check an admission receipt.

The command answers one question offline: *may this adapter spawn, and can
that answer be proved?* It re-derives the adapter's admission evidence from
the local tree and the installed binary -- the pinned contract's content hash,
the capability profile's content hash, the golden-transcript replay outcome,
and the nightly canary's attestation -- folds it into the deterministic replay
fingerprint, and compares that against the sealed admission receipt on disk.

A mismatch fails closed. The binary moved, the contract changed, or the
transcript diverged, and the receipt no longer attests the surface that is
actually installed. Two operators on the same binary version derive the same
fingerprint, so a divergence names its cause instead of reading as flake.

``--seal`` writes and anchors a fresh receipt from the current evidence. That
is the emit path a passing conformance run uses, and it is symmetric: a
skipped or failing adapter seals a *refusal* receipt naming the reason, the
capabilities withheld, and the remediation, so an unverified adapter leaves a
record rather than a silence that reads as permission.

Exit codes:

* ``0`` - the adapter is admitted (or ``--seal`` sealed an admission receipt).
* ``1`` - the adapter is refused; the receipt says why.
* ``2`` - unknown adapter name.

Refs: #2610.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import click

from bernstein.adapters.admission import (
    ADMISSION_TTL_SECONDS,
    build_admission_receipt,
    evaluate_admission,
    gate_decision,
    gather_admission_evidence,
    load_admission_receipt,
    receipt_sha256,
    seal_admission_receipt,
)

#: Default location for sealed admission receipts, relative to the repo root.
DEFAULT_RECEIPTS_DIR = Path(".sdd") / "adapters" / "admission"

FORMAT_TEXT = "text"
FORMAT_JSON = "json"
FORMAT_CHOICES = (FORMAT_TEXT, FORMAT_JSON)


def _known_adapter(name: str) -> bool:
    """True when ``name`` resolves in the live adapter registry."""
    from bernstein.adapters.registry import iter_adapter_specs

    return name == "generic" or any(key == name for key, _ in iter_adapter_specs())


def _render_text(receipt: dict[str, object], sha: str, *, stored_state: str) -> str:
    """Human-readable rendering of one admission decision."""
    lines = [
        f"adapter:             {receipt.get('adapter')}",
        f"binary:              {receipt.get('binary')} ({receipt.get('binary_path') or 'not on PATH'})",
        f"installed version:   {receipt.get('installed_version') or '(unknown)'}",
        f"contract hash:       {receipt.get('contract_hash') or '(no contract)'}",
        f"profile hash:        {receipt.get('profile_hash') or '(no profile)'}",
        f"replay fingerprint:  {receipt.get('replay_fingerprint')}",
        f"conformance run id:  {receipt.get('conformance_run_id')}",
        f"conformance verdict: {receipt.get('conformance_verdict')}",
        f"canary attestation:  {receipt.get('canary_verdict')}",
        f"sealed receipt:      {stored_state}",
        f"verdict:             {receipt.get('verdict')}",
    ]
    reason = receipt.get("reason")
    if reason:
        lines.append(f"reason:              {reason}")
    allowed = cast("list[object]", receipt.get("allowed_capabilities") or [])
    forbidden = cast("list[object]", receipt.get("forbidden_capabilities") or [])
    lines.append(f"allowed:             {', '.join(str(a) for a in allowed) or '<none>'}")
    lines.append(f"forbidden:           {', '.join(str(f) for f in forbidden) or '<none>'}")
    remediation = receipt.get("remediation")
    if remediation:
        lines.append(f"remediation:         {remediation}")
    lines.append(f"receipt sha256:      {sha}")
    return "\n".join(lines)


def _execute_verify(
    name: str,
    *,
    output_format: str,
    seal: bool,
    receipts_dir: Path,
    contracts_dir: Path | None = None,
    golden_dir: Path | None = None,
    now: datetime | None = None,
) -> int:
    """Derive, compare, optionally seal; return the process exit code.

    Split out from the Click command so tests exercise the full pipeline
    without ``click.testing``.
    """
    if not _known_adapter(name):
        click.echo(f"Unknown adapter: {name!r}", err=True)
        return 2

    clock = now if now is not None else datetime.now(UTC)
    stamp = clock.isoformat()

    if name == "mock":
        click.echo(
            f"Adapter {name!r} is exempt from receipt-gated admission "
            "(no pinned upstream surface to prove); nothing to verify.",
        )
        return 0

    evidence = gather_admission_evidence(name, contracts_dir=contracts_dir, golden_dir=golden_dir)
    live = evaluate_admission(evidence)

    if seal:
        receipt, sha, path = seal_admission_receipt(live, receipts_dir=receipts_dir, generated_at=stamp)
        stored_state = f"sealed -> {path}"
    else:
        stored, problem = load_admission_receipt(receipts_dir, name)
        decision = gate_decision(
            evidence,
            stored,
            problem,
            now=clock,
            ttl_seconds=ADMISSION_TTL_SECONDS,
        )
        receipt = build_admission_receipt(decision, generated_at=stamp)
        sha = receipt_sha256(receipt)
        stored_state = "present" if stored is not None else f"absent ({problem})"
        live = decision

    if output_format == FORMAT_JSON:
        click.echo(json.dumps({"receipt": receipt, "receipt_sha256": sha}, indent=2, sort_keys=True))
    else:
        click.echo(_render_text(receipt, sha, stored_state=stored_state))

    return 0 if live.admitted else 1


@click.command("verify")
@click.argument("name")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(list(FORMAT_CHOICES)),
    default=FORMAT_TEXT,
    show_default=True,
    help="Output format - human-readable text or the raw receipt as JSON.",
)
@click.option(
    "--seal",
    is_flag=True,
    default=False,
    help="Seal and anchor a fresh admission receipt from the current evidence.",
)
@click.option(
    "--receipts-dir",
    type=click.Path(path_type=Path),
    default=None,
    help=f"Directory holding sealed admission receipts (default: {DEFAULT_RECEIPTS_DIR}).",
)
def adapters_verify_cmd(
    name: str,
    output_format: str,
    seal: bool,
    receipts_dir: Path | None,
) -> None:
    """Re-derive NAME's admission fingerprint and check it against its receipt.

    Exits 1 when the adapter is refused, so the command is usable as a gate in
    a script without parsing its output.
    """
    rc = _execute_verify(
        name,
        output_format=output_format,
        seal=seal,
        receipts_dir=receipts_dir if receipts_dir is not None else DEFAULT_RECEIPTS_DIR,
    )
    sys.exit(rc)


def register_adapters_verify(group: click.Group) -> None:
    """Attach ``verify`` to an existing ``adapters`` group."""
    group.add_command(adapters_verify_cmd, "verify")


__all__ = [
    "DEFAULT_RECEIPTS_DIR",
    "FORMAT_CHOICES",
    "FORMAT_JSON",
    "FORMAT_TEXT",
    "adapters_verify_cmd",
    "register_adapters_verify",
]
