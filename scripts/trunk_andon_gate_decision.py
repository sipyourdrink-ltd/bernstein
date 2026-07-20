"""Decide whether a PR may merge while ``TRUNK_UNSTABLE`` is set.

Companion to the trunk andon gate step in
``.github/workflows/pr-policy.yml`` (gate landed via PR #1456). The
Andon gate's default behaviour holds every PR on a red trunk except
those labeled ``hotfix-cleared``. Two additional escapes are needed
for real-world operation:

1. ``force-merge`` label - escalation level above ``hotfix-cleared``.
   Used when the operator decides the hold itself is causing more harm
   than the trunk regression. Surfaces a louder warning so the override
   is visible in run logs.
2. ``[trunk-andon-override]`` token in the PR commit-message body -
   self-attestation override. Allows a shepherd agent to override
   without round-tripping to a human to apply a label.

The script reads inputs from environment variables (set by the workflow
in stand-alone-invocation mode) and prints a structured verdict on
stdout for downstream steps to consume:

  decision=pass|fail
  reason=trunk_healthy|label_hotfix_cleared|label_force_merge|commit_override|trunk_unstable_no_override

Exit code is always 0 to keep the workflow flowing; the caller decides
how to fail the gate based on the verdict.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass

COMMIT_OVERRIDE_TOKEN = "[trunk-andon-override]"

# Labels in priority order. First match wins.
OVERRIDE_LABELS = (
    "hotfix-cleared",
    "force-merge",
)


@dataclass(frozen=True)
class Inputs:
    unstable: bool
    labels: tuple[str, ...]
    pr_body: str
    head_commit_msg: str


def _parse_labels(raw: str) -> tuple[str, ...]:
    """Parse labels passed as either a JSON array (default GHA shape) or
    a whitespace/comma-separated string (manual workflow_dispatch)."""
    raw = raw.strip()
    if not raw:
        return ()
    if raw.startswith("["):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return ()
        return tuple(str(item).strip() for item in data if str(item).strip())
    # Fallback: comma-or-space separated.
    return tuple(re.split(r"[\s,]+", raw))


def load_inputs() -> Inputs:
    return Inputs(
        unstable=os.environ.get("TRUNK_UNSTABLE", "false").strip().lower() == "true",
        labels=_parse_labels(os.environ.get("PR_LABELS", "[]")),
        pr_body=os.environ.get("PR_BODY", ""),
        head_commit_msg=os.environ.get("PR_HEAD_COMMIT_MSG", ""),
    )


def decide(inputs: Inputs) -> tuple[str, str]:
    """Return (decision, reason). decision is 'pass' or 'fail'."""
    if not inputs.unstable:
        return ("pass", "trunk_healthy")
    label_set = {lbl.strip() for lbl in inputs.labels if lbl.strip()}
    for lbl in OVERRIDE_LABELS:
        if lbl in label_set:
            return ("pass", f"label_{lbl.replace('-', '_')}")
    haystack = f"{inputs.pr_body}\n{inputs.head_commit_msg}"
    if COMMIT_OVERRIDE_TOKEN in haystack:
        return ("pass", "commit_override")
    return ("fail", "trunk_unstable_no_override")


def emit(decision: str, reason: str) -> None:
    """Print machine-readable lines AND attach to GITHUB_OUTPUT if set."""
    print(f"decision={decision}")
    print(f"reason={reason}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"decision={decision}\n")
            fh.write(f"reason={reason}\n")


def main() -> int:
    inputs = load_inputs()
    decision, reason = decide(inputs)
    emit(decision, reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
