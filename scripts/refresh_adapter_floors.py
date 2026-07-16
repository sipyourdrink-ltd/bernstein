#!/usr/bin/env python3
"""Adapter security-floor refresh driver (issue #2515).

Thin argparse front-end over :mod:`bernstein.adapters.floor_refresh`. Reads a
machine-readable advisory feed, diffs it against the in-code floor map, and
emits a *reviewed, data-only diff accompanied by a signed update receipt*: a
floor bump lands as a one-line data edit in ``src/bernstein/adapters/
advisories.py`` (regenerated between the floor-map markers), never a logic
change, and the update receipt binds the old and new floor-map content hashes
into a content-addressed artefact anchored to the HMAC audit chain.

The feed schema::

    {"schema_version": 1,
     "adapters": {
        "aider": {"min_safe_version": "0.60.0",
                  "advisory_id": "BSA-0001",
                  "note": "..."},
        ...}}

Exit codes:

* 0 -- ran cleanly (diff may be empty; a no-op refresh is deterministic).
* 1 -- the feed could not be parsed.

Usage::

    uv run python scripts/refresh_adapter_floors.py --feed feed.json \
        [--write] [--receipt-out floor_update_receipt.json] [--anchor]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from bernstein.adapters.floor_refresh import (  # noqa: E402
    build_floor_update_receipt,
    current_floor_map,
    diff_floor_maps,
    parse_feed,
    receipt_sha256,
    write_advisory_block,
)

_ADVISORIES_PATH = REPO_ROOT / "src" / "bernstein" / "adapters" / "advisories.py"


def main(argv: list[str] | None = None) -> int:
    """Refresh the floor map from a feed; return the process exit code."""
    parser = argparse.ArgumentParser(description="Adapter security-floor refresh")
    parser.add_argument("--feed", required=True, help="Path to the machine-readable advisory feed (JSON).")
    parser.add_argument(
        "--write",
        action="store_true",
        help="Regenerate the floor-map block in advisories.py (data-only diff).",
    )
    parser.add_argument(
        "--receipt-out",
        default=None,
        help="Write the signed update receipt to this path (JSON).",
    )
    parser.add_argument(
        "--anchor",
        action="store_true",
        help="Anchor the update receipt into the .sdd/audit HMAC chain.",
    )
    args = parser.parse_args(argv)

    try:
        feed_data = json.loads(Path(args.feed).read_text(encoding="utf-8"))
        new_map = parse_feed(feed_data)
    except (OSError, ValueError) as exc:
        print(f"error: could not parse feed: {exc}", file=sys.stderr)
        return 1

    old_map = current_floor_map()
    diff = diff_floor_maps(old_map, new_map)
    receipt = build_floor_update_receipt(old_map, new_map, generated_at=datetime.now(UTC).isoformat())
    sha = receipt_sha256(receipt)

    print("Adapter security-floor refresh")
    print(f"  old floor-map hash: {receipt['old_floor_map_hash']}")
    print(f"  new floor-map hash: {receipt['new_floor_map_hash']}")
    print(f"  update receipt:     sha256:{sha}")
    if diff.is_empty:
        print("  diff: (none) - the feed matches the in-code floor map")
    else:
        for name in diff.to_dict()["added"]:
            print(f"  + {name}")
        for name in diff.to_dict()["removed"]:
            print(f"  - {name}")
        for change in diff.to_dict()["changed"]:
            print(f"  ~ {change['adapter']}.{change['field']}: {change['old']} -> {change['new']}")

    if args.write:
        write_advisory_block(_ADVISORIES_PATH, new_map)
        print(f"  wrote data-only floor-map block to {_ADVISORIES_PATH}")

    if args.receipt_out:
        doc = {"receipt": receipt, "receipt_sha256": sha}
        Path(args.receipt_out).write_text(
            json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"  wrote signed update receipt to {args.receipt_out}")

    if args.anchor:
        from bernstein.core.security.audit_chain import (
            AuditChainStore,
            record_adapter_floor_update_receipt,
        )

        chain = AuditChainStore(REPO_ROOT / ".sdd" / "audit")
        record_adapter_floor_update_receipt(
            chain=chain,
            receipt_sha256=sha,
            old_floor_map_hash=receipt["old_floor_map_hash"],
            new_floor_map_hash=receipt["new_floor_map_hash"],
            diff=receipt["diff"],
        )
        print("  anchored update receipt into .sdd/audit")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
