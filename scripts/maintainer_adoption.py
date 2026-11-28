#!/usr/bin/env python3
"""Inspect immutable maintainer declarations accepted on protected main.

This is an offline evidence reader, not a quorum exception or check publisher.
The caller must pin a commit from the repository's ordinarily reviewed default
branch. Git author fields, comments and candidate-branch files confer no trust.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

LEDGER_PATH = ".github/maintainer-adoption-ledger.jsonl"
TRUSTED_DEFAULT_REF = "refs/remotes/origin/main"
SCHEMA = "bernstein.maintainer-adoption.v1"
POLICY_PATHS = (
    "scripts/maintainer_adoption.py",
    "scripts/quorum_check.py",
    "scripts/quorum_request_reviews.py",
    "scripts/queue_hygiene.py",
    "scripts/publish_required_check.py",
    ".github/quorum-roster.toml",
    ".github/CODEOWNERS",
    ".github/workflows/quorum.yml",
    ".github/workflows/quorum-rerun.yml",
    "docs/governance/review-charter.md",
    "docs/governance/maintainer-adoption.md",
)
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}\Z")
_MAX_BYTES = 16 * 1024 * 1024
_MAX_REVISIONS = 10_000
_MAX_HISTORY_BYTES = 128 * 1024 * 1024


def canonical_bytes(value: Any) -> bytes:
    """Canonical inert JSON, rejecting non-finite numbers."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def policy_digest(root: Path) -> str:
    """Bind every policy and publication input, without a ledger self-reference."""
    return digest({name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in POLICY_PATHS})


def _object(value: Any, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("exact declaration schema required")
    return value


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError("invalid declaration identity")
    return value.casefold()


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate declaration key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")


@dataclass(frozen=True)
class Subject:
    repository: str
    pull_request: int
    base_sha: str
    head_sha: str
    policy_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.repository, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository
        ):
            raise ValueError("invalid declaration repository")
        if type(self.pull_request) is not int or self.pull_request <= 0:
            raise ValueError("invalid declaration pull request")
        if any(not isinstance(value, str) or not _SHA.fullmatch(value) for value in (self.base_sha, self.head_sha)):
            raise ValueError("invalid declaration commit")
        if not isinstance(self.policy_sha256, str) or not _DIGEST.fullmatch(self.policy_sha256):
            raise ValueError("invalid declaration policy digest")
        # GitHub repository identity is case-insensitive, including direct API use.
        object.__setattr__(self, "repository", self.repository.casefold())

    @classmethod
    def parse(cls, value: Any) -> Subject:
        return cls(**_object(value, {"repository", "pull_request", "base_sha", "head_sha", "policy_sha256"}))

    @property
    def revocation_key(self) -> tuple[str, int, str]:
        """A revoked head stays revoked even if the base or policy later moves."""
        return self.repository, self.pull_request, self.head_sha


def _report(value: Any, subject: Subject, *, security: bool = False) -> str:
    envelope = _object(value, {"report", "report_sha256"})
    row = _object(envelope["report"], {"execution_id", "head_sha", "outcome", "findings", "checks", "summary"})
    identity = _identifier(row["execution_id"])
    if row["head_sha"] != subject.head_sha or row["outcome"] != "PASS":
        raise ValueError("review must pass on the exact declaration head")
    if envelope["report_sha256"] != digest(row):
        raise ValueError("invalid report content digest")
    if row["findings"] != []:
        raise ValueError("unresolved report findings remain blocking")
    checks = row["checks"]
    if (
        not isinstance(checks, list)
        or len(checks) > 100
        or any(not isinstance(check, str) or not check.strip() or len(check) > 4000 for check in checks)
        or (security and not checks)
    ):
        raise ValueError("bounded executed security checks required")
    if not isinstance(row["summary"], str) or not row["summary"].strip() or len(row["summary"]) > 8000:
        raise ValueError("bounded substantive report required")
    return identity


@dataclass(frozen=True)
class Declaration:
    declaration_id: str
    subject: Subject
    producer_ids: frozenset[str]
    reviewer_ids: tuple[str, ...]
    security_execution_id: str
    accepted_commit: str


@dataclass(frozen=True)
class Ledger:
    declarations: tuple[Declaration, ...]
    revoked_heads: frozenset[tuple[str, int, str]]
    pinned_commit_sha: str

    def find(self, subject: Subject) -> Declaration | None:
        if subject.revocation_key in self.revoked_heads:
            return None
        return next((row for row in reversed(self.declarations) if row.subject == subject), None)


def replay(snapshots: Iterable[tuple[str, bytes]], *, pinned_commit_sha: str) -> Ledger:
    """Require append-only accepted snapshots and replay permanent revocations.

    Snapshot order is the protected default branch's first-parent history.
    Every prior byte must remain present. A malformed later snapshot invalidates
    the whole ledger rather than revealing an earlier declaration.
    """
    previous = b""
    declarations: list[Declaration] = []
    by_id: dict[str, Declaration] = {}
    seen_records: set[str] = set()
    revoked: set[tuple[str, int, str]] = set()
    total_bytes = 0
    for revision, (commit, content) in enumerate(snapshots, start=1):
        total_bytes += len(content)
        if revision > _MAX_REVISIONS or total_bytes > _MAX_HISTORY_BYTES:
            raise ValueError("declaration history exceeds inspection bound")
        if not _SHA.fullmatch(commit) or len(content) > _MAX_BYTES:
            raise ValueError("invalid accepted ledger snapshot")
        if not content.startswith(previous) or (content and not content.endswith(b"\n")):
            raise ValueError("immutable ledger prefix changed")
        for line in content[len(previous) :].splitlines():
            row = json.loads(line, object_pairs_hook=_unique, parse_constant=_invalid_constant)
            _object(row, {"schema", "record_id", "operation", "payload"})
            if canonical_bytes(row) != line or row["schema"] != SCHEMA:
                raise ValueError("noncanonical or unsupported declaration")
            record_id = row["record_id"]
            body = {key: value for key, value in row.items() if key != "record_id"}
            if not isinstance(record_id, str) or record_id != digest(body) or record_id in seen_records:
                raise ValueError("invalid or duplicate declaration content identity")
            seen_records.add(record_id)
            if row["operation"] == "adopt":
                payload = _object(row["payload"], {"subject", "producer_execution_ids", "reviews", "security_tests"})
                subject = Subject.parse(payload["subject"])
                producers = payload["producer_execution_ids"]
                if not isinstance(producers, list) or not 1 <= len(producers) <= 64:
                    raise ValueError("producer execution identities required")
                producer_ids = frozenset(_identifier(value) for value in producers)
                reviews = payload["reviews"]
                if len(producer_ids) != len(producers) or not isinstance(reviews, list) or not 2 <= len(reviews) <= 8:
                    raise ValueError("two distinct independent reviews required")
                reviewers = tuple(_report(value, subject) for value in reviews)
                security_id = _report(payload["security_tests"], subject, security=True)
                if len(set(reviewers)) != len(reviewers) or producer_ids.intersection(reviewers):
                    raise ValueError("review execution identities must be independent")
                if security_id in producer_ids or security_id in reviewers:
                    raise ValueError("security execution identity must be independent")
                if subject.revocation_key in revoked:
                    raise ValueError("a revoked head requires a new revision and review")
                declaration = Declaration(record_id, subject, producer_ids, reviewers, security_id, commit)
                declarations.append(declaration)
                by_id[record_id] = declaration
            elif row["operation"] == "revoke":
                payload = _object(row["payload"], {"declaration_id", "reason"})
                target = payload["declaration_id"]
                if not isinstance(target, str) or target not in by_id:
                    raise ValueError("revocation must name an accepted declaration")
                if (
                    not isinstance(payload["reason"], str)
                    or not payload["reason"].strip()
                    or len(payload["reason"]) > 4000
                ):
                    raise ValueError("bounded revocation reason required")
                revoked.add(by_id[target].subject.revocation_key)
            else:
                raise ValueError("unsupported declaration operation")
        previous = content
    return Ledger(tuple(declarations), frozenset(revoked), pinned_commit_sha)


def _git(repo: Path, *args: str) -> bytes:
    environment = os.environ.copy()
    environment.update(
        GIT_NO_LAZY_FETCH="1",
        GIT_ALLOW_PROTOCOL="",
        GIT_TERMINAL_PROMPT="0",
        GIT_GRAFT_FILE=os.devnull,
    )
    # Unsupported --no-lazy-fetch also fails closed; no legacy transport fallback.
    result = subprocess.run(
        [
            "git",
            "--no-lazy-fetch",
            "--no-replace-objects",
            "-c",
            "core.commitGraph=false",
            "-c",
            "log.showSignature=false",
            *args,
        ],
        cwd=repo,
        env=environment,
        capture_output=True,
        timeout=60,
    )
    if result.returncode:
        raise ValueError("trusted declaration history unavailable")
    return result.stdout


def load_ledger(repo: Path, pinned_commit_sha: str) -> Ledger:
    """Read accepted history only; no network, candidate checkout or comment input."""
    if not _SHA.fullmatch(pinned_commit_sha):
        raise ValueError("an exact trusted default-branch commit is required")
    current = _git(repo, "rev-parse", "--verify", f"{TRUSTED_DEFAULT_REF}^{{commit}}").decode("ascii").strip()
    if pinned_commit_sha != current:
        raise ValueError("pin differs from the trusted default-branch snapshot")
    if _git(repo, "rev-parse", "--is-shallow-repository").strip() != b"false":
        raise ValueError("complete accepted declaration history is required")
    _git(repo, "cat-file", "-e", f"{pinned_commit_sha}^{{commit}}")
    commits = _git(
        repo,
        "log",
        "--first-parent",
        "--full-history",
        "--reverse",
        f"--max-count={_MAX_REVISIONS + 1}",
        "--format=%H",
        pinned_commit_sha,
        "--",
        LEDGER_PATH,
    )
    history = commits.decode("ascii").splitlines()
    if len(history) > _MAX_REVISIONS:
        raise ValueError("declaration history exceeds inspection bound")

    def snapshots() -> Iterable[tuple[str, bytes]]:
        total_bytes = 0
        for commit in history:
            size = int(_git(repo, "cat-file", "-s", f"{commit}:{LEDGER_PATH}"))
            total_bytes += size
            if size < 0 or size > _MAX_BYTES or total_bytes > _MAX_HISTORY_BYTES:
                raise ValueError("declaration ledger exceeds inspection size bound")
            yield commit, _git(repo, "show", f"{commit}:{LEDGER_PATH}")

    return replay(snapshots(), pinned_commit_sha=pinned_commit_sha)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--trusted-commit", dest="pinned_commit_sha", required=True)
    args = parser.parse_args()
    try:
        ledger = load_ledger(args.repo, args.pinned_commit_sha)
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, f"declaration evidence unavailable: {exc}\n")
    print(
        json.dumps(
            {
                "trusted_commit": ledger.pinned_commit_sha,
                "declarations": len(ledger.declarations),
                "revoked_heads": len(ledger.revoked_heads),
                "grants_merge_authority": False,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
