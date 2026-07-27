#!/usr/bin/env python3
"""Diff the live `main-merge-queue` ruleset against its intended spec.

The merge-queue configuration decides two things that fail silently in
opposite directions:

``check_response_timeout_minutes``
    Below the real CI wall time, every queue entry is ejected as timed
    out. The queue looks enabled and nothing ever merges through it.

``required_status_checks``
    A context that branch protection requires to ENTER the queue but the
    ruleset does not require to MERGE from it is a gate the queue drops.
    Nothing reports an error; the check simply stops being enforced on
    the commit that actually lands.

Both drifted on the shipped ruleset and both survived, because the
intended configuration lived only in prose and prose does not compare
itself to anything. `docs/operations/merge-queue-ruleset.json` is the
machine-readable intent - it is the exact body `merge-queue.md` Step 1
PUTs - and this script reads the live ruleset back and reports the
difference.

Run it before flipping `enforcement` to `active`, and again after:

    python scripts/verify_merge_queue_ruleset.py
    python scripts/verify_merge_queue_ruleset.py --ruleset-id 16719298

Offline, against a captured payload:

    python scripts/verify_merge_queue_ruleset.py --live-file ruleset.json

Exit codes: 0 the live ruleset matches the spec, 1 it drifted, 2 the
ruleset or the spec could not be read.

`enforcement` is deliberately NOT compared. The spec pins it to
`disabled` so the corrective PUT cannot enable the queue as a side
effect, while a correctly flipped queue is `active`; treating that as
drift would make the script fail exactly when the flip succeeded.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = REPO_ROOT / "docs" / "operations" / "merge-queue-ruleset.json"
DEFAULT_REPO = "sipyourdrink-ltd/bernstein"
DEFAULT_RULESET_ID = 16719298

# Merge-queue parameters compared one by one so the report names the
# field an operator has to change, not "the rule differs".
MERGE_QUEUE_FIELDS = (
    "merge_method",
    "grouping_strategy",
    "max_entries_to_build",
    "min_entries_to_merge",
    "max_entries_to_merge",
    "min_entries_to_merge_wait_minutes",
    "check_response_timeout_minutes",
)


@dataclass(frozen=True)
class Drift:
    """One field on which the live ruleset disagrees with the spec."""

    field: str
    expected: Any
    actual: Any
    detail: str


def load_intended(path: Path | str = SPEC_PATH) -> dict[str, Any]:
    """Read the intended ruleset spec."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return data


def _rule(ruleset: dict[str, Any], rule_type: str) -> dict[str, Any] | None:
    for rule in ruleset.get("rules") or []:
        if isinstance(rule, dict) and rule.get("type") == rule_type:
            params = rule.get("parameters")
            return params if isinstance(params, dict) else {}
    return None


def merge_queue_parameters(ruleset: dict[str, Any]) -> dict[str, Any]:
    """The `merge_queue` rule's parameters, or `{}` when the rule is absent."""
    return _rule(ruleset, "merge_queue") or {}


def required_contexts(ruleset: dict[str, Any]) -> list[str]:
    """Sorted required status check context names.

    Sorted because GitHub does not promise an order, so a reordered
    response is not a configuration change.
    """
    params = _rule(ruleset, "required_status_checks") or {}
    contexts = params.get("required_status_checks") or []
    return sorted(str(entry.get("context", "")) for entry in contexts if isinstance(entry, dict))


def diff_ruleset(live: dict[str, Any], intended: dict[str, Any]) -> list[Drift]:
    """Report every way `live` disagrees with `intended`.

    An empty list means the live ruleset is safe to enable.
    """
    drifts: list[Drift] = []

    live_mq = _rule(live, "merge_queue")
    if live_mq is None:
        drifts.append(
            Drift(
                field="merge_queue",
                expected="a merge_queue rule",
                actual="absent",
                detail="the ruleset carries no merge_queue rule, so no queue exists",
            )
        )
    else:
        want_mq = merge_queue_parameters(intended)
        for field in MERGE_QUEUE_FIELDS:
            if field not in want_mq:
                continue
            expected = want_mq[field]
            actual = live_mq.get(field)
            if actual != expected:
                drifts.append(
                    Drift(
                        field=field,
                        expected=expected,
                        actual=actual,
                        detail=f"{field}: live {actual!r}, spec {expected!r}",
                    )
                )

    live_checks = _rule(live, "required_status_checks")
    if live_checks is None:
        drifts.append(
            Drift(
                field="required_status_checks",
                expected=required_contexts(intended),
                actual="absent",
                detail=(
                    "the ruleset carries no required_status_checks rule, so the "
                    "queue would merge without gating on any check"
                ),
            )
        )
    else:
        want = required_contexts(intended)
        have = required_contexts(live)
        if have != want:
            missing = [c for c in want if c not in have]
            extra = [c for c in have if c not in want]
            parts = []
            if missing:
                parts.append("missing " + ", ".join(repr(c) for c in missing))
            if extra:
                parts.append("unexpected " + ", ".join(repr(c) for c in extra))
            drifts.append(
                Drift(
                    field="required_status_checks",
                    expected=want,
                    actual=have,
                    detail="; ".join(parts),
                )
            )

    return drifts


def fetch_live(repo: str, ruleset_id: int) -> dict[str, Any]:
    """Read the live ruleset through `gh api`."""
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}/rulesets/{ruleset_id}"],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("gh is not installed or not on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"gh api repos/{repo}/rulesets/{ruleset_id} failed: {exc.stderr.strip()}") from exc
    return json.loads(result.stdout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--ruleset-id", type=int, default=DEFAULT_RULESET_ID)
    parser.add_argument("--spec", default=str(SPEC_PATH))
    parser.add_argument(
        "--live-file",
        help="read the live ruleset from a file instead of calling gh api",
    )
    args = parser.parse_args(argv)

    try:
        intended = load_intended(args.spec)
    except (OSError, ValueError) as exc:
        print(f"error: cannot read the spec: {exc}", file=sys.stderr)
        return 2

    try:
        if args.live_file:
            live = json.loads(Path(args.live_file).read_text(encoding="utf-8"))
        else:
            live = fetch_live(args.repo, args.ruleset_id)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: cannot read the live ruleset: {exc}", file=sys.stderr)
        return 2

    drifts = diff_ruleset(live, intended)
    enforcement = live.get("enforcement", "unknown")
    print(f"ruleset {live.get('name', '?')} (id {live.get('id', '?')})")
    print(f"enforcement: {enforcement}")

    if not drifts:
        print("OK: the live ruleset matches docs/operations/merge-queue-ruleset.json")
        return 0

    print(f"DRIFT: {len(drifts)} field(s) disagree with the spec")
    for drift in drifts:
        print(f"  - {drift.field}: {drift.detail}")
        print(f"      live: {drift.actual!r}")
        print(f"      spec: {drift.expected!r}")
    print()
    print(
        "Apply Step 1 of docs/operations/merge-queue.md before enabling the "
        "queue:\n"
        f"  gh api -X PUT repos/{args.repo}/rulesets/{args.ruleset_id} "
        "--input docs/operations/merge-queue-ruleset.json"
    )
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
