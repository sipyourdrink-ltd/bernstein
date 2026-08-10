#!/usr/bin/env python3
"""Bind the committed browser renders to the SPA build they claim to show.

The terminal surface is deterministically reproducible, so its render is
regenerated and compared exactly (``scripts/render_tui_snapshot.py``). The
browser surface is not: font hinting, antialiasing and GPU compositing all
move pixels between machines, so a pixel comparison would flake, get marked
flaky, and then be deleted. That is a worse outcome than no check.

So this checks something weaker on purpose, and says so. Each committed render
of the web UI is bound to a content hash of the SPA bundle that ships in the
wheel (``src/bernstein/gui/static/``). When the bundle moves and the renders do
not, the gate fails and names them.

What that proves: nobody shipped a UI change while leaving the published
screenshots behind. What it does not prove: that a render is *correct* - a
screenshot bound to the current bundle can still show a screen nobody would
recognise. Correctness of a browser render is a human judgement; staleness is
the part a machine can hold, and staleness is the failure that actually
happens.

Usage::

    python scripts/bind_webui_renders.py           # verify, exit 1 on drift
    python scripts/bind_webui_renders.py --update  # rebind to today's bundle
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_DIR = REPO_ROOT / "src" / "bernstein" / "gui" / "static"
ASSET_DIR = REPO_ROOT / "docs" / "assets"
BINDING = ASSET_DIR / "webui-renders.json"

#: Renders of the SPA. ``web-dashboard.png`` is deliberately absent: it shows
#: the server-rendered ``/dashboard`` page, which is a different surface with a
#: different source of truth.
RENDER_GLOB = "webui-*.png"


def bundle_digest(bundle_dir: Path | None = None) -> tuple[str, int]:
    """Return (sha256, file count) over every file in the SPA build.

    Hashes paths as well as contents, so a renamed asset moves the digest even
    when the bytes are unchanged - a rename is exactly the kind of build change
    that ships a new UI.

    The directory is resolved at call time rather than bound as a default, so
    the gate a test drives is the same code path the gate runs in CI.
    """
    bundle_dir = bundle_dir or BUNDLE_DIR
    digest = hashlib.sha256()
    files = sorted(path for path in bundle_dir.rglob("*") if path.is_file())
    for path in files:
        digest.update(path.relative_to(bundle_dir).as_posix().encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest(), len(files)


def committed_renders(asset_dir: Path | None = None) -> list[str]:
    """Return the names of every committed SPA render, sorted."""
    return sorted(path.name for path in (asset_dir or ASSET_DIR).glob(RENDER_GLOB))


def load_binding(path: Path = BINDING) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def build_binding(previous: dict[str, object] | None = None) -> dict[str, object]:
    """Return the binding document for the current bundle and renders.

    A render already recorded keeps its provenance; a newly bound one is marked
    ``adopted``, which reads as "bound to this bundle without evidence it was
    captured from it". Anything captured by a human running the documented
    command is theirs to mark ``captured``.
    """
    sha, count = bundle_digest()
    prior: dict[str, str] = {}
    if previous:
        recorded = previous.get("renders")
        if isinstance(recorded, dict):
            prior = {name: str(entry) for name, entry in recorded.items()}
    return {
        "spa_bundle_sha256": sha,
        "spa_bundle_files": count,
        "renders": {name: prior.get(name, "adopted") for name in committed_renders()},
    }


def verify() -> list[str]:
    """Return human-readable problems with the committed binding, if any."""
    if not BINDING.exists():
        return [f"{BINDING.relative_to(REPO_ROOT)} is missing; run with --update"]

    binding = load_binding()
    problems: list[str] = []

    sha, count = bundle_digest()
    recorded_sha = binding.get("spa_bundle_sha256")
    recorded = binding.get("renders")
    names = sorted(recorded) if isinstance(recorded, dict) else []

    if recorded_sha != sha:
        problems.append(
            "the SPA bundle in the wheel has changed since these renders were bound, "
            "so the published screenshots may show a UI that no longer exists.\n"
            f"  bound to:   {recorded_sha}\n"
            f"  bundle now: {sha} ({count} files)\n"
            "  affected renders: " + ", ".join(names or ["(none recorded)"])
        )

    missing = sorted(set(committed_renders()) - set(names))
    if missing:
        problems.append(
            f"these renders are committed but bound to no bundle, so nothing would notice them going stale: {missing}"
        )

    vanished = sorted(set(names) - set(committed_renders()))
    if vanished:
        problems.append(f"these renders are bound but no longer committed: {vanished}")

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--update", action="store_true", help="rebind the renders to the current bundle")
    args = parser.parse_args(argv)

    if args.update:
        previous = load_binding() if BINDING.exists() else None
        binding = build_binding(previous)
        BINDING.write_text(json.dumps(binding, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"bound {len(binding['renders'])} render(s) to bundle {binding['spa_bundle_sha256'][:12]}…")  # type: ignore[index]
        return 0

    problems = verify()
    if not problems:
        print("browser renders are bound to the SPA bundle that ships today")
        return 0

    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    print(
        "\nRe-capture the affected screens, then rebind:\n"
        "  python3 scripts/capture_webui_renders.py             # all of them\n"
        "  python3 scripts/capture_webui_renders.py tasks costs # or just these\n"
        "  uv run python scripts/bind_webui_renders.py --update\n"
        "See docs/contributing/render-freshness.md.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
