#!/usr/bin/env python3
"""Adapter conformance canary driver (issue #2368).

Thin argparse front-end over :func:`bernstein.adapters.canary.run_matrix`,
executed nightly by ``.github/workflows/adapter-conformance-canary.yml``
and runnable locally by operators.

For every matrix target the run: probes the installed binary, captures the
upstream version, checks the adapter contract in process, seals a
content-addressed receipt, folds the outcome into the failure-threshold
state, and updates the last-green projection (JSON + docs table) on a
pass. Threshold-crossing deduped regressions are written to
``issues_to_open.json`` for the workflow to open.

Exit codes:

* 0 -- no conformance failures (skips are fine).
* 1 -- at least one adapter failed conformance this run.

Usage::

    uv run python scripts/adapter_canary.py [--adapter agy] \
        [--out-dir .sdd/runtime/adapter-canary] [--update-docs]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from bernstein.adapters.canary import (  # noqa: E402
    CANARY_MATRIX,
    LAST_GREEN_DOC_PATH,
    LAST_GREEN_JSON_PATH,
    ReceiptSetError,
    load_last_green,
    run_matrix,
    verify_last_green_projection,
)
from bernstein.core.security.audit_chain import AuditChainStore  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Callable

    from bernstein.adapters.canary import CanaryTarget, MatrixRunResult


def run_nightly_canary(
    targets: tuple[CanaryTarget, ...],
    *,
    out_dir: Path,
    generated_at: str,
    update_docs: bool = False,
    which: Callable[[str], str | None] | None = None,
    contracts_dir: Path | None = None,
    audit_key: bytes | None = None,
    audit_key_path: Path | None = None,
) -> MatrixRunResult:
    """Run the canary matrix with every receipt anchored into the HMAC chain.

    This is the nightly entrypoint's core: it constructs the run's
    :class:`AuditChainStore` and threads it through :func:`run_matrix` so
    each sealed receipt hash is mirrored into the HMAC audit chain (the
    ``adapter.canary_receipt`` event), making the docstring/docs claim
    true for the automated path -- not only when a chain is passed by an
    in-run caller.

    Durability: the chain's JSONL segments are written under
    ``<out_dir>/receipts/audit-chain`` so the existing "Upload receipts"
    workflow step captures them as an artifact. The receipt->chain
    binding therefore survives the ephemeral CI runner with no workflow
    change: a verifier holding a receipt file can recompute its hash and
    match it against a persisted chain entry offline.

    Args:
        targets: The canary matrix targets to probe.
        out_dir: Run scratch directory for receipts, state, and the
            audit-chain segment.
        generated_at: Deterministic UTC timestamp stamped into every
            receipt (drives the content-addressed receipt identity).
        update_docs: When true, write the packaged last-green projection
            (JSON + docs table) in place; otherwise keep it scratch-local.
        which: Optional binary resolver override (hermetic tests).
        contracts_dir: Optional contracts directory override (hermetic
            tests).
        audit_key: Optional raw HMAC key. When omitted the store loads or
            creates a key via the canonical resolver.
        audit_key_path: Optional HMAC key file path override.

    Returns:
        The :class:`MatrixRunResult` produced by :func:`run_matrix`.
    """
    audit_chain = AuditChainStore(
        audit_dir=out_dir / "receipts" / "audit-chain",
        key=audit_key,
        key_path=audit_key_path,
    )
    return run_matrix(
        targets,
        receipts_dir=out_dir / "receipts",
        state_path=out_dir / "state.json",
        # --update-docs writes the packaged projection in place (the
        # workflow commits it via PR); otherwise keep the run scratch-local.
        last_green_path=LAST_GREEN_JSON_PATH if update_docs else out_dir / "last_green.json",
        docs_path=LAST_GREEN_DOC_PATH if update_docs else None,
        generated_at=generated_at,
        which=which,
        contracts_dir=contracts_dir,
        audit_chain=audit_chain,
    )


def _verify_projection(out_dir: Path) -> int:
    """Re-derive the committed projection from the receipts on disk.

    Reads the receipts back off disk in a fresh process rather than reusing
    anything ``run_matrix`` held in memory, so a fault in the projection step
    cannot cancel itself out by being checked with its own state (#3940).
    """
    receipts_dir = out_dir / "receipts"
    docs = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(receipts_dir.glob("*.json"))]
    if not docs:
        print(f"no receipts under {receipts_dir}; nothing to verify", file=sys.stderr)
        return 1

    try:
        mismatches = verify_last_green_projection(docs, load_last_green())
    except ReceiptSetError as exc:
        print(f"receipt set is not one run's worth: {exc}", file=sys.stderr)
        return 1

    if mismatches:
        print(f"last_green.json is not a projection of the {len(docs)} receipt(s) in {receipts_dir}:", file=sys.stderr)
        for mismatch in mismatches:
            print(f"  {mismatch.kind}: {mismatch.adapter} - {mismatch.detail}", file=sys.stderr)
        return 1

    print(f"last_green.json verified against {len(docs)} receipt(s) in {receipts_dir}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the canary matrix; return the process exit code."""
    parser = argparse.ArgumentParser(description="Adapter conformance canary")
    parser.add_argument(
        "--adapter",
        action="append",
        default=None,
        help="Restrict the run to one adapter (repeatable). Default: full matrix.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=REPO_ROOT / ".sdd" / "runtime" / "adapter-canary",
        help="Directory for receipts, state, and the issue payload file.",
    )
    parser.add_argument(
        "--update-docs",
        action="store_true",
        help="Regenerate the last-green table in docs/adapters/conformance-canary.md and the packaged last_green.json.",
    )
    parser.add_argument(
        "--verify-projection",
        action="store_true",
        help="Re-derive last_green.json from the receipts on disk and exit non-zero on a mismatch. Runs no probes.",
    )
    args = parser.parse_args(argv)

    if args.verify_projection:
        return _verify_projection(args.out_dir)

    targets = CANARY_MATRIX
    if args.adapter:
        wanted = set(args.adapter)
        targets = tuple(t for t in CANARY_MATRIX if t.adapter in wanted)
        missing = wanted - {t.adapter for t in targets}
        if missing:
            parser.error(f"unknown canary adapter(s): {', '.join(sorted(missing))}")

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    result = run_nightly_canary(
        targets,
        out_dir=out_dir,
        generated_at=generated_at,
        update_docs=args.update_docs,
    )

    issues_path = out_dir / "issues_to_open.json"
    issues_path.write_text(
        json.dumps(result.issues_to_open, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    for outcome in result.outcomes:
        print(f"{outcome.adapter}: {outcome.verdict} (version {outcome.installed_version or 'unknown'})")
        for failure in outcome.failures:
            print(f"  - {failure}")
    print(f"receipts: {len(result.receipt_paths)} sealed under {out_dir / 'receipts'}")
    print(f"issue payloads: {len(result.issues_to_open)} written to {issues_path}")

    return 1 if result.regressions else 0


if __name__ == "__main__":
    raise SystemExit(main())
