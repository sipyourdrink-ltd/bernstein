#!/usr/bin/env python3
"""Review-bot acknowledgement gate.

Parses CodeRabbit and Sourcery comments on a PR, classifies each as
must-address (bug/security/potential-issue/refactor-with-correctness) or
informational (nit/style/note), and verifies every must-address finding is
either acknowledged in the PR body via a `<!-- bot-ack: <id> ... -->` marker
or addressed in a subsequent commit.

It also tracks, per configured review bot, whether that bot actually produced
a review for the current head commit. Zero findings from a bot that was rate
limited is not the same result as zero findings from a bot that reviewed the
diff and found nothing, and the summary reports the two differently.

The gate posts a sticky summary comment on the PR (replacing any prior
summary it posted) and exits 1 if any must-address finding is unresolved.

Usage:
    python scripts/review_bot_ack.py \\
        --owner sipyourdrink-ltd --repo bernstein --pr 1576 [--strict]

Required environment:
    GH_TOKEN  GitHub token with `pull-requests: write` and `contents: read`.

Exit codes:
    0  Every must-address finding is fixed or acknowledged.
    1  At least one must-address finding is open, or `--require-review` was
       passed and some configured bot produced no review for the head commit.
    2  Internal error (HTTP failure, malformed JSON, etc.).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

REVIEW_BOT_LOGINS = {"coderabbitai[bot]", "sourcery-ai[bot]"}

# Tags that mark a finding as must-address. Matching is case-insensitive
# against the comment body. CodeRabbit uses headings like
# `**Potential issue**` or `_⚠️ Potential issue_`. Sourcery uses
# `**issue:**`, `**bug:**`, `**security:**`, `**suggestion (security):**`.
MUST_ADDRESS_PATTERNS = (
    r"potential issue",
    r"\bissue\b\s*:",
    r"\bbug\b\s*:",
    r"\bsecurity\b\s*:",
    r"suggestion\s*\(security\)",
    r"suggestion\s*\(bug",
    r"refactor\s*\(.*correctness",
    r"_⚠️\s*potential issue",
)

# Tags that mark a finding as informational. Anything that matches any
# informational pattern AND no must-address pattern is treated as skippable.
INFORMATIONAL_PATTERNS = (
    r"\bnit\b",
    r"\bstyle\b",
    r"\bnote\b\s*:",
    r"suggestion\s*\(style",
    r"suggestion\s*\(nit",
    r"suggestion\s*\(testing",
    r"refactor suggestion",
    r"\*\*note\*\*",
)

# Per-bot review coverage for the current head commit. Only BOT_REVIEWED is a
# clean result: the other three each mean the finding count for that bot is
# "not measured", which is not the same as "measured, and it was zero".
BOT_REVIEWED = "reviewed"
BOT_DECLINED = "declined"
BOT_STALE = "stale"
BOT_ABSENT = "absent"

_BOT_STATUS_TEXT = {
    BOT_REVIEWED: "reviewed this head commit",
    BOT_DECLINED: "did not run (rate limited or otherwise declined)",
    BOT_STALE: "reviewed an earlier head commit only",
    BOT_ABSENT: "produced no review on this pull request",
}

# Bodies a review bot posts *instead of* a review. Matched case-insensitively.
# Verbatim shapes: CodeRabbit's "Review limit reached" warning carries the
# `rate limited by coderabbit.ai` marker comment; Sourcery replies with its
# weekly diff-character limit as a submitted review.
DID_NOT_RUN_PATTERNS = (
    r"rate limited by coderabbit\.ai",
    r"review limit reached",
    r"you have reached your weekly rate limit",
    r"reached your (?:daily|monthly) rate limit",
)

_SHA_RE = re.compile(r"\b[0-9a-f]{40}\b", re.IGNORECASE)

STICKY_HEADER = "<!-- review-bot-ack-summary: managed -->"
ACK_MARKER_RE = re.compile(
    r"<!--\s*bot-ack:\s*(?P<id>[\w./-]+)\s*(?:reason=(?P<reason>[^>]+?))?\s*-->",
    re.IGNORECASE,
)
NIT_BATCH_SKIP_RE = re.compile(r"<!--\s*bot-ack:\s*nit-batch-skipped\s*-->", re.IGNORECASE)


@dataclass
class Finding:
    comment_id: int
    author: str
    path: str | None
    body: str
    severity: str  # "must-address" | "informational"
    source: str  # "review-comment" | "issue-comment"
    html_url: str = ""

    @property
    def short(self) -> str:
        first = self.body.strip().splitlines()[0] if self.body.strip() else ""
        return first[:140]


@dataclass
class BotArtifact:
    """One thing a review bot left on the PR.

    ``commit_id`` is populated for submitted reviews and review comments and
    is ``None`` for top-level issue comments, which is how CodeRabbit posts
    its review; that shape is anchored via the head SHA in ``body`` instead.
    """

    author: str
    body: str
    commit_id: str | None = None
    kind: str = "review"  # "review" | "review-comment" | "issue-comment"


@dataclass
class BotStatus:
    """Whether one configured review bot reviewed the current head commit."""

    login: str
    status: str
    detail: str = ""

    @property
    def clean(self) -> bool:
        """True only when this bot actually produced a review for the head."""
        return self.status == BOT_REVIEWED


@dataclass
class GateOutcome:
    findings: list[Finding] = field(default_factory=list)
    must_unresolved: list[Finding] = field(default_factory=list)
    must_acked: list[Finding] = field(default_factory=list)
    informational: list[Finding] = field(default_factory=list)
    head_sha: str = ""
    bot_statuses: list[BotStatus] = field(default_factory=list)

    @property
    def unreviewed_bots(self) -> list[BotStatus]:
        """Configured bots whose finding count is not a measured zero."""
        return [status for status in self.bot_statuses if not status.clean]


def gh_request(
    method: str,
    url: str,
    token: str,
    data: dict[str, Any] | None = None,
) -> Any:
    payload = None if data is None else json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API {method} {url} failed: {exc.code} {body[:300]}") from exc
    if not body:
        return None
    return json.loads(body)


def paginate(url: str, token: str) -> list[Any]:
    out: list[Any] = []
    page = 1
    while True:
        full = f"{url}{'&' if '?' in url else '?'}per_page=100&page={page}"
        chunk = gh_request("GET", full, token) or []
        if not isinstance(chunk, list):
            return out
        out.extend(chunk)
        if len(chunk) < 100:
            break
        page += 1
        if page > 30:
            break  # hard cap on pagination
    return out


def classify(body: str) -> str:
    low = body.lower()
    is_must = any(re.search(p, low) for p in MUST_ADDRESS_PATTERNS)
    is_info = any(re.search(p, low) for p in INFORMATIONAL_PATTERNS)
    if is_must and not is_info:
        return "must-address"
    if is_must and is_info:
        # When both tags appear, must-address wins so we don't lose a real bug.
        return "must-address"
    return "informational"


def looks_like_a_declined_review(body: str) -> bool:
    """True when a bot posted a rate-limit notice instead of a review."""
    low = body.lower()
    return any(re.search(pattern, low) for pattern in DID_NOT_RUN_PATTERNS)


def covers_head(artifact: BotArtifact, head_sha: str) -> bool:
    """True when this artefact is anchored to ``head_sha``.

    Reviews and review comments carry ``commit_id``. Top-level comments do
    not, so the head SHA is looked for among the 40-hex commit ids the body
    names - CodeRabbit's summary states the range it reviewed.
    """
    if not head_sha:
        return False
    if artifact.commit_id and artifact.commit_id.lower() == head_sha.lower():
        return True
    return any(match.group(0).lower() == head_sha.lower() for match in _SHA_RE.finditer(artifact.body))


def classify_bot_run(login: str, artifacts: list[BotArtifact], head_sha: str) -> BotStatus:
    """Determine whether ``login`` produced a review for ``head_sha``."""
    own = [artifact for artifact in artifacts if artifact.author == login]
    if not own:
        return BotStatus(login=login, status=BOT_ABSENT, detail=_BOT_STATUS_TEXT[BOT_ABSENT])

    reviews_for_head = [a for a in own if covers_head(a, head_sha) and not looks_like_a_declined_review(a.body)]
    if reviews_for_head:
        return BotStatus(login=login, status=BOT_REVIEWED, detail=_BOT_STATUS_TEXT[BOT_REVIEWED])

    if any(looks_like_a_declined_review(a.body) for a in own):
        return BotStatus(login=login, status=BOT_DECLINED, detail=_BOT_STATUS_TEXT[BOT_DECLINED])

    return BotStatus(login=login, status=BOT_STALE, detail=_BOT_STATUS_TEXT[BOT_STALE])


def classify_review_coverage(artifacts: list[BotArtifact], head_sha: str) -> list[BotStatus]:
    """Classify every configured review bot, seen on this PR or not."""
    return [classify_bot_run(login, artifacts, head_sha) for login in sorted(REVIEW_BOT_LOGINS)]


def bot_artifacts_from(sources: dict[str, list[Any]]) -> list[BotArtifact]:
    """Collect every artefact the configured review bots left on the PR."""
    artifacts: list[BotArtifact] = []
    for kind, items in sources.items():
        for item in items:
            login = (item.get("user") or {}).get("login", "")
            if login not in REVIEW_BOT_LOGINS:
                continue
            artifacts.append(
                BotArtifact(
                    author=login,
                    body=item.get("body") or "",
                    commit_id=item.get("commit_id"),
                    kind=kind,
                )
            )
    return artifacts


def fetch_comment_sources(owner: str, repo: str, pr: int, token: str) -> dict[str, list[Any]]:
    """Paginate each bot-comment endpoint once, keyed by artefact kind.

    Findings and review coverage are both derived from these three lists, so
    they are fetched here rather than in each consumer: ``paginate`` walks up
    to 30 pages per endpoint and this gate runs on every pull request.
    """
    base = f"https://api.github.com/repos/{owner}/{repo}"
    return {
        "review": paginate(f"{base}/pulls/{pr}/reviews", token),
        "review-comment": paginate(f"{base}/pulls/{pr}/comments", token),
        "issue-comment": paginate(f"{base}/issues/{pr}/comments", token),
    }


def findings_from(sources: dict[str, list[Any]]) -> list[Finding]:
    """Extract must-address / informational findings from fetched comments."""
    findings: list[Finding] = []
    for c in sources.get("review-comment", []):
        login = (c.get("user") or {}).get("login", "")
        if login not in REVIEW_BOT_LOGINS:
            continue
        body = c.get("body") or ""
        findings.append(
            Finding(
                comment_id=int(c["id"]),
                author=login,
                path=c.get("path"),
                body=body,
                severity=classify(body),
                source="review-comment",
                html_url=c.get("html_url") or "",
            )
        )
    for c in sources.get("issue-comment", []):
        login = (c.get("user") or {}).get("login", "")
        if login not in REVIEW_BOT_LOGINS:
            continue
        body = c.get("body") or ""
        # Skip summary/review-guide blocks; they're not actionable findings.
        if "<!-- generated by sourcery-ai[bot]: start review_guide -->" in body.lower():
            continue
        if "summarize by coderabbit.ai" in body.lower() and "rate limit" in body.lower():
            continue
        if "summarize by coderabbit.ai" in body.lower() and "actionable comments posted: 0" in body.lower():
            continue
        sev = classify(body)
        if sev == "informational":
            # Top-level bot comments are usually summaries; only keep
            # informational records when explicitly actionable.
            continue
        findings.append(
            Finding(
                comment_id=int(c["id"]),
                author=login,
                path=None,
                body=body,
                severity=sev,
                source="issue-comment",
                html_url=c.get("html_url") or "",
            )
        )
    return findings


def pr_body_and_head(owner: str, repo: str, pr: int, token: str) -> tuple[str, str]:
    """Return the PR body and its current head SHA in one request."""
    data = gh_request("GET", f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr}", token) or {}
    head_sha = ((data.get("head") or {}).get("sha")) or ""
    return data.get("body") or "", head_sha


def ack_ids(body: str) -> tuple[set[str], bool]:
    ids = {m.group("id") for m in ACK_MARKER_RE.finditer(body)}
    nit_batch = bool(NIT_BATCH_SKIP_RE.search(body))
    return ids, nit_batch


def fixup_addresses(owner: str, repo: str, pr: int, token: str) -> set[str]:
    """Return the set of comment IDs explicitly referenced by a fixup commit.

    A commit message containing `bot-ack: <id>` (or `addresses: <id>`) on the
    PR branch is treated as evidence that the finding was applied.
    """
    base = f"https://api.github.com/repos/{owner}/{repo}"
    commits = paginate(f"{base}/pulls/{pr}/commits", token)
    out: set[str] = set()
    pat = re.compile(r"bot-ack:\s*(\d+)|addresses:\s*(\d+)", re.IGNORECASE)
    for c in commits:
        msg = ((c.get("commit") or {}).get("message")) or ""
        for m in pat.finditer(msg):
            out.add(m.group(1) or m.group(2))
    return out


def evaluate(owner: str, repo: str, pr: int, token: str) -> GateOutcome:
    sources = fetch_comment_sources(owner, repo, pr, token)
    findings = findings_from(sources)
    body, head_sha = pr_body_and_head(owner, repo, pr, token)
    acked, nit_batch = ack_ids(body)
    commit_acks = fixup_addresses(owner, repo, pr, token)
    out = GateOutcome(
        findings=findings,
        head_sha=head_sha,
        bot_statuses=classify_review_coverage(bot_artifacts_from(sources), head_sha),
    )
    for f in findings:
        if f.severity == "informational":
            out.informational.append(f)
            continue
        if str(f.comment_id) in acked or str(f.comment_id) in commit_acks:
            out.must_acked.append(f)
        else:
            out.must_unresolved.append(f)
    # Informational findings can be cleared in one shot via nit-batch-skipped.
    if nit_batch:
        # Just informational; the marker is a documentation hint for humans.
        pass
    return out


def _render_review_coverage(outcome: GateOutcome) -> list[str]:
    """Render the per-bot coverage block that qualifies the counts above."""
    if not outcome.bot_statuses:
        return []
    head = f" for head `{outcome.head_sha[:8]}`" if outcome.head_sha else ""
    lines = [f"### Review coverage{head}", ""]
    for status in outcome.bot_statuses:
        mark = "reviewed" if status.clean else "**not counted**"
        lines.append(f"- `{status.login}`: {mark} - {status.detail}")
    lines.append("")
    unreviewed = outcome.unreviewed_bots
    if unreviewed:
        names = ", ".join(f"`{status.login}`" for status in unreviewed)
        lines.append(
            f"{len(unreviewed)} of {len(outcome.bot_statuses)} configured review bots "
            f"({names}) produced no review for this head commit, so the finding counts "
            "above are not a clean result: they are the counts from the bots that did "
            "run. Re-request a review, or record why the gap is acceptable."
        )
        lines.append("")
    return lines


def render_summary(outcome: GateOutcome) -> str:
    lines = [STICKY_HEADER, "## Review-bot acknowledgement summary", ""]
    total_must = len(outcome.must_unresolved) + len(outcome.must_acked)
    lines.append(
        f"- Must-address findings: **{total_must}** "
        f"({len(outcome.must_acked)} acknowledged, "
        f"{len(outcome.must_unresolved)} open)"
    )
    lines.append(f"- Informational findings: {len(outcome.informational)}")
    lines.append("")
    lines.extend(_render_review_coverage(outcome))
    if outcome.must_unresolved:
        lines.append("### Open must-address findings")
        lines.append("")
        for f in outcome.must_unresolved:
            loc = f.path or "(general)"
            lines.append(f"- [{f.author}] `{loc}` (id `{f.comment_id}`): {f.short}")
        lines.append("")
        lines.append(
            "Each open finding must be either fixed in a fixup commit "
            "(`bot-ack: <id>` in the commit message) or acknowledged "
            "in the PR body with `<!-- bot-ack: <id> reason=... -->`."
        )
    else:
        lines.append("All must-address findings are resolved or acknowledged.")
    return "\n".join(lines).rstrip() + "\n"


def exit_code(outcome: GateOutcome, *, require_review: bool = False) -> int:
    """Map an outcome to the process exit status.

    An unreviewed bot fails only under ``--require-review``. `review-bot-ack`
    is a required context on `main`, so an upstream rate limit turning into a
    hard failure would wedge every open pull request; by default the gap is
    reported in the summary and the gate keeps failing only on open findings.
    """
    if outcome.must_unresolved:
        return 1
    if require_review and outcome.unreviewed_bots:
        return 1
    return 0


def upsert_sticky(owner: str, repo: str, pr: int, token: str, body: str) -> None:
    base = f"https://api.github.com/repos/{owner}/{repo}"
    comments = paginate(f"{base}/issues/{pr}/comments", token)
    existing_id = None
    for c in comments:
        if STICKY_HEADER in (c.get("body") or ""):
            existing_id = c.get("id")
            break
    if existing_id is not None:
        gh_request(
            "PATCH",
            f"{base}/issues/comments/{existing_id}",
            token,
            data={"body": body},
        )
        return
    gh_request(
        "POST",
        f"{base}/issues/{pr}/comments",
        token,
        data={"body": body},
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--owner", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--pr", required=True, type=int)
    p.add_argument(
        "--no-comment",
        action="store_true",
        help="Skip the sticky summary comment (for local runs).",
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help="Reserved; gate already fails on unresolved must-address.",
    )
    p.add_argument(
        "--require-review",
        action="store_true",
        help=(
            "Also fail when a configured review bot produced no review for the "
            "head commit, instead of only reporting the gap in the summary."
        ),
    )
    args = p.parse_args(argv)

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("error: GH_TOKEN or GITHUB_TOKEN must be set", file=sys.stderr)
        return 2

    try:
        outcome = evaluate(args.owner, args.repo, args.pr, token)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    summary = render_summary(outcome)
    print(summary)
    if not args.no_comment:
        try:
            upsert_sticky(args.owner, args.repo, args.pr, token, summary)
        except Exception as exc:
            print(f"warning: could not post sticky summary: {exc}", file=sys.stderr)

    for status in outcome.unreviewed_bots:
        print(f"warning: {status.login} {status.detail}", file=sys.stderr)

    return exit_code(outcome, require_review=args.require_review)


if __name__ == "__main__":
    sys.exit(main())
