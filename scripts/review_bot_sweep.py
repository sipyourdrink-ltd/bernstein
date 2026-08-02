#!/usr/bin/env python3
"""Post-merge sweeper for unprocessed review-bot findings.

Walks recently merged PRs and collects must-address findings that were
not addressed before merge. Writes a Markdown manifest the calling
workflow can attach to a follow-up PR.

Heuristic for "addressed":
    A finding is treated as addressed when EITHER
      (a) the PR body contains a `<!-- bot-ack: <id> ... -->` marker, OR
      (b) a commit on the PR carries `bot-ack: <id>` or `addresses: <id>`
          in its message.
    The script does not attempt to diff-match suggested code against the
    merged tree; that requires fragile NLP. The marker convention is the
    contract enforced by the pre-merge gate.

Usage:
    python scripts/review_bot_sweep.py --owner X --repo Y \\
        [--since-days 30] [--out manifest.md]
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path

# Reuse classification + GitHub helpers from the gate script.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from review_bot_ack import (  # noqa: E402
    Finding,
    evaluate,
    gh_request,
)


def list_merged_prs(owner: str, repo: str, token: str, since: dt.datetime) -> list[dict]:
    # GitHub search caps at 1000 results; fine for a 30-day window.
    q = f"repo:{owner}/{repo}+is:pr+is:merged+merged:>{since.date().isoformat()}"
    url = f"https://api.github.com/search/issues?q={q}&sort=updated&order=desc"
    page = 1
    out: list[dict] = []
    while True:
        full = f"{url}&per_page=100&page={page}"
        data = gh_request("GET", full, token) or {}
        items = data.get("items") or []
        out.extend(items)
        if len(items) < 100:
            break
        page += 1
        if page > 10:
            break
    return out


def render_manifest(
    owner: str,
    repo: str,
    misses: list[tuple[int, str, list[Finding]]],
    window_days: int,
) -> str:
    if not misses:
        return ""
    today = dt.date.today().isoformat()
    lines = [
        f"# Deferred review-bot findings - {today}",
        "",
        (f"Sweep window: last {window_days} days of merged PRs in `{owner}/{repo}`."),
        "",
        (
            "Automated review tools flag legitimate correctness and security "
            "issues. This PR consolidates findings that merged without an "
            "explicit acknowledgement or fixup commit. Each entry needs "
            "either a code change in this PR (with `bot-ack: <id>` in the "
            "commit message) or a one-line ack added to the source PR body."
        ),
        "",
        "## Missed findings",
        "",
    ]
    total = 0
    for pr_n, pr_title, findings in misses:
        lines.append(f"### PR #{pr_n} - {pr_title}")
        lines.append("")
        for f in findings:
            loc = f.path or "(general)"
            lines.append(f"- [{f.author}] `{loc}` (comment id `{f.comment_id}`): {f.short}")
            if f.html_url:
                lines.append(f"  - link: {f.html_url}")
            total += 1
        lines.append("")
    lines.insert(
        4,
        f"Total missed must-address findings: **{total}** across **{len(misses)}** PRs.",
    )
    lines.insert(5, "")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--owner", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--since-days", type=int, default=30)
    p.add_argument("--out", default="review-sweep-manifest.md")
    p.add_argument(
        "--max-prs",
        type=int,
        default=60,
        help="Cap on PRs inspected per sweep run.",
    )
    args = p.parse_args(argv)

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("error: GH_TOKEN or GITHUB_TOKEN must be set", file=sys.stderr)
        return 2

    since = dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=args.since_days)
    try:
        prs = list_merged_prs(args.owner, args.repo, token, since)
    except Exception as exc:
        print(f"error: list merged PRs failed: {exc}", file=sys.stderr)
        return 2
    prs = prs[: args.max_prs]
    misses: list[tuple[int, str, list[Finding]]] = []
    for item in prs:
        pr_n = int(item.get("number") or 0)
        if not pr_n:
            continue
        try:
            outcome = evaluate(args.owner, args.repo, pr_n, token)
        except Exception as exc:
            print(f"warning: skip PR #{pr_n}: {exc}", file=sys.stderr)
            continue
        if outcome.must_unresolved:
            misses.append((pr_n, item.get("title") or "(no title)", outcome.must_unresolved))

    manifest = render_manifest(args.owner, args.repo, misses, args.since_days)
    Path(args.out).write_text(manifest, encoding="utf-8")
    if manifest:
        print(f"wrote manifest to {args.out}: {sum(len(m[2]) for m in misses)} findings across {len(misses)} PRs")
    else:
        print("no missed findings - sweep clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
