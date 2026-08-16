#!/usr/bin/env python3
"""Audit the live repository ruleset that protects a branch.

`main`'s guarantees - the merge queue, the required status check, blocked
force-pushes, blocked deletion, no bypass - are enforced entirely by a
repository **ruleset**, not classic branch protection. The classic
`GET /repos/{owner}/{repo}/branches/{branch}/protection` endpoint cannot
represent ruleset-only rules at all: it has no field for `merge_queue` and
none for `bypass_actors`, so an audit built on it stays silent exactly when
the merge-queue rule is dropped or a bypass actor is added - the two
regressions that matter most. Every other invariant it could see is
already covered by the ruleset read below, so this script does not read
the classic endpoint in any form, primary or secondary.

Three ruleset-native calls, each pulling distinct weight:

`GET /repos/{owner}/{repo}/rules/branches/{branch}`
    The effective, GitHub-resolved rule set for the branch - the union of
    every ruleset that targets it. Source of rule-type presence
    (`merge_queue`, `non_fast_forward`, `deletion`) and the required
    status-check contexts.

`GET /repos/{owner}/{repo}/rulesets`
    Cheap listing of every ruleset with its `enforcement` state. Checked
    before the per-ruleset detail calls: a ruleset flipped to `evaluate`
    still contributes rules to the effective set above (GitHub does not
    drop it), so its rules being present is not proof they are enforced.

`GET /repos/{owner}/{repo}/rulesets/{id}`
    Full detail for each ruleset referenced by the effective rules - the
    only place `bypass_actors` is exposed.

Usage::

    python scripts/check_branch_ruleset_audit.py
    python scripts/check_branch_ruleset_audit.py --repo owner/repo --branch main

Offline, against captured payloads::

    python scripts/check_branch_ruleset_audit.py \\
        --rules-file rules.json --rulesets-file rulesets.json \\
        --ruleset-details-file details.json

Exit codes: 0 every invariant holds, 1 one or more invariants violated,
2 the live state (or required-context canary) could not be read.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
CANARY_PATH = REPO_ROOT / ".github" / "workflows" / "required-check-canary.yml"
CONTEXTS_ENV_KEY = "BRANCH_PROTECTION_CONTEXTS_JSON"
DEFAULT_BRANCH = "main"

# Rule types that must apply to the branch with no parameters to inspect
# beyond their presence. `required_status_checks` is checked separately
# because its contexts have to match the canary, not just exist.
REQUIRED_PRESENCE_RULE_TYPES = ("merge_queue", "non_fast_forward", "deletion")


@dataclass(frozen=True)
class Violation:
    """One way the live ruleset disagrees with an audited invariant."""

    rule: str
    detail: str


def read_required_contexts(canary_path: Path | None = None) -> list[str]:
    """Read required status-check contexts from the in-tree canary.

    Scans for the ``BRANCH_PROTECTION_CONTEXTS_JSON:`` line rather than
    parsing the file as YAML, so this script carries no dependency beyond
    the standard library - the same approach the audit workflow used
    inline before this script existed.
    """
    path = CANARY_PATH if canary_path is None else canary_path
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith(f"{CONTEXTS_ENV_KEY}:"):
            continue
        raw = stripped.split(":", 1)[1].strip()
        if raw.startswith(("'", '"')) and raw.endswith(raw[0]):
            raw = raw[1:-1]
        parsed = json.loads(raw)
        if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
            raise ValueError(f"{path} {CONTEXTS_ENV_KEY} must be a JSON list of strings")
        contexts = [item for item in parsed if item]
        if contexts:
            return contexts
    raise ValueError(f"{path} does not define {CONTEXTS_ENV_KEY}")


def _rule_types(rules: list[dict[str, Any]]) -> set[str]:
    return {rule["type"] for rule in rules if isinstance(rule, dict) and isinstance(rule.get("type"), str)}


def _required_status_check_contexts(rules: list[dict[str, Any]]) -> set[str]:
    contexts: set[str] = set()
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("type") != "required_status_checks":
            continue
        params = rule.get("parameters")
        if not isinstance(params, dict):
            continue
        for entry in params.get("required_status_checks") or []:
            if isinstance(entry, dict) and isinstance(entry.get("context"), str):
                contexts.add(entry["context"])
    return contexts


def _ruleset_ids(rules: list[dict[str, Any]]) -> set[int]:
    return {rule["ruleset_id"] for rule in rules if isinstance(rule, dict) and isinstance(rule.get("ruleset_id"), int)}


def _ruleset_summaries_by_id(rulesets: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {entry["id"]: entry for entry in rulesets if isinstance(entry, dict) and isinstance(entry.get("id"), int)}


def evaluate(
    rules: list[dict[str, Any]],
    rulesets: list[dict[str, Any]],
    ruleset_details: dict[int, dict[str, Any]],
    required_contexts: list[str],
) -> list[Violation]:
    """Check the branch's effective rules against the audited invariants.

    `rules` is the `rules/branches/{branch}` response (effective rules).
    `rulesets` is the `rulesets` listing (id + enforcement, no rule detail).
    `ruleset_details` maps each `ruleset_id` referenced by `rules` to its
    `rulesets/{id}` response, the only source of `bypass_actors`.
    """
    violations: list[Violation] = []

    if not rules:
        violations.append(Violation("rules", "no rules apply to this branch - protection is effectively absent"))
        return violations

    present = _rule_types(rules)

    for rule_type in REQUIRED_PRESENCE_RULE_TYPES:
        if rule_type not in present:
            violations.append(Violation(rule_type, f"no '{rule_type}' rule applies to this branch"))

    if "required_status_checks" not in present:
        violations.append(
            Violation("required_status_checks", "no 'required_status_checks' rule applies to this branch")
        )
    else:
        live = _required_status_check_contexts(rules)
        expected = set(required_contexts)
        missing = sorted(expected - live)
        extra = sorted(live - expected)
        if missing:
            violations.append(Violation("required_status_checks", f"missing required context(s): {missing}"))
        if extra:
            violations.append(Violation("required_status_checks", f"unexpected required context(s): {extra}"))

    ruleset_summaries = _ruleset_summaries_by_id(rulesets)
    ids = _ruleset_ids(rules)
    if not ids:
        violations.append(
            Violation("ruleset_id", "no rule in the effective set carries a ruleset_id; cannot verify bypass actors")
        )

    for ruleset_id in sorted(ids):
        summary = ruleset_summaries.get(ruleset_id)
        if summary is None:
            violations.append(
                Violation(
                    "rulesets",
                    f"ruleset {ruleset_id} backs main's effective rules but is absent from the rulesets listing",
                )
            )
        else:
            enforcement = summary.get("enforcement")
            if enforcement != "active":
                name = summary.get("name", "?")
                violations.append(
                    Violation("enforcement", f"ruleset {ruleset_id} ({name}) is '{enforcement}', not 'active'")
                )

        detail = ruleset_details.get(ruleset_id)
        if detail is None:
            violations.append(
                Violation("ruleset_detail", f"ruleset {ruleset_id} detail was not read; cannot verify bypass actors")
            )
            continue
        name = detail.get("name", "?")
        if "bypass_actors" not in detail:
            # Absent and empty are different answers on the wire: the API
            # omits the key entirely for a caller without Administration:
            # read, and returns [] only when an admin-scoped caller sees a
            # genuinely empty list. Treating the omission as "no bypass"
            # lets an under-scoped BRANCH_PROTECTION_AUDIT_TOKEN skip this
            # assertion silently while everything else keeps passing.
            violations.append(
                Violation(
                    "bypass_actors",
                    f"ruleset {ruleset_id} ({name}) returned no bypass_actors key -- the API omits "
                    "it for callers without Administration: read, so bypass is unproven, not empty",
                )
            )
        elif detail["bypass_actors"]:
            violations.append(
                Violation(
                    "bypass_actors",
                    f"ruleset {ruleset_id} ({name}) allows bypass: {detail['bypass_actors']}",
                )
            )

    return violations


def _gh(args: list[str]) -> str:
    """Run a ``gh`` command and return stdout."""
    try:
        result = subprocess.run(["gh", *args], capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise RuntimeError("the GitHub CLI (gh) is required to audit the live ruleset") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(exc.stderr.strip() or f"gh {' '.join(args)} failed") from exc
    return result.stdout


def fetch_effective_rules(repo: str, branch: str) -> list[dict[str, Any]]:
    """Read the GitHub-resolved effective rules for `branch`."""
    payload = json.loads(_gh(["api", f"repos/{repo}/rules/branches/{branch}"]))
    if not isinstance(payload, list):
        raise ValueError(f"repos/{repo}/rules/branches/{branch} did not return a list")
    return [item for item in payload if isinstance(item, dict)]


def fetch_rulesets(repo: str) -> list[dict[str, Any]]:
    """List every ruleset on the repository."""
    payload = json.loads(_gh(["api", f"repos/{repo}/rulesets"]))
    if not isinstance(payload, list):
        raise ValueError(f"repos/{repo}/rulesets did not return a list")
    return [item for item in payload if isinstance(item, dict)]


def fetch_ruleset_detail(repo: str, ruleset_id: int) -> dict[str, Any]:
    """Read one ruleset's full detail, including `bypass_actors`."""
    payload = json.loads(_gh(["api", f"repos/{repo}/rulesets/{ruleset_id}"]))
    if not isinstance(payload, dict):
        raise ValueError(f"repos/{repo}/rulesets/{ruleset_id} did not return an object")
    return payload


def _load_json_list(path: str) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"{path}: expected a JSON list")
    return [item for item in payload if isinstance(item, dict)]


def _load_ruleset_details_file(path: str) -> dict[int, dict[str, Any]]:
    """Read `{"<ruleset_id>": {...detail...}}` and key it by int id."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected a JSON object keyed by ruleset id")
    return {int(key): value for key, value in payload.items() if isinstance(value, dict)}


def _print_report(repo: str, branch: str, rules: list[dict[str, Any]], violations: list[Violation]) -> None:
    print(f"Branch ruleset audit for {repo}@{branch}")
    print(f"  effective rule types: {sorted(_rule_types(rules))}")
    print(f"  rulesets referenced : {sorted(_ruleset_ids(rules))}")

    if not violations:
        print("Live branch ruleset satisfies every audited invariant.")
        return

    print(f"::error::{len(violations)} branch ruleset invariant(s) violated")
    for violation in violations:
        print(f"::error title=Branch ruleset audit::{violation.rule}: {violation.detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", help="OWNER/REPO (defaults to $GITHUB_REPOSITORY)")
    parser.add_argument("--branch", default=DEFAULT_BRANCH)
    parser.add_argument("--rules-file", help="Read effective rules from a file instead of gh api")
    parser.add_argument("--rulesets-file", help="Read the rulesets listing from a file instead of gh api")
    parser.add_argument(
        "--ruleset-details-file",
        help='Read ruleset detail from a file instead of gh api; JSON object keyed by ruleset id, e.g. {"123": {...}}',
    )
    args = parser.parse_args(argv)

    repo = args.repo or os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        print("::error::--repo not given and GITHUB_REPOSITORY is not set")
        return 2

    try:
        required_contexts = read_required_contexts()
    except (OSError, ValueError) as exc:
        print(f"::error::cannot read required contexts from the required-check canary: {exc}")
        return 2

    offline = args.rules_file is not None

    try:
        rules = _load_json_list(args.rules_file) if args.rules_file else fetch_effective_rules(repo, args.branch)
        rulesets = _load_json_list(args.rulesets_file) if args.rulesets_file else fetch_rulesets(repo)

        ruleset_details: dict[int, dict[str, Any]] = (
            _load_ruleset_details_file(args.ruleset_details_file) if args.ruleset_details_file else {}
        )
        if not offline:
            for ruleset_id in sorted(_ruleset_ids(rules)):
                if ruleset_id not in ruleset_details:
                    ruleset_details[ruleset_id] = fetch_ruleset_detail(repo, ruleset_id)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"::error::Unable to read the live branch ruleset for {repo}@{args.branch}: {exc}")
        return 2

    violations = evaluate(rules, rulesets, ruleset_details, required_contexts)
    _print_report(repo, args.branch, rules, violations)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
