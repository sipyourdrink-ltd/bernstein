#!/usr/bin/env python3
"""Publish a weekly, deterministic snapshot of this repository's public health.

Consumed by ``.github/workflows/project-pulse.yml``. Three stages, deliberately
split so the numbers, the history and the page are separable concerns:

* ``collect`` queries the public GitHub REST API plus two in-repo sources and
  writes ``pulse.json``. It fails closed: any HTTP error, unexpected payload,
  or unreadable local source aborts with a non-zero exit and leaves no output
  file behind, so a partial page can never be published as a complete one.
* ``history`` appends this week's row to ``history.json``, one row per
  collection date, so the page can show movement instead of a single number.
  A re-run on the same date replaces its own row rather than duplicating it.
* ``render`` is a pure function of that JSON. Identical input yields
  byte-identical output (sorted keys, fixed number formatting, ISO dates, and
  no clock reads other than the collected ``generated_at`` date), so the weekly
  idempotent upsert does not thrash the issue body. It produces the Markdown
  page and, with ``--svg-dir``, a light and a dark SVG card the page embeds.

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
# The weekly history, the SVG card and the charts on the page are projections
# of these same fields over time (one row per collection date); they add no
# field of their own.
#
# Explicitly out of scope, and not to be added: individual logins, e-mail
# addresses, per-person leaderboards, commit-hour or timezone histograms,
# review-comment attribution, anything sourced outside the public API and this
# repository's own tree.
# ---------------------------------------------------------------------------

from __future__ import annotations

import argparse
import json
import math
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
from xml.sax.saxutils import escape as _xml_escape

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

#: Branch that carries the rendered card, the page and the weekly history.
#: The workflow pushes there; nothing on it is ever merged anywhere.
DATA_BRANCH = "project-pulse"

#: Weeks of history kept. Two years is enough to see a trend and small enough
#: that the file never needs pagination.
HISTORY_KEEP_WEEKS = 104

#: Weeks shown in the card sparklines and in the trend charts on the page.
TREND_WEEKS = 8

#: Per-week fields copied into the history. A strict subset of the allow-list
#: above: the history introduces no field of its own.
HISTORY_FIELDS = (
    "commits_main_7d",
    "distinct_outside_authors",
    "grabbable",
    "issue_close_lag_hours_median",
    "issues_closed_count",
    "merged_prs_by_author_class",
    "pr_merge_lag_hours_median",
    "pr_merged_count",
    "pr_merged_within_24h_pct",
)


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
# History (one row per collection date)
# ---------------------------------------------------------------------------


def history_row(data: dict[str, Any]) -> dict[str, Any]:
    """Project one collection onto its history row (allow-listed fields only)."""
    row: dict[str, Any] = {"generated_at": str(data["generated_at"])}
    for key in HISTORY_FIELDS:
        row[key] = data[key]
    return row


def load_history(path: Path) -> dict[str, Any]:
    """Read *path*, or an empty history when it does not exist yet.

    A file that exists but does not parse as a history is an error, not an
    empty history: silently restarting the series would erase the trend.
    """
    if not path.exists():
        return {"weeks": []}
    try:
        history = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PulseError(f"history {path} is unreadable: {exc}") from exc
    weeks = history.get("weeks") if isinstance(history, dict) else None
    if not isinstance(weeks, list) or not all(isinstance(w, dict) and "generated_at" in w for w in weeks):
        raise PulseError(f"history {path} is not a list of dated rows")
    return history


def append_history(history: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    """Return *history* with *data*'s row upserted, sorted by date, capped."""
    fresh = history_row(data)
    rows = [row for row in history.get("weeks", []) if row.get("generated_at") != fresh["generated_at"]]
    rows.append(fresh)
    rows.sort(key=lambda row: str(row["generated_at"]))
    return {"weeks": rows[-HISTORY_KEEP_WEEKS:]}


def _trend_rows(data: dict[str, Any], history: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The last :data:`TREND_WEEKS` rows, always ending with the current one."""
    return append_history(history or {"weeks": []}, data)["weeks"][-TREND_WEEKS:]


# ---------------------------------------------------------------------------
# Formatting (shared by the page and the card)
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


def _split_unit(text: str) -> tuple[str, str]:
    """``"3.4 h"`` -> ``("3.4", "h")``; ``"88.2%"`` -> ``("88.2", "%")``."""
    if text.endswith("%"):
        return text[:-1], "%"
    number, _, unit = text.partition(" ")
    return number, unit


def _nice_ceiling(value: float) -> int:
    """Smallest round axis maximum comfortably above *value* (deterministic)."""
    if value <= 0:
        return 10
    magnitude = 10 ** max(len(str(int(value))) - 1, 0)
    return int(math.ceil(value * 1.1 / magnitude) * magnitude)


def _image_base(data: dict[str, Any], image_base: str | None) -> str:
    base = image_base or f"https://raw.githubusercontent.com/{data['repo']}/{DATA_BRANCH}"
    return base.rstrip("/")


# ---------------------------------------------------------------------------
# Rendering: the page (pure Markdown)
# ---------------------------------------------------------------------------


def _mermaid_trend(rows: list[dict[str, Any]]) -> list[str]:
    """Two small charts over the weekly history; nothing when there is no trend."""
    if len(rows) < 2:
        return []
    labels = ", ".join(f'"{str(row["generated_at"])[5:]}"' for row in rows)
    merged = [int(row["pr_merged_count"]) for row in rows]
    lines = [
        "## Trend",
        "",
        f"The last {len(rows)} weekly collections. The full series is `history.json` on the",
        f"`{DATA_BRANCH}` branch.",
        "",
        "```mermaid",
        "xychart-beta",
        '    title "Merged pull requests per week"',
        f"    x-axis [{labels}]",
        f'    y-axis "Merged" 0 --> {_nice_ceiling(max(merged))}',
        f"    bar [{', '.join(str(v) for v in merged)}]",
        "```",
    ]
    lagged = [row for row in rows if row.get("pr_merge_lag_hours_median") is not None]
    if len(lagged) >= 2:
        lag_labels = ", ".join(f'"{str(row["generated_at"])[5:]}"' for row in lagged)
        lags = [float(row["pr_merge_lag_hours_median"]) for row in lagged]
        lines += [
            "",
            "```mermaid",
            "xychart-beta",
            '    title "Median merge lag, hours"',
            f"    x-axis [{lag_labels}]",
            f'    y-axis "Hours" 0 --> {_nice_ceiling(max(lags))}',
            f"    line [{', '.join(f'{v:.1f}' for v in lags)}]",
            "```",
        ]
    lines.append("")
    return lines


def render(data: dict[str, Any], history: dict[str, Any] | None = None, image_base: str | None = None) -> str:
    """Render *data* as Markdown. Pure: same input, byte-identical output.

    *history* adds the trend section and is optional; *image_base* is where
    the workflow publishes the card (defaults to the raw URL of the data
    branch). Both are inputs, not clock or network reads.
    """
    repo = str(data["repo"])
    generated = str(data["generated_at"])
    windows = data["windows"]
    classes = data["merged_prs_by_author_class"]
    grab = data["grabbable"]
    adapters = data["adapters"]
    release = data["latest_release"]
    l10n = data["readme_translations"]
    base = f"https://github.com/{repo}"
    images = _image_base(data, image_base)
    grabbable_query = f"{base}/issues?q=is%3Aissue+is%3Aopen+label%3Aup-for-grabs+no%3Aassignee"
    alt = (
        f"Project pulse for {repo}, {generated}: median merge lag {_hours(data['pr_merge_lag_hours_median'])}, "
        f"{_pct(data['pr_merged_within_24h_pct'])} merged within 24 hours, {data['pr_merged_count']} merged "
        f"pull requests in {windows['pr_days']} days, {grab['up_for_grabs_unassigned']} unassigned up-for-grabs issues."
    )

    lines: list[str] = [
        "# Project pulse",
        "",
        f"Median time from pull request opened to merged over the last {windows['pr_days']} days: "
        f"**{_hours(data['pr_merge_lag_hours_median'])}**.",
        "",
        "<picture>",
        f'  <source media="(prefers-color-scheme: dark)" srcset="{images}/pulse-dark.svg?v={generated}">',
        f'  <img alt="{_xml_escape(alt, {chr(34): "&quot;"})}" src="{images}/pulse.svg?v={generated}" width="880">',
        "</picture>",
        "",
        "> [!TIP]",
        f"> [Issues that are free to pick up]({grabbable_query}): "
        f"{grab['up_for_grabs_unassigned']} unassigned of {grab['up_for_grabs_open']} labelled up-for-grabs, "
        f"{grab['good_first_issue_unassigned']} unassigned good first issues.",
        "",
    ]
    lines += _mermaid_trend(_trend_rows(data, history))
    lines += [
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
        "```mermaid",
        "pie showData",
        f"    title Merged pull requests by author class, {windows['pr_days']} d",
        f'    "Outside contributors" : {classes[CLASS_OUTSIDE]}',
        f'    "Maintainer" : {classes[CLASS_MAINTAINER]}',
        f'    "Automation" : {classes[CLASS_AUTOMATION]}',
        "```",
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
        "<details>",
        "<summary>How this page is made</summary>",
        "",
        f"Generated {generated} from the public GitHub API and this repository's own tree.",
        "Aggregates only: no individual logins, no per-person ranking, no data that is not already public.",
        f"The card, the charts and the weekly history on the `{DATA_BRANCH}` branch are projections of the",
        "same ten fields. Regenerate with `scripts/project_pulse.py`; the field allow-list is documented at",
        f"the top of that file and in [docs/project-pulse.md]({base}/blob/main/docs/project-pulse.md).",
        "",
        "</details>",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Rendering: the card (pure SVG, one file per colour scheme)
# ---------------------------------------------------------------------------

CARD_WIDTH = 880
CARD_HEIGHT = 432

#: The same stacks GitHub's own interface uses, so the card reads as part of
#: the page it is embedded in. An SVG shown through ``<img>`` cannot load a
#: web font, and must not try.
_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans',Helvetica,Arial,sans-serif"
_MONO = "ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,'Liberation Mono',monospace"

THEMES: dict[str, dict[str, str]] = {
    "light": {
        "bg": "#ffffff",
        "border": "#d0d7de",
        "tile": "#f6f8fa",
        "fg": "#1f2328",
        "muted": "#656d76",
        "faint": "#afb8c1",
        "track": "#d8dee4",
        "green": "#1a7f37",
        "green_fill": "#2da44e",
        "purple": "#8250df",
        "blue": "#0969da",
        "orange": "#bc4c00",
    },
    "dark": {
        "bg": "#0d1117",
        "border": "#30363d",
        "tile": "#161b22",
        "fg": "#e6edf3",
        "muted": "#8b949e",
        "faint": "#484f58",
        "track": "#30363d",
        "green": "#3fb950",
        "green_fill": "#3fb950",
        "purple": "#a371f7",
        "blue": "#58a6ff",
        "orange": "#d29922",
    },
}


def _esc(value: Any) -> str:
    return _xml_escape(str(value), {'"': "&quot;"})


def _num(value: float) -> str:
    """Coordinate formatting: one decimal, no ``-0.0``, no float noise."""
    text = f"{value:.1f}"
    return "0.0" if text == "-0.0" else text


def _polyline(values: list[float], x: float, y: float, width: float, height: float) -> str:
    """Points of a sparkline through *values*, left to right, in a box."""
    if len(values) < 2:
        return ""
    low, high = min(values), max(values)
    span = (high - low) or 1.0
    step = width / (len(values) - 1)
    return " ".join(
        f"{_num(x + step * i)},{_num(y + height - height * (value - low) / span)}" for i, value in enumerate(values)
    )


def _delta(current: float | None, previous: float | None, *, lower_is_better: bool | None, fmt: Any) -> tuple[str, str]:
    """``(text, colour-key)`` for a week-over-week change, or ``("", "")``."""
    if current is None or previous is None:
        return "", ""
    diff = current - previous
    if abs(diff) < 0.05:
        return "no change vs last week", "muted"
    arrow = "▲" if diff > 0 else "▼"
    text = f"{arrow} {fmt(abs(diff))} vs last week"
    if lower_is_better is None:
        return text, "muted"
    improved = (diff < 0) == lower_is_better
    return text, "green" if improved else "orange"


def _card_style(c: dict[str, str], ring_offset: float) -> str:
    return (
        "<style>"
        f"text{{font-family:{_FONT};fill:{c['fg']}}}"
        ".mono{font-family:" + _MONO + "}"
        ".muted{fill:" + c["muted"] + "}"
        ".green{fill:" + c["green"] + "}"
        ".orange{fill:" + c["orange"] + "}"
        "@keyframes rise{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}"
        "@keyframes grow{from{transform:scaleX(0)}to{transform:scaleX(1)}}"
        "@keyframes draw{to{stroke-dashoffset:0}}"
        f"@keyframes ring{{to{{stroke-dashoffset:{_num(ring_offset)}}}}}"
        ".rise{animation:rise .6s cubic-bezier(.2,.7,.2,1) both}"
        ".d1{animation-delay:.05s}.d2{animation-delay:.12s}.d3{animation-delay:.19s}.d4{animation-delay:.26s}"
        ".d5{animation-delay:.33s}.d6{animation-delay:.4s}"
        ".grow{transform-box:fill-box;transform-origin:left center;"
        "animation:grow .9s cubic-bezier(.2,.7,.2,1) both .35s}"
        ".spark{fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;"
        "stroke-dasharray:600;stroke-dashoffset:600;animation:draw 1.4s ease-out forwards .45s}"
        ".pulse{fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;"
        "stroke-dasharray:120;stroke-dashoffset:120;animation:draw 1.2s ease-out forwards .1s}"
        ".ring{fill:none;stroke-linecap:round;animation:ring 1.3s cubic-bezier(.2,.7,.2,1) forwards .4s}"
        "@media (prefers-reduced-motion:reduce){*{animation:none!important;stroke-dashoffset:0!important}"
        f".ring{{stroke-dashoffset:{_num(ring_offset)}!important}}}}"
        "</style>"
    )


def _tile(
    c: dict[str, str],
    x: int,
    y: int,
    width: int,
    height: int,
    delay: str,
    label: str,
    value: str,
    unit: str,
    caption: str,
    delta: tuple[str, str],
    extra: str = "",
) -> str:
    value_width = 17.5 * len(value)
    text, colour = delta
    parts = [
        f'<g class="rise {delay}">',
        f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="8" fill="{c["tile"]}" stroke="{c["border"]}"/>',
        f'<text x="{x + 16}" y="{y + 24}" font-size="12" class="muted">{_esc(label)}</text>',
        f'<text x="{x + 16}" y="{y + 64}" font-size="30" font-weight="600" class="mono">{_esc(value)}</text>',
    ]
    if unit:
        parts.append(
            f'<text x="{_num(x + 20 + value_width)}" y="{y + 64}" font-size="14" class="muted">{_esc(unit)}</text>'
        )
    parts.append(f'<text x="{x + 16}" y="{y + 84}" font-size="11" class="muted">{_esc(caption)}</text>')
    if text:
        parts.append(f'<text x="{x + 16}" y="{y + 100}" font-size="11" class="{colour}">{_esc(text)}</text>')
    parts.append(extra)
    parts.append("</g>")
    return "".join(parts)


def _chip(c: dict[str, str], x: int, y: int, text: str, delay: str) -> tuple[str, int]:
    width = round(len(text) * 6.5 + 20)
    svg = (
        f'<g class="rise {delay}">'
        f'<rect x="{x}" y="{y}" width="{width}" height="28" rx="6" fill="{c["tile"]}" stroke="{c["border"]}"/>'
        f'<text x="{x + 10}" y="{y + 18}" font-size="11">{_esc(text)}</text>'
        "</g>"
    )
    return svg, width


def render_svg(data: dict[str, Any], history: dict[str, Any] | None = None, theme: str = "light") -> str:
    """Render the card as SVG. Pure: same input and theme, byte-identical output.

    The card is a picture of the same allow-listed fields the page carries,
    styled to sit inside GitHub's own interface in either colour scheme. It
    animates once on load with CSS only (no script, nothing loaded from
    anywhere), and holds still for readers who prefer reduced motion.
    """
    if theme not in THEMES:
        raise PulseError(f"unknown card theme {theme!r}; expected one of {', '.join(sorted(THEMES))}")
    c = THEMES[theme]
    repo = str(data["repo"])
    generated = str(data["generated_at"])
    windows = data["windows"]
    classes = data["merged_prs_by_author_class"]
    grab = data["grabbable"]
    adapters = data["adapters"]
    release = data["latest_release"]
    l10n = data["readme_translations"]
    rows = _trend_rows(data, history)
    previous = rows[-2] if len(rows) >= 2 else None

    lag = data["pr_merge_lag_hours_median"]
    within = data["pr_merged_within_24h_pct"]
    merged = int(data["pr_merged_count"])
    ring_radius = 26.0
    circumference = 2 * math.pi * ring_radius
    ring_offset = circumference * (1 - (within or 0.0) / 100.0)

    title = f"Project pulse for {repo}, {generated}"
    desc = (
        f"Median merge lag {_hours(lag)}; {_pct(within)} merged within 24 hours; {merged} merged pull requests "
        f"in {windows['pr_days']} days; {grab['up_for_grabs_unassigned']} unassigned up-for-grabs issues."
    )
    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{CARD_WIDTH}" height="{CARD_HEIGHT}" '
        f'viewBox="0 0 {CARD_WIDTH} {CARD_HEIGHT}" role="img" aria-labelledby="pulse-title pulse-desc">',
        f'<title id="pulse-title">{_esc(title)}</title>',
        f'<desc id="pulse-desc">{_esc(desc)}</desc>',
        _card_style(c, ring_offset),
        f'<rect x="0.5" y="0.5" width="{CARD_WIDTH - 1}" height="{CARD_HEIGHT - 1}" rx="12" '
        f'fill="{c["bg"]}" stroke="{c["border"]}"/>',
    ]

    # Header: a heartbeat trace that draws itself, the title, the repository.
    out.append(
        f'<polyline class="pulse" stroke="{c["green"]}" '
        'points="24,34 34,34 39,22 45,46 51,28 56,34 68,34"/>'
        f'<text x="80" y="39" font-size="16" font-weight="600">Project pulse</text>'
        f'<text x="{CARD_WIDTH - 24}" y="39" font-size="12" text-anchor="end" class="muted">'
        f"{_esc(repo)} · {_esc(generated)}</text>"
    )

    # Row 1: four tiles.
    tile_y, tile_h, tile_w, gap = 60, 140, 196, 16
    xs = [24 + i * (tile_w + gap) for i in range(4)]

    # Tile 1: median merge lag with a sparkline of the recent weeks.
    lag_series = [
        float(row["pr_merge_lag_hours_median"]) for row in rows if row.get("pr_merge_lag_hours_median") is not None
    ]
    lag_value, lag_unit = _split_unit(_hours(lag))
    spark = ""
    points = _polyline(lag_series, xs[0] + 16, tile_y + 110, 160, 16)
    if points:
        last_x, last_y = points.rsplit(" ", 1)[-1].split(",")
        spark = (
            f'<polyline class="spark" stroke="{c["green"]}" points="{points}"/>'
            f'<circle cx="{last_x}" cy="{last_y}" r="3" fill="{c["green"]}"/>'
        )
    out.append(
        _tile(
            c,
            xs[0],
            tile_y,
            tile_w,
            tile_h,
            "d1",
            "Median merge lag",
            lag_value,
            lag_unit,
            f"PR opened → merged, {windows['pr_days']} d",
            _delta(
                lag, previous.get("pr_merge_lag_hours_median") if previous else None, lower_is_better=True, fmt=_hours
            ),
            spark,
        )
    )

    # Tile 2: share merged within 24 hours, as a ring gauge.
    ring_cx, ring_cy = xs[1] + tile_w - 40, tile_y + 70
    ring = (
        f'<circle cx="{ring_cx}" cy="{ring_cy}" r="{_num(ring_radius)}" fill="none" '
        f'stroke="{c["track"]}" stroke-width="6"/>'
        f'<circle class="ring" cx="{ring_cx}" cy="{ring_cy}" r="{_num(ring_radius)}" '
        f'stroke="{c["green_fill"]}" stroke-width="6" '
        f'stroke-dasharray="{_num(circumference)}" stroke-dashoffset="{_num(circumference)}" '
        f'transform="rotate(-90 {ring_cx} {ring_cy})"/>'
    )
    within_value, within_unit = _split_unit(_pct(within))
    out.append(
        _tile(
            c,
            xs[1],
            tile_y,
            tile_w,
            tile_h,
            "d2",
            "Merged within 24 h",
            within_value,
            within_unit,
            f"of merged PRs, {windows['pr_days']} d",
            _delta(
                within,
                previous.get("pr_merged_within_24h_pct") if previous else None,
                lower_is_better=False,
                fmt=lambda v: f"{v:.1f} pt",
            ),
            ring,
        )
    )

    # Tile 3: merged pull requests with weekly bars.
    merged_series = [int(row["pr_merged_count"]) for row in rows]
    bars = ""
    if len(merged_series) >= 2:
        top = max(merged_series) or 1
        slot = 160 / len(merged_series)
        bar_w = max(int(slot) - 4, 4)
        pieces = []
        for i, count in enumerate(merged_series):
            h = max(round(16 * count / top, 1), 1.0)
            bx = xs[2] + 16 + slot * i
            fill = c["blue"] if i == len(merged_series) - 1 else c["track"]
            pieces.append(
                f'<rect x="{_num(bx)}" y="{_num(tile_y + 126 - h)}" width="{bar_w}" height="{_num(h)}" '
                f'rx="1.5" fill="{fill}"/>'
            )
        bars = "".join(pieces)
    out.append(
        _tile(
            c,
            xs[2],
            tile_y,
            tile_w,
            tile_h,
            "d3",
            "Merged pull requests",
            str(merged),
            "",
            f"last {windows['pr_days']} d · {data['issues_closed_count']} issues closed",
            _delta(
                float(merged),
                float(previous["pr_merged_count"]) if previous else None,
                lower_is_better=None,
                fmt=lambda v: f"{round(v)}",
            ),
            bars,
        )
    )

    # Tile 4: what a newcomer can take today.
    open_count = int(grab["up_for_grabs_open"])
    unassigned = int(grab["up_for_grabs_unassigned"])
    share = (unassigned / open_count) if open_count else 0.0
    grab_bar = (
        f'<rect x="{xs[3] + 16}" y="{tile_y + 114}" width="160" height="8" rx="4" fill="{c["track"]}"/>'
        f'<rect class="grow" x="{xs[3] + 16}" y="{tile_y + 114}" width="{_num(160 * share)}" height="8" '
        f'rx="4" fill="{c["purple"]}"/>'
    )
    out.append(
        _tile(
            c,
            xs[3],
            tile_y,
            tile_w,
            tile_h,
            "d4",
            "Free to pick up",
            str(unassigned),
            "",
            f"unassigned of {open_count} up-for-grabs",
            (f"{grab['good_first_issue_unassigned']} good first issues unassigned", "muted"),
            grab_bar,
        )
    )

    # Row 2: who merges what (stacked bar) and the issue side.
    row_y, row_h = 216, 104
    left_w, right_x = 520, 24 + 520 + 16
    out.append(
        f'<g class="rise d5">'
        f'<rect x="24" y="{row_y}" width="{left_w}" height="{row_h}" rx="8" fill="{c["tile"]}" stroke="{c["border"]}"/>'
        f'<text x="40" y="{row_y + 26}" font-size="12" class="muted">'
        f"Merged pull requests by author class · {windows['pr_days']} d</text>"
    )
    total = sum(int(classes[k]) for k in (CLASS_OUTSIDE, CLASS_MAINTAINER, CLASS_AUTOMATION))
    inner = left_w - 32
    segments = (
        (CLASS_OUTSIDE, "Outside", c["purple"]),
        (CLASS_MAINTAINER, "Maintainer", c["blue"]),
        (CLASS_AUTOMATION, "Automation", c["faint"]),
    )
    cursor = 40.0
    out.append(f'<rect x="40" y="{row_y + 42}" width="{inner}" height="14" rx="7" fill="{c["track"]}"/>')
    for key, _label, colour in segments:
        count = int(classes[key])
        width = inner * count / total if total else 0.0
        if width > 0:
            out.append(
                f'<rect class="grow" x="{_num(cursor)}" y="{row_y + 42}" width="{_num(width)}" height="14" '
                f'fill="{colour}"/>'
            )
        cursor += width
    out.append(
        f'<rect x="40" y="{row_y + 42}" width="{inner}" height="14" rx="7" fill="none" '
        f'stroke="{c["tile"]}" stroke-width="3"/>'
    )
    legend_x = 40
    for key, label, colour in segments:
        count = int(classes[key])
        pct = f"{100.0 * count / total:.1f}%" if total else "n/a"
        text = f"{label} {count} · {pct}"
        out.append(
            f'<circle cx="{legend_x + 5}" cy="{row_y + 79}" r="5" fill="{colour}"/>'
            f'<text x="{legend_x + 16}" y="{row_y + 83}" font-size="11">{_esc(text)}</text>'
        )
        legend_x += int(len(text) * 6.4) + 30
    out.append("</g>")

    close_value, close_unit = _split_unit(_hours(data["issue_close_lag_hours_median"]))
    out.append(
        f'<g class="rise d6">'
        f'<rect x="{right_x}" y="{row_y}" width="{CARD_WIDTH - 24 - right_x}" height="{row_h}" rx="8" '
        f'fill="{c["tile"]}" stroke="{c["border"]}"/>'
        f'<text x="{right_x + 16}" y="{row_y + 26}" font-size="12" class="muted">'
        f"Issues · {windows['issue_days']} d</text>"
        f'<text x="{right_x + 16}" y="{row_y + 62}" font-size="24" font-weight="600" class="mono">'
        f"{_esc(close_value)}</text>"
        f'<text x="{_num(right_x + 20 + 14 * len(close_value))}" y="{row_y + 62}" font-size="13" class="muted">'
        f"{_esc(close_unit)}</text>"
        f'<text x="{right_x + 16}" y="{row_y + 83}" font-size="11" class="muted">median opened → closed</text>'
        f'<text x="{right_x + 156}" y="{row_y + 62}" font-size="24" font-weight="600" class="mono">'
        f"{data['distinct_outside_authors']}</text>"
        f'<text x="{right_x + 156}" y="{row_y + 83}" font-size="11" class="muted">'
        f"outside authors, {windows['outside_author_days']} d</text>"
        "</g>"
    )

    # Row 3: project state chips.
    chip_y, chip_x = 336, 24
    chips = [
        f"{data['commits_main_7d']} commits · {windows['commit_days']} d",
        f"last commit {data['days_since_last_commit']} d ago",
        f"{adapters['registered']} adapters · {adapters['selectable']} selectable",
        f"{release['tag']} · {release['date']}",
        f"READMEs {l10n['in_sync']}/{l10n['total']} in sync",
    ]
    for i, text in enumerate(chips):
        svg, width = _chip(c, chip_x, chip_y, text, f"d{min(i + 2, 6)}")
        out.append(svg)
        chip_x += width + 8

    out.append(
        f'<text x="24" y="{CARD_HEIGHT - 20}" font-size="11" class="muted">'
        "Counts only — no logins, no per-person ranking. Source: scripts/project_pulse.py"
        "</text>"
    )
    out.append("</svg>\n")
    return "".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _token() -> str:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not token:
        raise PulseError("GH_TOKEN or GITHUB_TOKEN must be set")
    return token


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PulseError(f"{path} is unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise PulseError(f"{path} is not a JSON object")
    return data


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

    history_parser = sub.add_parser("history", help="upsert this collection into history.json")
    history_parser.add_argument("input", type=Path, help="path to a pulse.json produced by `collect`")
    history_parser.add_argument("--history", required=True, type=Path, help="history.json to create or update")

    render_parser = sub.add_parser("render", help="render a collected pulse.json to Markdown on stdout")
    render_parser.add_argument("input", type=Path, help="path to a pulse.json produced by `collect`")
    render_parser.add_argument("--history", type=Path, help="history.json for the trend section and sparklines")
    render_parser.add_argument("--image-base", help="URL prefix the page loads the card from")
    render_parser.add_argument("--svg-dir", type=Path, help="also write pulse.svg and pulse-dark.svg here")

    svg_parser = sub.add_parser("render-svg", help="render the card to SVG on stdout")
    svg_parser.add_argument("input", type=Path, help="path to a pulse.json produced by `collect`")
    svg_parser.add_argument("--history", type=Path, help="history.json for the sparklines")
    svg_parser.add_argument("--theme", choices=sorted(THEMES), default="light")

    args = parser.parse_args(argv)
    try:
        if args.stage == "collect":
            data = collect(GitHubClient(_token()), args.repo, args.repo_root.resolve(), datetime.now(tz=UTC))
            # Written only after every field is in hand, so an aborted run
            # leaves no half-collected file for `render` to publish.
            args.out.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(f"collected {len(data)} fields for {args.repo} -> {args.out}", file=sys.stderr)
        elif args.stage == "history":
            history = append_history(load_history(args.history), _read_json(args.input))
            args.history.write_text(json.dumps(history, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(f"history now holds {len(history['weeks'])} week(s) -> {args.history}", file=sys.stderr)
        elif args.stage == "render":
            data = _read_json(args.input)
            history = load_history(args.history) if args.history else None
            if args.svg_dir:
                args.svg_dir.mkdir(parents=True, exist_ok=True)
                (args.svg_dir / "pulse.svg").write_text(render_svg(data, history, "light"), encoding="utf-8")
                (args.svg_dir / "pulse-dark.svg").write_text(render_svg(data, history, "dark"), encoding="utf-8")
            sys.stdout.write(render(data, history, args.image_base))
        else:
            data = _read_json(args.input)
            history = load_history(args.history) if args.history else None
            sys.stdout.write(render_svg(data, history, args.theme))
    except PulseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
