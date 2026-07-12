#!/usr/bin/env python3
"""Static-analysis sweeper: turn open Sonar findings into backlog tickets.

Walks the project's Sonar findings via ``/api/issues/search`` and emits
one backlog ticket per new finding into ``.sdd/backlog/open/``. De-dup is
keyed on the Sonar stable issue ``key`` field (stored in the ticket
frontmatter as ``sonar_issue_key``), so re-runs are idempotent.

This script intentionally **never** consumes the raw Sonar ``message``
or ``htmlDesc`` text in the ticket body. Every public-facing string is
synthesised from a pre-vetted rule-family blurb table so the emitted
ticket reads as ordinary engineering hygiene regardless of what the
upstream vendor prose contains.

Usage
-----

    python scripts/sweep_sonar_findings.py \\
        --severity-min MAJOR \\
        --max-per-day 25 \\
        --out-dir .sdd/backlog/open \\
        [--dry-run] [--create-gh-issues] [--fixture path/to/issues.json]

The ``--create-gh-issues`` flag promotes both P0 and P1 tickets to a GH
issue via ``gh issue create``. P0 covers BLOCKER findings; P1 covers
CRITICAL and MAJOR. P2 tickets (MINOR, INFO) stay file-only so the GH
issue feed does not get flooded by low-severity churn.

SARIF mode
----------

    python scripts/sweep_sonar_findings.py --emit-sarif sonar.sarif \\
        [--fixture path/to/issues.json]

``--emit-sarif PATH`` reuses the same Sonar client to fetch *all*
findings (issues of every type plus security hotspots) and writes them
as a SARIF 2.1.0 document at ``PATH``. This is the path that feeds
GitHub code scanning (the Security tab), so vulnerability detail stays
visible to maintainers rather than public issue readers. In SARIF mode
the script never writes backlog tickets and never opens GitHub issues.
Output is deterministic (rules and results are sorted on stable keys)
so an unchanged finding set produces byte-identical SARIF on re-run.

Exit codes
----------

- 0: clean run (may have emitted zero or more tickets).
- 1: Sonar API failed after retries; no partial tickets emitted.
- 2: misconfiguration (missing env vars, bad CLI args).
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as _dt
import json
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import yaml

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Sequence

    SleepFn = Callable[[float], None]
    RunnerFn = Callable[..., Any]
else:
    SleepFn = Any
    RunnerFn = Any

# Make the bernstein package importable when running from the source tree.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from bernstein.core.observability.sonar import (  # noqa: E402
    SonarConfig,
    load_config,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_OUT_DIR = Path(".sdd/backlog/open")
DEFAULT_BACKLOG_ROOTS: tuple[Path, ...] = (
    Path(".sdd/backlog/open"),
    Path(".sdd/backlog/claimed"),
    Path(".sdd/backlog/closed"),
    Path(".sdd/backlog/done"),
    Path(".sdd/backlog/deferred"),
)

SEVERITY_ORDER: tuple[str, ...] = ("BLOCKER", "CRITICAL", "MAJOR", "MINOR", "INFO")
SEVERITY_RANK: dict[str, int] = {sev: idx for idx, sev in enumerate(SEVERITY_ORDER)}

# Priority is the project's P0/P1/P2 enum (see docs/sdd/ticket_schema.md).
#
# MAJOR is intentionally promoted to P1 (was P2). Rationale: the MAJOR
# cohort accumulates much faster than operators can hand-triage it, so
# leaving it at P2 (file-only) means the queue grows without bound while
# the underlying static-analysis debt compounds. Promoting MAJOR to P1
# lets the sweeper auto-open GH issues for MAJOR findings alongside
# BLOCKER (P0) and CRITICAL (P1), which routes them onto the same
# tracker surface operators already monitor. MINOR and INFO stay at P2
# so the GH-issue feed is not flooded with low-severity churn.
SEVERITY_TO_PRIORITY: dict[str, str] = {
    "BLOCKER": "P0",
    "CRITICAL": "P1",
    "MAJOR": "P1",
    "MINOR": "P2",
    "INFO": "P2",
}

# Effort defaults: most static-analysis findings are small, one or two
# files worth of refactor. Override via the rule-family table when a
# given rule family is known to span more.
DEFAULT_EFFORT = "S"

# Ticket "type" tag mapping (informational; the validator only checks
# top-level required keys).
SONAR_TYPE_TO_TICKET_TYPE: dict[str, str] = {
    "BUG": "fix",
    "VULNERABILITY": "fix",
    "CODE_SMELL": "refactor",
    "SECURITY_HOTSPOT": "fix",
}

DEFAULT_PAGE_SIZE = 500
MAX_PAGES = 20

DEFAULT_TIMEOUT_SECONDS = 15.0
RETRY_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 2.0
_BACKOFF_JITTER_FRACTION = 0.25


# ---------------------------------------------------------------------------
# Rule-family blurb table (public-safe ## Why bodies)
#
# Every blurb is reviewed at code-review time. New rule keys land as PRs
# that add a row here. The raw Sonar message/htmlDesc is NEVER substituted
# in: vendor prose can carry phrasing the project's public-artefact
# discipline disallows.
# ---------------------------------------------------------------------------


# (rule-prefix, category-slug, blurb)
RULE_FAMILY_BLURBS: tuple[tuple[str, str, str], ...] = (
    (
        "python:S3776",
        "cognitive-complexity",
        (
            "Cognitive complexity exceeds the per-function cap configured in "
            "the static-analysis rules. Split the function into smaller helpers "
            "so each one stays readable and unit-testable in isolation."
        ),
    ),
    (
        "python:S1192",
        "duplicated-strings",
        (
            "Repeated string literal should be extracted to a module-level "
            "constant so it has a single source of truth and is searchable."
        ),
    ),
    (
        "python:S5754",
        "broad-except",
        (
            "Broad exception clause hides specific failure modes. Narrow the "
            "except clause to the exact exception types the caller is meant to "
            "handle."
        ),
    ),
    (
        "python:S1481",
        "unused-local",
        (
            "Local variable is declared but never read. Remove it so the "
            "function reads cleanly and any future maintainer is not misled."
        ),
    ),
    (
        "python:S107",
        "long-parameter-list",
        (
            "Function parameter count exceeds the per-function cap. Group "
            "related parameters into a dataclass or keyword-only block."
        ),
    ),
    (
        "python:S1186",
        "empty-function",
        (
            "Function body is empty. Either implement the function or remove "
            "it so the callable surface matches the project's documented API."
        ),
    ),
    (
        "python:S125",
        "commented-out-code",
        (
            "Commented-out code block should be removed; version control "
            "preserves history and stale comments mislead future readers."
        ),
    ),
    (
        "python:S1066",
        "collapsible-if",
        ("Nested if statement can be merged with its parent so the branching reads as a single condition."),
    ),
    (
        "python:S1854",
        "useless-assignment",
        (
            "Value assigned to a local is overwritten before it is read. "
            "Remove the dead assignment so the data flow reads cleanly."
        ),
    ),
    (
        "python:S5806",
        "redefined-builtin",
        (
            "Local name shadows a Python builtin. Rename the local so the "
            "builtin remains accessible inside the function."
        ),
    ),
    (
        "python:S1226",
        "reassigned-parameter",
        (
            "Function parameter is reassigned inside the body. Introduce a "
            "local variable so the original argument value stays visible."
        ),
    ),
    (
        "python:S1172",
        "unused-parameter",
        (
            "Function parameter is declared but never used. Remove it or "
            "rename it with a leading underscore to mark it intentional."
        ),
    ),
    (
        "python:S5547",
        "weak-cryptography",
        (
            "Cryptographic primitive is below the project's configured "
            "strength baseline. Replace with a primitive that meets the "
            "documented baseline."
        ),
    ),
    (
        "python:S2068",
        "hardcoded-credential",
        (
            "Hard-coded credential literal must not ship in source. Move the "
            "value to an env-var or the project's secrets store."
        ),
    ),
    (
        "python:S5443",
        "tempfile-permissions",
        (
            "Temporary file or directory is created with permissive default "
            "permissions. Restrict the mode so the file is not readable by "
            "other local users."
        ),
    ),
    (
        "python:S4423",
        "weak-tls",
        (
            "TLS configuration accepts a protocol version below the project's "
            "configured baseline. Restrict the configured protocol set."
        ),
    ),
    (
        "python:S5659",
        "jwt-without-strong-validation",
        (
            "JWT verification path accepts a weak signature algorithm. Pin "
            "the accepted algorithm set to the documented baseline."
        ),
    ),
    (
        "python:S3457",
        "format-string-mismatch",
        (
            "Format string and its arguments do not line up. Align the "
            "placeholders with the actual values so the formatted output is "
            "correct at runtime."
        ),
    ),
    (
        "python:S1117",
        "shadowed-name",
        ("Local name shadows an outer-scope name. Rename the inner local so both scopes stay readable."),
    ),
    (
        "python:S1542",
        "function-naming",
        (
            "Function name does not match the project's naming convention. "
            "Rename so the symbol matches the configured lint rule."
        ),
    ),
    # Bug families
    (
        "python:S2589",
        "always-true-condition",
        (
            "Condition always evaluates the same way and the dependent branch "
            "is dead code. Remove or fix the condition so the branch reflects "
            "actual runtime behaviour."
        ),
    ),
    (
        "python:S5806",
        "unreachable-code",
        ("Block of code is unreachable. Delete it so future readers do not spend time on dead branches."),
    ),
    (
        "python:S930",
        "wrong-argument-count",
        (
            "Function call passes the wrong number of arguments. Update the "
            "call site so it matches the callee's signature."
        ),
    ),
    # MAJOR-cohort additions for the widened sweeper. Each entry is
    # framed as ordinary engineering hygiene and never reuses raw vendor
    # prose.
    (
        "python:S5886",
        "return-type-mismatch",
        (
            "Declared return type does not line up with the value the "
            "function actually returns. Align the annotation with the "
            "returned value or fix the returned value so the static type "
            "contract holds."
        ),
    ),
    (
        "python:S5864",
        "isinstance-non-type",
        (
            "isinstance call is given a second argument that is not a class "
            "or a tuple of classes. Pass an actual class object so the "
            "runtime check behaves as the call site reads."
        ),
    ),
    (
        "python:S3358",
        "nested-ternary",
        (
            "Nested ternary expression is hard to read at the call site. "
            "Lift the inner condition into a named local or an if-block so "
            "the branching is explicit."
        ),
    ),
    (
        "python:S5869",
        "regex-character-class-duplicate",
        (
            "Regular-expression character class contains a duplicate "
            "element. Remove the duplicate so the regex reads cleanly and "
            "the intent is unambiguous."
        ),
    ),
    (
        "python:S1244",
        "float-equality",
        (
            "Floating-point values are compared with == or !=, which is "
            "fragile due to rounding. Use math.isclose or compare to an "
            "explicit tolerance instead."
        ),
    ),
    (
        "python:S5843",
        "regex-super-linear",
        (
            "Regular-expression pattern is susceptible to super-linear "
            "backtracking on adversarial input. Restructure the pattern to "
            "use non-capturing groups, possessive quantifiers, or anchored "
            "alternatives so worst-case matching stays linear."
        ),
    ),
    (
        "python:S1764",
        "identical-subexpressions",
        (
            "Both sides of a binary operator are identical sub-expressions, "
            "so the operator either has no effect or hides a bug. Rewrite "
            "the expression so each side carries the intended value."
        ),
    ),
    (
        "python:S8495",
        "typing-misuse",
        (
            "Generic-typing construct is used in a way the type system "
            "cannot resolve. Update the annotation so it matches the "
            "documented usage of the typing primitive."
        ),
    ),
    (
        "python:S3923",
        "all-branches-identical",
        (
            "All branches of the conditional structure have the same "
            "implementation, so the branching is redundant. Collapse the "
            "structure or fix the branch bodies so each branch contributes "
            "distinct behaviour."
        ),
    ),
)


DEFAULT_BLURB = (
    "Static-analysis finding flagged under rule key {rule_key}. Resolve so "
    "the file conforms to the project's configured maintainability rules."
)


# Strings that must never appear in any emitted ## Why. Belt-and-braces.
FORBIDDEN_SUBSTRINGS: tuple[str, ...] = (
    "audience",
    "funnel",
    "adoption",
    "conversion",
    "retention",
    "buyer",
    "monetisation",
    "monetization",
    "roi",
    "brand",
    "moat",
    "competitor",
    "competitive",
    "flywheel",
    "marketing",
    "scenario prognos",
    "premortem",
    "ai search",
    "aio probe",
    "umami",
    "\u2014",  # U+2014; generated prose stays ASCII-punctuation only
    "\u2013",  # U+2013; generated prose stays ASCII-punctuation only
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Finding:
    """One Sonar finding, normalised.

    The first seven fields are consumed by both the backlog-ticket path
    and the SARIF path. The trailing fields (``message``, ``tags``,
    ``rule_name``, ``vulnerability_probability``) are populated only for
    the SARIF path; the ticket path never reads them (it synthesises its
    public-safe prose from the vetted blurb table instead).
    """

    key: str
    rule: str
    severity: str
    type: str
    component: str
    line: int | None
    creation_date: str  # ISO-8601 timestamp string, used only for sorting.
    message: str = ""
    tags: tuple[str, ...] = ()
    rule_name: str = ""
    vulnerability_probability: str | None = None

    @property
    def severity_rank(self) -> int:
        return SEVERITY_RANK.get(self.severity, len(SEVERITY_ORDER))


# ---------------------------------------------------------------------------
# Sanitisation
# ---------------------------------------------------------------------------


def safe_why(rule_key: str, severity: str, component: str, line: int | None) -> str:
    """Return a public-safe ``## Why`` body for a Sonar finding.

    Uses only the rule key to look up a pre-vetted blurb from
    ``RULE_FAMILY_BLURBS`` or the ``DEFAULT_BLURB`` fallback. Never
    consumes the raw Sonar ``message`` or ``htmlDesc``.
    """
    del severity, component, line  # accepted for signature stability
    blurb = DEFAULT_BLURB.format(rule_key=rule_key)
    for prefix, _category, text in RULE_FAMILY_BLURBS:
        if rule_key == prefix or rule_key.startswith(prefix + ":"):
            blurb = text
            break
    return blurb


def _assert_no_forbidden(text: str, context: str = "") -> None:
    """Raise AssertionError if ``text`` contains any forbidden substring."""
    lower = text.lower()
    for forbidden in FORBIDDEN_SUBSTRINGS:
        if forbidden.lower() in lower:
            raise AssertionError(
                f"emitted text in {context!r} contains forbidden substring {forbidden!r}: {text[:120]!r}"
            )


# ---------------------------------------------------------------------------
# Backlog walker and de-dup index
# ---------------------------------------------------------------------------


_FRONTMATTER_DELIM = "---"


def _parse_frontmatter(path: Path) -> dict[str, Any] | None:
    """Parse YAML frontmatter from a Markdown ticket. Returns ``None`` on error."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith(_FRONTMATTER_DELIM):
        return None
    parts = text.split(_FRONTMATTER_DELIM, 2)
    if len(parts) < 3:
        return None
    raw = parts[1]
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _walk_yaml_tickets(roots: Iterable[Path]) -> Iterator[tuple[Path, dict[str, Any]]]:
    """Yield ``(path, frontmatter)`` for every parseable ticket under ``roots``."""
    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        for path in sorted(root.glob("*.md")):
            fm = _parse_frontmatter(path)
            if fm is None:
                continue
            yield path, fm
        for path in sorted(root.glob("*.yaml")):
            fm = None
            try:
                fm = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError):
                fm = None
            if isinstance(fm, dict):
                yield path, fm


@dataclasses.dataclass(frozen=True)
class DedupIndex:
    """Pre-computed lookups so the sweeper can decide skip-or-emit fast."""

    keys: frozenset[str]
    open_rule_component_line: frozenset[tuple[str, str, int | None]]


_OPEN_STATES: frozenset[str] = frozenset({"open", "claimed", "in_progress", "blocked"})


def build_dedup_index(roots: Iterable[Path]) -> DedupIndex:
    """Walk backlog folders and build the key + rule/component/line indices."""
    keys: set[str] = set()
    rcl: set[tuple[str, str, int | None]] = set()
    for _path, fm in _walk_yaml_tickets(roots):
        key = fm.get("sonar_issue_key")
        if isinstance(key, str) and key:
            keys.add(key)
        status = fm.get("status")
        rule = fm.get("sonar_rule")
        comp = fm.get("sonar_component")
        line = fm.get("sonar_line")
        if (
            isinstance(status, str)
            and status in _OPEN_STATES
            and isinstance(rule, str)
            and isinstance(comp, str)
            and (line is None or isinstance(line, int))
        ):
            rcl.add((rule, comp, line))
    return DedupIndex(keys=frozenset(keys), open_rule_component_line=frozenset(rcl))


# ---------------------------------------------------------------------------
# Sonar API client
# ---------------------------------------------------------------------------


def _auth(token: str) -> tuple[str, str]:
    """Sonar HTTP basic auth: token as username, empty password."""
    return token, ""


def _jitter_sleep(base: float) -> None:
    """Sleep ``base`` seconds with +/- 25 percent jitter."""
    if base <= 0:
        return
    delta = base * _BACKOFF_JITTER_FRACTION
    time.sleep(base + random.uniform(-delta, delta))


class SonarAPIError(RuntimeError):
    """Raised when the Sonar API fails after the configured retries."""


def _request_with_retries(
    client: httpx.Client,
    url: str,
    params: dict[str, Any],
    *,
    sleep_fn: SleepFn = _jitter_sleep,
) -> httpx.Response:
    """GET with exponential backoff on 5xx and Retry-After-aware 429 retry."""
    last_exc: Exception | None = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            resp = client.get(url, params=params)
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt == RETRY_ATTEMPTS:
                raise SonarAPIError(f"Sonar request failed: {exc}") from exc
            sleep_fn(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
            continue
        if 200 <= resp.status_code < 300:
            return resp
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            wait = 1.0
            if retry_after is not None:
                try:
                    wait = float(retry_after)
                except ValueError:
                    wait = 1.0
            sleep_fn(wait)
            # 429 gets exactly one retry (per design); fall through on next loop.
            continue
        if 500 <= resp.status_code < 600:
            if attempt == RETRY_ATTEMPTS:
                raise SonarAPIError(f"Sonar returned {resp.status_code} after {attempt} attempts")
            sleep_fn(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
            continue
        raise SonarAPIError(f"Sonar returned {resp.status_code}: {resp.text[:200]}")
    if last_exc is not None:
        raise SonarAPIError(f"Sonar request failed: {last_exc}") from last_exc
    raise SonarAPIError("Sonar request failed after retries")


def fetch_findings(
    config: SonarConfig,
    *,
    severities: Sequence[str],
    page_size: int = DEFAULT_PAGE_SIZE,
    client: httpx.Client | None = None,
    sleep_fn: SleepFn = _jitter_sleep,
) -> list[Finding]:
    """Page through ``/api/issues/search`` and return all open findings."""
    url = f"{config.host}/api/issues/search"
    findings: list[Finding] = []
    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS, auth=_auth(config.token))
    assert client is not None
    try:
        page = 1
        while page <= MAX_PAGES:
            params: dict[str, Any] = {
                "componentKeys": config.project_key,
                "resolved": "false",
                "severities": ",".join(severities),
                "types": "CODE_SMELL,BUG,VULNERABILITY",
                "s": "CREATION_DATE",
                "asc": "false",
                "ps": str(page_size),
                "p": str(page),
            }
            resp = _request_with_retries(client, url, params, sleep_fn=sleep_fn)
            payload = resp.json()
            if not isinstance(payload, dict):
                break
            issues = payload.get("issues") or []
            for raw in issues:
                if not isinstance(raw, dict):
                    continue
                finding = _normalise_issue(raw)
                if finding is not None:
                    findings.append(finding)
            paging = payload.get("paging") or {}
            try:
                total = int(paging.get("total", 0))
                page_idx = int(paging.get("pageIndex", page))
                size = int(paging.get("pageSize", page_size))
            except (TypeError, ValueError):
                break
            if size <= 0 or page_idx * size >= total:
                break
            page += 1
    finally:
        if owns_client:
            client.close()
    return findings


def _normalise_issue(
    raw: dict[str, Any],
    *,
    rules_by_key: dict[str, str] | None = None,
) -> Finding | None:
    """Coerce one raw Sonar issue dict into a :class:`Finding`.

    ``rules_by_key`` maps a Sonar rule key to its human-readable name (as
    returned in the ``rules`` block when ``additionalFields=rules`` is
    requested). It is used only to populate the SARIF ``rule_name``; the
    ticket path passes ``None`` and never reads the field.
    """
    key = raw.get("key")
    rule = raw.get("rule")
    severity = raw.get("severity")
    issue_type = raw.get("type")
    component = raw.get("component")
    line = raw.get("line")
    creation = raw.get("creationDate") or ""
    if not (
        isinstance(key, str)
        and key
        and isinstance(rule, str)
        and isinstance(severity, str)
        and isinstance(issue_type, str)
        and isinstance(component, str)
    ):
        return None
    if line is not None and not isinstance(line, int):
        try:
            line = int(line)
        except (TypeError, ValueError):
            line = None
    if not isinstance(creation, str):
        creation = ""
    message = raw.get("message")
    message = message if isinstance(message, str) else ""
    raw_tags = raw.get("tags")
    tags: tuple[str, ...] = tuple(t for t in raw_tags if isinstance(t, str)) if isinstance(raw_tags, list) else ()
    rule_name = ""
    if rules_by_key is not None:
        candidate = rules_by_key.get(rule)
        if isinstance(candidate, str):
            rule_name = candidate
    return Finding(
        key=key,
        rule=rule,
        severity=severity,
        type=issue_type,
        component=component,
        line=line,
        creation_date=creation,
        message=message,
        tags=tags,
        rule_name=rule_name,
    )


def _normalise_hotspot(raw: dict[str, Any]) -> Finding | None:
    """Coerce one raw Sonar security-hotspot dict into a :class:`Finding`.

    Hotspots come from ``/api/hotspots/search`` and use a different shape
    than issues: the rule key is ``ruleKey``, there is no ``severity``
    field, and the risk band lives in ``vulnerabilityProbability``
    (``HIGH`` / ``MEDIUM`` / ``LOW``). The synthesised :class:`Finding`
    carries ``type="SECURITY_HOTSPOT"`` and an empty ``severity`` so the
    SARIF layer keys its severity off the probability instead.
    """
    key = raw.get("key")
    rule = raw.get("ruleKey")
    component = raw.get("component")
    line = raw.get("line")
    probability = raw.get("vulnerabilityProbability")
    message = raw.get("message")
    creation = raw.get("creationDate") or ""
    if not (isinstance(key, str) and key and isinstance(rule, str) and isinstance(component, str)):
        return None
    if line is not None and not isinstance(line, int):
        try:
            line = int(line)
        except (TypeError, ValueError):
            line = None
    return Finding(
        key=key,
        rule=rule,
        severity="",
        type="SECURITY_HOTSPOT",
        component=component,
        line=line,
        creation_date=creation if isinstance(creation, str) else "",
        message=message if isinstance(message, str) else "",
        tags=(),
        rule_name="",
        vulnerability_probability=probability if isinstance(probability, str) else None,
    )


def fetch_hotspots(
    config: SonarConfig,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    client: httpx.Client | None = None,
    sleep_fn: SleepFn = _jitter_sleep,
) -> list[Finding]:
    """Page through ``/api/hotspots/search`` and return open hotspots.

    Reuses the same retry/auth client the issues fetch uses. The default
    (no ``status`` filter) returns hotspots still awaiting review, which
    is the open cohort we want to surface in the Security tab.
    """
    url = f"{config.host}/api/hotspots/search"
    findings: list[Finding] = []
    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS, auth=_auth(config.token))
    assert client is not None
    try:
        page = 1
        while page <= MAX_PAGES:
            params: dict[str, Any] = {
                "projectKey": config.project_key,
                "ps": str(page_size),
                "p": str(page),
            }
            resp = _request_with_retries(client, url, params, sleep_fn=sleep_fn)
            payload = resp.json()
            if not isinstance(payload, dict):
                break
            for raw in payload.get("hotspots") or []:
                if not isinstance(raw, dict):
                    continue
                finding = _normalise_hotspot(raw)
                if finding is not None:
                    findings.append(finding)
            paging = payload.get("paging") or {}
            try:
                total = int(paging.get("total", 0))
                page_idx = int(paging.get("pageIndex", page))
                size = int(paging.get("pageSize", page_size))
            except (TypeError, ValueError):
                break
            if size <= 0 or page_idx * size >= total:
                break
            page += 1
    finally:
        if owns_client:
            client.close()
    return findings


def collect_sarif_findings(
    config: SonarConfig,
    *,
    client: httpx.Client | None = None,
    sleep_fn: SleepFn = _jitter_sleep,
) -> list[Finding]:
    """Fetch every finding for the SARIF export: all issues plus hotspots.

    Unlike :func:`fetch_findings` (which the ticket path narrows by
    severity), this pulls issues of every type and severity and enriches
    them with the rule-name map so the SARIF driver carries readable rule
    metadata. Then it appends the security hotspots.
    """
    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS, auth=_auth(config.token))
    assert client is not None
    try:
        url = f"{config.host}/api/issues/search"
        raw_issues: list[dict[str, Any]] = []
        rules_by_key: dict[str, str] = {}
        page = 1
        while page <= MAX_PAGES:
            params: dict[str, Any] = {
                "componentKeys": config.project_key,
                "resolved": "false",
                "types": "CODE_SMELL,BUG,VULNERABILITY",
                "additionalFields": "rules",
                "s": "CREATION_DATE",
                "asc": "false",
                "ps": str(DEFAULT_PAGE_SIZE),
                "p": str(page),
            }
            resp = _request_with_retries(client, url, params, sleep_fn=sleep_fn)
            payload = resp.json()
            if not isinstance(payload, dict):
                break
            for rule_obj in payload.get("rules") or []:
                if not isinstance(rule_obj, dict):
                    continue
                rkey = rule_obj.get("key")
                rname = rule_obj.get("name")
                if isinstance(rkey, str):
                    rules_by_key[rkey] = rname if isinstance(rname, str) else ""
            for raw in payload.get("issues") or []:
                if isinstance(raw, dict):
                    raw_issues.append(raw)
            paging = payload.get("paging") or {}
            try:
                total = int(paging.get("total", 0))
                page_idx = int(paging.get("pageIndex", page))
                size = int(paging.get("pageSize", DEFAULT_PAGE_SIZE))
            except (TypeError, ValueError):
                break
            if size <= 0 or page_idx * size >= total:
                break
            page += 1

        findings: list[Finding] = []
        for raw in raw_issues:
            finding = _normalise_issue(raw, rules_by_key=rules_by_key)
            if finding is not None:
                findings.append(finding)
        findings.extend(fetch_hotspots(config, client=client, sleep_fn=sleep_fn))
        return findings
    finally:
        if owns_client:
            client.close()


# ---------------------------------------------------------------------------
# Ticket emission
# ---------------------------------------------------------------------------


_SLUG_INVALID = re.compile(r"[^a-z0-9-]+")


def _slugify(text: str, *, max_len: int = 60) -> str:
    """Reduce ``text`` to a kebab slug fit for filenames and ticket ids."""
    lower = text.lower().replace("_", "-").replace("/", "-").replace(":", "-")
    cleaned = _SLUG_INVALID.sub("-", lower)
    collapsed = re.sub(r"-+", "-", cleaned).strip("-")
    return collapsed[:max_len].strip("-")


def _component_tail(component: str) -> str:
    """Return the file-name portion of a Sonar component key."""
    # component looks like "bernstein:src/bernstein/core/agents/spawn_supervisor.py"
    if ":" in component:
        _, _, tail = component.partition(":")
    else:
        tail = component
    return Path(tail).name or component


def _ticket_filename(finding: Finding, day: str) -> str:
    ticket_type = SONAR_TYPE_TO_TICKET_TYPE.get(finding.type, "refactor")
    tail = _component_tail(finding.component)
    line_suffix = f"-{finding.line}" if finding.line else ""
    slug_input = f"{finding.rule}-{tail}{line_suffix}"
    slug = _slugify(slug_input, max_len=60)
    return f"{day}-{ticket_type}-{slug}.md"


def _ticket_id(finding: Finding, day: str) -> str:
    ticket_type = SONAR_TYPE_TO_TICKET_TYPE.get(finding.type, "refactor")
    tail = _component_tail(finding.component)
    line_suffix = f"-{finding.line}" if finding.line else ""
    slug_input = f"{ticket_type}-{day}-{finding.rule}-{tail}{line_suffix}"
    slug = _slugify(slug_input, max_len=80)
    # Ensure starts with [a-z0-9] per schema pattern.
    if not slug or not slug[0].isalnum():
        slug = f"sonar-{slug}".strip("-")
    return slug


def _ticket_title(finding: Finding) -> str:
    ticket_type = SONAR_TYPE_TO_TICKET_TYPE.get(finding.type, "refactor")
    tail = _component_tail(finding.component)
    location = tail
    if finding.line:
        location = f"{tail}:{finding.line}"
    return f"{ticket_type}(static-analysis): address {finding.rule} in {location}"


def _component_path(component: str) -> str:
    """Strip the project prefix from a Sonar component key."""
    if ":" in component:
        _, _, tail = component.partition(":")
        return tail
    return component


def _format_frontmatter(data: dict[str, Any]) -> str:
    """YAML-dump frontmatter with stable key order."""
    # PyYAML's default flow style is fine, but we want plain ASCII output
    # and a stable order (insertion order preserved by dict in 3.12+).
    text = yaml.safe_dump(
        data,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=False,
        width=1000,
    )
    return text


def render_ticket(finding: Finding, *, day: str) -> str:
    """Build the full Markdown body for one ticket."""
    ticket_type = SONAR_TYPE_TO_TICKET_TYPE.get(finding.type, "refactor")
    priority = SEVERITY_TO_PRIORITY.get(finding.severity, "P2")
    title = _ticket_title(finding)
    component_path = _component_path(finding.component)

    why = safe_why(finding.rule, finding.severity, finding.component, finding.line)
    _assert_no_forbidden(why, context=f"safe_why({finding.rule})")
    _assert_no_forbidden(title, context="ticket title")

    location_text = component_path
    if finding.line:
        location_text = f"{component_path}:{finding.line}"

    tldr = (
        f"Static-analysis rule `{finding.rule}` is open in `{location_text}`. "
        f"Resolve so the file matches the project's configured rule set on "
        f"the next scan."
    )
    _assert_no_forbidden(tldr, context="TL;DR")

    ac_lines = [
        (f"The finding at `{location_text}` is resolved on the next static-analysis scan."),
        (
            "Behaviour is preserved: existing unit tests covering the touched "
            "code path continue to pass without modification."
        ),
        (
            "Any new helpers or constants introduced carry docstrings and "
            "explicit type hints matching the surrounding module."
        ),
        (
            f"The static-analysis issue with key `{finding.key}` is absent "
            f"from the next scan output for this rule and component."
        ),
    ]
    for ac in ac_lines:
        _assert_no_forbidden(ac, context="acceptance criterion")

    fm: dict[str, Any] = {
        "id": _ticket_id(finding, day),
        "created": day,
        "status": "open",
        "priority": priority,
        "effort": DEFAULT_EFFORT,
        "title": title,
        "type": ticket_type,
        "tags": ["static-analysis", "quality", finding.severity.lower()],
        "acceptance_criteria": ac_lines,
        "sonar_issue_key": finding.key,
        "sonar_rule": finding.rule,
        "sonar_component": component_path,
        "sonar_line": finding.line if finding.line is not None else None,
        "sonar_severity": finding.severity,
        "sonar_type": finding.type,
    }

    fm_text = _format_frontmatter(fm)
    body_lines = [
        f"# {title}",
        "",
        "## TL;DR",
        "",
        tldr,
        "",
        "## Why",
        "",
        why,
        "",
        "## Acceptance criteria",
        "",
    ]
    for idx, ac in enumerate(ac_lines, start=1):
        body_lines.append(f"{idx}. {ac}")
    body_lines.extend(
        [
            "",
            "## Out of scope",
            "",
            (f"- Other rule findings in the same file that are not flagged by `{finding.rule}`."),
            "- Changing the configured rule itself.",
            "",
            (
                f"<!-- sonar-finding-imported: {finding.key} "
                f"{_dt.datetime.now(tz=_dt.UTC).isoformat(timespec='seconds')} -->"
            ),
            "",
        ]
    )

    body = "\n".join(body_lines)
    _assert_no_forbidden(body, context="ticket body")
    return f"---\n{fm_text}---\n\n{body}"


def emit_ticket(
    finding: Finding,
    out_dir: Path,
    *,
    day: str,
    dry_run: bool = False,
) -> tuple[Path, str, bool]:
    """Write the ticket file, returning ``(path, body, wrote)``.

    Uses exclusive-create open so two concurrent runs cannot clobber each
    other; the loser logs a skip and continues.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = _ticket_filename(finding, day)
    path = out_dir / filename
    body = render_ticket(finding, day=day)
    if dry_run:
        return path, body, False
    try:
        with path.open("x", encoding="utf-8") as fh:
            fh.write(body)
    except FileExistsError:
        return path, body, False
    return path, body, True


# ---------------------------------------------------------------------------
# Severity filter and per-day cap
# ---------------------------------------------------------------------------


def filter_by_severity(findings: Iterable[Finding], severity_min: str) -> list[Finding]:
    """Keep only findings at ``severity_min`` or higher."""
    min_rank = SEVERITY_RANK.get(severity_min.upper(), 0)
    return [f for f in findings if f.severity_rank <= min_rank]


def cap_per_day(findings: Sequence[Finding], cap: int) -> list[Finding]:
    """Sort by ``(severity_rank, creation_date desc)`` and apply the cap."""
    ordered = sorted(
        findings,
        key=lambda f: (f.severity_rank, -_creation_sort_key(f.creation_date)),
    )
    if cap <= 0:
        return list(ordered)
    return list(ordered[:cap])


def _creation_sort_key(text: str) -> float:
    """Return an epoch-ish float for the timestamp (newest first when negated)."""
    if not text:
        return 0.0
    # Sonar timestamps look like '2026-05-20T08:14:02+0000'. Trim the colon-less
    # offset format so fromisoformat accepts it on 3.12+ (3.11 needs the fix).
    norm = text
    if len(norm) >= 5 and (norm[-5] == "+" or norm[-5] == "-") and norm[-3] != ":":
        norm = f"{norm[:-2]}:{norm[-2:]}"
    try:
        return _dt.datetime.fromisoformat(norm).timestamp()
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# GH-issue creation (opt-in)
# ---------------------------------------------------------------------------


def maybe_create_gh_issue(
    ticket_path: Path,
    title: str,
    *,
    enable: bool,
    priority: str,
    runner: RunnerFn | None = None,
) -> str | None:
    """Optionally create a GH issue and return its URL or ``None``.

    The auto-promotion covers the P0 and P1 priority buckets. P0 maps to
    BLOCKER severity; P1 covers CRITICAL and MAJOR. P2 (MINOR, INFO)
    stays file-only so the GH issue feed does not flood with low
    severity churn.

    ``runner`` defaults to :func:`subprocess.run`, resolved at call time
    so tests can monkeypatch ``subprocess.run`` on the module.
    """
    if runner is None:
        runner = subprocess.run
    if not enable:
        return None
    if priority not in ("P0", "P1"):
        # P0 and P1 auto-promote so MAJOR and CRITICAL findings land on
        # the same tracker surface operators already monitor. Lower
        # priorities stay file-only until an operator picks them up.
        return None
    if not ticket_path.exists():
        return None
    cmd = [
        "gh",
        "issue",
        "create",
        "--title",
        title,
        "--body-file",
        str(ticket_path),
    ]
    try:
        result = runner(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    url = (result.stdout or "").strip().splitlines()
    if not url:
        return None
    return url[-1]


# ---------------------------------------------------------------------------
# SARIF emission (feeds GitHub code scanning / the Security tab)
# ---------------------------------------------------------------------------

SARIF_SCHEMA_URI = "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json"
SARIF_VERSION = "2.1.0"

# GitHub reads ``security-severity`` (a 0-10 string) to bucket a finding
# into critical/high/medium/low in the Security tab. Map Sonar's issue
# severities and hotspot probability bands onto that scale.
SECURITY_SEVERITY_BY_SEVERITY: dict[str, str] = {
    "BLOCKER": "9.5",
    "CRITICAL": "8.0",
    "MAJOR": "5.5",
    "MINOR": "3.0",
    "INFO": "1.0",
}
SECURITY_SEVERITY_BY_PROBABILITY: dict[str, str] = {
    "HIGH": "8.0",
    "MEDIUM": "5.0",
    "LOW": "3.0",
}


def _sarif_security_severity(finding: Finding) -> str | None:
    """Return a 0-10 ``security-severity`` string for security findings.

    Only vulnerabilities and security hotspots carry one; bugs and code
    smells return ``None`` so GitHub does not badge them as security
    alerts.
    """
    if finding.type == "SECURITY_HOTSPOT":
        return SECURITY_SEVERITY_BY_PROBABILITY.get((finding.vulnerability_probability or "").upper())
    if finding.type == "VULNERABILITY":
        return SECURITY_SEVERITY_BY_SEVERITY.get(finding.severity)
    return None


def _sarif_level(finding: Finding) -> str:
    """Map a finding to a SARIF result level (error / warning / note)."""
    if finding.type == "SECURITY_HOTSPOT":
        probability = (finding.vulnerability_probability or "").upper()
        if probability == "HIGH":
            return "error"
        if probability == "MEDIUM":
            return "warning"
        return "note"
    if finding.type == "VULNERABILITY" and finding.severity in ("BLOCKER", "CRITICAL"):
        return "error"
    if finding.severity == "MAJOR":
        return "warning"
    return "note"


def build_sarif(findings: Sequence[Finding], *, host: str) -> dict[str, Any]:
    """Build a SARIF 2.1.0 document from Sonar findings.

    Deterministic: rules are sorted by id and results by a stable
    (uri, startLine, ruleId, key) tuple, so an unchanged finding set
    yields byte-identical output on re-run.
    """
    host = host.rstrip("/")

    # Aggregate one rule object per unique rule key.
    rule_state: dict[str, dict[str, Any]] = {}
    for finding in findings:
        key = finding.rule
        entry = rule_state.setdefault(key, {"name": key, "tags": set(), "security": None})
        if finding.rule_name and entry["name"] == key:
            entry["name"] = finding.rule_name
        for tag in finding.tags:
            entry["tags"].add(tag)
        severity = _sarif_security_severity(finding)
        if severity is not None:
            current = entry["security"]
            # Keep the highest security-severity seen for the rule.
            if current is None or float(severity) > float(current):
                entry["security"] = severity

    rules: list[dict[str, Any]] = []
    for key in sorted(rule_state):
        entry = rule_state[key]
        properties: dict[str, Any] = {"tags": sorted(entry["tags"])}
        if entry["security"] is not None:
            properties["security-severity"] = entry["security"]
        rules.append(
            {
                "id": key,
                "name": entry["name"],
                "shortDescription": {"text": entry["name"]},
                "helpUri": f"{host}/coding_rules?open={key}&rule_key={key}",
                "properties": properties,
            }
        )

    results: list[dict[str, Any]] = []
    for finding in findings:
        uri = _component_path(finding.component)
        line = finding.line if (finding.line is not None and finding.line > 0) else 1
        results.append(
            {
                "ruleId": finding.rule,
                "level": _sarif_level(finding),
                "message": {"text": finding.message or finding.rule},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": uri},
                            "region": {"startLine": line},
                        }
                    }
                ],
                "partialFingerprints": {"sonarFindingKey": finding.key},
            }
        )
    results.sort(
        key=lambda r: (
            r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"],
            r["locations"][0]["physicalLocation"]["region"]["startLine"],
            r["ruleId"],
            r["partialFingerprints"]["sonarFindingKey"],
        )
    )

    return {
        "$schema": SARIF_SCHEMA_URI,
        "version": SARIF_VERSION,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "SonarQube",
                        "informationUri": host,
                        "rules": rules,
                    }
                },
                "results": results,
            }
        ],
    }


def _load_sarif_fixture(path: Path) -> list[Finding]:
    """Load findings for the SARIF path from a saved JSON fixture.

    Accepts a payload with an ``issues`` array (Sonar issue shape), an
    optional ``hotspots`` array (Sonar hotspot shape), and an optional
    ``rules`` array (``{key, name}``) used to fill rule names.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return []
    rules_by_key: dict[str, str] = {}
    for rule_obj in payload.get("rules") or []:
        if isinstance(rule_obj, dict) and isinstance(rule_obj.get("key"), str):
            name = rule_obj.get("name")
            rules_by_key[rule_obj["key"]] = name if isinstance(name, str) else ""
    findings: list[Finding] = []
    for raw in payload.get("issues") or []:
        if isinstance(raw, dict):
            finding = _normalise_issue(raw, rules_by_key=rules_by_key)
            if finding is not None:
                findings.append(finding)
    for raw in payload.get("hotspots") or []:
        if isinstance(raw, dict):
            finding = _normalise_hotspot(raw)
            if finding is not None:
                findings.append(finding)
    return findings


def _resolve_sarif_host(config: SonarConfig | None) -> str:
    """Pick the host used for ``informationUri`` and rule ``helpUri``."""
    if config is not None:
        return config.host
    env_host = (os.environ.get("SONAR_HOST_URL") or "").strip().rstrip("/")
    return env_host or "https://sonarqube.invalid"


def run_emit_sarif(args: argparse.Namespace) -> int:
    """Fetch all findings and write a SARIF document. No tickets, no issues."""
    out_path = Path(args.emit_sarif)
    if args.fixture:
        try:
            findings = _load_sarif_fixture(Path(args.fixture))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"error: failed to load fixture: {exc}", file=sys.stderr)
            return 2
        host = _resolve_sarif_host(load_config())
    else:
        config = load_config()
        if config is None:
            print("error: SONAR_HOST_URL and SONAR_TOKEN must be set", file=sys.stderr)
            return 2
        try:
            findings = collect_sarif_findings(config)
        except SonarAPIError as exc:
            print(f"error: sonar fetch failed: {exc}", file=sys.stderr)
            return 1
        host = config.host

    sarif = build_sarif(findings, host=host)
    if out_path.parent and not out_path.parent.exists():
        out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(sarif, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    n_rules = len(sarif["runs"][0]["tool"]["driver"]["rules"])
    n_results = len(sarif["runs"][0]["results"])
    print(f"sonar-sarif: findings={len(findings)} rules={n_rules} results={n_results} out={out_path}")
    return 0


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def _today_utc() -> str:
    return _dt.datetime.now(tz=_dt.UTC).strftime("%Y-%m-%d")


def _load_fixture(path: Path) -> list[Finding]:
    """Load Sonar findings from a saved JSON fixture (for tests and dry-run)."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    issues = payload.get("issues") if isinstance(payload, dict) else None
    if not isinstance(issues, list):
        return []
    out: list[Finding] = []
    for raw in issues:
        if isinstance(raw, dict):
            f = _normalise_issue(raw)
            if f is not None:
                out.append(f)
    return out


def run_sweep(args: argparse.Namespace) -> int:
    """End-to-end sweep: fetch, filter, cap, dedup, emit."""
    out_dir = Path(args.out_dir)
    backlog_roots: tuple[Path, ...]
    if args.backlog_root:
        # When a single root is given (used in tests), still walk all sub-states
        # if they exist next to it.
        root = Path(args.backlog_root)
        backlog_roots = (
            root / "open",
            root / "claimed",
            root / "closed",
            root / "done",
            root / "deferred",
        )
    else:
        backlog_roots = DEFAULT_BACKLOG_ROOTS

    day = args.day or _today_utc()

    # Load findings: fixture path bypasses network entirely.
    if args.fixture:
        try:
            findings = _load_fixture(Path(args.fixture))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"error: failed to load fixture: {exc}", file=sys.stderr)
            return 2
    else:
        config = load_config()
        if config is None:
            print(
                "error: SONAR_HOST_URL and SONAR_TOKEN must be set",
                file=sys.stderr,
            )
            return 2
        severities = _selected_severities(args.severity_min)
        try:
            findings = fetch_findings(config, severities=severities)
        except SonarAPIError as exc:
            print(f"error: sonar fetch failed: {exc}", file=sys.stderr)
            return 1

    filtered = filter_by_severity(findings, args.severity_min)

    # Build the dedup index after we have the candidate set so we can also
    # report on the (rule, component, line) fallback.
    index = build_dedup_index(backlog_roots)

    deduped: list[Finding] = []
    for f in filtered:
        if f.key in index.keys:
            continue
        if (f.rule, _component_path(f.component), f.line) in index.open_rule_component_line:
            continue
        deduped.append(f)

    capped = cap_per_day(deduped, args.max_per_day)

    emitted: list[tuple[Path, Finding]] = []
    skipped_existing = 0
    for f in capped:
        path, body, wrote = emit_ticket(f, out_dir, day=day, dry_run=args.dry_run)
        if args.dry_run:
            print(f"would-emit {path}")
            emitted.append((path, f))
            continue
        if not wrote:
            print(f"skipped (file already exists) {path}")
            skipped_existing += 1
            continue
        emitted.append((path, f))
        if args.create_gh_issues:
            priority = SEVERITY_TO_PRIORITY.get(f.severity, "P2")
            url = maybe_create_gh_issue(
                path,
                _ticket_title(f),
                enable=True,
                priority=priority,
            )
            if url:
                _append_gh_trailer(path, url)
        del body  # already written

    print(
        f"sonar-sweep: fetched={len(findings)} filtered={len(filtered)} "
        f"deduped={len(deduped)} capped={len(capped)} emitted={len(emitted)} "
        f"skipped_existing={skipped_existing} dry_run={args.dry_run}"
    )
    return 0


def _selected_severities(severity_min: str) -> list[str]:
    """Translate ``--severity-min`` to the Sonar API ``severities`` list."""
    rank = SEVERITY_RANK.get(severity_min.upper(), 0)
    return [sev for sev in SEVERITY_ORDER if SEVERITY_RANK[sev] <= rank]


def _append_gh_trailer(path: Path, url: str) -> None:
    """Append a ``gh-issue-created`` HTML comment trailer to the ticket file."""
    now = _dt.datetime.now(tz=_dt.UTC).isoformat(timespec="seconds")
    trailer = f"<!-- gh-issue-created: {url} {now} -->\n"
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(trailer)
    except OSError:
        return


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--severity-min",
        default="MAJOR",
        choices=list(SEVERITY_ORDER),
        help="Minimum severity to include (default: MAJOR).",
    )
    p.add_argument(
        "--max-per-day",
        type=int,
        default=25,
        help="Maximum tickets to emit in this run (default: 25).",
    )
    p.add_argument(
        "--out-dir",
        default=str(DEFAULT_OUT_DIR),
        help="Output directory for ticket files.",
    )
    p.add_argument(
        "--backlog-root",
        default=None,
        help=(
            "Root that contains open/, claimed/, closed/, done/, deferred/ "
            "subfolders. When set, overrides the default .sdd/backlog/* roots. "
            "Used by tests to isolate against tmp dirs."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print would-be ticket paths without writing files.",
    )
    p.add_argument(
        "--create-gh-issues",
        action="store_true",
        help=(
            "For P0 and P1 tickets (BLOCKER, CRITICAL, MAJOR), also call "
            "`gh issue create` and trailer the file with the issue URL."
        ),
    )
    p.add_argument(
        "--fixture",
        default=None,
        help=("Read findings from a saved JSON fixture instead of calling Sonar. Useful for local dry-runs and tests."),
    )
    p.add_argument(
        "--day",
        default=None,
        help="Override the UTC day used in filenames and ticket ids (YYYY-MM-DD).",
    )
    p.add_argument(
        "--emit-sarif",
        default=None,
        metavar="PATH",
        help=(
            "Write all findings (issues of every type plus security "
            "hotspots) as a SARIF 2.1.0 document at PATH instead of "
            "emitting backlog tickets. Feeds GitHub code scanning."
        ),
    )
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if getattr(args, "emit_sarif", None):
        return run_emit_sarif(args)
    return run_sweep(args)


if __name__ == "__main__":
    sys.exit(main())
