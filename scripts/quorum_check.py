#!/usr/bin/env python3
"""Decide whether a pull request has the review the charter asks for.

`docs/governance/review-charter.md` section 3 says an automated `quorum`
check may enforce the review rules later; this is that check. It reports a
pass or a failure with a table naming what is missing and who can supply it.

Why a script and not the branch rule
------------------------------------
GitHub's "required approvals" rule applies the same number to everybody, and
its bypass list does not apply when a pull request enters the merge queue. So
the rule either blocks the maintainer's own infrastructure changes and every
change the project's automation makes, or it is switched off for everyone.
The charter does not say that: it exempts automation (section 1), it lets the
maintainer merge while committers keep their veto (section 6), and it asks
for MORE than two approvals on large or sensitive changes (section 3). All of
that fits in a check; none of it fits in the rule. The branch rule keeps the
parts GitHub does well - the queue, the required status checks, no force
pushes - and this check owns the review question.

What it reads, and from where
-----------------------------
The roster and CODEOWNERS come from the pull request's BASE branch, never
from the branch under review, so a pull request cannot promote its own author
or make itself unowned. The workflow checks out the base commit for the same
reason. Everything else (reviews, commits, files) comes from the API, which
reports the pull request as GitHub sees it.

The rules, in the order they are applied
----------------------------------------
0. A draft is neutral: the queue does not take drafts, so there is nothing to
   decide yet.
1. A change to a governance path is held open for 72 hours after the last
   push that changed its net diff, so committers can object (section 10).
   This applies to every author, the maintainer included.
2. Automation merges its own changes on green CI (section 1), except on the
   paths where a mistake is expensive - anything with `sandbox`, `security`,
   `audit` or `auth` in it, plus `.github/`, `schemas/` and `proto/` - where
   it needs the maintainer's approval.
3. A pull request opened by `github-actions[bot]` needs the maintainer's
   approval whatever it touches: anyone who can push a branch can open one.
4. The maintainer's own pull requests merge without approvals, and any
   standing `changes requested` from a committer blocks them (section 6).
   Until 2026-10-05 a pull request the maintainer has pushed to and then
   declared adopted in the thread is treated the same way (section 3), if it
   needs only the ordinary two approvals, touches no path a CODEOWNERS line
   of its own covers, and touches nothing under `.github/`, `schemas/` or
   `proto/`. Anywhere else the notice is ignored and rule 5 applies.
5. Everyone else needs the quorum: two approvals, at least one from a core
   reviewer, none of them from anyone who wrote or pushed the change. Over
   400 changed lines, or on a path containing `sandbox`, `security` or
   `audit`, a third approval is required and two of the three must be core.
   Over 1,000 changed lines the maintainer approves or the change is split.
   A protected path (any CODEOWNERS entry that is not `*`) needs an approval
   from one of its owners: a line naming several people is satisfied by any
   one of them, as GitHub's own code-owner review works, and never by the
   author or by anyone who pushed to the branch.

   A protected path (any CODEOWNERS entry that is not `*`) needs its owner.
   Until 2026-10-05 the maintainer's approval alone satisfies the ordinary
   two-approval quorum (the backlog window, charter section 3); the third
   approval on large or sensitive changes is unchanged.


Approvals count only when they were given on the current head commit: a push
after an approval means nobody has read what is about to merge - unless the
push left the pull request's net diff exactly as it was. The net diff of a
commit is what GitHub's three-dot compare of the base branch with that commit
shows, file by file; its fingerprint is a SHA-256 over each file's name,
previous name, status and patch, with the line numbers taken out of the hunk
headers so that a base branch moving underneath does not change it. An
approval on an earlier commit whose fingerprint equals the head's counts:
merging the base branch in, or rebasing onto it, does not void it.

The same fingerprint decides who counts as having pushed. Everyone who wrote
a commit or is named in a Co-authored-by trailer is a contributor, as is
everyone who committed one - except someone whose every commit on the branch
merged the base branch in without changing the fingerprint, or came out of a
force push that left the fingerprint as it was on a history they had not
contributed to. Keeping a branch current is not writing it. The objection
window in rule 1 runs from the most recent push that changed the fingerprint.

Whenever a fingerprint cannot be worked out - a compare GitHub truncates, a
file with no patch (binary or too large), an API error - it matches nothing,
and each rule falls back to what it did before: approvals bind to the head
commit, the committer stays a contributor, the window runs from the last push.

A `changes requested` survives a push, and stays counted until its author
withdraws it. A review whose state is `commented` does not replace an earlier
verdict, which is how GitHub itself treats it.

Exit codes: 0 pass or neutral, 1 fail. The failure table is written to the
job summary as well as stdout.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

if TYPE_CHECKING:
    from collections.abc import Callable, Collection

ROSTER_PATH = ".github/quorum-roster.toml"
CODEOWNERS_PATH = ".github/CODEOWNERS"

# Section 3: over this many changed lines a third approval is required, and
# over the second number the change is split or goes to the maintainer.
THIRD_APPROVAL_LINES = 400
MAINTAINER_OR_SPLIT_LINES = 1000

# Section 3, adopted pull requests: until this date a change the maintainer
# has pushed to merges as their own once they say so in the thread. The notice
# is matched literally; it must be given on the current head commit, so a
# later push needs a new one - even a push that leaves the net diff alone,
# which an approval survives. It is honoured only where the change would
# otherwise need the ordinary two approvals and no owner's: not on a large or
# sensitive change, not on a path a CODEOWNERS line of its own covers, and not
# under ADOPTION_EXCLUDED_PREFIXES.
ADOPTION_ENDS = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
ADOPTION_NOTICE = "Adopted by the maintainer: whole diff read, fixes pushed, CI green."

# Section 3: these words in a path make a change sensitive whatever its size.
SENSITIVE_WORDS = ("sandbox", "security", "audit")

# The words are matched against the whole path, which also catches the tests
# and the release notes that accompany the code carrying them: "auditor" in
# `tests/conformance/auditor/` reads as "audit", and nine pull requests that
# add nothing but conformance vectors waited on a third approval for it. A
# test cannot loosen the control it exercises and a release note cannot loosen
# anything, so the escalation reads the paths that carry the implementation.
# These two prefixes are exempted rather than `src/` being allowlisted: an
# allowlist would also stop escalating `.github/`, `schemas/` and `proto/`,
# which is a change nobody asked for.
SENSITIVE_EXEMPT_PREFIXES = ("tests/", "docs/")

# Section 1 as decided for automation: the paths where automation stops being
# allowed to merge its own work. Wider than SENSITIVE_WORDS because a change
# to the workflows, the schemas or the wire protocol is not something the
# project's automation should be able to land unseen.
AUTOMATION_STOP_WORDS = (*SENSITIVE_WORDS, "auth")
AUTOMATION_STOP_PREFIXES = (".github/", "schemas/", "proto/")

# The same three trees are closed to adoption: a workflow, a schema or the wire
# protocol changed by a contributor needs its approvals, not the notice.
ADOPTION_EXCLUDED_PREFIXES = AUTOMATION_STOP_PREFIXES

# The net diff. GitHub's compare lists at most this many files and says
# nothing when it stops, so a list this long is treated as cut short.
COMPARE_FILE_LIMIT = 300
# The pull request's commit list stops at this many; past it the history the
# contributor rule reads is incomplete, and nobody is excused by it.
PR_COMMIT_LIMIT = 250
# Compare calls one run may make. Only the head, commits carrying an approval,
# merge commits and their first parents, and force-push endpoints are ever
# compared; past the budget a fingerprint is uncomputable, which fails closed.
COMPARE_BUDGET = 40
# How far back the window's anchor is followed through diff-neutral updates.
ANCHOR_STEPS = 50
FORCE_PUSHES_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      timelineItems(itemTypes: [HEAD_REF_FORCE_PUSHED_EVENT], last: 100) {
        totalCount
        nodes { ... on HeadRefForcePushedEvent { createdAt beforeCommit { oid } afterCommit { oid } } }
      }
    }
  }
}
"""

# Section 10: an amendment stays open so committers can object. The charter
# anchors the window on the approval; for a change that needs no approval the
# equivalent anchor is the last push that changed the net diff, which is what
# this check uses. A push that only merged the base branch in or rebased onto
# it leaves the change under objection as it was, so it does not restart it.
GOVERNANCE_WINDOW = timedelta(hours=72)
GOVERNANCE_PATHS = (
    "docs/governance/review-charter.md",
    "GOVERNANCE.md",
    "MAINTAINERS.md",
    ".github/CODEOWNERS",
    ".github/quorum-roster.toml",
    "scripts/quorum_check.py",
    "scripts/queue_hygiene.py",
)

GITHUB_ACTIONS_BOT = "github-actions[bot]"

# Section 3, backlog window: until this moment the maintainer's approval on
# the current head satisfies the ordinary quorum (two approvals, one core) on
# its own. Over a hundred pull requests were waiting on a second approval with
# four core reviewers carrying 40-80 open review requests each; the window
# lets the maintainer drain that backlog without lowering the bar on large or
# sensitive changes, which keep the third approval. After the date this
# constant is dead code and the branch below goes with it.
BACKLOG_WINDOW_ENDS = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def gh_json(*args: str) -> Any:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


@dataclass(frozen=True)
class Roster:
    maintainer: str
    core_reviewers: frozenset[str]
    committers: frozenset[str]
    automation: frozenset[str]

    @property
    def quorum_holders(self) -> frozenset[str]:
        """Everyone whose approval counts toward a human quorum."""
        return self.core_reviewers | self.committers | {self.maintainer}

    @property
    def core(self) -> frozenset[str]:
        """Everyone who can supply the core reviewer's approval."""
        return self.core_reviewers | {self.maintainer}


def load_roster(root: str = ".") -> Roster:
    with open(os.path.join(root, ROSTER_PATH), "rb") as handle:
        raw = tomllib.load(handle)
    return Roster(
        maintainer=raw["maintainer"],
        core_reviewers=frozenset(raw.get("core_reviewers", [])),
        committers=frozenset(raw.get("committers", [])),
        automation=frozenset(raw.get("automation", [])),
    )


def load_codeowners(root: str = ".") -> list[tuple[str, list[str]]]:
    """CODEOWNERS as (pattern, owners) in file order. Last match wins."""
    path = os.path.join(root, CODEOWNERS_PATH)
    if not os.path.isfile(path):
        return []
    entries: list[tuple[str, list[str]]] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            pattern, *owners = line.split()
            if owners:
                entries.append((pattern, [o.lstrip("@") for o in owners]))
    return entries


def path_matches(pattern: str, path: str) -> bool:
    """CODEOWNERS matching, in the subset of the syntax this repository uses.

    `*` matches everything; a pattern ending in `/` matches everything under
    that directory; a leading `/` anchors at the repository root; anything
    else is matched as a glob against the whole path and against its
    basename, which is how GitHub treats an unanchored pattern.
    """
    if pattern == "*":
        return True
    anchored = pattern.startswith("/")
    cleaned = pattern.lstrip("/")
    if cleaned.endswith("/"):
        return path.startswith(cleaned) if anchored else f"/{path}".find(f"/{cleaned}") >= 0
    if anchored:
        return path == cleaned or fnmatch.fnmatch(path, cleaned)
    return fnmatch.fnmatch(path, cleaned) or fnmatch.fnmatch(os.path.basename(path), cleaned)


def owners_for(path: str, entries: list[tuple[str, list[str]]]) -> tuple[list[str], bool]:
    """Owners of a path and whether they come from a specific rule.

    The boolean says the path is protected in the sense of charter section 4:
    it matched a rule of its own rather than falling through to the catch-all.
    """
    owners: list[str] = []
    specific = False
    for pattern, entry_owners in entries:
        if path_matches(pattern, path):
            owners = entry_owners
            specific = pattern != "*"
    return owners, specific


@dataclass
class Review:
    login: str
    state: str
    commit_id: str
    submitted_at: str
    body: str = ""


@dataclass
class PullRequest:
    number: int
    author: str
    is_draft: bool
    head_sha: str
    changed_lines: int
    paths: list[str]
    reviews: list[Review]
    # Everyone who wrote or pushed any commit on the branch, plus anyone named
    # in a Co-authored-by trailer: none of them can approve it (section 3),
    # except the ones in `update_only`.
    contributors: set[str]
    last_push: datetime
    # Earlier commits whose net diff is the head's: an approval given on one
    # of them was given on what is about to merge.
    same_diff: frozenset[str] = frozenset()
    # Contributors whose every commit merged the base branch in, or came out
    # of a rewrite, without changing the net diff: they kept the branch current
    # and wrote none of it, so they may still approve.
    update_only: frozenset[str] = frozenset()
    # When the net diff last changed. None when that cannot be worked out, and
    # the objection window then runs from `last_push`.
    diff_changed_at: datetime | None = None

    def is_current(self, sha: str) -> bool:
        """Whether a review given on `sha` was given on what is about to merge."""
        return sha == self.head_sha or sha in self.same_diff

    @property
    def writers(self) -> set[str]:
        """The contributors who cannot approve it."""
        return self.contributors - self.update_only


_CO_AUTHOR = re.compile(r"Co-authored-by:[^<]*<([^>]+)>", re.IGNORECASE)


def _co_authors(message: str) -> set[str]:
    logins: set[str] = set()
    for match in _CO_AUTHOR.finditer(message):
        handle = match.group(1).split("@")[0]
        # `12345+login@users.noreply.github.com` is the form GitHub writes.
        logins.add(handle.split("+")[-1])
    return logins


def _logins_from_commit(raw: dict[str, Any]) -> set[str]:
    logins: set[str] = set()
    for role in ("author", "committer"):
        login = ((raw.get(role) or {}).get("login")) or ""
        if login:
            logins.add(login)
    return logins | _co_authors((raw.get("commit") or {}).get("message") or "")


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True)
class Commit:
    """One commit on the branch, as the pull request and compare APIs list it."""

    sha: str
    parents: tuple[str, ...]
    author: str
    committer: str
    co_authors: frozenset[str]
    committed_at: datetime | None


def _commit(raw: Any) -> Commit | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("sha"), str) or not raw["sha"]:
        return None
    body = raw.get("commit") or {}
    return Commit(
        sha=raw["sha"],
        parents=tuple(str(parent.get("sha") or "") for parent in raw.get("parents") or [] if isinstance(parent, dict)),
        author=((raw.get("author") or {}).get("login")) or "",
        committer=((raw.get("committer") or {}).get("login")) or "",
        co_authors=frozenset(_co_authors(body.get("message") or "")),
        committed_at=_parse_time((body.get("committer") or {}).get("date")),
    )


def _history(raw_commits: Any) -> dict[str, Commit] | None:
    """The branch's commits by sha, or None when any of them cannot be read."""
    if not isinstance(raw_commits, list):
        return None
    history: dict[str, Commit] = {}
    for raw in raw_commits:
        commit = _commit(raw)
        if commit is None:
            return None
        history[commit.sha] = commit
    return history


def _merges_base(commit: Commit, history: dict[str, Commit]) -> bool:
    """A merge whose other parents are not on the branch, so came from the base.

    `history` is the branch's complete commit list, which never includes a
    commit the base branch already has.
    """
    return len(commit.parents) > 1 and all(parent not in history for parent in commit.parents[1:])


def _latest_commit(sha: str, history: dict[str, Commit]) -> datetime | None:
    """The newest committer date among `sha` and its ancestors on the branch."""
    latest: datetime | None = None
    seen: set[str] = set()
    stack = [sha]
    while stack:
        current = stack.pop()
        if current in seen or current not in history:
            continue
        seen.add(current)
        commit = history[current]
        if commit.committed_at is None:
            return None
        latest = commit.committed_at if latest is None else max(latest, commit.committed_at)
        stack.extend(commit.parents)
    return latest


_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", re.MULTILINE)


def net_diff_fingerprint(compare: Any) -> str | None:
    """SHA-256 of a three-dot compare's file changes, or None if it cannot be trusted.

    Each file contributes its name, previous name, status and patch, in
    filename order, with `@@ -a,b +c,d @@ tail` reduced to `@@ tail`: a base
    branch that moved lines above the change shifts those numbers and nothing
    else. A list at GitHub's file limit may have been cut short, and a file
    without a patch (binary, or too large to show) hides its change, so either
    makes the fingerprint uncomputable.
    """
    files = compare.get("files") if isinstance(compare, dict) else None
    if not isinstance(files, list) or len(files) >= COMPARE_FILE_LIMIT:
        return None
    entries: list[list[str]] = []
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("patch"), str):
            return None
        entries.append(
            [
                str(item.get("filename") or ""),
                str(item.get("previous_filename") or ""),
                str(item.get("status") or ""),
                _HUNK_HEADER.sub("@@", item["patch"]),
            ]
        )
    entries.sort(key=lambda entry: entry[0])
    encoded = json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class Snapshot:
    """The pull request as it stood at one commit."""

    fingerprint: str | None
    # Its own commits (base...commit) by sha; None when the list is cut short.
    commits: dict[str, Commit] | None


UNREADABLE = Snapshot(None, None)


@dataclass(frozen=True)
class ForcePush:
    at: datetime
    before: str
    after: str


class NetDiff:
    """What the API says about how one pull request's net diff evolved.

    Each commit is compared with the base branch at most once per run and at
    most COMPARE_BUDGET times in all. Every failure reads as "unknown", and
    every rule treats "unknown" as "changed".
    """

    def __init__(self, repo: str, number: int, base: str, api: Callable[..., Any]) -> None:
        self.repo = repo
        self.number = number
        self.base = base
        self.api = api
        self._snapshots: dict[str, Snapshot] = {}
        self._force_pushes: list[ForcePush] | None = None
        self._force_pushes_read = False
        self._contributed: dict[tuple[str, int], bool] = {}

    def snapshot(self, sha: str) -> Snapshot:
        if sha not in self._snapshots:
            self._snapshots[sha] = self._compare(sha)
        return self._snapshots[sha]

    def _compare(self, sha: str) -> Snapshot:
        if not sha or not self.base or len(self._snapshots) >= COMPARE_BUDGET:
            return UNREADABLE
        try:
            raw = self.api("api", f"repos/{self.repo}/compare/{quote(self.base, safe='/')}...{sha}")
        except (subprocess.CalledProcessError, OSError, ValueError):
            return UNREADABLE
        if not isinstance(raw, dict):
            return UNREADABLE
        commits = raw.get("commits")
        complete = isinstance(commits, list) and raw.get("total_commits") == len(commits)
        return Snapshot(net_diff_fingerprint(raw), _history(commits) if complete else None)

    def fingerprint(self, sha: str) -> str | None:
        return self.snapshot(sha).fingerprint

    def same(self, first: str, second: str) -> bool:
        """Whether two commits carry the same net diff. Unknown is never the same."""
        known = self.fingerprint(first)
        return known is not None and known == self.fingerprint(second)

    def force_pushes(self) -> list[ForcePush] | None:
        """The branch's force pushes, oldest first; None when they cannot all be read."""
        if not self._force_pushes_read:
            self._force_pushes_read = True
            self._force_pushes = self._read_force_pushes()
        return self._force_pushes

    def _read_force_pushes(self) -> list[ForcePush] | None:
        owner, _, name = self.repo.partition("/")
        try:
            raw = self.api(
                "api",
                "graphql",
                "-f",
                f"query={FORCE_PUSHES_QUERY}",
                "-f",
                f"owner={owner}",
                "-f",
                f"name={name}",
                "-F",
                f"number={self.number}",
            )
            timeline = raw["data"]["repository"]["pullRequest"]["timelineItems"]
            nodes = timeline["nodes"]
            if timeline["totalCount"] > len(nodes):
                return None
            pushes: list[ForcePush] = []
            for node in nodes:
                at = _parse_time(node.get("createdAt"))
                if at is None:
                    return None
                before = (node.get("beforeCommit") or {}).get("oid") or ""
                after = (node.get("afterCommit") or {}).get("oid") or ""
                pushes.append(ForcePush(at, before, after))
        except (subprocess.CalledProcessError, OSError, ValueError, KeyError, TypeError, AttributeError):
            return None
        return sorted(pushes, key=lambda push: push.at)

    def merged_base_only(self, commit: Commit, history: dict[str, Commit]) -> bool:
        """A merge that brought the base branch in and left the net diff as it was."""
        return _merges_base(commit, history) and self.same(commit.parents[0], commit.sha)

    def contributes(self, who: str, history: dict[str, Commit], upto: int | None = None) -> bool:
        """Whether `who` wrote or pushed anything in `history` beyond keeping it current.

        `history` is the branch's commits at some moment, and `upto` limits the
        force pushes considered to the ones before that moment. Writing a
        commit, or being named as its co-author, always counts. Committing one
        written by someone else counts unless the commit is a merge of the base
        branch or came out of a rewrite, and the net diff was left alone.
        """
        for commit in history.values():
            if who in commit.co_authors:
                return True
            if who not in (commit.author, commit.committer) or self.merged_base_only(commit, history):
                continue
            if who == commit.author or not self.rewritten_for(who, commit, upto):
                return True
        return False

    def rewritten_for(self, who: str, commit: Commit, upto: int | None) -> bool:
        """Whether `commit` came out of a force push that left the net diff alone,
        and rewrote a history `who` had not contributed to.

        The rewrite is the latest force push whose result holds the commit and
        whose starting point did not. Reading the starting point is what stops
        a rewrite from excusing a commit its rewriter had already pushed under
        someone else's name.
        """
        pushes = self.force_pushes()
        if pushes is None:
            return False
        for index in reversed(range(len(pushes) if upto is None else upto)):
            push = pushes[index]
            after = self.snapshot(push.after).commits
            before = self.snapshot(push.before).commits
            if after is None or before is None:
                return False
            if commit.sha not in after or commit.sha in before:
                continue
            if not self.same(push.before, push.after):
                return False
            if (who, index) not in self._contributed:
                self._contributed[who, index] = self.contributes(who, before, index)
            return not self._contributed[who, index]
        return False

    def changed_at(self, head: str, history: dict[str, Commit]) -> datetime | None:
        """When the net diff last changed, or None when that cannot be worked out.

        Walks back from the head across the updates that left the fingerprint
        as it was - a merge of the base branch, a force push that only rebased
        - to the push that produced it: a force push's own time, or for an
        ordinary push the newest committer date in what it delivered, which is
        what the last push has always meant here.
        """
        pushes = self.force_pushes()
        if pushes is None:
            return None
        sha, known, upto = head, history, len(pushes)
        for _ in range(ANCHOR_STEPS):
            index = next((i for i in reversed(range(upto)) if pushes[i].after == sha), None)
            if index is not None:
                push = pushes[index]
                before, after = self.fingerprint(push.before), self.fingerprint(push.after)
                if before is None or after is None:
                    return None
                if before != after:
                    delivered = _latest_commit(sha, known)
                    return push.at if delivered is None else max(push.at, delivered)
                sha, upto = push.before, index
                earlier = self.snapshot(sha).commits
                if earlier is None:
                    return None
                known = earlier
                continue
            commit = known.get(sha)
            if commit is None:
                return None
            if _merges_base(commit, known):
                first, merged = self.fingerprint(commit.parents[0]), self.fingerprint(commit.sha)
                if first is None or merged is None:
                    return None
                if first == merged:
                    sha = commit.parents[0]
                    continue
            return _latest_commit(sha, known)
        return None


def fetch_pull_request(
    repo: str,
    number: int,
    candidates: Collection[str] | None = None,
    api: Callable[..., Any] | None = None,
) -> PullRequest:
    """The pull request as GitHub reports it, with its net diff worked out.

    `candidates` are the people whose approval could count - the roster and
    the code owners. The net diff is only compared for their reviews and their
    commits, which keeps the compare calls few; None means everyone.
    """
    call = api or gh_json
    raw = call("api", f"repos/{repo}/pulls/{number}")
    files = call("api", f"repos/{repo}/pulls/{number}/files", "--paginate")
    raw_reviews = call("api", f"repos/{repo}/pulls/{number}/reviews", "--paginate")
    commits = call("api", f"repos/{repo}/pulls/{number}/commits", "--paginate")

    contributors: set[str] = set()
    pushed_at = None
    for commit in commits:
        contributors |= _logins_from_commit(commit)
        date = ((commit.get("commit") or {}).get("committer") or {}).get("date")
        if date:
            moment = datetime.fromisoformat(date.replace("Z", "+00:00"))
            pushed_at = moment if pushed_at is None or moment > pushed_at else pushed_at

    author = (raw.get("user") or {}).get("login", "")
    head_sha = (raw.get("head") or {}).get("sha", "")
    paths = [f["filename"] for f in files]
    reviews = [
        Review(
            login=(r.get("user") or {}).get("login", ""),
            state=r.get("state", ""),
            commit_id=r.get("commit_id") or "",
            submitted_at=r.get("submitted_at") or "",
            body=r.get("body") or "",
        )
        for r in raw_reviews
    ]

    def counts(login: str) -> bool:
        return candidates is None or login in candidates

    diffs = NetDiff(repo, number, (raw.get("base") or {}).get("ref", ""), call)
    earlier = {
        review.commit_id
        for login, review in standing_reviews(reviews).items()
        if review.state == "APPROVED" and review.commit_id and review.commit_id != head_sha and counts(login)
    }
    same_diff = frozenset(sha for sha in sorted(earlier) if diffs.same(sha, head_sha))

    # The contributor rule and the window read the branch's whole history; a
    # list GitHub cut short leaves both where they were.
    history = _history(commits) if len(commits) < PR_COMMIT_LIMIT else None
    update_only: frozenset[str] = frozenset()
    diff_changed_at = None
    if history is not None:
        update_only = frozenset(
            who for who in sorted(contributors - {author}) if counts(who) and not diffs.contributes(who, history)
        )
        if any(path in GOVERNANCE_PATHS for path in paths):
            diff_changed_at = diffs.changed_at(head_sha, history)

    return PullRequest(
        number=number,
        author=author,
        is_draft=bool(raw.get("draft")),
        head_sha=head_sha,
        changed_lines=int(raw.get("additions") or 0) + int(raw.get("deletions") or 0),
        paths=paths,
        reviews=reviews,
        contributors=contributors,
        last_push=pushed_at or datetime.now(UTC),
        same_diff=same_diff,
        update_only=update_only,
        diff_changed_at=diff_changed_at,
    )


def standing_reviews(reviews: list[Review]) -> dict[str, Review]:
    """Each person's current verdict.

    A `commented` review does not replace an earlier approval or request for
    changes - GitHub keeps the earlier verdict standing - so it is skipped
    rather than treated as a newer state.

    A dismissed review does replace it. GitHub dismisses an approval as stale
    on the next push, and by then the approval had already withdrawn any
    earlier request for changes; skipping the dismissal would bring that
    request back. The dismissed review is kept as the person's latest, and it
    counts as neither an approval nor a request for changes.
    """
    latest: dict[str, Review] = {}
    for review in reviews:
        if not review.login or review.state in ("COMMENTED", "PENDING"):
            continue
        current = latest.get(review.login)
        if current is None or review.submitted_at >= current.submitted_at:
            latest[review.login] = review
    return latest


@dataclass
class Requirement:
    text: str
    met: bool
    who: str


@dataclass
class Verdict:
    passed: bool
    title: str
    requirements: list[Requirement] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"### `quorum`: {self.title}", ""]
        if self.requirements:
            lines += ["| Requirement | Met | Who can satisfy it |", "|---|---|---|"]
            for req in self.requirements:
                lines.append(f"| {req.text} | {'yes' if req.met else '**no**'} | {req.who} |")
            lines.append("")
        lines += self.notes
        lines.append("")
        lines.append(
            "Rules: [`docs/governance/review-charter.md`](docs/governance/review-charter.md) "
            "sections 1, 3, 4, 6 and 10. Roster: `.github/quorum-roster.toml`."
        )
        return "\n".join(lines)


def _names(logins: set[str] | frozenset[str]) -> str:
    return ", ".join(f"@{login}" for login in sorted(logins)) or "nobody on the roster"


def _sensitive_paths(paths: list[str]) -> list[str]:
    return sorted(
        p for p in paths if not p.startswith(SENSITIVE_EXEMPT_PREFIXES) and any(word in p for word in SENSITIVE_WORDS)
    )


def adoptable(pr: PullRequest, owners: list[tuple[str, list[str]]]) -> bool:
    """Whether the adoption notice may stand in for approvals on this change.

    Only where the ordinary quorum would apply: not the tier that asks for a
    third approval (large or sensitive), no path a CODEOWNERS line of its own
    covers, and nothing under ADOPTION_EXCLUDED_PREFIXES. Everywhere else the
    notice is ignored and the change needs its approvals.
    """
    if pr.changed_lines > THIRD_APPROVAL_LINES or _sensitive_paths(pr.paths):
        return False
    return not any(owners_for(p, owners)[1] or p.startswith(ADOPTION_EXCLUDED_PREFIXES) for p in pr.paths)


def evaluate(pr: PullRequest, roster: Roster, owners: list[tuple[str, list[str]]], now: datetime) -> Verdict:
    if pr.is_draft:
        return Verdict(True, "draft, nothing to decide yet")

    standing = standing_reviews(pr.reviews)
    approvals = {
        login for login, review in standing.items() if review.state == "APPROVED" and pr.is_current(review.commit_id)
    }
    stale_approvals = {
        login
        for login, review in standing.items()
        if review.state == "APPROVED" and not pr.is_current(review.commit_id)
    }
    carried_approvals = {
        login for login, review in standing.items() if review.state == "APPROVED" and review.commit_id in pr.same_diff
    }
    changes_requested = {login for login, review in standing.items() if review.state == "CHANGES_REQUESTED"}

    verdict = Verdict(True, "")
    blocking = changes_requested & (roster.quorum_holders | {roster.maintainer})
    if blocking:
        verdict.requirements.append(
            Requirement(
                f"no standing *changes requested* (open: {_names(blocking)})",
                False,
                "the reviewer who asked for changes, by approving or dismissing their review",
            )
        )

    # 1. The objection window on governance changes, for every author.
    governance = [p for p in pr.paths if p in GOVERNANCE_PATHS]
    if governance:
        elapsed = now - (pr.diff_changed_at or pr.last_push)
        met = elapsed >= GOVERNANCE_WINDOW
        remaining = GOVERNANCE_WINDOW - elapsed
        hours = max(0, int(remaining.total_seconds() // 3600))
        verdict.requirements.append(
            Requirement(
                f"72 hours open for objections (touches {', '.join(f'`{p}`' for p in governance)})",
                met,
                "nobody - it merges once the window closes" + ("" if met else f", about {hours}h from now"),
            )
        )

    adoption_open = now < ADOPTION_ENDS and adoptable(pr, owners)
    adopted = (
        adoption_open
        and roster.maintainer in pr.contributors
        and any(
            review.login == roster.maintainer and review.commit_id == pr.head_sha and ADOPTION_NOTICE in review.body
            for review in pr.reviews
        )
    )

    if pr.author in roster.automation:
        stops = sorted(
            p
            for p in pr.paths
            if any(word in p for word in AUTOMATION_STOP_WORDS) or p.startswith(AUTOMATION_STOP_PREFIXES)
        )
        if stops:
            met = roster.maintainer in approvals
            verdict.requirements.append(
                Requirement(
                    f"maintainer approval: automation changing {', '.join(f'`{p}`' for p in stops[:4])}"
                    + (" and others" if len(stops) > 4 else ""),
                    met,
                    f"@{roster.maintainer}",
                )
            )
        else:
            verdict.notes.append("Automation merging its own change on green CI (charter, section 1).")
    elif pr.author == GITHUB_ACTIONS_BOT:
        verdict.requirements.append(
            Requirement(
                "maintainer approval: a pull request opened by the workflow account",
                roster.maintainer in approvals,
                f"@{roster.maintainer}",
            )
        )
    elif pr.author == roster.maintainer:
        verdict.notes.append(
            "The maintainer's own change merges without approvals; a committer's "
            "*changes requested* blocks it (charter, section 6)."
        )
    elif adopted:
        verdict.notes.append(
            "Adopted by the maintainer, who pushed to the branch and said so in the thread: "
            "it merges as their own change until 2026-10-05, and a committer's "
            "*changes requested* blocks it (charter, section 3)."
        )
    else:
        eligible = roster.quorum_holders - {pr.author} - pr.writers
        approving = approvals & eligible
        approving_core = approving & roster.core

        sensitive = _sensitive_paths(pr.paths)
        large = pr.changed_lines > THIRD_APPROVAL_LINES
        need_total, need_core = (3, 2) if (large or sensitive) else (2, 1)
        reason = (
            f"{pr.changed_lines} changed lines"
            if large
            else (f"touches `{sensitive[0]}`" if sensitive else f"{pr.changed_lines} changed lines")
        )

        text = f"{need_total} approvals, {need_core} from a core reviewer ({reason})"
        met = len(approving) >= need_total and len(approving_core) >= need_core
        who = _names(eligible - approving)
        if need_total == 2 and now < BACKLOG_WINDOW_ENDS:
            text += f", or the maintainer's alone until {BACKLOG_WINDOW_ENDS:%Y-%m-%d}"
            met = met or roster.maintainer in approving
            if roster.maintainer not in approving:
                who += f", or @{roster.maintainer} alone"
        verdict.requirements.append(Requirement(text, met, who))

        if pr.changed_lines > MAINTAINER_OR_SPLIT_LINES:
            verdict.requirements.append(
                Requirement(
                    f"maintainer approval or a split ({pr.changed_lines} changed lines, "
                    f"over {MAINTAINER_OR_SPLIT_LINES})",
                    roster.maintainer in approvals,
                    f"@{roster.maintainer}, or split the change",
                )
            )

        # A CODEOWNERS line that names several people is satisfied by any one
        # of them, which is how GitHub's own code-owner review reads it, so
        # the paths are grouped by the owner set of the line that won them
        # and each group is one requirement: a line with five owners asks for
        # one approval, not five. The author and anyone who pushed to the
        # branch cannot be that one (section 3).
        protected: dict[frozenset[str], list[str]] = {}
        for path in pr.paths:
            path_owners, specific = owners_for(path, owners)
            if specific:
                protected.setdefault(frozenset(path_owners), []).append(path)
        for owner_set, paths in sorted(protected.items(), key=lambda item: sorted(item[0])):
            eligible_owners = owner_set - {pr.author} - pr.writers
            verdict.requirements.append(
                Requirement(
                    f"approval from the owner of {', '.join(f'`{p}`' for p in paths[:3])}"
                    + (" and others" if len(paths) > 3 else ""),
                    bool(eligible_owners & approvals),
                    _names(eligible_owners) if eligible_owners else "nobody: every owner wrote or pushed this change",
                )
            )

    if (
        not adopted
        and adoption_open
        and pr.author not in roster.automation | {GITHUB_ACTIONS_BOT, roster.maintainer}
        and roster.maintainer in pr.contributors
    ):
        verdict.notes.append(
            f"@{roster.maintainer} has pushed to this branch; it merges as the maintainer's own change "
            f"once they post the notice `{ADOPTION_NOTICE}` as a review on the current head (charter, section 3)."
        )

    if stale_approvals:
        verdict.notes.append(
            f"Approvals given before the last push do not count: {_names(stale_approvals)}. "
            "The push changed the net diff, or the diff could not be compared, so nobody has read "
            "what is about to merge (charter, section 2)."
        )

    if carried_approvals:
        verdict.notes.append(
            f"Approvals given on an earlier commit with the same net diff count: {_names(carried_approvals)}. "
            "The pushes since only merged the base branch in or rebased onto it (charter, section 2)."
        )

    verdict.passed = all(req.met for req in verdict.requirements)
    missing = sum(1 for req in verdict.requirements if not req.met)
    verdict.title = "review requirements met" if verdict.passed else f"{missing} requirement(s) missing"
    return verdict


def annotation(verdict: Verdict) -> str:
    """The one line GitHub shows next to the red check.

    It names the first unmet requirement and who can meet it, so a contributor
    reads "waiting for two approvals" rather than "something is missing". The
    full table stays in the job summary.
    """
    unmet = [req for req in verdict.requirements if not req.met]
    if not unmet:
        return verdict.title or "unknown"
    first = unmet[0]
    rest = f" (+{len(unmet) - 1} more in the job summary)" if len(unmet) > 1 else ""
    result = f"waiting for: {first.text} - {first.who}{rest}".replace("`", "")
    return result or "waiting for: unknown reason"


def pr_number_from_env(env: dict[str, str]) -> int | None:
    """The pull request under test, for each event that can trigger this.

    On `merge_group` the ref names the queued pull request, which is the one
    whose reviews still have to hold at the moment it merges.
    """
    if env.get("GITHUB_EVENT_NAME") == "merge_group":
        match = re.search(r"/pr-(\d+)-", env.get("GITHUB_REF", ""))
        return int(match.group(1)) if match else None
    event_path = env.get("GITHUB_EVENT_PATH")
    if event_path and os.path.isfile(event_path):
        with open(event_path, encoding="utf-8") as handle:
            event = json.load(handle)
        number = (event.get("pull_request") or {}).get("number")
        if number:
            return int(number)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--pr", type=int, default=None)
    parser.add_argument("--root", default=".", help="where to read the roster and CODEOWNERS from")
    args = parser.parse_args(argv)

    number = args.pr or pr_number_from_env(dict(os.environ))
    if number is None:
        print("quorum: no pull request in this event; nothing to check")
        return 0

    roster = load_roster(args.root)
    owners = load_codeowners(args.root)
    candidates = roster.quorum_holders | {owner for _, line in owners for owner in line}
    pr = fetch_pull_request(args.repo, number, candidates)
    verdict = evaluate(pr, roster, owners, datetime.now(UTC))

    summary = verdict.summary()
    print(summary)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(summary + "\n")
    if not verdict.passed:
        print(f"::error title=quorum::{annotation(verdict)}")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
