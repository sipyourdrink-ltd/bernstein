"""Immutable declaration custody and revocation controls, using local Git."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "maintainer_adoption.py"


@pytest.fixture(scope="module")
def adoption():
    spec = importlib.util.spec_from_file_location("maintainer_adoption_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def report(adoption, identity="review-1", *, checks=None):
    body = {
        "execution_id": identity,
        "head_sha": "b" * 40,
        "outcome": "PASS",
        "findings": [],
        "checks": ["local deterministic controls"] if checks is None else checks,
        "summary": "Exact-head review passed.",
    }
    return {"report": body, "report_sha256": adoption.digest(body)}


def payload(adoption):
    return {
        "subject": {
            "repository": "team/project",
            "pull_request": 42,
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            "policy_sha256": "c" * 64,
        },
        "producer_execution_ids": ["producer-1"],
        "reviews": [report(adoption), report(adoption, "review-2")],
        "security_tests": report(adoption, "security-1"),
    }


def record(adoption, data=None, *, operation="adopt"):
    row = {"schema": adoption.SCHEMA, "operation": operation, "payload": payload(adoption) if data is None else data}
    row["record_id"] = adoption.digest(row)
    return row


def line(adoption, row):
    return adoption.canonical_bytes(row) + b"\n"


def test_immutable_declaration_binds_subject_and_report_content(adoption):
    row = record(adoption)
    ledger = adoption.replay([("1" * 40, line(adoption, row))], trusted_commit="1" * 40)
    subject = adoption.Subject.parse(row["payload"]["subject"])
    declaration = ledger.find(subject)
    assert declaration.declaration_id == row["record_id"]
    assert declaration.accepted_commit == "1" * 40
    assert ledger.find(adoption.Subject("other/project", 42, "a" * 40, "b" * 40, "c" * 64)) is None


@pytest.mark.parametrize("mutation", ["edit", "delete", "truncate", "reformat"])
def test_accepted_declaration_bytes_cannot_be_replaced(adoption, mutation):
    original = line(adoption, record(adoption))
    changed = {
        "edit": original.replace(b"Exact-head", b"Different"),
        "delete": b"",
        "truncate": original[:-1],
        "reformat": b" " + original,
    }[mutation]
    with pytest.raises(ValueError, match="prefix"):
        adoption.replay([("1" * 40, original), ("2" * 40, changed)], trusted_commit="2" * 40)


def test_revocation_is_permanent_and_old_declaration_remains(adoption):
    row = record(adoption)
    original = line(adoption, row)
    revocation = record(
        adoption, {"declaration_id": row["record_id"], "reason": "Maintainer withdrew adoption"}, operation="revoke"
    )
    current = original + line(adoption, revocation)
    ledger = adoption.replay([("1" * 40, original), ("2" * 40, current)], trusted_commit="2" * 40)
    assert len(ledger.declarations) == 1
    assert ledger.find(adoption.Subject.parse(row["payload"]["subject"])) is None
    with pytest.raises(ValueError, match="prefix"):
        adoption.replay([("1" * 40, original), ("2" * 40, current), ("3" * 40, original)], trusted_commit="3" * 40)
    replacement = payload(adoption)
    replacement["subject"]["base_sha"] = "e" * 40
    with pytest.raises(ValueError, match="revoked head"):
        adoption.replay(
            [("2" * 40, current), ("3" * 40, current + line(adoption, record(adoption, replacement)))],
            trusted_commit="3" * 40,
        )


@pytest.mark.parametrize(
    "case", ["duplicate-review", "producer-review", "wrong-head", "report-digest", "findings", "security-empty"]
)
def test_invalid_review_evidence_never_qualifies(adoption, case):
    data = payload(adoption)
    if case == "duplicate-review":
        data["reviews"][1] = data["reviews"][0]
    elif case == "producer-review":
        data["reviews"][0] = report(adoption, "producer-1")
    elif case == "wrong-head":
        data["subject"]["head_sha"] = "d" * 40
    elif case == "report-digest":
        data["reviews"][0]["report_sha256"] = "f" * 64
    elif case == "findings":
        data["reviews"][0]["report"]["findings"] = ["unresolved"]
        data["reviews"][0]["report_sha256"] = adoption.digest(data["reviews"][0]["report"])
    elif case == "security-empty":
        data["security_tests"] = report(adoption, "security-1", checks=[])
    with pytest.raises(ValueError):
        adoption.replay([("1" * 40, line(adoption, record(adoption, data)))], trusted_commit="1" * 40)


def test_noncanonical_duplicate_and_unknown_version_fail_closed(adoption):
    row = record(adoption)
    bad = json.dumps(row).encode() + b"\n"
    with pytest.raises(ValueError):
        adoption.replay([("1" * 40, bad)], trusted_commit="1" * 40)
    duplicate = line(adoption, row)
    with pytest.raises(ValueError, match="duplicate"):
        adoption.replay([("1" * 40, duplicate + duplicate)], trusted_commit="1" * 40)
    row["schema"] = "bernstein.maintainer-adoption.v99"
    with pytest.raises(ValueError):
        adoption.replay([("1" * 40, line(adoption, row))], trusted_commit="1" * 40)


def git(repo, *args):
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def commit(repo, contents, adoption):
    path = repo / adoption.LEDGER_PATH
    path.parent.mkdir(exist_ok=True)
    if contents is None:
        path.unlink()
    else:
        path.write_bytes(contents)
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "accepted ledger revision")
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", adoption.TRUSTED_DEFAULT_REF, sha)
    return sha


def test_real_git_history_retains_revocation_and_rejects_removed_ledger(adoption, tmp_path):
    git(tmp_path, "init", "--initial-branch=main")
    git(tmp_path, "config", "user.name", "Fixture")
    git(tmp_path, "config", "user.email", "fixture@example.invalid")
    row = record(adoption)
    original = line(adoption, row)
    first = commit(tmp_path, original, adoption)
    assert adoption.load_ledger(tmp_path, first).find(adoption.Subject.parse(row["payload"]["subject"])) is not None
    revoked = original + line(
        adoption, record(adoption, {"declaration_id": row["record_id"], "reason": "Withdrawn"}, operation="revoke")
    )
    second = commit(tmp_path, revoked, adoption)
    with pytest.raises(ValueError, match="trusted default-branch"):
        adoption.load_ledger(tmp_path, first)
    assert adoption.load_ledger(tmp_path, second).find(adoption.Subject.parse(row["payload"]["subject"])) is None
    deleted = commit(tmp_path, None, adoption)
    with pytest.raises(ValueError, match="history unavailable"):
        adoption.load_ledger(tmp_path, deleted)


def test_reader_is_not_an_active_quorum_or_publishing_path(adoption):
    for name in (
        "scripts/quorum_check.py",
        "scripts/publish_required_check.py",
        ".github/workflows/quorum.yml",
        ".github/workflows/quorum-rerun.yml",
    ):
        assert "maintainer_adoption" not in (ROOT / name).read_text()
    assert not (ROOT / adoption.LEDGER_PATH).exists()


def test_policy_digest_covers_reader_and_privileged_publisher(adoption, tmp_path):
    for name in adoption.POLICY_PATHS:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)
    original = adoption.policy_digest(tmp_path)
    (tmp_path / "scripts/publish_required_check.py").write_text("changed publisher")
    assert adoption.policy_digest(tmp_path) != original
