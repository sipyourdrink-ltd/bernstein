#!/usr/bin/env python3
"""Report required check contexts that are absent from a commit.

A pull request whose head commit carries no check-run for a required context
looks exactly like a pull request whose checks all passed: the failure
histogram reads zero either way. The two states are opposites - "0 failures
because everything passed" and "0 failures because nothing ran" - and this
script names which one a commit is in.

The required contexts are read from the in-tree canary
(``.github/workflows/required-check-canary.yml``, key
``BRANCH_PROTECTION_CONTEXTS_JSON``), the same source the scheduled
branch-protection audit compares live settings against. Nothing here reads or
changes branch protection.

Every context is classified as one of:

``missing``   no check-run with that name exists on the commit
``pending``   a check-run exists but has not completed
``failing``   completed with failure / timed_out / cancelled / action_required
``skipped``   completed as skipped
``passing``   completed as success or neutral

The exit code is non-zero only for ``missing`` - a real failure is the merge
gate's business, an absent context is this script's.

Usage::

    python scripts/check_required_contexts.py --pr 3032
    python scripts/check_required_contexts.py --sha "$GITHUB_SHA"
    python scripts/check_required_contexts.py --sha abc123 --check-runs runs.json
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
CANARY_PATH = REPO_ROOT / ".github" / "workflows" / "required-check-canary.yml"
CONTEXTS_ENV_KEY = "BRANCH_PROTECTION_CONTEXTS_JSON"

MISSING = "missing"
PENDING = "pending"
FAILING = "failing"
SKIPPED = "skipped"
PASSING = "passing"

_FAILING_CONCLUSIONS = frozenset({"failure", "timed_out", "cancelled", "action_required", "stale"})
_PASSING_CONCLUSIONS = frozenset({"success", "neutral"})


@dataclass(frozen=True)
class ContextState:
    """The state of one required context on a commit."""

    name: str
    state: str
    detail: str


@dataclass(frozen=True)
class PresenceReport:
    """Presence of every required context on a commit."""

    sha: str
    states: tuple[ContextState, ...]

    @property
    def missing(self) -> tuple[ContextState, ...]:
        """Required contexts with no check-run at all."""
        return tuple(state for state in self.states if state.state == MISSING)

    @property
    def ok(self) -> bool:
        """True when every required context is present in some form."""
        return not self.missing


def read_required_contexts(canary_path: Path | None = None) -> list[str]:
    """Read the required contexts from the in-tree canary workflow."""
    path = CANARY_PATH if canary_path is None else canary_path
    try:
        import yaml
    except ModuleNotFoundError as exc:  # pragma: no cover - dev env has pyyaml
        raise RuntimeError("pyyaml is required to read the required-check canary") from exc

    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw = _find_contexts_value(document)
    if raw is None:
        raise ValueError(f"{path} does not define {CONTEXTS_ENV_KEY}")
    parsed = json.loads(raw)
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ValueError(f"{path} {CONTEXTS_ENV_KEY} must be a JSON list of strings")
    contexts = [item for item in parsed if item]
    if not contexts:
        raise ValueError(f"{path} {CONTEXTS_ENV_KEY} is empty")
    return contexts


def _find_contexts_value(document: object) -> str | None:
    """Return the raw contexts JSON from any ``env:`` block in the document."""
    if isinstance(document, dict):
        for key, value in document.items():
            if key == CONTEXTS_ENV_KEY and isinstance(value, str):
                return value
            found = _find_contexts_value(value)
            if found is not None:
                return found
    elif isinstance(document, list):
        for item in document:
            found = _find_contexts_value(item)
            if found is not None:
                return found
    return None


def classify(required: list[str], check_runs: list[dict[str, Any]], sha: str = "") -> PresenceReport:
    """Classify each required context against the commit's check-runs."""
    by_name: dict[str, list[dict[str, Any]]] = {}
    for run in check_runs:
        name = run.get("name")
        if isinstance(name, str):
            by_name.setdefault(name, []).append(run)

    states: list[ContextState] = []
    for context in required:
        runs = by_name.get(context)
        if not runs:
            states.append(ContextState(context, MISSING, "no check-run with this name on the commit"))
            continue
        run = runs[-1]
        status = str(run.get("status", ""))
        conclusion = str(run.get("conclusion") or "")
        if status != "completed":
            states.append(ContextState(context, PENDING, f"status={status or 'unknown'}"))
        elif conclusion in _FAILING_CONCLUSIONS:
            states.append(ContextState(context, FAILING, f"conclusion={conclusion}"))
        elif conclusion == SKIPPED:
            states.append(ContextState(context, SKIPPED, "conclusion=skipped"))
        elif conclusion in _PASSING_CONCLUSIONS:
            states.append(ContextState(context, PASSING, f"conclusion={conclusion}"))
        else:
            states.append(ContextState(context, PENDING, f"conclusion={conclusion or 'unknown'}"))
    return PresenceReport(sha=sha, states=tuple(states))


def summary_line(report: PresenceReport) -> str:
    """Return the one-line verdict a reader needs."""
    if report.missing:
        names = ", ".join(state.name for state in report.missing)
        return f"{len(report.missing)} required context(s) never ran on {report.sha or 'the commit'}: {names}"
    failing = [state for state in report.states if state.state == FAILING]
    if failing:
        return f"all required contexts present; {len(failing)} failing"
    pending = [state for state in report.states if state.state == PENDING]
    if pending:
        return f"all required contexts present; {len(pending)} still running"
    return "all required contexts present and completed"


def _gh(args: list[str]) -> str:
    """Run a ``gh`` command and return stdout."""
    try:
        result = subprocess.run(["gh", *args], capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise RuntimeError("the GitHub CLI (gh) is required to fetch check-runs") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(exc.stderr.strip() or f"gh {' '.join(args)} failed") from exc
    return result.stdout


def resolve_pr_head(pr: str, repo: str | None) -> str:
    """Return the head commit SHA of a pull request."""
    args = ["pr", "view", pr, "--json", "headRefOid"]
    if repo:
        args += ["--repo", repo]
    payload = json.loads(_gh(args))
    head = payload.get("headRefOid")
    if not isinstance(head, str) or not head:
        raise RuntimeError(f"could not resolve the head commit of PR {pr}")
    return head


def fetch_check_runs(sha: str, repo: str | None) -> list[dict[str, Any]]:
    """Return every check-run recorded against a commit."""
    slug = repo or "{owner}/{repo}"
    endpoint = f"repos/{slug}/commits/{sha}/check-runs?per_page=100"
    output = _gh(["api", "--paginate", endpoint, "--jq", ".check_runs[]"])
    runs: list[dict[str, Any]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if isinstance(entry, dict):
            runs.append(entry)
    return runs


def _load_check_runs_file(path: str) -> list[dict[str, Any]]:
    """Read check-runs from a file (or stdin), accepting the API or list shape."""
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    payload = json.loads(raw)
    if isinstance(payload, dict):
        payload = payload.get("check_runs", [])
    if not isinstance(payload, list):
        raise ValueError("check-runs input must be a list or an object with check_runs")
    return [entry for entry in payload if isinstance(entry, dict)]


def _print_report(report: PresenceReport) -> None:
    """Print the per-context table and the verdict."""
    print(f"Required-context presence on {report.sha or 'commit'}:")
    for state in report.states:
        marker = "x" if state.state == MISSING else "-"
        print(f"  {marker} {state.name}: {state.state} ({state.detail})")
    print(summary_line(report))
    for state in report.missing:
        print(
            f"::error title=Required context never ran::{state.name} has no check-run on "
            f"{report.sha or 'the head commit'}. A green PR page here means the context was "
            f"never evaluated, not that it passed."
        )


def main() -> int:
    """Entry point: report required contexts absent from a commit."""
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--sha", help="Commit SHA to inspect")
    target.add_argument("--pr", help="Pull request number; its head commit is inspected")
    parser.add_argument("--repo", help="OWNER/REPO (defaults to the current repository)")
    parser.add_argument("--check-runs", help="Read check-runs from a JSON file instead of the API ('-' for stdin)")
    parser.add_argument("--json", action="store_true", help="Emit the report as JSON")
    args = parser.parse_args()

    required = read_required_contexts()
    sha = args.sha or resolve_pr_head(args.pr, args.repo)
    check_runs = _load_check_runs_file(args.check_runs) if args.check_runs else fetch_check_runs(sha, args.repo)
    report = classify(required, check_runs, sha=sha)

    if args.json:
        print(
            json.dumps(
                {
                    "sha": report.sha,
                    "required": required,
                    "states": [
                        {"name": state.name, "state": state.state, "detail": state.detail} for state in report.states
                    ],
                    "missing": [state.name for state in report.missing],
                    "summary": summary_line(report),
                },
                indent=2,
            )
        )
    else:
        _print_report(report)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
