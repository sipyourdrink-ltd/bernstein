"""Synthetic receipt and authenticated-API tests; no live reviews or providers."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
HEAD, BASE, POLICY = "d" * 40, "b" * 40, "f" * 64


@pytest.fixture(scope="module")
def qc():
    spec = importlib.util.spec_from_file_location("quorum_adoption_under_test", ROOT / "scripts/quorum_check.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _report(qc, identity="reviewer/one", *, security=False):
    report = {
        "execution_id": identity,
        "head_sha": HEAD,
        "outcome": "PASS",
        "findings": [],
        "checks": ["synthetic security command: passed"] if security else [],
        "summary": "Synthetic terminal report; whole candidate reviewed.",
    }
    return {"report": report, "report_sha256": qc.evidence.report_digest(report)}


def _row(qc, *, sensitive=False):
    return {
        "schema": qc.evidence.SCHEMA,
        "repository": "owner/repo",
        "pull_request": 7,
        "base_sha": BASE,
        "head_sha": HEAD,
        "policy_sha256": POLICY,
        "producer_execution_ids": ["producer/session"],
        "reviews": [_report(qc)],
        "security_tests": _report(qc, "test/session", security=True) if sensitive else None,
    }


def _bindings(*, sensitive=False):
    return {
        "repository": "owner/repo",
        "number": 7,
        "base_sha": BASE,
        "head_sha": HEAD,
        "policy_sha256": POLICY,
        "authors": {"contributor"},
        "required_reviews": 2 if sensitive else 1,
    }


def _body(qc, row):
    return qc.evidence.MARKER + json.dumps(row)


def _comment(qc, row, *, login="owner", user_id=42, comment_id=1, updated="2026-11-10T10:00:00Z"):
    return {
        "body": _body(qc, row),
        "user": {"login": login, "id": user_id},
        "id": comment_id,
        "updated_at": updated,
        "html_url": f"https://github.com/owner/repo/pull/7#issuecomment-{comment_id}",
    }


def test_valid_ordinary_report_is_authenticated_and_digest_checked(qc):
    result = qc.evidence.from_comments([_comment(qc, _row(qc))], maintainer="owner", maintainer_id=42, **_bindings())
    assert result.valid and result.reviewer_ids == ("reviewer/one",)


@pytest.mark.parametrize("login,user_id", [("stranger", 42), ("owner", 99), ("owner", True)])
def test_copying_a_maintainer_declaration_does_not_grant_authority(qc, login, user_id):
    assert (
        qc.evidence.from_comments(
            [_comment(qc, _row(qc), login=login, user_id=user_id)],
            maintainer="owner",
            maintainer_id=42,
            **_bindings(),
        )
        is None
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("repository", "other/repo"),
        ("pull_request", 8),
        ("pull_request", True),
        ("base_sha", "a" * 40),
        ("head_sha", "a" * 40),
        ("policy_sha256", "a" * 64),
        ("schema", "other"),
        ("producer_execution_ids", []),
        ("producer_execution_ids", ["one", "ONE"]),
    ],
)
def test_wrong_pr_base_head_policy_and_missing_custody_are_rejected(qc, field, value):
    row = _row(qc)
    row[field] = value
    with pytest.raises(ValueError):
        qc.evidence.validate(_body(qc, row), **_bindings())


def test_report_body_tampering_does_not_preserve_its_digest(qc):
    row = _row(qc)
    row["reviews"][0]["report"]["summary"] = "Modified after review."
    with pytest.raises(ValueError, match="digest"):
        qc.evidence.validate(_body(qc, row), **_bindings())


@pytest.mark.parametrize("identity", ["producer/session", "PRODUCER/SESSION", "contributor"])
def test_producers_and_author_cannot_count_as_reviewers(qc, identity):
    row = _row(qc)
    row["reviews"] = [_report(qc, identity)]
    with pytest.raises(ValueError, match="independent"):
        qc.evidence.validate(_body(qc, row), **_bindings())


def test_sensitive_reports_need_two_distinct_execution_ids(qc):
    row = _row(qc, sensitive=True)
    with pytest.raises(ValueError, match="at least 2"):
        qc.evidence.validate(_body(qc, row), **_bindings(sensitive=True))
    row["reviews"].append(copy.deepcopy(row["reviews"][0]))
    with pytest.raises(ValueError, match="distinct"):
        qc.evidence.validate(_body(qc, row), **_bindings(sensitive=True))
    row["reviews"][1] = _report(qc, "reviewer/two")
    assert len(qc.evidence.validate(_body(qc, row), **_bindings(sensitive=True))) == 2


@pytest.mark.parametrize("defect", ["missing", "stale", "failed", "findings", "no_commands", "tampered"])
def test_security_test_evidence_cannot_be_missing_stale_failed_or_tampered(qc, defect):
    row = _row(qc, sensitive=True)
    row["reviews"].append(_report(qc, "reviewer/two"))
    report = row["security_tests"]["report"]
    if defect == "missing":
        row["security_tests"] = None
    elif defect == "stale":
        report["head_sha"] = BASE
    elif defect == "failed":
        report["outcome"] = "ERROR"
    elif defect == "findings":
        report["findings"] = ["Unresolved security finding."]
    elif defect == "no_commands":
        report["checks"] = []
    else:
        report["summary"] = "Tampered."
    if defect not in ("tampered", "missing"):
        row["security_tests"]["report_sha256"] = qc.evidence.report_digest(report)
    with pytest.raises(ValueError):
        qc.evidence.validate(_body(qc, row), **_bindings(sensitive=True))


def test_latest_invalid_maintainer_declaration_does_not_revive_an_older_pass(qc):
    old = _comment(qc, _row(qc))
    revoked = _row(qc)
    revoked["reviews"] = []
    result = qc.evidence.from_comments(
        [old, _comment(qc, revoked, comment_id=2, updated="2026-11-10T11:00:00Z")],
        maintainer="owner",
        maintainer_id=42,
        **_bindings(),
    )
    assert not result.valid


def test_unknown_and_duplicate_fields_do_not_have_ambiguous_meaning(qc):
    row = _row(qc)
    row["approved"] = True
    with pytest.raises(ValueError):
        qc.evidence.validate(_body(qc, row), **_bindings())
    body = _body(qc, _row(qc)).replace('"pull_request": 7', '"pull_request": 7, "pull_request": 7')
    with pytest.raises(ValueError, match="duplicate"):
        qc.evidence.validate(body, **_bindings())


def test_policy_digest_changes_for_policy_but_not_unrelated_files(qc, tmp_path):
    for name in qc.evidence.POLICY_PATHS:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("trusted policy\n")
    initial = qc.evidence.policy_digest(tmp_path)
    (tmp_path / "unrelated.txt").write_text("ordinary work\n")
    assert qc.evidence.policy_digest(tmp_path) == initial
    (tmp_path / "scripts/quorum_check.py").write_text("candidate policy\n")
    assert qc.evidence.policy_digest(tmp_path) != initial


def _pr(qc, *, author="contributor", paths=None):
    return qc.PullRequest(
        number=7,
        author=author,
        is_draft=False,
        head_sha=HEAD,
        base_sha=BASE,
        changed_lines=10,
        paths=paths or ["docs/guide.md"],
        reviews=[],
        contributors={author},
        last_push=datetime(2026, 11, 1, tzinfo=UTC),
    )


def _roster(qc):
    return qc.Roster("owner", frozenset({"core"}), frozenset({"committer"}), frozenset({"bot[bot]"}))


def test_author_preserving_exception_does_not_invent_human_approvals(qc):
    pr = _pr(qc)
    pr.adoption = qc.evidence.Evidence(True, "maintainer attested", reviewer_ids=("reviewer/one",))
    verdict = qc.evaluate(pr, _roster(qc), [("/docs/", ["owner"])], datetime(2026, 11, 10, tzinfo=UTC))
    assert verdict.passed and pr.author == "contributor"
    assert any("Real current human approvals: 0" in note for note in verdict.notes)


def test_maintainer_cannot_self_certify_sensitive_work_without_two_reviews(qc):
    pr = _pr(qc, author="owner", paths=["scripts/quorum_check.py"])
    verdict = qc.evaluate(pr, _roster(qc), [], datetime(2026, 11, 10, tzinfo=UTC))
    assert not verdict.passed
    assert any("2 independent" in requirement.text and not requirement.met for requirement in verdict.requirements)


def test_adoption_keeps_objections_and_governance_window(qc):
    pr = _pr(qc, paths=["scripts/quorum_check.py"])
    now = datetime(2026, 11, 10, tzinfo=UTC)
    pr.last_push = now - timedelta(hours=1)
    pr.adoption = qc.evidence.Evidence(True, "maintainer attested", reviewer_ids=("reviewer/one", "reviewer/two"))
    pr.reviews = [qc.Review("core", "CHANGES_REQUESTED", HEAD, now.isoformat())]
    verdict = qc.evaluate(pr, _roster(qc), [], now)
    assert not verdict.passed
    assert sum(not requirement.met for requirement in verdict.requirements) == 2


def test_bot_using_the_new_exception_has_the_same_independence_floor(qc):
    pr = _pr(qc, author="bot[bot]", paths=["src/bernstein/core/security/policy.py"])
    pr.adoption = qc.evidence.Evidence(False, "producer cannot review")
    assert not qc.evaluate(pr, _roster(qc), [], datetime(2026, 11, 10, tzinfo=UTC)).passed


def test_fresh_api_authorship_and_default_policy_bind_the_exception(qc, monkeypatch):
    row = _row(qc)
    comment = _comment(qc, row)
    calls = []

    def api(*args):
        calls.append(args)
        if args[1].endswith("/comments"):
            assert args[-2:] == ("--paginate", "--slurp")
            return [[comment]]
        assert args == ("api", "users/owner")
        return {"login": "owner", "id": 42}

    monkeypatch.setattr(qc.evidence, "policy_digest", lambda root: POLICY)
    pr = _pr(qc)
    assert qc.fetch_adoption("owner/repo", pr, _roster(qc), api=api).valid
    row["policy_sha256"] = "a" * 64
    comment["body"] = _body(qc, row)
    assert not qc.fetch_adoption("owner/repo", pr, _roster(qc), api=api).valid
    assert ("api", "users/owner") in calls


def test_untrusted_pr_body_or_comment_cannot_self_certify(qc):
    pr = _pr(qc)
    pr.body = _body(qc, _row(qc))

    def api(*args):
        assert args[1].endswith("/comments")
        return [[_comment(qc, _row(qc), login="contributor", user_id=99)]]

    assert qc.fetch_adoption("owner/repo", pr, _roster(qc), api=api) is None


@pytest.mark.parametrize(
    "profile",
    [{"login": "owner"}, {"login": "owner", "id": True}, {"login": "owner", "id": 0}, {"login": "other", "id": 42}],
)
def test_an_unverifiable_fresh_maintainer_identity_fails_closed(qc, profile):
    def api(*args):
        return [[_comment(qc, _row(qc))]] if args[1].endswith("/comments") else profile

    with pytest.raises(ValueError, match="fresh maintainer"):
        qc.fetch_adoption("owner/repo", _pr(qc), _roster(qc), api=api)


def test_invalid_adoption_findings_cannot_fall_back_to_other_votes(qc):
    pr = _pr(qc)
    pr.reviews = [qc.Review(login, "APPROVED", HEAD, "2026-11-10T10:00:00Z") for login in ("core", "committer")]
    pr.adoption = qc.evidence.Evidence(False, "unresolved findings")
    assert not qc.evaluate(pr, _roster(qc), [], datetime(2026, 11, 10, tzinfo=UTC)).passed


@pytest.mark.parametrize("author", ["owner", "contributor", "bot[bot]"])
def test_qualified_draft_passes_content_only_and_never_full_quorum(qc, author):
    pr = _pr(qc, author=author)
    pr.is_draft = True
    pr.adoption = qc.evidence.Evidence(True, "maintainer attested", reviewer_ids=("reviewer/one",))
    now = datetime(2026, 11, 10, tzinfo=UTC)
    assert not qc.evaluate(pr, _roster(qc), [], now).passed
    content = qc.evaluate_content(pr, _roster(qc), [], now)
    assert content.passed and pr.is_draft
    assert "content qualification" in content.summary()
    assert "no quorum success" in content.summary()
    assert "public-action authority" in content.summary()


@pytest.mark.parametrize("defect", ["invalid", "missing", "security", "objection", "window"])
def test_content_qualification_keeps_evidence_and_sensitive_policy(qc, defect):
    pr = _pr(qc, author="owner", paths=["scripts/quorum_check.py"])
    pr.is_draft = True
    row = _row(qc, sensitive=True)
    row["reviews"].append(_report(qc, "reviewer/two"))
    now = datetime(2026, 11, 10, tzinfo=UTC)
    if defect == "invalid":
        row["head_sha"] = BASE
    elif defect == "security":
        row["security_tests"] = None
    elif defect == "objection":
        pr.reviews = [qc.Review("core", "CHANGES_REQUESTED", HEAD, now.isoformat())]
    elif defect == "window":
        pr.last_push = now - timedelta(hours=1)
    if defect != "missing":
        pr.adoption = qc.evidence.from_comments(
            [_comment(qc, row)], maintainer="owner", maintainer_id=42, **_bindings(sensitive=True)
        )
    assert not qc.evaluate_content(pr, _roster(qc), [], now).passed


def test_cli_full_verdict_blocks_a_qualified_draft_and_content_grants_no_authority(qc, monkeypatch, capsys):
    pr = _pr(qc, author="owner")
    pr.is_draft = True
    monkeypatch.setattr(qc, "load_roster", lambda root: _roster(qc))
    monkeypatch.setattr(qc, "load_codeowners", lambda root: [])
    monkeypatch.setattr(qc, "fetch_pull_request", lambda *args: pr)
    monkeypatch.setattr(
        qc, "fetch_adoption", lambda *args: qc.evidence.Evidence(True, "attested", reviewer_ids=("reviewer/one",))
    )
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert qc.main(["--repo", "owner/repo", "--pr", "7"]) == 1
    assert "::error title=quorum::" in capsys.readouterr().out
    assert qc.main(["--evaluate-content", "--repo", "owner/repo", "--pr", "7"]) == 0
    output = capsys.readouterr().out
    assert "no quorum success" in output and "public-action authority" in output
    assert "### `quorum`:" not in output
    pr.adoption = None
    monkeypatch.setattr(qc, "fetch_adoption", lambda *args: qc.evidence.Evidence(False, "tampered"))
    assert qc.main(["--evaluate-content", "--repo", "owner/repo", "--pr", "7"]) == 1


@pytest.mark.parametrize("action", ["edited", "deleted"])
def test_authenticated_marker_removal_or_deletion_recomputes_policy(qc, action):
    event = {
        "action": action,
        "issue": {"pull_request": {"url": "synthetic"}},
        "comment": {"user": {"login": "owner", "id": 42}, "body": "marker removed"},
    }
    assert qc.evidence.authorized_comment_event(event, maintainer="owner", maintainer_id=42)
    event["comment"]["user"]["id"] = 99
    assert not qc.evidence.authorized_comment_event(event, maintainer="owner", maintainer_id=42)


@pytest.mark.parametrize("defect", ["login", "boolean_id", "not_pr", "ordinary_create", "unknown_action"])
def test_comment_recompute_requires_real_maintainer_identity_and_relevant_event(qc, defect):
    event = {
        "action": "created",
        "issue": {"pull_request": {"url": "synthetic"}},
        "comment": {"user": {"login": "owner", "id": 42}, "body": qc.evidence.MARKER + "{}"},
    }
    assert qc.evidence.authorized_comment_event(event, maintainer="owner", maintainer_id=42)
    if defect == "login":
        event["comment"]["user"]["login"] = "contributor"
    elif defect == "boolean_id":
        event["comment"]["user"]["id"] = True
    elif defect == "not_pr":
        event["issue"] = {}
    elif defect == "ordinary_create":
        event["comment"]["body"] = "ordinary comment"
    else:
        event["action"] = "unknown"
    assert not qc.evidence.authorized_comment_event(event, maintainer="owner", maintainer_id=42)


@pytest.mark.parametrize(
    "draft,valid_evidence,conclusion", [(True, True, "failure"), (True, False, "failure"), (False, True, "success")]
)
def test_trusted_publisher_uses_real_full_evaluator_for_drafts(tmp_path, draft, valid_evidence, conclusion):
    """Execute the workflow's real shell; stub GitHub reads/writes, not policy."""
    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/quorum-rerun.yml").read_text())
    run = next(
        step["run"]
        for step in workflow["jobs"]["attestation"]["steps"]
        if step.get("name") == "Recompute and publish the real policy verdict"
    )
    binary = tmp_path / "bin"
    binary.mkdir()
    gh = binary / "gh"
    gh.write_text(f"#!{sys.executable}\nprint({(HEAD + ' ' + BASE)!r})\n")
    gh.chmod(0o755)
    python = binary / "python3"
    python.write_text(
        f"""#!{sys.executable}
import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
if sys.argv[1] == "scripts/publish_required_check.py":
    Path({str(tmp_path / "publication.json")!r}).write_text(json.dumps(sys.argv[2:]))
    raise SystemExit(0)
assert sys.argv[1] == "scripts/quorum_check.py"
spec = importlib.util.spec_from_file_location("actual_quorum", {str(ROOT / "scripts/quorum_check.py")!r})
qc = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = qc
spec.loader.exec_module(qc)
pr = qc.PullRequest(7, "owner", {draft!r}, {HEAD!r}, 10, ["docs/guide.md"], [],
                   {{"owner"}}, datetime(2026, 11, 1, tzinfo=UTC), base_sha={BASE!r})
qc.load_roster = lambda root: qc.Roster("owner", frozenset(), frozenset(), frozenset())
qc.load_codeowners = lambda root: []
qc.fetch_pull_request = lambda *args: pr
qc.fetch_adoption = lambda *args: qc.evidence.Evidence({valid_evidence!r}, "synthetic API receipt",
                                                     reviewer_ids=("independent/session",))
raise SystemExit(qc.main(sys.argv[2:]))
"""
    )
    python.chmod(0o755)
    env = dict(os.environ, PATH=f"{binary}{os.pathsep}{os.environ['PATH']}", REPO="owner/repo", PR_NUMBER="7")
    env.pop("GITHUB_STEP_SUMMARY", None)
    result = subprocess.run(["bash", "-c", run], cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    publication = json.loads((tmp_path / "publication.json").read_text())
    assert publication[publication.index("--conclusion") + 1] == conclusion
    assert publication[publication.index("--sha") + 1] == HEAD
    if draft:
        assert "draft: not ready for quorum" in publication[publication.index("--summary") + 1]
