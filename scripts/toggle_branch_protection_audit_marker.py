#!/usr/bin/env python3
"""Toggle marker issues for branch protection audit failures and recovery.

Distinguishes two failure modes:
1. "Unreachable / Auth failure" (exit code 2 or missing token):
   Label `branch-protection-unreachable`
   Title: `Branch protection audit cannot read live rulesets (auth/credential failure)`
2. "Drift / Invariants violated" (exit code 1):
   Label `branch-protection-drift`
   Title: `Branch protection audit detected ruleset drift on main`

When the audit succeeds (exit code 0):
Closes any open marker issues labeled `branch-protection-unreachable` or
`branch-protection-drift`.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

LABEL_UNREACHABLE = "branch-protection-unreachable"
LABEL_DRIFT = "branch-protection-drift"

TITLE_UNREACHABLE = "Branch protection audit cannot read live rulesets (auth/credential failure)"
TITLE_DRIFT = "Branch protection audit detected ruleset drift on main"

# GitHub caps a label description at 100 characters and rejects a longer one
# with HTTP 422. `gh label create --force` then fails, and because
# ensure_label only warns, the marker issue is opened with a label that was
# never created. That is fatal for the drift path, whose label does not
# already exist. Keep both of these under the cap; the test pins it.
DESC_UNREACHABLE = "Open while the audit cannot read live rulesets; closed automatically on recovery"
DESC_DRIFT = "Open while live branch protection disagrees with in-tree invariants; closed on recovery"


def _gh(args: list[str]) -> str:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed (exit {proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout.strip()


def ensure_label(repo: str, name: str, description: str, color: str) -> None:
    try:
        _gh(
            [
                "label",
                "create",
                name,
                "--repo",
                repo,
                "--description",
                description,
                "--color",
                color,
                "--force",
            ]
        )
    except Exception as exc:
        print(f"Warning: could not ensure label {name}: {exc}", file=sys.stderr)


def list_open_markers(repo: str, label: str) -> list[int]:
    try:
        raw = _gh(
            [
                "api",
                f"repos/{repo}/issues?labels={label}&state=open&per_page=100",
                "--jq",
                "[.[] | select(.pull_request | not) | .number]",
            ]
        )
        if not raw:
            return []
        data = json.loads(raw)
        return [int(num) for num in data if isinstance(num, int)]
    except Exception as exc:
        print(f"Warning: could not list markers for label {label}: {exc}", file=sys.stderr)
        return []


def open_marker(repo: str, label: str, title: str, body: str) -> None:
    open_issues = list_open_markers(repo, label)
    if open_issues:
        print(f"Marker issue with label '{label}' already open: #{open_issues[0]}")
        return
    try:
        _gh(
            [
                "api",
                "-X",
                "POST",
                f"repos/{repo}/issues",
                "-f",
                f"title={title}",
                "-f",
                f"body={body}",
                "-f",
                f"labels[]={label}",
            ]
        )
        print(f"Opened new marker issue with label '{label}'.")
    except Exception as exc:
        print(f"Warning: failed to open marker issue: {exc}", file=sys.stderr)


def close_markers(repo: str, label: str, close_comment: str) -> None:
    open_issues = list_open_markers(repo, label)
    for issue_num in open_issues:
        try:
            _gh(
                [
                    "api",
                    "-X",
                    "POST",
                    f"repos/{repo}/issues/{issue_num}/comments",
                    "-f",
                    f"body={close_comment}",
                ]
            )
            _gh(
                [
                    "api",
                    "-X",
                    "PATCH",
                    f"repos/{repo}/issues/{issue_num}",
                    "-f",
                    "state=closed",
                ]
            )
            print(f"Closed marker issue #{issue_num} ({label}).")
        except Exception as exc:
            print(f"Warning: failed to close marker issue #{issue_num}: {exc}", file=sys.stderr)


def sync_markers(repo: str, exit_code: int, detail: str = "") -> None:
    summary_lines: list[str] = [
        "## Branch Protection Audit",
        "",
    ]

    if exit_code == 0:
        summary_lines.extend(
            [
                "Status: **PASS (Healthy)**",
                "",
                "All live ruleset invariants on `main` match in-tree expectations.",
            ]
        )
        close_markers(
            repo,
            LABEL_UNREACHABLE,
            "Branch protection audit successfully read live rulesets and all invariants passed. Closing marker.",
        )
        close_markers(
            repo,
            LABEL_DRIFT,
            "Live branch protection now satisfies all in-tree invariants. Closing marker.",
        )

    elif exit_code == 1:
        summary_lines.extend(
            [
                "Status: **FAIL (Ruleset Drift Detected)**",
                "",
                "The live ruleset on `main` violated one or more audited invariants.",
            ]
        )
        if detail:
            summary_lines.extend(["", "```", detail, "```"])
        ensure_label(repo, LABEL_DRIFT, DESC_DRIFT, "B60205")
        body = "\n".join(
            [
                "The scheduled branch protection audit detected drift between live rulesets and "
                "in-tree invariants on `main`.",
                "",
                detail if detail else "One or more required rules or status checks were missing or misconfigured.",
                "",
                "This issue closes automatically when the next audit run confirms ruleset compliance.",
            ]
        )
        open_marker(repo, LABEL_DRIFT, TITLE_DRIFT, body)
        close_markers(
            repo,
            LABEL_UNREACHABLE,
            "Audit was able to reach GitHub API (drift was detected). Closing unreachability marker.",
        )

    else:  # exit_code >= 2
        summary_lines.extend(
            [
                "Status: **ERROR (Unreachable / Credential Failure)**",
                "",
                "The audit could not read live repository rulesets from GitHub API.",
            ]
        )
        if detail:
            summary_lines.extend(["", "```", detail, "```"])
        ensure_label(repo, LABEL_UNREACHABLE, DESC_UNREACHABLE, "d73a4a")
        body = "\n".join(
            [
                "The scheduled branch protection audit failed to read live rulesets from the GitHub API.",
                "",
                f"Details: {detail}"
                if detail
                else (
                    "Check whether `BRANCH_PROTECTION_AUDIT_TOKEN` secret is set and has "
                    "`Administration: read` permissions."
                ),
                "",
                "This issue closes automatically when the next audit run successfully reads the live rulesets.",
            ]
        )
        open_marker(repo, LABEL_UNREACHABLE, TITLE_UNREACHABLE, body)
        close_markers(
            repo,
            LABEL_DRIFT,
            "Audit could not read live rulesets; closing drift marker to avoid false state.",
        )

    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        try:
            with open(summary_file, "a", encoding="utf-8") as f:
                f.write("\n".join(summary_lines) + "\n")
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", help="GitHub repo OWNER/REPO (defaults to GITHUB_REPOSITORY)")
    parser.add_argument(
        "--exit-code", type=int, default=0, help="Exit code from check_branch_ruleset_audit.py (0, 1, or 2)"
    )
    parser.add_argument("--result-file", help="Path to JSON file containing {exit_code: int, detail: str}")
    parser.add_argument("--detail", default="", help="Error or violation details string")
    args = parser.parse_args(argv)

    repo = args.repo or os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        print("Error: repository not specified and GITHUB_REPOSITORY not set", file=sys.stderr)
        return 1

    exit_code = args.exit_code
    detail = args.detail

    if args.result_file and Path(args.result_file).is_file():
        try:
            data = json.loads(Path(args.result_file).read_text(encoding="utf-8"))
            if isinstance(data, dict):
                exit_code = int(data.get("exit_code", exit_code))
                detail = str(data.get("detail", detail))
        except (OSError, ValueError, TypeError) as exc:
            print(f"Warning: could not parse result-file {args.result_file}: {exc}", file=sys.stderr)

    sync_markers(repo, exit_code, detail)
    return exit_code if exit_code != 0 else 0


if __name__ == "__main__":
    sys.exit(main())
