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

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from bernstein.adapters.canary import (  # noqa: E402
    CANARY_MATRIX,
    LAST_GREEN_DOC_PATH,
    LAST_GREEN_JSON_PATH,
    run_matrix,
)


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
    args = parser.parse_args(argv)

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

    result = run_matrix(
        targets,
        receipts_dir=out_dir / "receipts",
        state_path=out_dir / "state.json",
        # --update-docs writes the packaged projection in place (the
        # workflow commits it via PR); otherwise keep the run scratch-local.
        last_green_path=LAST_GREEN_JSON_PATH if args.update_docs else out_dir / "last_green.json",
        docs_path=LAST_GREEN_DOC_PATH if args.update_docs else None,
        generated_at=generated_at,
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
