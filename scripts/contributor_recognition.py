#!/usr/bin/env python3
"""Contributor recognition: collect the facts, render the drafts, publish on request.

Consumed by ``.github/workflows/contributor-recognition.yml`` and by the
maintainer by hand. Four stages, kept apart so each can be checked alone:

* ``collect`` reads the public GitHub REST API and writes ``state.json``: every
  outside contributor with a pull request merged in the last 90 days, and the
  lines each of them added in pull requests merged in the last 30 days, with
  generated files left out. It fails closed like ``project_pulse.py``: any API
  error, unexpected payload or unreadable file aborts with no output, so a
  partial cohort is never rendered as a complete one.
* ``render`` is a pure function of ``state.json`` and ``opt-ins.toml``. The
  same input gives byte-identical output. It writes the periodic GitHub
  comment (everyone who merged, alphabetical, facts only) and the LinkedIn
  draft (only people who opted in, with the pull requests they merged, and
  no prose about them: the maintainer writes the sentence per person).
* ``publish-issue`` / ``publish-comment`` post through ``gh api``, dry-run by
  default. The issue publisher creates the operational issue, posts one
  collapsible translation comment per language, then patches the language
  switcher under the title with the comment anchors it now knows.
* ``parse-reply`` reads an opt-in email body (``KEY=VALUE`` lines) and prints
  the registry entry to paste into ``community/recognition/opt-ins.toml``.

What this never does: rank people by volume in anything it publishes, invent
a sentence about anyone, decide who counts (the gate is a soft filter for one
LinkedIn flow and the exception path is a person), or post anywhere without
``--yes``.

Stdlib only. Sort order is ``str.casefold`` of the login everywhere a list of
people appears.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

REPO_DEFAULT = "sipyourdrink-ltd/bernstein"
MAINTAINER_LOGIN = "chernistry"
#: Accounts that open pull requests but are project automation or agent lanes
#: presenting as ``User``; never credited as contributors. Extend in
#: ``opt-ins.toml`` under ``[exclusions]`` rather than here.
AGENT_LOGINS = frozenset({"sujeito-operator", "claude", "qwencoder", "codex", "fleet", "blut-agent"})

EMAIL_ADDRESS = "forte@bernstein.run"
EMAIL_SUBJECT = "BRNSTN-PR-LNKD"
COHORT_DAYS = 90
GATE_DAYS = 30
DEFAULT_GATE_LINES = 1000
#: GitHub stops being a reliable notifier well before a comment reaches a
#: hundred @mentions, and a mass ping reads as a blast. Above this many, only
#: the gate and the opt-ins are pinged.
MENTION_CAP = 50
ISRAEL_TZ = "Asia/Jerusalem"
REPLY_BY_TIMEZONES = (
    ("Israel", "Asia/Jerusalem"),
    ("India", "Asia/Kolkata"),
    ("Central Europe", "Europe/Warsaw"),
    ("UTC", "UTC"),
)

#: Generated or vendored paths whose added lines say nothing about a person's
#: work. ``fnmatch`` patterns against the path as GitHub reports it.
GENERATED_PATHS: tuple[str, ...] = (
    "uv.lock",
    "*.lock",
    "package-lock.json",
    "*/package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "docs/i18n/README.*.md",
    "docs/sdd/module-map.md",
    "docs/api/*",
    "CHANGELOG.md",
    "docs/CHANGELOG.md",
    "*.svg",
    "*.min.js",
    "*.min.css",
    "*_pb2.py",
    "*_pb2_grpc.py",
    "*_pb2.pyi",
    "*/dist/*",
    "*/__snapshots__/*",
    "*.snap",
)

#: One row per translation comment, in publication order. ``summary`` is the
#: whole visible text of the collapsed comment; the flag and native name form
#: the switcher entry under the issue title.
LANGUAGES: tuple[tuple[str, str, str, str], ...] = (
    ("hi", "🇮🇳", "हिन्दी", "Hindi translation"),
    ("zh-Hans", "🇨🇳", "简体中文", "简体中文翻译"),
    ("vi", "🇻🇳", "Tiếng Việt", "Bản dịch tiếng Việt"),
    ("es", "🇪🇸", "Español", "Traducción al español"),
    ("pt", "🇵🇹", "Português", "Tradução em português"),
    ("ru", "🇷🇺", "Русский", "Русский перевод"),
    ("uk", "🇺🇦", "Українська", "Український переклад"),
    ("pl", "🇵🇱", "Polski", "Polskie tłumaczenie"),
)

SWITCHER_PLACEHOLDER = "{{LANGUAGE_SWITCHER}}"
REPLY_BY_PLACEHOLDER = "{{REPLY_BY}}"
PERIOD_MARKER = "<!-- tool:recognition-period:{end}:v1 -->"
TRANSLATION_MARKER = "<!-- tool:recognition-translation:{code}:v1 -->"

REPLY_KEYS = ("GITHUB", "OPT_IN", "RECOMMENDATION", "NOTE")
_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
_KV_RE = re.compile(r"^\s*([A-Za-z_]+)\s*=\s*(.*?)\s*$")
_QUOTED_RE = re.compile(r"^\s*>")


class RecognitionError(RuntimeError):
    """Collection or publication failed. Nothing is written; exit non-zero."""


# ---------------------------------------------------------------- time


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def reply_by(published: datetime, days: int = 14, hour: int = 20) -> dict[str, str]:
    """``days`` after publication at ``hour``:00 Israel time, in the four zones the post names."""
    local = published.astimezone(ZoneInfo(ISRAEL_TZ)) + timedelta(days=days)
    deadline = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    fmt = "%a %d %b %Y, %H:%M"
    return {label: deadline.astimezone(ZoneInfo(zone)).strftime(fmt) for label, zone in REPLY_BY_TIMEZONES}


def reply_by_block(times: dict[str, str]) -> str:
    lines = ["To be in the next post, reply by **" + times["Israel"] + " Israel time**"]
    others = [f"{label} {when}" for label, when in times.items() if label != "Israel"]
    lines[0] += " (" + " · ".join(others) + "). After that you are simply in the one after."
    return lines[0]


# ---------------------------------------------------------------- people


def author_class(user: dict[str, Any], extra_agents: frozenset[str] = frozenset()) -> str:
    """``outside`` | ``maintainer`` | ``automation``; mirrors project_pulse and adds the agent lanes."""
    login = str(user.get("login") or "")
    if user.get("type") == "Bot" or login.endswith("[bot]") or login in AGENT_LOGINS or login in extra_agents:
        return "automation"
    if login == MAINTAINER_LOGIN:
        return "maintainer"
    return "outside"


def is_generated(path: str, patterns: tuple[str, ...] = GENERATED_PATHS) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in patterns)


def sort_logins(logins: Any) -> list[str]:
    return sorted(set(logins), key=lambda s: (s.casefold(), s))


@dataclass(frozen=True)
class OptIn:
    login: str
    opt_in: bool
    recommendation: bool = False
    review_before_publish: bool = False
    exception: bool = False
    source: str = ""
    since: str = ""
    note: str = ""


@dataclass
class Registry:
    opt_ins: dict[str, OptIn] = field(default_factory=dict)  # keyed by casefolded login
    extra_agents: frozenset[str] = frozenset()

    def get(self, login: str) -> OptIn | None:
        return self.opt_ins.get(login.casefold())


def load_registry(path: Path) -> Registry:
    """Read ``opt-ins.toml``; a login listed twice is an error, not a merge."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RecognitionError(f"cannot read {path}: {exc}") from exc
    reg = Registry(extra_agents=frozenset(str(x) for x in (data.get("exclusions") or {}).get("agent_logins") or ()))
    for row in data.get("contributor") or []:
        login = str(row.get("login") or "").strip()
        if not _LOGIN_RE.match(login):
            raise RecognitionError(f"{path}: invalid login {login!r}")
        key = login.casefold()
        if key in reg.opt_ins:
            raise RecognitionError(f"{path}: {login} is listed twice")
        reg.opt_ins[key] = OptIn(
            login=login,
            opt_in=bool(row.get("opt_in", False)),
            recommendation=bool(row.get("recommendation", False)),
            review_before_publish=bool(row.get("review_before_publish", False)),
            exception=bool(row.get("exception", False)),
            source=str(row.get("source") or ""),
            since=str(row.get("since") or ""),
            note=str(row.get("note") or ""),
        )
    return reg


# ---------------------------------------------------------------- collect


GRAPHQL_URL = "https://api.github.com/graphql"
#: Search returns at most 1,000 results per query; ten days of merges stays
#: well under that on this repository, and a slice that does not fails closed.
SLICE_DAYS = 10
SEARCH_CAP = 1000
FILES_PER_PR = 100
RATE_LIMIT_RETRIES = 3

SEARCH_QUERY = f"""
query($q: String!, $after: String) {{
  search(query: $q, type: ISSUE, first: 50, after: $after) {{
    issueCount
    pageInfo {{ hasNextPage endCursor }}
    nodes {{
      ... on PullRequest {{
        number title mergedAt baseRefName changedFiles additions
        author {{ login __typename }}
        files(first: {FILES_PER_PR}) {{ nodes {{ path additions }} }}
      }}
    }}
  }}
}}
"""


class GraphQLClient:
    """POST to the GitHub GraphQL API. Every failure raises RecognitionError; there is no partial result."""

    def __init__(self, token: str) -> None:
        self._token = token

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            request = urllib.request.Request(
                GRAPHQL_URL,
                data=data,
                method="POST",
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                    "User-Agent": "bernstein-contributor-recognition",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    raw = response.read()
                break
            except urllib.error.HTTPError as exc:
                retry_after = exc.headers.get("retry-after")
                if exc.code not in (403, 429) or retry_after is None or attempt == RATE_LIMIT_RETRIES:
                    raise RecognitionError(f"GitHub GraphQL {exc.code}") from exc
                time.sleep(min(float(retry_after), 120.0))
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise RecognitionError(f"GitHub GraphQL request failed: {exc}") from exc
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RecognitionError("GitHub GraphQL returned an undecodable body") from exc
        if not isinstance(body, dict):
            raise RecognitionError("GitHub GraphQL returned a non-object body")
        return body


def _search_merged(client: Any, repo: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Every pull request merged in [start, end], all pages; malformed or truncated -> RecognitionError."""
    q = f"repo:{repo} is:pr is:merged merged:{iso(start)}..{iso(end)}"
    out: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        body = client.graphql(SEARCH_QUERY, {"q": q, "after": after})
        if body.get("errors"):
            raise RecognitionError(f"GitHub GraphQL errors for {q}: {body['errors']}")
        search = (body.get("data") or {}).get("search") if isinstance(body.get("data"), dict) else None
        if not isinstance(search, dict) or not isinstance(search.get("nodes"), list):
            raise RecognitionError(f"unexpected search payload for {q}")
        if int(search.get("issueCount") or 0) > SEARCH_CAP:
            raise RecognitionError(f"{q} matches more than {SEARCH_CAP} pull requests; shorten SLICE_DAYS")
        out.extend(n for n in search["nodes"] if isinstance(n, dict) and n)
        info = search.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            return out
        after = info.get("endCursor")
        if not isinstance(after, str):
            raise RecognitionError(f"search page without a cursor for {q}")


def collect(
    client: Any,
    repo: str,
    now: datetime,
    registry: Registry,
    cache: dict[str, Any],
    on_slice: Any = None,
) -> dict[str, Any]:
    """Merged PRs of the 90-day cohort via GraphQL search in SLICE_DAYS windows.

    ``on_slice()`` runs after each window, so a caller can persist ``cache``
    as it grows; an interrupted run keeps what it already counted.
    """
    cohort_since = now - timedelta(days=COHORT_DAYS)
    gate_since = now - timedelta(days=GATE_DAYS)
    since_s, gate_s = iso(cohort_since), iso(gate_since)
    prs: dict[int, dict[str, Any]] = {}
    start = cohort_since
    while start < now:
        end = min(start + timedelta(days=SLICE_DAYS), now)
        for node in _search_merged(client, repo, start, end):
            try:
                prs[int(node["number"])] = node  # slice bounds are inclusive; the number dedups
            except (KeyError, TypeError, ValueError) as exc:
                raise RecognitionError(f"pull request without a number in {repo} search") from exc
        start = end
        if on_slice is not None:
            on_slice()
    people: dict[str, dict[str, Any]] = {}
    for number in sorted(prs):
        pr = prs[number]
        merged_at = pr.get("mergedAt")
        if not isinstance(merged_at, str) or merged_at < since_s or merged_at > iso(now):
            continue
        author = pr.get("author")
        if not isinstance(author, dict) or not author.get("login"):
            continue  # deleted account ("ghost"): nobody to credit
        user = {"login": str(author["login"]), "type": "Bot" if author.get("__typename") == "Bot" else "User"}
        if author_class(user, registry.extra_agents) != "outside":
            continue
        if pr.get("baseRefName") != "main":
            continue
        login = user["login"]
        entry = people.setdefault(
            login, {"login": login, "merged_prs_90d": [], "added_30d": 0, "added_30d_raw": 0, "prs_30d": 0}
        )
        entry["merged_prs_90d"].append({"number": number, "title": str(pr.get("title") or ""), "merged_at": merged_at})
        if merged_at >= gate_s:
            counted, raw = _pr_additions(pr, merged_at, cache)
            entry["added_30d"] += counted
            entry["added_30d_raw"] += raw
            entry["prs_30d"] += 1
    return {
        "schema": 1,
        "repo": repo,
        "generated_at": iso(now),
        "cohort_since": since_s,
        "gate_since": gate_s,
        "gate_days": GATE_DAYS,
        "cohort_days": COHORT_DAYS,
        "contributors": [people[k] for k in sort_logins(people)],
    }


def _pr_additions(pr: dict[str, Any], merged_at: str, cache: dict[str, Any]) -> tuple[int, int]:
    """Added lines of one merged PR, generated paths excluded; a merged PR's files never change, so cached.

    The search returns the first FILES_PER_PR files. Past that, the unlisted
    additions (``additions`` minus the listed ones) count as non-generated:
    over-counting a huge PR is safer for a soft gate than silently dropping it.
    """
    key = str(pr["number"])
    hit = cache.get(key)
    if isinstance(hit, dict) and hit.get("merged_at") == merged_at:
        return int(hit["counted"]), int(hit["raw"])
    files = (pr.get("files") or {}).get("nodes")
    if not isinstance(files, list):
        raise RecognitionError(f"pull request #{key} came back without its file list")
    counted = listed = 0
    for f in files:
        adds = int((f or {}).get("additions") or 0)
        listed += adds
        if not is_generated(str((f or {}).get("path") or "")):
            counted += adds
    raw = int(pr.get("additions") or listed)
    if int(pr.get("changedFiles") or 0) > len(files):
        counted += max(raw - listed, 0)
    cache[key] = {"merged_at": merged_at, "counted": counted, "raw": raw, "files": int(pr.get("changedFiles") or 0)}
    return counted, raw


# ---------------------------------------------------------------- render


def _pr_links(repo: str, prs: list[dict[str, Any]], limit: int = 6) -> str:
    shown = [f"[#{p['number']}](https://github.com/{repo}/pull/{p['number']})" for p in prs[:limit]]
    more = len(prs) - len(shown)
    return ", ".join(shown) + (f" and {more} more" if more > 0 else "")


def mention_set(above: list[dict[str, Any]], below: list[dict[str, Any]], registry: Registry) -> set[str]:
    """Who gets an @mention: above the gate, opted in, or merged in the gate window; capped at MENTION_CAP."""
    core = {c["login"] for c in above} | {
        c["login"] for c in below if (r := registry.get(c["login"])) and (r.opt_in or r.exception)
    }
    recent = {c["login"] for c in below if c.get("prs_30d", 0) > 0}
    return core | recent if len(core | recent) <= MENTION_CAP else core


def _name(login: str, pinged: set[str]) -> str:
    return f"@{login}" if login in pinged else f"[{login}](https://github.com/{login})"


def render_period_comment(state: dict[str, Any], registry: Registry, gate: int = DEFAULT_GATE_LINES) -> str:
    """The periodic GitHub comment: everyone who merged, alphabetical, facts only."""
    repo = state["repo"]
    end = state["generated_at"][:10]
    above, below = [], []
    for c in state["contributors"]:
        (above if c["added_30d"] >= gate else below).append(c)
    pinged = mention_set(above, below, registry)
    out = [PERIOD_MARKER.format(end=end), f"### Contributor recognition, period ending {end}", ""]
    out.append(
        f"Window: pull requests merged into `main` between {state['cohort_since'][:10]} and {end} "
        f"(90 days); the soft gate counts added lines in pull requests merged in the last {state['gate_days']} days "
        f"(since {state['gate_since'][:10]}), generated files excluded. Names are alphabetical. "
        f"Opt-in status comes from `community/recognition/opt-ins.toml`; to change yours, email "
        f"{EMAIL_ADDRESS} with the subject `{EMAIL_SUBJECT}`."
    )
    out.append("")
    out.append(f"**Above the soft gate (≥ {gate:,} added lines in {state['gate_days']} days):** {len(above)}")
    out.append("")
    if above:
        out.append("| Contributor | Opt-in | Merged, 30 d | Added lines, 30 d | Merged, 90 d |")
        out.append("|---|---|---|---|---|")
        for c in above:
            rec = registry.get(c["login"])
            status = "yes" if rec and rec.opt_in else ("no" if rec else "not yet")
            out.append(
                f"| {_name(c['login'], pinged)} | {status} | {c['prs_30d']} | {c['added_30d']:,} | "
                f"{len(c['merged_prs_90d'])}: {_pr_links(repo, c['merged_prs_90d'])} |"
            )
    else:
        out.append("_Nobody cleared the gate this period; the list below is the whole cohort._")
    out.append("")
    out.append(f"**Also merged in the last 90 days:** {len(below)}")
    out.append("")
    if below:
        out.append(", ".join(f"{_name(c['login'], pinged)} ({len(c['merged_prs_90d'])})" for c in below))
        out.append("")
    exceptions = [c["login"] for c in below if (r := registry.get(c["login"])) and r.exception]
    if exceptions:
        names = ", ".join(f"@{x}" for x in sort_logins(exceptions))
        out.append("Included in the next post by the exception path: " + names)
        out.append("")
    out.append(
        "Pinged: everyone above the gate, everyone who has opted in, and everyone with a merge in the last "
        f"{state['gate_days']} days. The rest are linked, not pinged, so an old one-off pull request does not "
        "earn a notification."
    )
    out.append("")
    out.append(
        "The gate is a filter for one LinkedIn flow, not a measure of anyone's work; reviews, security, docs and "
        f"release work often land at forty lines. Meaningful work below the line: email {EMAIL_ADDRESS}, subject "
        f"`{EMAIL_SUBJECT}`, and say so."
    )
    return "\n".join(out) + "\n"


def render_linkedin_draft(state: dict[str, Any], registry: Registry, gate: int = DEFAULT_GATE_LINES) -> str:
    """The LinkedIn draft: opted-in people who cleared the gate or hold an exception, with their facts."""
    repo = state["repo"]
    end = state["generated_at"][:10]
    rows, review = [], []
    for c in state["contributors"]:
        rec = registry.get(c["login"])
        if not (rec and rec.opt_in):
            continue
        if c["added_30d"] < gate and not rec.exception:
            continue
        rows.append((c, rec))
        if rec.review_before_publish:
            review.append(c["login"])
    out = [f"# LinkedIn draft, period ending {end}", ""]
    out.append(
        "DRAFT for the maintainer. Facts below come from GitHub; the sentence per person is yours to write "
        "from the pull requests listed, and nothing else is added by this script. Alphabetical order."
    )
    out.append("")
    if review:
        out.append("Send the final wording before publishing to: " + ", ".join(f"@{x}" for x in sort_logins(review)))
        out.append("")
    if not rows:
        out.append("_No opted-in contributor cleared the gate this period._")
        return "\n".join(out) + "\n"
    out.append("Thank you to the people who built Bernstein with us over the last month:")
    out.append("")
    for c, _rec in rows:
        titles = "; ".join(p["title"] for p in c["merged_prs_90d"][-4:])
        out.append(f"- @{c['login']} — <one sentence, from: {titles}> ({_pr_links(repo, c['merged_prs_90d'], 4)})")
    out.append("")
    out.append(
        "Everyone above opted in. If you contributed and want to be in the next one, the how is pinned in the "
        f"repository: https://github.com/{repo}/issues?q=is%3Aissue+label%3Apinned+recognition"
    )
    return "\n".join(out) + "\n"


def translation_comment(code: str, text: str) -> str:
    """One collapsible comment per language: marker, summary line, the translation inside ``<details>``."""
    row = next((r for r in LANGUAGES if r[0] == code), None)
    if row is None:
        raise RecognitionError(f"unknown language code {code!r}")
    _, flag, _native, summary = row
    body = text.strip()
    return (
        f"{TRANSLATION_MARKER.format(code=code)}\n"
        f"<details>\n<summary>{flag} {summary}</summary>\n\n"
        f"{body}\n\n"
        f"_Translation of the English text above; the English version is the one that counts if they differ._\n"
        f"</details>\n"
    )


def language_switcher(anchors: dict[str, str] | None) -> str:
    """The row under the title. With no anchors yet, every entry points at the issue itself (``#``)."""
    parts = []
    for code, flag, native, _ in LANGUAGES:
        href = (anchors or {}).get(code) or "#"
        parts.append(f"[{flag} {native}]({href})")
    return " · ".join(parts)


def apply_placeholders(body: str, switcher: str, reply_block: str) -> str:
    return body.replace(SWITCHER_PLACEHOLDER, switcher).replace(REPLY_BY_PLACEHOLDER, reply_block)


# ---------------------------------------------------------------- reply parsing


def parse_reply(text: str) -> dict[str, Any]:
    """Read ``KEY=VALUE`` lines from an opt-in mail. Returns ``{"ok": bool, "fields": …, "errors": […]}``.

    Keys are case-insensitive, the first occurrence wins, quoted lines (``> ``,
    the template echoed back in a reply) are skipped, unknown keys are ignored. Valid means a plausible GitHub login
    and ``OPT_IN`` of YES or NO.
    """
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if _QUOTED_RE.match(line):
            continue  # a reply on top of our own mail echoes the template; only the fresh lines count
        m = _KV_RE.match(line)
        if not m:
            continue
        key = m.group(1).upper()
        if key in REPLY_KEYS and key not in fields:
            fields[key] = m.group(2).strip().strip("`'\"")
    errors = []
    login = fields.get("GITHUB", "").lstrip("@")
    if not _LOGIN_RE.match(login):
        errors.append("GITHUB= must be a GitHub login (letters, digits, single hyphens)")
    opt = fields.get("OPT_IN", "").upper()
    if opt not in ("YES", "NO"):
        errors.append("OPT_IN= must be YES or NO")
    rec = fields.get("RECOMMENDATION", "NO").upper()
    if rec not in ("YES", "NO"):
        errors.append("RECOMMENDATION= must be YES or NO")
    if errors:
        return {"ok": False, "fields": fields, "errors": errors}
    return {
        "ok": True,
        "errors": [],
        "fields": {"GITHUB": login, "OPT_IN": opt, "RECOMMENDATION": rec, "NOTE": fields.get("NOTE", "")},
    }


def registry_entry(parsed: dict[str, Any], received: str, source: str = "email") -> str:
    """A ready-to-paste ``[[contributor]]`` block."""
    f = parsed["fields"]
    note = f["NOTE"].replace('"', "'")
    lines = [
        "[[contributor]]",
        f'login = "{f["GITHUB"]}"',
        f"opt_in = {'true' if f['OPT_IN'] == 'YES' else 'false'}",
        f"recommendation = {'true' if f['RECOMMENDATION'] == 'YES' else 'false'}",
        "review_before_publish = false",
        "exception = false",
        f'source = "{source} {EMAIL_SUBJECT} {received}"',
        f'since = "{received}"',
    ]
    if note:
        lines.append(f'note = "{note}"')
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- gh plumbing (publish)


def gh_json(args: list[str], payload: dict[str, Any] | None = None) -> Any:
    cmd = ["gh", "api", *args]
    proc = subprocess.run(
        cmd + (["--input", "-"] if payload is not None else []),
        input=json.dumps(payload) if payload is not None else None,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RecognitionError(f"gh api {' '.join(args[:3])} failed: {proc.stderr.strip()[:300]}")
    return json.loads(proc.stdout or "null")


def publish_issue(
    repo: str,
    title: str,
    body_template: str,
    translations: dict[str, str],
    published: datetime,
    labels: list[str],
    yes: bool,
    runner: Any = gh_json,
) -> dict[str, Any]:
    """Create the issue, post the translation comments, patch the switcher with real anchors."""
    times = reply_by(published)
    languages = [c for c, *_ in LANGUAGES if c in translations]
    plan: dict[str, Any] = {"title": title, "labels": labels, "languages": languages, "reply_by": times}
    if not yes:
        plan["body_preview"] = apply_placeholders(body_template, language_switcher(None), reply_by_block(times))[:600]
        return plan
    body0 = apply_placeholders(body_template, language_switcher(None), reply_by_block(times))
    issue = runner(["-X", "POST", f"repos/{repo}/issues"], {"title": title, "body": body0, "labels": labels})
    number = int(issue["number"])
    anchors: dict[str, str] = {}
    for code, *_ in LANGUAGES:
        if code not in translations:
            continue
        payload = {"body": translation_comment(code, translations[code])}
        comment = runner(["-X", "POST", f"repos/{repo}/issues/{number}/comments"], payload)
        anchors[code] = f"#issuecomment-{comment['id']}"
    body1 = apply_placeholders(body_template, language_switcher(anchors), reply_by_block(times))
    runner(["-X", "PATCH", f"repos/{repo}/issues/{number}"], {"body": body1})
    plan.update({"number": number, "anchors": anchors, "url": issue.get("html_url")})
    return plan


def publish_comment(repo: str, issue: int, body: str, yes: bool, runner: Any = gh_json) -> dict[str, Any]:
    """Post the periodic comment once: a comment carrying the same period marker is a no-op."""
    marker = body.splitlines()[0] if body.startswith("<!-- tool:recognition-period") else ""
    if marker:
        existing = runner([f"repos/{repo}/issues/{issue}/comments?per_page=100"])
        if any(marker in str(c.get("body") or "") for c in (existing or [])):
            return {"skipped": "a comment for this period already exists", "issue": issue}
    if not yes:
        return {"would_post": body[:400], "issue": issue}
    posted = runner(["-X", "POST", f"repos/{repo}/issues/{issue}/comments"], {"body": body})
    return {"posted": posted.get("html_url"), "issue": issue}


# ---------------------------------------------------------------- main


def _load_translations(directory: Path) -> dict[str, str]:
    out = {}
    for code, *_ in LANGUAGES:
        p = directory / f"{code}.md"
        if p.is_file():
            text = p.read_text(encoding="utf-8")
            if text.strip():
                out[code] = text
    return out


def _now(arg: str | None) -> datetime:
    return parse_ts(arg) if arg else datetime.now(UTC).replace(microsecond=0)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=REPO_DEFAULT)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect", help="query GitHub, write state.json (fails closed)")
    c.add_argument("--out", type=Path, required=True)
    c.add_argument("--opt-ins", type=Path, default=Path("community/recognition/opt-ins.toml"))
    c.add_argument("--cache", type=Path, default=None, help="per-PR additions cache, read and updated")
    c.add_argument("--now", default=None, help="ISO timestamp (tests, reproducible runs)")
    c.add_argument("--token", default=None, help="GitHub token (default: $GITHUB_TOKEN or $GH_TOKEN)")

    r = sub.add_parser("render", help="pure: state.json + opt-ins.toml -> periodic comment + LinkedIn draft")
    r.add_argument("--state", type=Path, required=True)
    r.add_argument("--opt-ins", type=Path, default=Path("community/recognition/opt-ins.toml"))
    r.add_argument("--gate", type=int, default=DEFAULT_GATE_LINES)
    r.add_argument("--out-comment", type=Path, required=True)
    r.add_argument("--out-linkedin", type=Path, required=True)

    p = sub.add_parser("parse-reply", help="read an opt-in mail body from stdin or a file; print the registry entry")
    p.add_argument("file", nargs="?", default="-")
    p.add_argument("--received", default=None, help="date the mail arrived, YYYY-MM-DD (default: today)")

    b = sub.add_parser("reply-by", help="print the reply deadline for a publication date")
    b.add_argument("--published", default=None)

    t = sub.add_parser("translations", help="write the translation comment bodies to a directory")
    t.add_argument("--source-dir", type=Path, default=Path("community/recognition/translations"))
    t.add_argument("--out-dir", type=Path, required=True)

    pi = sub.add_parser("publish-issue", help="create the issue with translation comments (dry-run without --yes)")
    pi.add_argument("--body", type=Path, default=Path("community/recognition/ISSUE.md"))
    pi.add_argument("--translations", type=Path, default=Path("community/recognition/translations"))
    pi.add_argument("--title", default="Contributor recognition: how the LinkedIn posts, opt-in and the soft gate work")
    pi.add_argument("--label", action="append", default=None)
    pi.add_argument("--published", default=None)
    pi.add_argument("--yes", action="store_true")

    pc = sub.add_parser("publish-comment", help="post a rendered periodic comment on the issue (dry-run without --yes)")
    pc.add_argument("--issue", type=int, required=True)
    pc.add_argument("--file", type=Path, required=True)
    pc.add_argument("--yes", action="store_true")

    args = ap.parse_args(argv)
    try:
        return _dispatch(args)
    except RecognitionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _dispatch(args: argparse.Namespace) -> int:
    if args.cmd == "collect":
        token = args.token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if not token:
            raise RecognitionError("a GitHub token is required (GITHUB_TOKEN or --token)")
        client = GraphQLClient(token)
        registry = load_registry(args.opt_ins)
        cache: dict[str, Any] = {}
        if args.cache and args.cache.is_file():
            try:
                cache = json.loads(args.cache.read_text(encoding="utf-8"))
            except ValueError:
                cache = {}

        def save_cache() -> None:
            if args.cache:
                args.cache.write_text(json.dumps(cache, indent=0, sort_keys=True) + "\n", encoding="utf-8")

        state = collect(client, args.repo, _now(args.now), registry, cache, on_slice=save_cache)
        args.out.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        save_cache()
        print(f"collected {len(state['contributors'])} contributors -> {args.out}")
        return 0
    if args.cmd == "render":
        state = json.loads(args.state.read_text(encoding="utf-8"))
        registry = load_registry(args.opt_ins)
        args.out_comment.write_text(render_period_comment(state, registry, args.gate), encoding="utf-8")
        args.out_linkedin.write_text(render_linkedin_draft(state, registry, args.gate), encoding="utf-8")
        print(f"rendered -> {args.out_comment}, {args.out_linkedin}")
        return 0
    if args.cmd == "parse-reply":
        text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
        parsed = parse_reply(text)
        received = args.received or datetime.now(UTC).strftime("%Y-%m-%d")
        print(json.dumps(parsed, ensure_ascii=False))
        if not parsed["ok"]:
            return 2
        print(registry_entry(parsed, received))
        return 0
    if args.cmd == "reply-by":
        print(json.dumps(reply_by(_now(args.published)), indent=1))
        return 0
    if args.cmd == "translations":
        args.out_dir.mkdir(parents=True, exist_ok=True)
        found = _load_translations(args.source_dir)
        for code, text in found.items():
            (args.out_dir / f"{code}.md").write_text(translation_comment(code, text), encoding="utf-8")
        print(f"{len(found)} translation comment(s) -> {args.out_dir}")
        return 0
    if args.cmd == "publish-issue":
        translations = _load_translations(args.translations)
        result = publish_issue(
            args.repo,
            args.title,
            args.body.read_text(encoding="utf-8"),
            translations,
            _now(args.published),
            args.label or ["pinned", "docs"],
            args.yes,
        )
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0
    if args.cmd == "publish-comment":
        result = publish_comment(args.repo, args.issue, args.file.read_text(encoding="utf-8"), args.yes)
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0
    raise RecognitionError(f"unknown command {args.cmd}")


if __name__ == "__main__":
    sys.exit(main())
