"""CLI tests for ``bernstein review-receipt emit|verify`` (#2296).

``verify --pr <url>`` recomputes ``issue_hash`` and ``diff_hash`` from the PR
inputs and checks the Ed25519 signature offline. The PR inputs (issue body +
diff) are supplied from files so the test stays offline (no ``gh`` shell-out).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.review_receipt_cmd import review_receipt_group

_PR_URL = "https://github.com/acme/widget/pull/42"
_ISSUE = "the login path leaks tokens"
_DIFF = "--- a/x\n+++ b/x\n-leak\n+redact\n"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    (tmp_path / ".sdd").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _emit(project: Path) -> None:
    issue_file = project / "issue.md"
    diff_file = project / "pr.diff"
    issue_file.write_text(_ISSUE, encoding="utf-8")
    diff_file.write_text(_DIFF, encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        review_receipt_group,
        [
            "emit",
            "--pr",
            _PR_URL,
            "--repo",
            "acme/widget",
            "--issue",
            str(issue_file),
            "--plan",
            "-",
            "--diff",
            str(diff_file),
            "--journal-head",
            "abc123",
            "--verdict",
            "approve",
            "-w",
            str(project),
        ],
        input="do the fix\n",
    )
    assert result.exit_code == 0, result.output


def test_emit_then_verify_ok(project: Path) -> None:
    _emit(project)
    runner = CliRunner()
    result = runner.invoke(
        review_receipt_group,
        [
            "verify",
            "--pr",
            _PR_URL,
            "--issue",
            str(project / "issue.md"),
            "--diff",
            str(project / "pr.diff"),
            "-w",
            str(project),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "OK" in result.output


def test_verify_tampered_diff_exit_2(project: Path) -> None:
    _emit(project)
    tampered = project / "pr.diff"
    tampered.write_text(_DIFF + "sneaky\n", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        review_receipt_group,
        [
            "verify",
            "--pr",
            _PR_URL,
            "--issue",
            str(project / "issue.md"),
            "--diff",
            str(tampered),
            "-w",
            str(project),
        ],
    )
    assert result.exit_code == 2, result.output
    assert "MISMATCH" in result.output


def test_verify_no_receipt_exit_1(project: Path) -> None:
    (project / "issue.md").write_text(_ISSUE, encoding="utf-8")
    (project / "pr.diff").write_text(_DIFF, encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        review_receipt_group,
        [
            "verify",
            "--pr",
            _PR_URL,
            "--issue",
            str(project / "issue.md"),
            "--diff",
            str(project / "pr.diff"),
            "-w",
            str(project),
        ],
    )
    assert result.exit_code == 1, result.output


# ---------------------------------------------------------------------------
# ``verify --chain`` -- the fix-until-green contour's per-pass receipts (#4481)
# ---------------------------------------------------------------------------

_RULES = "## Guard\n\n- Do not flag the vendored parser.\n"
_PASS_DIFFS = ("--- a/x\n+++ b/x\n-leak\n+redact\n", "--- a/x\n+++ b/x\n-leak\n+scrub\n")


def _emit_chain(project: Path) -> str:
    """Emit two contour passes and return the ruleset digest they used."""
    from bernstein.core.quality.review_pipeline.contour import PassReceiptRequest, receipt_emitter
    from bernstein.core.quality.review_pipeline.ruleset import parse_ruleset
    from bernstein.core.quality.review_pipeline.scope import compute_resolution_hash, resolve_scope

    (project / "issue.md").write_text(_ISSUE, encoding="utf-8")
    (project / "rules.md").write_text(_RULES, encoding="utf-8")
    digest = parse_ruleset(_RULES).digest
    # Each pass binds the scope it resolved conventions under, same as a real
    # contour pass; no changed conventions are in scope for this fixture.
    resolution_hash = compute_resolution_hash(resolve_scope((), ()))
    emit = receipt_emitter(workdir=project, pr_url=_PR_URL, repo="acme/widget", issue_body=_ISSUE, timestamp=1000)
    previous = ""
    for index, (diff, verdict) in enumerate(zip(_PASS_DIFFS, ("request_changes", "approve"), strict=True), start=1):
        (project / "pr.diff").write_text(diff, encoding="utf-8")
        previous = emit(
            PassReceiptRequest(
                pass_index=index,
                diff=diff.encode("utf-8"),
                verdict=verdict,
                ruleset_digest=digest,
                prev_entry_hash=previous,
                resolution_hash=resolution_hash,
            )
        )
    return digest


def _verify_chain(project: Path, *, rules: Path | None) -> object:
    argv = [
        "verify",
        "--chain",
        "--pr",
        _PR_URL,
        "--issue",
        str(project / "issue.md"),
        "--diff",
        str(project / "pr.diff"),
        "-w",
        str(project),
    ]
    if rules is not None:
        argv.extend(["--rules", str(rules)])
    return CliRunner().invoke(review_receipt_group, argv)


def test_verify_chain_accepts_every_pass_of_the_contour(project: Path) -> None:
    _emit_chain(project)

    result = _verify_chain(project, rules=project / "rules.md")

    assert result.exit_code == 0, result.output
    assert "passes  2" in result.output


def test_verify_chain_rejects_a_ruleset_the_passes_were_not_reviewed_under(project: Path) -> None:
    _emit_chain(project)
    moved = project / "moved-rules.md"
    moved.write_text("## Guard\n\n- Something else entirely.\n", encoding="utf-8")

    result = _verify_chain(project, rules=moved)

    assert result.exit_code == 2, result.output
    assert "ruleset" in result.output
