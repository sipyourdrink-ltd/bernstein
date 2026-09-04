"""Unit tests for the executable audit-receipt conformance corpus (#4987).

Covers the acceptance criteria named in the issue:

* every numbered requirement has >=1 positive and >=1 negative corpus case
  (and the completeness guard actually fails the build when one is missing);
* our own writer/verifier passes the full corpus;
* a deliberately non-conforming synthetic producer fails, naming the
  requirement it violated;
* a verifier that accepts a negative case is reported as non-conforming;
* the CLI (`bernstein audit receipt conform`) exposes both modes with the
  documented exit codes.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.core.security.audit_receipt_conformance import (
    REQUIREMENTS,
    AuditReceiptConformanceError,
    CorpusCase,
    Requirement,
    assert_corpus_completeness,
    build_corpus,
    default_verifier_path,
    evaluate_receipt,
    run_corpus,
)

# ---------------------------------------------------------------------------
# Corpus completeness
# ---------------------------------------------------------------------------


def test_every_numbered_requirement_has_a_positive_and_negative_corpus_case() -> None:
    corpus = build_corpus()
    have_positive = {c.requirement_id for c in corpus if c.expect_valid}
    have_negative = {c.requirement_id for c in corpus if not c.expect_valid}
    for req in REQUIREMENTS:
        assert req.id in have_positive, f"{req.id} has no positive corpus case"
        assert req.id in have_negative, f"{req.id} has no negative corpus case"


def test_requirement_ids_are_unique_and_stable_looking() -> None:
    ids = [req.id for req in REQUIREMENTS]
    assert len(ids) == len(set(ids)), "duplicate requirement id"
    for req_id in ids:
        assert req_id.startswith("AR-"), f"{req_id} does not follow the AR-<FAMILY>-<NNN> convention"


def test_adding_a_requirement_without_a_corpus_case_fails_the_build() -> None:
    """The exact completeness guard build_corpus() itself relies on."""
    extra = Requirement("AR-NEW-999", "shared", "a brand new rule with no case yet")
    incomplete_requirements = (*REQUIREMENTS, extra)
    # The existing corpus never mentions AR-NEW-999 at all.
    with pytest.raises(AuditReceiptConformanceError, match="AR-NEW-999"):
        assert_corpus_completeness(incomplete_requirements, build_corpus())


def test_a_requirement_with_only_a_positive_case_still_fails_completeness() -> None:
    only_positive = (Requirement("AR-ONLY-POS", "shared", "..."),)
    corpus = (CorpusCase("AR-ONLY-POS-positive", "AR-ONLY-POS", True, "all", {}),)
    with pytest.raises(AuditReceiptConformanceError, match="AR-ONLY-POS"):
        assert_corpus_completeness(only_positive, corpus)


def test_a_requirement_with_only_a_negative_case_still_fails_completeness() -> None:
    only_negative = (Requirement("AR-ONLY-NEG", "shared", "..."),)
    corpus = (CorpusCase("AR-ONLY-NEG-negative", "AR-ONLY-NEG", False, "all", {}),)
    with pytest.raises(AuditReceiptConformanceError, match="AR-ONLY-NEG"):
        assert_corpus_completeness(only_negative, corpus)


def test_a_fully_covered_requirement_set_passes_completeness() -> None:
    covered = (Requirement("AR-OK-001", "shared", "..."),)
    corpus = (
        CorpusCase("AR-OK-001-positive", "AR-OK-001", True, "all", {}),
        CorpusCase("AR-OK-001-negative", "AR-OK-001", False, "all", {}),
    )
    assert_corpus_completeness(covered, corpus)  # does not raise


# ---------------------------------------------------------------------------
# Our own writer/verifier passes the full corpus (no privileged path: it is
# run through exactly the same run_corpus() driver any other implementation
# would be)
# ---------------------------------------------------------------------------


def test_our_own_writer_and_verifier_pass_the_full_corpus(tmp_path: Path) -> None:
    corpus = build_corpus()
    run = run_corpus(corpus, verifier_path=default_verifier_path(), tmp_dir=tmp_path)
    non_conformant = [v.requirement_id for v in run.verdicts if not v.conformant]
    assert non_conformant == [], f"our own verifier is non-conformant on: {non_conformant}"
    assert run.ok is True
    # Every requirement in the registry was actually exercised.
    assert {v.requirement_id for v in run.verdicts} == {r.id for r in REQUIREMENTS}


@pytest.mark.parametrize("req_id", [r.id for r in REQUIREMENTS])
def test_each_requirements_positive_case_is_accepted_by_our_verifier(req_id: str, tmp_path: Path) -> None:
    corpus = [c for c in build_corpus() if c.requirement_id == req_id]
    run = run_corpus(corpus, verifier_path=default_verifier_path(), tmp_dir=tmp_path)
    assert run.ok is True


@pytest.mark.parametrize("req_id", [r.id for r in REQUIREMENTS])
def test_each_requirements_negative_case_is_rejected_by_our_verifier(req_id: str, tmp_path: Path) -> None:
    corpus = [c for c in build_corpus() if c.requirement_id == req_id and not c.expect_valid]
    assert corpus, f"no negative case built for {req_id}"
    run = run_corpus(corpus, verifier_path=default_verifier_path(), tmp_dir=tmp_path)
    assert run.ok is True


# ---------------------------------------------------------------------------
# A verifier that accepts a negative case is reported as non-conforming
# ---------------------------------------------------------------------------


def _write_always_accept_verifier(tmp_path: Path) -> Path:
    script = tmp_path / "always_accept.py"
    script.write_text(
        textwrap.dedent(
            """\
            import sys
            if __name__ == "__main__":
                sys.exit(0)
            """,
        ),
        encoding="utf-8",
    )
    return script


def _write_always_reject_verifier(tmp_path: Path) -> Path:
    script = tmp_path / "always_reject.py"
    script.write_text(
        textwrap.dedent(
            """\
            import sys
            if __name__ == "__main__":
                sys.exit(1)
            """,
        ),
        encoding="utf-8",
    )
    return script


def test_a_verifier_that_accepts_every_negative_case_is_reported_non_conformant(tmp_path: Path) -> None:
    broken_verifier = _write_always_accept_verifier(tmp_path)
    corpus = build_corpus()
    run = run_corpus(corpus, verifier_path=broken_verifier, tmp_dir=tmp_path)
    assert run.ok is False
    # Every requirement's negative case was wrongly accepted -> every
    # requirement is non-conformant, and the report names all of them.
    assert {v.requirement_id for v in run.verdicts if not v.conformant} == {r.id for r in REQUIREMENTS}


def test_a_verifier_that_rejects_every_positive_case_is_also_reported_non_conformant(tmp_path: Path) -> None:
    broken_verifier = _write_always_reject_verifier(tmp_path)
    corpus = build_corpus()
    run = run_corpus(corpus, verifier_path=broken_verifier, tmp_dir=tmp_path)
    assert run.ok is False
    assert {v.requirement_id for v in run.verdicts if not v.conformant} == {r.id for r in REQUIREMENTS}


# ---------------------------------------------------------------------------
# A deliberately non-conforming synthetic producer fails, naming the
# requirement it violated
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("case_id", "expected_violated"),
    [
        ("AR-SUBJECT-001-negative", {"AR-SUBJECT-001", "AR-TAMPER-001"}),
        ("AR-COSE-001-negative", {"AR-COSE-001", "AR-COSE-002"}),
        ("AR-COSE-002-negative", {"AR-COSE-001", "AR-COSE-002"}),
        ("AR-INTOTO-001-negative", {"AR-INTOTO-001", "AR-INTOTO-002"}),
        ("AR-INTOTO-002-negative", {"AR-INTOTO-001", "AR-INTOTO-002"}),
        ("AR-TRANSPARENCY-001-negative", {"AR-TRANSPARENCY-001", "AR-TRANSPARENCY-002"}),
        ("AR-TRANSPARENCY-002-negative", {"AR-TRANSPARENCY-001", "AR-TRANSPARENCY-002"}),
    ],
)
def test_synthetic_non_conforming_producer_fails_naming_the_violated_requirement(
    case_id: str,
    expected_violated: set[str],
    tmp_path: Path,
) -> None:
    """A "producer" here is just a corpus negative case's receipt, used as a
    stand-in for an arbitrary non-conforming implementation's output -- the
    corpus does not know it is one of its own records at evaluation time.
    """
    corpus = build_corpus()
    producer_output = next(c.receipt for c in corpus if c.case_id == case_id)

    results = evaluate_receipt(producer_output, verifier_path=default_verifier_path(), tmp_dir=tmp_path)

    violated = {r.requirement_id for r in results if not r.conformant}
    assert violated == expected_violated, f"{case_id}: expected {expected_violated}, got {violated}"

    still_conformant = {r.requirement_id for r in results if r.conformant}
    assert still_conformant == {r.id for r in REQUIREMENTS} - expected_violated


def test_synthetic_producer_that_only_tampers_the_event_range_fails_every_requirement(tmp_path: Path) -> None:
    """AR-TAMPER-001's own statement: tampering the events must collapse
    every format present, not just the subject-binding check."""
    corpus = build_corpus()
    producer_output = next(c.receipt for c in corpus if c.case_id == "AR-TAMPER-001-negative")

    results = evaluate_receipt(producer_output, verifier_path=default_verifier_path(), tmp_dir=tmp_path)

    assert all(not r.conformant for r in results)


def test_a_fully_conforming_producer_output_violates_nothing(tmp_path: Path) -> None:
    corpus = build_corpus()
    base_receipt = next(c.receipt for c in corpus if c.case_id == "AR-SUBJECT-001-positive")

    results = evaluate_receipt(base_receipt, verifier_path=default_verifier_path(), tmp_dir=tmp_path)

    assert all(r.conformant for r in results)


# ---------------------------------------------------------------------------
# CLI: bernstein audit receipt conform
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_main():
    from bernstein.cli.main import cli

    return cli


def test_cli_conform_with_no_args_runs_the_corpus_against_our_own_verifier(cli_main) -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["audit", "receipt", "conform"])
    assert result.exit_code == 0, result.output
    assert "OVERALL" in result.output
    for req in REQUIREMENTS:
        assert req.id in result.output


def test_cli_conform_reports_exit_1_for_a_broken_verifier(cli_main, tmp_path: Path) -> None:
    broken_verifier = _write_always_accept_verifier(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli_main, ["audit", "receipt", "conform", "--verifier", str(broken_verifier)])
    assert result.exit_code == 1, result.output


def test_cli_conform_on_a_receipt_path_names_the_violated_requirement(cli_main, tmp_path: Path) -> None:
    corpus = build_corpus()
    bad = next(c.receipt for c in corpus if c.case_id == "AR-INTOTO-002-negative")
    receipt_file = tmp_path / "producer-output.json"
    receipt_file.write_text(json.dumps(bad), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(cli_main, ["audit", "receipt", "conform", str(receipt_file)])

    assert result.exit_code == 1, result.output
    assert "AR-INTOTO-001" in result.output
    assert "AR-INTOTO-002" in result.output
    assert "AR-COSE-001" in result.output  # listed as PASS -- still named in the table


def test_cli_conform_json_output_is_parseable(cli_main, tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli_main, ["audit", "receipt", "conform", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert {row["id"] for row in payload["requirements"]} == {r.id for r in REQUIREMENTS}


def test_cli_conform_exit_2_when_verifier_cannot_be_found(cli_main, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("BERNSTEIN_AUDIT_RECEIPT_VERIFIER", raising=False)
    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["audit", "receipt", "conform", "--verifier", str(tmp_path / "does-not-exist.py")],
    )
    # click itself rejects a --verifier path that does not exist (dir_okay=False, exists=True).
    assert result.exit_code == 2
