"""Validate maintainer-attested technical reviews, without executing their data.

The trust root is the configured maintainer's authenticated GitHub comment.
Digests prove the embedded reports have not changed; they do not prove provider
identity, execution or independence. The maintainer's trusted controller must
verify actual terminal reports and producer identities before publishing.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MARKER = "<!-- bernstein-maintainer-adoption:v1 -->\n"
SCHEMA = "bernstein.maintainer-adoption.v1"
POLICY_PATHS = (
    "scripts/quorum_check.py",
    "scripts/quorum_evidence.py",
    "scripts/quorum_request_reviews.py",
    "scripts/queue_hygiene.py",
    ".github/quorum-roster.toml",
    ".github/CODEOWNERS",
    ".github/workflows/quorum.yml",
    ".github/workflows/quorum-rerun.yml",
    "docs/governance/review-charter.md",
)
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_EXECUTION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}\Z")
_FIELDS = {
    "schema",
    "repository",
    "pull_request",
    "base_sha",
    "head_sha",
    "policy_sha256",
    "producer_execution_ids",
    "reviews",
    "security_tests",
}
_REPORT_FIELDS = {"execution_id", "head_sha", "outcome", "findings", "checks", "summary"}


def canonical_bytes(value: Any) -> bytes:
    """The public report-digest encoding: sorted compact UTF-8 JSON, no NaN."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def report_digest(report: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(report)).hexdigest()


def policy_digest(root: str | Path) -> str:
    """Bind all trusted policy inputs; unrelated main commits do not change it."""
    root = Path(root)
    files = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in POLICY_PATHS}
    return hashlib.sha256(canonical_bytes(files)).hexdigest()


@dataclass(frozen=True)
class Evidence:
    valid: bool
    reason: str
    url: str = ""
    reviewer_ids: tuple[str, ...] = ()


def _object(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{label}: exact schema fields required")
    return value


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not _EXECUTION_ID.fullmatch(value):
        raise ValueError("invalid execution identity")
    return value.casefold()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for key, value in pairs:
        if key in row:
            raise ValueError("duplicate JSON key")
        row[key] = value
    return row


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-JSON constant {value}")


def _report(envelope: Any, head: str, *, security: bool = False) -> str:
    envelope = _object(envelope, {"report", "report_sha256"}, "report envelope")
    report = _object(envelope["report"], _REPORT_FIELDS, "terminal report")
    digest = envelope["report_sha256"]
    if not isinstance(digest, str) or not _HEX64.fullmatch(digest) or digest != report_digest(report):
        raise ValueError("terminal report digest mismatch")
    identity = _identifier(report["execution_id"])
    if report["head_sha"] != head:
        raise ValueError("terminal report does not cover the exact head")
    if report["outcome"] != "PASS":
        raise ValueError("terminal report is not PASS; findings/errors remain blocking")
    if report["findings"] != []:
        raise ValueError("terminal report contains unresolved findings")
    checks = report["checks"]
    if (
        not isinstance(checks, list)
        or len(checks) > 100
        or any(not isinstance(check, str) or not check.strip() or len(check) > 4000 for check in checks)
    ):
        raise ValueError("invalid terminal report checks")
    if security and not checks:
        raise ValueError("sensitive integration needs executed security-test commands")
    summary = report["summary"]
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 8000:
        raise ValueError("terminal report needs a bounded substantive summary")
    return identity


def validate(
    body: str,
    *,
    repository: str,
    number: int,
    base_sha: str,
    head_sha: str,
    policy_sha256: str,
    authors: set[str],
    required_reviews: int,
) -> tuple[str, ...]:
    """Validate inert comment data; no URL, command or report is executed."""
    if len(body.encode("utf-8")) > 60_000 or not body.startswith(MARKER):
        raise ValueError("invalid attestation marker or size")
    row = _object(
        json.loads(body[len(MARKER) :], object_pairs_hook=_unique_object, parse_constant=_reject_constant),
        _FIELDS,
        "attestation",
    )
    if row["schema"] != SCHEMA or row["repository"] != repository:
        raise ValueError("attestation repository/schema mismatch")
    if type(row["pull_request"]) is not int or row["pull_request"] != number:
        raise ValueError("attestation pull request mismatch")
    for key, expected, pattern in (
        ("base_sha", base_sha, _HEX40),
        ("head_sha", head_sha, _HEX40),
        ("policy_sha256", policy_sha256, _HEX64),
    ):
        value = row[key]
        if not isinstance(value, str) or not pattern.fullmatch(value) or value != expected:
            raise ValueError(f"attestation {key} is stale or mismatched")
    producers = row["producer_execution_ids"]
    if not isinstance(producers, list) or not 1 <= len(producers) <= 64:
        raise ValueError("actual producer execution identities are required")
    producer_ids = {_identifier(value) for value in producers}
    if len(producer_ids) != len(producers):
        raise ValueError("duplicate producer execution identities")
    excluded = producer_ids | {author.casefold() for author in authors}
    reviews = row["reviews"]
    if not isinstance(reviews, list) or not required_reviews <= len(reviews) <= 8:
        raise ValueError(f"at least {required_reviews} independent technical reviews required")
    identities = tuple(_report(report, head_sha) for report in reviews)
    if len(set(identities)) != len(identities) or excluded.intersection(identities):
        raise ValueError("reviewer identities must be distinct and independent of authors/producers")
    security = row["security_tests"]
    if required_reviews >= 2 or security is not None:
        _report(security, head_sha, security=True)
    return identities


def authorized_comment_event(event: dict[str, Any], *, maintainer: str, maintainer_id: int) -> bool:
    """Authenticate a trusted issue_comment event before publishing a verdict.

    Recompute all maintainer edits/deletions: a removed marker must invalidate
    an earlier successful check too. The workflow resolves the configured
    maintainer's ID through the fresh API, never through comment body data.
    """
    issue = event.get("issue") or {}
    comment = event.get("comment") or {}
    user = comment.get("user") or {}
    if (
        not issue.get("pull_request")
        or not isinstance(user.get("login"), str)
        or user["login"].casefold() != maintainer.casefold()
        or type(maintainer_id) is not int
        or maintainer_id <= 0
        or type(user.get("id")) is not int
        or user["id"] != maintainer_id
    ):
        return False
    action = event.get("action")
    if action in {"edited", "deleted"}:
        return True
    body = comment.get("body")
    return action == "created" and isinstance(body, str) and body.startswith(MARKER)


def from_comments(
    comments: list[dict[str, Any]],
    *,
    maintainer: str,
    maintainer_id: int,
    **bindings: Any,
) -> Evidence | None:
    """Latest authenticated declaration wins, including an invalid/revoked one.

    Other users cannot grant an exception or invalidate one by copying its
    marker. Comment authors and the maintainer id come from fresh GitHub API
    responses, never from the JSON payload or the pull request body.
    """
    candidates = [
        comment
        for comment in comments
        if isinstance(comment.get("body"), str)
        and comment["body"].startswith(MARKER)
        and (comment.get("user") or {}).get("login", "").casefold() == maintainer.casefold()
        and type((comment.get("user") or {}).get("id")) is int
        and (comment.get("user") or {})["id"] == maintainer_id
        and type(maintainer_id) is int
        and maintainer_id > 0
    ]
    if not candidates:
        return None
    latest = max(candidates, key=lambda c: (c.get("updated_at") or c.get("created_at") or "", c.get("id") or 0))
    url = latest.get("html_url") or ""
    try:
        identities = validate(latest["body"], **bindings)
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        return Evidence(False, str(exc), url)
    return Evidence(True, "authenticated maintainer attestation; report digests verified", url, identities)
