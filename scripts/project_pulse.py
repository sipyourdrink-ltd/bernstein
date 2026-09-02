#!/usr/bin/env python3
"""Publish a weekly, deterministic snapshot of this repository's public health.

Consumed by ``.github/workflows/project-pulse.yml``. Two stages, deliberately
split so the numbers and the page are separable concerns:

* ``collect`` queries the public GitHub REST API plus two in-repo sources and
  writes ``pulse.json``. It fails closed: any HTTP error, unexpected payload,
  or unreadable local source aborts with a non-zero exit and leaves no output
  file behind, so a partial page can never be published as a complete one.
* ``render`` is a pure function of that JSON. Identical input yields a
  byte-identical page (sorted keys, fixed number formatting, ISO dates, and no
  clock reads other than the collected ``generated_at`` date), so the weekly
  idempotent upsert does not thrash the issue body.

The page answers one question a prospective contributor has before opening a
pull request: will it be looked at? The headline is the median time from PR
opened to merged over the last 30 days, followed by a link to the issues that
are free to pick up.

Stdlib only for the HTTP path; the two in-repo metrics import the same
sources the README count guards already use, so the page cannot drift from
the code it describes.

"""

# ---------------------------------------------------------------------------
# PUBLISHED FIELD ALLOW-LIST
#
# Everything the rendered page may contain. Aggregates only. Adding a field
# here is a deliberate decision, not an implementation detail: anything not on
# this list must not be collected and must not be rendered.
#
#   1.  pr_merge_lag_hours_median      median PR opened -> merged, 30 days
#   2.  pr_merged_within_24h_pct       share of those merged inside 24 hours
#   3.  merged_prs_by_author_class     counts per class: outside / maintainer
#                                      / automation. Counts only, never names
#                                      beyond the two documented account
#                                      labels, never a per-person ranking.
#   4.  distinct_outside_authors_90d   cardinality only, no logins
#   5.  issue_close_lag_hours_median   median issue opened -> closed, 30 days
#   6.  grabbable                      open up-for-grabs / good first issue
#                                      counts and how many are unassigned
#   7.  commits_main_7d,               commit volume on the default branch
#       days_since_last_commit
#   8.  adapters                       registry size, read from the registry
#   9.  latest_release                 tag name and publication date
#  10.  readme_translations            translated READMEs in sync vs stale
#
# Explicitly out of scope, and not to be added: individual logins, e-mail
# addresses, per-person leaderboards, commit-hour or timezone histograms,
# review-comment attribution, anything sourced outside the public API and this
# repository's own tree.
# ---------------------------------------------------------------------------

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

API_ROOT = "https://api.github.com"
USER_AGENT = "bernstein-project-pulse"

PR_WINDOW_DAYS = 30
ISSUE_WINDOW_DAYS = 30
OUTSIDE_AUTHOR_WINDOW_DAYS = 90
COMMIT_WINDOW_DAYS = 7

#: Slice width for search queries. The Search API returns at most 1000 results
#: per query; on a busy week this repository merges enough pull requests that a
#: single 30-day query would silently truncate. Slicing the window into weeks
#: keeps every query far below the cap, and a slice that still exceeds it is a
#: hard error rather than a quietly short median.
SLICE_DAYS = 7
SEARCH_PAGE_SIZE = 100
SEARCH_RESULT_CAP = 1000

#: Spacing between Search API calls. The authenticated search limit is 30
#: requests per minute; a full collection issues roughly 25.
SEARCH_INTERVAL_SECONDS = 2.5

#: The maintainer account. Its merged pull requests are counted separately
#: from outside contributions so the outside number is not flattered by them.
MAINTAINER_LOGIN = "chernistry"

#: This repository's automation app. Any account GitHub reports as a Bot is
#: also counted here, so a second automation account cannot silently land in
#: the "outside contributor" bucket and inflate it.
AUTOMATION_LOGIN = "bernstein-orchestrator[bot]"

CLASS_OUTSIDE = "outside"
CLASS_MAINTAINER = "maintainer"
CLASS_AUTOMATION = "automation"


class PulseError(RuntimeError):
    """Collection failed. Nothing is written and the process exits non-zero."""


# ---------------------------------------------------------------------------
# HTTP layer (mocked wholesale in the unit tests)
# ---------------------------------------------------------------------------


class GitHubClient:
    """Minimal read-only GitHub API client over ``urllib``.

    Every failure mode -- transport error, non-2xx status, undecodable body --
    raises :class:`PulseError`. There is no ``|| true`` path: a page built from
    a failed query would report a healthy-looking zero.
    """

    def __init__(self, token: str, *, interval_seconds: float = SEARCH_INTERVAL_SECONDS) -> None:
        self._token = token
        self._interval = interval_seconds
        self._last_call = 0.0

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if self._last_call and elapsed < self._interval:
            time.sleep(self._interval - elapsed)
        self._last_call = time.monotonic()

    def get(self, path: str, params: dict[str, str] | None = None) -> tuple[Any, dict[str, str]]:
        """GET *path*, returning ``(decoded_body, response_headers)``."""
        self._throttle()
        url = f"{API_ROOT}/{path.lstrip('/')}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
                headers = {k.lower(): v for k, v in response.headers.items()}
        except urllib.error.HTTPError as exc:
            raise PulseError(f"GitHub API {exc.code} for {url}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PulseError(f"GitHub API request failed for {url}: {exc}") from exc
        try:
            return json.loads(raw.decode("utf-8")), headers
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PulseError(f"GitHub API returned an undecodable body for {url}") from exc


def _search(client: GitHubClient, query: str, *, page: int = 1) -> dict[str, Any]:
    body, _ = client.get(
        "search/issues",
        {"q": query, "per_page": str(SEARCH_PAGE_SIZE), "page": str(page), "advanced_search": "true"},
    )
    if not isinstance(body, dict) or "total_count" not in body or "items" not in body:
        raise PulseError(f"unexpected search payload for query: {query}")
    return body


def _search_total(client: GitHubClient, query: str) -> int:
    return int(_search(client, query)["total_count"])


def _search_items(client: GitHubClient, query: str) -> list[dict[str, Any]]:
    """Return every item matching *query*, refusing to truncate silently."""
    first = _search(client, query)
    total = int(first["total_count"])
    if total > SEARCH_RESULT_CAP:
        raise PulseError(f"query exceeds the {SEARCH_RESULT_CAP}-result API cap ({total}): {query}")
    items: list[dict[str, Any]] = list(first["items"])
    page = 2
    while len(items) < total:
        batch = _search(client, query, page=page)["items"]
        if not batch:
            raise PulseError(f"search pagination stalled at {len(items)}/{total} for query: {query}")
        items.extend(batch)
        page += 1
    return items[:total]


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _parse_ts(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _slices(now: datetime, days: int) -> list[tuple[str, str]]:
    """Split ``[now - days, now]`` into inclusive ``YYYY-MM-DD`` date ranges.

    Deterministic given *now*: the slice boundaries are derived from the date
    only, so a collection run at any hour of the same day produces the same
    queries.
    """
    end = now.date()
    start = end - timedelta(days=days)
    out: list[tuple[str, str]] = []
    cursor = start
    while cursor <= end:
        stop = min(cursor + timedelta(days=SLICE_DAYS - 1), end)
        out.append((cursor.isoformat(), stop.isoformat()))
        cursor = stop + timedelta(days=1)
    return out


def _median_hours(deltas: list[float]) -> float | None:
    return round(statistics.median(deltas), 1) if deltas else None


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def _author_class(user: dict[str, Any]) -> str:
    login = str(user.get("login") or "")
    if user.get("type") == "Bot" or login.endswith("[bot]") or login == AUTOMATION_LOGIN:
        return CLASS_AUTOMATION
    if login == MAINTAINER_LOGIN:
        return CLASS_MAINTAINER
    return CLASS_OUTSIDE


def _collect_pull_requests(client: GitHubClient, repo: str, now: datetime) -> dict[str, Any]:
    """Merge lag, 24-hour share, and author-class counts over the PR window."""
    lags: list[float] = []
    within_24h = 0
    by_class = {CLASS_AUTOMATION: 0, CLASS_MAINTAINER: 0, CLASS_OUTSIDE: 0}
    for start, stop in _slices(now, PR_WINDOW_DAYS):
        query = f"repo:{repo} is:pr is:merged merged:{start}..{stop}"
        for item in _search_items(client, query):
            pull = item.get("pull_request") or {}
            merged_at = pull.get("merged_at")
            created_at = item.get("created_at")
            if not merged_at or not created_at:
                raise PulseError(f"merged pull request without timestamps in slice {start}..{stop}")
            hours = (_parse_ts(merged_at) - _parse_ts(created_at)).total_seconds() / 3600.0
            lags.append(hours)
            if hours <= 24.0:
                within_24h += 1
            by_class[_author_class(item.get("user") or {})] += 1
    merged = len(lags)
    return {
        "merged_prs_by_author_class": by_class,
        "pr_merge_lag_hours_median": _median_hours(lags),
        "pr_merged_count": merged,
        "pr_merged_within_24h_pct": round(100.0 * within_24h / merged, 1) if merged else None,
    }


def _collect_outside_authors(client: GitHubClient, repo: str, now: datetime) -> int:
    """Cardinality of distinct non-bot, non-maintainer merged-PR authors."""
    logins: set[str] = set()
    for start, stop in _slices(now, OUTSIDE_AUTHOR_WINDOW_DAYS):
        query = f"repo:{repo} is:pr is:merged merged:{start}..{stop}"
        for item in _search_items(client, query):
            user = item.get("user") or {}
            if _author_class(user) == CLASS_OUTSIDE:
                logins.add(str(user.get("login") or ""))
    return len(logins)


def _collect_issues(client: GitHubClient, repo: str, now: datetime) -> dict[str, Any]:
    lags: list[float] = []
    for start, stop in _slices(now, ISSUE_WINDOW_DAYS):
        query = f"repo:{repo} is:issue is:closed closed:{start}..{stop}"
        for item in _search_items(client, query):
            closed_at, created_at = item.get("closed_at"), item.get("created_at")
            if not closed_at or not created_at:
                raise PulseError(f"closed issue without timestamps in slice {start}..{stop}")
            lags.append((_parse_ts(closed_at) - _parse_ts(created_at)).total_seconds() / 3600.0)
    return {
        "issue_close_lag_hours_median": _median_hours(lags),
        "issues_closed_count": len(lags),
    }


def _collect_grabbable(client: GitHubClient, repo: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for key, label in (("up_for_grabs", "up-for-grabs"), ("good_first_issue", "good first issue")):
        base = f'repo:{repo} is:issue is:open label:"{label}"'
        out[f"{key}_open"] = _search_total(client, base)
        out[f"{key}_unassigned"] = _search_total(client, f"{base} no:assignee")
    return out


def _last_page_count(client: GitHubClient, path: str, params: dict[str, str]) -> int:
    """Exact item count via the ``rel="last"`` link on a ``per_page=1`` query.

    One request instead of paging thousands of commits. When no ``last`` link
    is present the result set fits on the single requested page.
    """
    body, headers = client.get(path, {**params, "per_page": "1"})
    if not isinstance(body, list):
        raise PulseError(f"expected a list payload from {path}")
    link = headers.get("link", "")
    for part in link.split(","):
        if 'rel="last"' in part:
            url = part.split(";")[0].strip().strip("<>")
            last = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("page", ["1"])[0]
            return int(last)
    return len(body)


def _collect_commits(client: GitHubClient, repo: str, now: datetime) -> dict[str, Any]:
    since = (now - timedelta(days=COMMIT_WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    count = _last_page_count(client, f"repos/{repo}/commits", {"sha": "main", "since": since})
    head, _ = client.get(f"repos/{repo}/commits", {"sha": "main", "per_page": "1"})
    if not isinstance(head, list) or not head:
        raise PulseError("default branch returned no commits")
    committed = head[0].get("commit", {}).get("committer", {}).get("date")
    if not committed:
        raise PulseError("head commit carries no committer date")
    return {
        "commits_main_7d": count,
        "days_since_last_commit": max((now - _parse_ts(committed)).days, 0),
    }


def _collect_release(client: GitHubClient, repo: str) -> dict[str, str]:
    body, _ = client.get(f"repos/{repo}/releases/latest")
    if not isinstance(body, dict) or not body.get("tag_name") or not body.get("published_at"):
        raise PulseError("latest release payload is missing a tag or publication date")
    return {"date": str(body["published_at"])[:10], "tag": str(body["tag_name"])}


def _collect_adapters(repo_root: Path) -> dict[str, int]:
    """Adapter counts from the registry, via the sources the README guards use.

    ``_enumerate_rows`` backs ``bernstein integrations list``, and
    ``selectable_adapter_names`` backs every ``--cli`` choice. Reading them
    here means the page cannot claim a number the code does not back.
    """
    src = str((repo_root / "src").resolve())
    if src not in sys.path:
        sys.path.insert(0, src)
    try:
        from bernstein.adapters.registry import selectable_adapter_names
        from bernstein.cli.commands.integrations_cmd import _enumerate_rows
    except ImportError as exc:
        raise PulseError(f"adapter registry is not importable from {src}: {exc}") from exc
    return {"registered": len(_enumerate_rows()), "selectable": len(selectable_adapter_names())}


def _configured_languages(repo_root: Path) -> list[str]:
    data = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    langs = data.get("tool", {}).get("bernstein", {}).get("readme-l10n", {}).get("languages", [])
    if not isinstance(langs, list):
        raise PulseError("[tool.bernstein.readme-l10n] languages is malformed")
    return [str(lang) for lang in langs]


def _collect_translations(repo_root: Path) -> dict[str, int]:
    """Translated-README freshness, read off ``readme-l10n verify``.

    That command is offline (it compares committed section hashes), so it runs
    on a CI runner without network access to anything but the checkout. Exit 0
    means every translation is in sync, exit 1 means at least one drifted; any
    other exit is a broken tool, not a finding, and fails the collection.
    """
    total = len(_configured_languages(repo_root))
    executable = shutil.which("bernstein")
    command = [executable] if executable else [sys.executable, "-m", "bernstein"]
    command += ["readme-l10n", "verify", "--workdir", str(repo_root)]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=600, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise PulseError(f"readme-l10n verify could not be run: {exc}") from exc
    if proc.returncode not in (0, 1):
        raise PulseError(f"readme-l10n verify exited {proc.returncode}: {proc.stderr.strip()[:300]}")
    in_sync = sum(1 for line in proc.stdout.splitlines() if line.startswith("OK       docs/i18n/README."))
    if proc.returncode == 0:
        in_sync = total
    return {"in_sync": in_sync, "stale": max(total - in_sync, 0), "total": total}


def collect(client: GitHubClient, repo: str, repo_root: Path, now: datetime) -> dict[str, Any]:
    """Gather every allow-listed field, or raise :class:`PulseError`."""
    data: dict[str, Any] = {
        "adapters": _collect_adapters(repo_root),
        "generated_at": now.date().isoformat(),
        "grabbable": _collect_grabbable(client, repo),
        "latest_release": _collect_release(client, repo),
        "readme_translations": _collect_translations(repo_root),
        "repo": repo,
        "windows": {
            "commit_days": COMMIT_WINDOW_DAYS,
            "issue_days": ISSUE_WINDOW_DAYS,
            "outside_author_days": OUTSIDE_AUTHOR_WINDOW_DAYS,
            "pr_days": PR_WINDOW_DAYS,
        },
    }
    data.update(_collect_pull_requests(client, repo, now))
    data.update(_collect_issues(client, repo, now))
    data.update(_collect_commits(client, repo, now))
    data["distinct_outside_authors"] = _collect_outside_authors(client, repo, now)
    return data


# ---------------------------------------------------------------------------
# Rendering (pure)
# ---------------------------------------------------------------------------


def _hours(value: float | None) -> str:
    """Format an hour count as a stable, human-readable duration."""
    if value is None:
        return "n/a"
    if value < 1.0:
        return f"{round(value * 60):d} min"
    if value < 48.0:
        return f"{value:.1f} h"
    return f"{value / 24.0:.1f} d"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}%"


def render(data: dict[str, Any]) -> str:
    """Render *data* as Markdown. Pure: same input, byte-identical output."""
    repo = str(data["repo"])
    generated = str(data["generated_at"])
    windows = data["windows"]
    classes = data["merged_prs_by_author_class"]
    grab = data["grabbable"]
    adapters = data["adapters"]
    release = data["latest_release"]
    l10n = data["readme_translations"]
    base = f"https://github.com/{repo}"
    grabbable_query = f"{base}/issues?q=is%3Aissue+is%3Aopen+label%3Aup-for-grabs+no%3Aassignee"

    lines: list[str] = [
        "# Project pulse",
        "",
        f"Median time from pull request opened to merged over the last {windows['pr_days']} days: "
        f"**{_hours(data['pr_merge_lag_hours_median'])}**.",
        "",
        f"[Issues that are free to pick up]({grabbable_query}) "
        f"({grab['up_for_grabs_unassigned']} unassigned of {grab['up_for_grabs_open']} labelled up-for-grabs).",
        "",
        "## Review and merge",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Median PR merge lag ({windows['pr_days']} d) | {_hours(data['pr_merge_lag_hours_median'])} |",
        f"| Merged within 24 h ({windows['pr_days']} d) | {_pct(data['pr_merged_within_24h_pct'])} |",
        f"| Merged PRs ({windows['pr_days']} d) | {data['pr_merged_count']} |",
        f"| Median issue open to close ({windows['issue_days']} d) | {_hours(data['issue_close_lag_hours_median'])} |",
        f"| Issues closed ({windows['issue_days']} d) | {data['issues_closed_count']} |",
        "",
        "## Who merges what",
        "",
        "Counts only, by account class. Outside contributions are everything that is neither the",
        "maintainer account nor an automation account, so the outside number is never flattered.",
        "",
        "| Author class | Merged PRs |",
        "| --- | --- |",
        f"| Outside contributors | {classes[CLASS_OUTSIDE]} |",
        f"| Maintainer | {classes[CLASS_MAINTAINER]} |",
        f"| Automation | {classes[CLASS_AUTOMATION]} |",
        "",
        f"Distinct outside authors with a merged PR in the last {windows['outside_author_days']} days: "
        f"**{data['distinct_outside_authors']}**.",
        "",
        "## Work you can pick up",
        "",
        "| Label | Open | Unassigned |",
        "| --- | --- | --- |",
        f"| up-for-grabs | {grab['up_for_grabs_open']} | {grab['up_for_grabs_unassigned']} |",
        f"| good first issue | {grab['good_first_issue_open']} | {grab['good_first_issue_unassigned']} |",
        "",
        "## Project state",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Commits to main ({windows['commit_days']} d) | {data['commits_main_7d']} |",
        f"| Days since last commit | {data['days_since_last_commit']} |",
        f"| Adapters in the registry | {adapters['registered']} wired in, {adapters['selectable']} selectable |",
        f"| Latest release | {release['tag']} ({release['date']}) |",
        f"| Translated READMEs | {l10n['in_sync']} in sync, {l10n['stale']} stale, of {l10n['total']} |",
        "",
        "---",
        "",
        f"Generated {generated} from the public GitHub API and this repository's own tree.",
        "Aggregates only: no individual logins, no per-person ranking, no data that is not already public.",
        "Regenerate with `scripts/project_pulse.py`; the field allow-list is documented at the top of that",
        f"file and in [docs/project-pulse.md]({base}/blob/main/docs/project-pulse.md).",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _token() -> str:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not token:
        raise PulseError("GH_TOKEN or GITHUB_TOKEN must be set")
    return token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="stage", required=True)

    collect_parser = sub.add_parser("collect", help="query the public API and write pulse.json")
    collect_parser.add_argument("--repo", required=True, help="OWNER/NAME")
    collect_parser.add_argument("--out", required=True, type=Path, help="path to write pulse.json")
    collect_parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository checkout used for the adapter and translation counts",
    )

    render_parser = sub.add_parser("render", help="render a collected pulse.json to Markdown on stdout")
    render_parser.add_argument("input", type=Path, help="path to a pulse.json produced by `collect`")

    args = parser.parse_args(argv)
    try:
        if args.stage == "collect":
            data = collect(GitHubClient(_token()), args.repo, args.repo_root.resolve(), datetime.now(tz=UTC))
            # Written only after every field is in hand, so an aborted run
            # leaves no half-collected file for `render` to publish.
            args.out.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(f"collected {len(data)} fields for {args.repo} -> {args.out}", file=sys.stderr)
        else:
            sys.stdout.write(render(json.loads(args.input.read_text(encoding="utf-8"))))
    except PulseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
