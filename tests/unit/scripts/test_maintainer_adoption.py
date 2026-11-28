"""Immutable declaration custody and revocation controls, using local Git."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
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
    ledger = adoption.replay([("1" * 40, line(adoption, row))], pinned_commit_sha="1" * 40)
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
        adoption.replay([("1" * 40, original), ("2" * 40, changed)], pinned_commit_sha="2" * 40)


def test_revocation_is_permanent_and_old_declaration_remains(adoption):
    row = record(adoption)
    original = line(adoption, row)
    revocation = record(
        adoption, {"declaration_id": row["record_id"], "reason": "Maintainer withdrew adoption"}, operation="revoke"
    )
    current = original + line(adoption, revocation)
    ledger = adoption.replay([("1" * 40, original), ("2" * 40, current)], pinned_commit_sha="2" * 40)
    assert len(ledger.declarations) == 1
    assert ledger.find(adoption.Subject.parse(row["payload"]["subject"])) is None
    with pytest.raises(ValueError, match="prefix"):
        adoption.replay([("1" * 40, original), ("2" * 40, current), ("3" * 40, original)], pinned_commit_sha="3" * 40)
    replacement = payload(adoption)
    replacement["subject"]["base_sha"] = "e" * 40
    with pytest.raises(ValueError, match="revoked head"):
        adoption.replay(
            [("2" * 40, current), ("3" * 40, current + line(adoption, record(adoption, replacement)))],
            pinned_commit_sha="3" * 40,
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
        adoption.replay([("1" * 40, line(adoption, record(adoption, data)))], pinned_commit_sha="1" * 40)


def test_noncanonical_duplicate_and_unknown_version_fail_closed(adoption):
    row = record(adoption)
    bad = json.dumps(row).encode() + b"\n"
    with pytest.raises(ValueError):
        adoption.replay([("1" * 40, bad)], pinned_commit_sha="1" * 40)
    duplicate = line(adoption, row)
    with pytest.raises(ValueError, match="duplicate"):
        adoption.replay([("1" * 40, duplicate + duplicate)], pinned_commit_sha="1" * 40)
    row["schema"] = "bernstein.maintainer-adoption.v99"
    with pytest.raises(ValueError):
        adoption.replay([("1" * 40, line(adoption, row))], pinned_commit_sha="1" * 40)


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


def init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")


def commit_policy_tree(repo: Path, adoption, overrides: dict[str, bytes | None] | None = None) -> str:
    overrides = overrides or {}
    for name in adoption.POLICY_PATHS:
        path = repo / name
        if name in overrides:
            content = overrides[name]
            if content is None:
                if path.exists() or path.is_symlink():
                    path.unlink()
                continue
        else:
            content = f"# policy file: {name}\nline2\n".encode()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "accepted policy snapshot")
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", adoption.TRUSTED_DEFAULT_REF, sha)
    return sha


def test_policy_digest_covers_reader_and_privileged_publisher(adoption, tmp_path):
    init_repo(tmp_path)
    first = commit_policy_tree(tmp_path, adoption)
    original = adoption.policy_digest(tmp_path, first)
    second = commit_policy_tree(
        tmp_path,
        adoption,
        overrides={"scripts/publish_required_check.py": b"changed publisher\n"},
    )
    assert adoption.policy_digest(tmp_path, second) != original


@pytest.mark.parametrize("repository", ["TEAM/Project", "team/PROJECT"])
def test_repository_identity_is_canonical_for_revocation_and_direct_lookup(adoption, repository):
    row = record(adoption)
    original = line(adoption, row)
    subject = adoption.Subject(repository, 42, "a" * 40, "b" * 40, "c" * 64)
    assert subject.repository == "team/project"
    assert adoption.replay([("1" * 40, original)], pinned_commit_sha="1" * 40).find(subject)
    revoked = original + line(
        adoption, record(adoption, {"declaration_id": row["record_id"], "reason": "Withdrawn"}, operation="revoke")
    )
    ledger = adoption.replay([("2" * 40, revoked)], pinned_commit_sha="2" * 40)
    assert ledger.find(subject) is None
    changed = payload(adoption)
    changed["subject"].update(repository=repository, base_sha="d" * 40, policy_sha256="e" * 64)
    with pytest.raises(ValueError, match="revoked head"):
        adoption.replay([("3" * 40, revoked + line(adoption, record(adoption, changed)))], pinned_commit_sha="3" * 40)


@pytest.mark.parametrize("override", [False, True])
def test_accepted_history_uses_original_ancestry(adoption, tmp_path, monkeypatch, override):
    git(tmp_path, "init", "--initial-branch=main")
    git(tmp_path, "config", "user.name", "Fixture")
    git(tmp_path, "config", "user.email", "fixture@example.invalid")
    row = record(adoption)
    original = line(adoption, row)
    commit(tmp_path, original, adoption)
    revoked = original + line(
        adoption, record(adoption, {"declaration_id": row["record_id"], "reason": "Withdrawn"}, operation="revoke")
    )
    commit(tmp_path, revoked, adoption)
    head = commit(tmp_path, original, adoption)
    graft = tmp_path / ("alternate-grafts" if override else ".git/info/grafts")
    graft.parent.mkdir(parents=True, exist_ok=True)
    graft.write_text(head + "\n")
    if override:
        monkeypatch.setenv("GIT_GRAFT_FILE", str(graft))
    with pytest.raises(ValueError, match="prefix"):
        adoption.load_ledger(tmp_path, head)


def test_missing_promisor_blob_stays_unavailable_without_transport(adoption, tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    source, clone = tmp_path / "source", tmp_path / "clone"
    source.mkdir()
    git(source, "init", "--initial-branch=main")
    git(source, "config", "user.name", "Fixture")
    git(source, "config", "user.email", "fixture@example.invalid")
    head = commit(source, line(adoption, record(adoption)), adoption)
    git(source, "config", "uploadpack.allowFilter", "true")
    git(tmp_path, "clone", "--filter=blob:none", "--no-checkout", source.as_uri(), str(clone))
    blob = git(source, "rev-parse", f"{head}:{adoption.LEDGER_PATH}")

    def absent():
        return (
            subprocess.run(
                ["git", "--no-lazy-fetch", "cat-file", "-e", blob],
                cwd=clone,
                capture_output=True,
                timeout=30,
            ).returncode
            != 0
        )

    assert absent()
    trace = tmp_path / "git.trace"
    monkeypatch.setenv("GIT_TRACE", str(trace))
    with pytest.raises(ValueError, match="history unavailable"):
        adoption.load_ledger(clone, head)
    events = trace.read_text()
    assert "fetch origin" not in events and "upload-pack" not in events
    assert absent()


def test_streaming_rejects_invalid_prefix_before_loading_later_snapshots(adoption, monkeypatch, tmp_path):
    original = line(adoption, record(adoption))
    commits = ["1" * 40, "2" * 40, "3" * 40]
    blobs = {commits[0]: original, commits[1]: b"", commits[2]: original}
    reads = []

    def fake_git(repo, *args):
        if args[0] == "rev-parse":
            return b"false" if args[1] == "--is-shallow-repository" else commits[-1].encode()
        if args[:2] == ("cat-file", "-e"):
            return b""
        if args[0] == "log":
            assert f"--max-count={adoption._MAX_REVISIONS + 1}" in args
            return "\n".join(commits).encode()
        sha = args[-1].split(":", 1)[0]
        if args[:2] == ("cat-file", "-s"):
            return str(len(blobs[sha])).encode()
        assert args[0] == "show"
        reads.append(sha)
        return blobs[sha]

    replay = adoption.replay

    def consume(snapshots, *, pinned_commit_sha):
        assert not isinstance(snapshots, (list, tuple)) and not reads
        return replay(snapshots, pinned_commit_sha=pinned_commit_sha)

    monkeypatch.setattr(adoption, "_git", fake_git)
    monkeypatch.setattr(adoption, "replay", consume)
    with pytest.raises(ValueError, match="prefix"):
        adoption.load_ledger(tmp_path, commits[-1])
    assert reads == commits[:2]


def test_history_limits_reject_excess_instead_of_truncating(adoption, tmp_path, monkeypatch):
    original = line(adoption, record(adoption))
    monkeypatch.setattr(adoption, "_MAX_HISTORY_BYTES", len(original) * 2 - 1)
    with pytest.raises(ValueError, match="inspection bound"):
        adoption.replay(iter([("1" * 40, original), ("2" * 40, original)]), pinned_commit_sha="2" * 40)
    monkeypatch.setattr(adoption, "_MAX_HISTORY_BYTES", len(original) * 10)
    monkeypatch.setattr(adoption, "_MAX_REVISIONS", 2)
    with pytest.raises(ValueError, match="inspection bound"):
        adoption.replay(iter([(str(i) * 40, original) for i in (1, 2, 3)]), pinned_commit_sha="3" * 40)

    def fake_git(repo, *args):
        if args[0] == "rev-parse":
            return b"false" if args[1] == "--is-shallow-repository" else b"3" * 40
        if args[:2] == ("cat-file", "-e"):
            return b""
        assert args[0] == "log" and "--max-count=3" in args
        return "\n".join(str(i) * 40 for i in (1, 2, 3)).encode()

    monkeypatch.setattr(adoption, "_git", fake_git)
    with pytest.raises(ValueError, match="inspection bound"):
        adoption.load_ledger(tmp_path, "3" * 40)


def test_unsupported_offline_git_capability_fails_closed(adoption, tmp_path, monkeypatch):
    def unsupported(command, **kwargs):
        assert "--no-lazy-fetch" in command
        assert kwargs["env"]["GIT_ALLOW_PROTOCOL"] == ""
        assert kwargs["env"]["GIT_GRAFT_FILE"] == os.devnull
        return subprocess.CompletedProcess(command, 129, stdout=b"", stderr=b"unsupported Git capability")

    monkeypatch.setattr(adoption.subprocess, "run", unsupported)
    with pytest.raises(ValueError, match="history unavailable"):
        adoption.load_ledger(tmp_path, "1" * 40)


def test_cli_main_prints_summary(adoption, tmp_path, monkeypatch, capsys):
    git(tmp_path, "init", "--initial-branch=main")
    git(tmp_path, "config", "user.name", "Fixture")
    git(tmp_path, "config", "user.email", "fixture@example.invalid")
    head = commit(tmp_path, line(adoption, record(adoption)), adoption)
    monkeypatch.setattr(sys, "argv", ["maintainer_adoption.py", "--repo", str(tmp_path), "--trusted-commit", head])
    adoption.main()
    assert json.loads(capsys.readouterr().out) == {
        "declarations": 1,
        "grants_merge_authority": False,
        "revoked_heads": 0,
        "trusted_commit": head,
    }


def test_load_ledger_returns_resolved_pinned_commit_sha_independent_of_caller_input(adoption, tmp_path):
    """B1 + CodeQL flow check: Ledger exposes pinned_commit_sha sourced from Git, not the caller string."""

    class _CallerPin(str):
        pass

    init_repo(tmp_path)
    head = commit(tmp_path, line(adoption, record(adoption)), adoption)
    caller_pin = _CallerPin(head)
    ledger = adoption.load_ledger(tmp_path, caller_pin)
    assert ledger.pinned_commit_sha == head
    assert type(ledger.pinned_commit_sha) is str
    assert ledger.pinned_commit_sha is not caller_pin
    assert not hasattr(ledger, "trusted_commit")


@pytest.mark.parametrize(
    ("bad_pin", "expected_error"),
    [
        ("not-a-sha", "exact trusted default-branch commit"),
        ("0" * 40, "pin differs from the trusted default-branch snapshot"),
    ],
    ids=["malformed-sha", "mismatched-sha"],
)
def test_cli_main_fails_closed_on_invalid_or_mismatched_trusted_commit(
    adoption, tmp_path, monkeypatch, capsys, bad_pin, expected_error
):
    init_repo(tmp_path)
    commit(tmp_path, line(adoption, record(adoption)), adoption)
    monkeypatch.setattr(sys, "argv", ["maintainer_adoption.py", "--repo", str(tmp_path), "--trusted-commit", bad_pin])
    with pytest.raises(SystemExit) as excinfo:
        adoption.main()
    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "declaration evidence unavailable" in captured.err
    assert expected_error in captured.err


def test_policy_digest_hashes_pinned_git_blobs_ignoring_candidate_dirty_tree_and_crlf(adoption, tmp_path, monkeypatch):
    """B2: policy_digest reads pinned Git blobs; immune to candidate edits, dirty tree, --no-checkout, and CRLF."""
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    source = tmp_path / "source"
    init_repo(source)
    git(source, "config", "core.autocrlf", "false")
    pinned_sha = commit_policy_tree(source, adoption)

    expected = adoption.digest(
        {name: hashlib.sha256(f"# policy file: {name}\nline2\n".encode()).hexdigest() for name in adoption.POLICY_PATHS}
    )
    baseline = adoption.policy_digest(source, pinned_sha)
    assert baseline == expected

    # 1. Candidate branch commit + dirty working-tree mutation must not change pinned policy_digest.
    git(source, "checkout", "-b", "candidate-pr")
    (source / "scripts/quorum_check.py").write_bytes(b"# candidate branch change\n")
    git(source, "add", "scripts/quorum_check.py")
    git(source, "commit", "-m", "candidate modification")
    candidate_sha = git(source, "rev-parse", "HEAD")
    (source / "scripts/publish_required_check.py").write_bytes(b"# uncommitted dirty edit\n")
    assert adoption.policy_digest(source, pinned_sha) == baseline
    with pytest.raises(ValueError, match="trusted default-branch"):
        adoption.policy_digest(source, candidate_sha)

    # Restore clean main checkout before cloning (origin/main remains pinned_sha).
    git(source, "checkout", "--", "scripts/publish_required_check.py")
    git(source, "checkout", "main")

    # 2. --no-checkout clone has no working-tree files at all and must produce the identical digest.
    no_checkout = tmp_path / "no-checkout"
    git(tmp_path, "clone", "--no-checkout", source.as_uri(), str(no_checkout))
    assert not (no_checkout / "scripts/maintainer_adoption.py").exists()
    assert adoption.policy_digest(no_checkout, pinned_sha) == baseline

    # 3. Windows-style CRLF checkout (core.autocrlf=true) writes CRLF on disk yet hashes LF Git blobs identically.
    crlf_clone = tmp_path / "crlf-clone"
    git(tmp_path, "clone", "-c", "core.autocrlf=true", source.as_uri(), str(crlf_clone))
    disk_bytes = (crlf_clone / "scripts/maintainer_adoption.py").read_bytes()
    assert b"\r\n" in disk_bytes
    assert adoption.policy_digest(crlf_clone, pinned_sha) == baseline


def test_policy_digest_rejects_missing_symlink_directory_promisor_and_oversized_entries(
    adoption, tmp_path, monkeypatch
):
    """B2 fail-closed checks: missing path, symlink, directory, promisor blob, and oversized policy blob."""
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    # 1. Missing required policy file fails closed.
    missing_repo = tmp_path / "missing"
    init_repo(missing_repo)
    missing_sha = commit_policy_tree(missing_repo, adoption, overrides={"scripts/queue_hygiene.py": None})
    with pytest.raises(ValueError, match="policy"):
        adoption.policy_digest(missing_repo, missing_sha)

    # 2. Symlink in POLICY_PATHS fails closed (mode 120000 is not a regular blob).
    sym_repo = tmp_path / "symlink"
    init_repo(sym_repo)
    commit_policy_tree(sym_repo, adoption)
    sym_target = sym_repo / "scripts/queue_hygiene.py"
    sym_target.unlink()
    sym_target.symlink_to("quorum_check.py")
    git(sym_repo, "add", "-A")
    git(sym_repo, "commit", "-m", "replace policy file with symlink")
    sym_sha = git(sym_repo, "rev-parse", "HEAD")
    git(sym_repo, "update-ref", adoption.TRUSTED_DEFAULT_REF, sym_sha)
    with pytest.raises(ValueError, match="policy"):
        adoption.policy_digest(sym_repo, sym_sha)

    # 3. Directory in POLICY_PATHS fails closed (mode 040000 tree is not a regular blob).
    dir_repo = tmp_path / "directory"
    init_repo(dir_repo)
    commit_policy_tree(dir_repo, adoption)
    dir_target = dir_repo / "scripts/queue_hygiene.py"
    dir_target.unlink()
    dir_target.mkdir()
    (dir_target / "nested.py").write_bytes(b"nested\n")
    git(dir_repo, "add", "-A")
    git(dir_repo, "commit", "-m", "replace policy file with directory")
    dir_sha = git(dir_repo, "rev-parse", "HEAD")
    git(dir_repo, "update-ref", adoption.TRUSTED_DEFAULT_REF, dir_sha)
    with pytest.raises(ValueError, match="policy"):
        adoption.policy_digest(dir_repo, dir_sha)

    # 4. Missing promisor blob on --filter=blob:none --no-checkout clone fails closed without transport fetch.
    prom_src, prom_clone = tmp_path / "prom-src", tmp_path / "prom-clone"
    init_repo(prom_src)
    prom_sha = commit_policy_tree(prom_src, adoption)
    git(prom_src, "config", "uploadpack.allowFilter", "true")
    git(tmp_path, "clone", "--filter=blob:none", "--no-checkout", prom_src.as_uri(), str(prom_clone))
    trace = tmp_path / "policy-git.trace"
    monkeypatch.setenv("GIT_TRACE", str(trace))
    with pytest.raises(ValueError):
        adoption.policy_digest(prom_clone, prom_sha)
    events = trace.read_text()
    assert "fetch origin" not in events and "upload-pack" not in events

    # 5. Oversized policy blob fails closed on ls-tree size before invoking cat-file blob.
    monkeypatch.delenv("GIT_TRACE", raising=False)
    monkeypatch.setattr(adoption, "_MAX_BYTES", 8)
    with pytest.raises(ValueError, match="bound"):
        adoption.policy_digest(prom_src, prom_sha)
