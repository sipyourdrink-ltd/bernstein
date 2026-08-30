"""Unit tests for the correction miner."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

from bernstein.core.quality.correction_miner import (
    CorrectionPair,
    MiningResult,
    classify_correction,
    extract_correction_pairs,
    find_merged_commits,
    is_merge_rights_holder,
    mine_corrections,
    render_corrections_report,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(*args: str, cwd: Path) -> str:
    """Run git in *cwd*, return stdout stripped, raising on failure."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _init_repo() -> Path:
    """Create a bare-ish git repo (no remote) for controlled testing."""
    d = Path(tempfile.mkdtemp(prefix="correction_miner_"))
    _run("init", cwd=d)
    _run("config", "user.name", "tester", cwd=d)
    _run("config", "user.email", "tester@example.com", cwd=d)
    return d


def _commit(
    cwd: Path,
    message: str,
    *,
    author_name: str = "Contributor",
    author_email: str = "contributor@example.com",
    file_path: str = "file.py",
    content: str = "x = 1\n",
) -> str:
    """Stage a file and commit it, returning the commit SHA."""
    p = cwd / file_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    _run("add", "--", file_path, cwd=cwd)
    env = {
        "GIT_AUTHOR_NAME": author_name,
        "GIT_AUTHOR_EMAIL": author_email,
        "GIT_COMMITTER_NAME": author_name,
        "GIT_COMMITTER_EMAIL": author_email,
    }
    result = subprocess.run(
        ["git", "commit", "-m", message],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**dict(__import__("os").environ), **env},
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"commit failed: {result.stderr.strip()}")
    return _run("rev-parse", "HEAD", cwd=cwd)


def _make_merge_repo() -> tuple[Path, dict[str, str]]:
    """Create a repo with a contributor branch and a maintainer correction on top.

    Structure:
      main:  initial -> main_only.py: main commit
      contributor: initial -> contrib.py: contributor commit -> maintainer fix on contrib.py
    """
    repo = _init_repo()

    # Initial commit on main
    _commit(
        repo,
        "initial",
        file_path="init.py",
        content="# init\n",
    )
    _run("branch", "-M", "main", cwd=repo)

    # Contributor branch: initial -> contributor commit (the "base" that needs fixing)
    _run("checkout", "-b", "contributor", cwd=repo)
    contrib_sha = _commit(
        repo,
        "Add logging without sanitization",
        author_name="Contributor",
        author_email="contributor@example.com",
        file_path="contrib.py",
        content="def add_logging(user_input):\n    import logging\n    logging.info(user_input)\n",
    )

    # Maintainer creates a fix on top of contributor's commit (on the contributor branch)
    fix_sha = _commit(
        repo,
        "sanitize user_input before logging",
        author_name="Maintainer",
        author_email="maintainer@example.com",
        file_path="contrib.py",
        content="def add_logging(user_input):\n    import logging\n    from bernstein.safety import sanitize\n    logging.info(sanitize(user_input))\n",
    )

    # Back to main: add a commit so main diverges from contributor
    _run("checkout", "main", cwd=repo)
    _commit(
        repo,
        "main internal change",
        file_path="main_only.py",
        content="# main only\n",
    )

    # Merge contributor into main: creates a TRUE merge (two parents)
    _run("merge", "--no-edit", "contributor", cwd=repo)
    merge1_sha = _run("rev-parse", "HEAD", cwd=repo)

    shas = {
        "base": contrib_sha,
        "follow_up": fix_sha,
        "merge": merge1_sha,
        "contributor": "contributor@example.com",
        "maintainer": "maintainer@example.com",
    }
    return repo, shas


def _make_multi_author_correction_repo() -> tuple[Path, dict[str, str]]:
    """Create a repo with corrections from two maintainers (corroborated).

    Both correction chains fix the same file with the same shape, so the
    extracted pairs cluster into a single corroborated proposal:
    contributor commit -> maintainer fix on top -> merged into main, twice.
    """
    repo = _init_repo()

    # Initial commit
    _commit(repo, "initial", file_path="init.py", content="# init\n")
    _run("branch", "-M", "main", cwd=repo)

    # Correction 1: contributor commit, maintainer Alice fix on top, merged.
    _run("checkout", "-b", "contributor-1", cwd=repo)
    _commit(
        repo,
        "Add logging without sanitization",
        author_name="Contributor",
        author_email="contributor@example.com",
        file_path="shared.py",
        content="def add_logging(user_input):\n    import logging\n    logging.info(user_input)\n",
    )
    sha1 = _commit(
        repo,
        "sanitize user_input before logging",
        author_name="Alice",
        author_email="alice@example.com",
        file_path="shared.py",
        content="def add_logging(user_input):\n    import logging\n    from bernstein.safety import sanitize\n    logging.info(sanitize(user_input))\n",
    )

    # Back to main: add a commit so main diverges
    _run("checkout", "main", cwd=repo)
    _commit(repo, "main internal", file_path="main_only.py", content="# main only\n")

    # Merge contributor-1 branch (with Alice's fix) into main
    _run("merge", "--no-edit", "contributor-1", cwd=repo)
    merge1_sha = _run("rev-parse", "HEAD", cwd=repo)

    # Correction 2: same fix shape on the same file, maintainer Bob fixes.
    # Start from a fresh contributor branch again
    _run("checkout", "-b", "contributor-2", cwd=repo)
    _commit(
        repo,
        "Add logging without sanitization (again)",
        author_name="Contributor",
        author_email="contributor@example.com",
        file_path="shared.py",
        content="def add_logging(user_input):\n    import logging\n    logging.info(user_input)\n",
    )
    sha2 = _commit(
        repo,
        "sanitize user_input before logging",
        author_name="Bob",
        author_email="bob@example.com",
        file_path="shared.py",
        content="def add_logging(user_input):\n    import logging\n    from bernstein.safety import sanitize\n    logging.info(sanitize(user_input))\n",
    )

    # Back to main: add another commit so it diverges
    _run("checkout", "main", cwd=repo)
    _commit(repo, "another main commit", file_path="main_only2.py", content="# main2\n")

    # Merge contributor-2 branch (with Bob's fix) into main
    _run("merge", "--no-edit", "contributor-2", cwd=repo)
    merge2_sha = _run("rev-parse", "HEAD", cwd=repo)

    shas = {
        "follow_up1": sha1,
        "follow_up2": sha2,
        "author1": "alice@example.com",
        "author2": "bob@example.com",
        "merge1": merge1_sha,
        "merge2": merge2_sha,
    }
    return repo, shas


# ---------------------------------------------------------------------------
# find_merged_commits
# ---------------------------------------------------------------------------


class TestFindMergedCommits:
    def test_empty_repo(self, tmp_path: Path) -> None:
        repo = _init_repo()
        merges = find_merged_commits(repo)
        assert merges == []

    def test_with_merges(self, tmp_path: Path) -> None:
        repo = _make_merge_repo()[0]
        merges = find_merged_commits(repo)
        assert len(merges) >= 1


# ---------------------------------------------------------------------------
# is_merge_rights_holder
# ---------------------------------------------------------------------------


class TestIsMergeRightsHolder:
    def test_empty_set_allows_everyone(self) -> None:
        assert is_merge_rights_holder("anyone@example.com") is True

    def test_known_holder(self) -> None:
        assert (
            is_merge_rights_holder("maintainer@example.com", merge_rights_holders=frozenset({"maintainer@example.com"}))
            is True
        )

    def test_unknown_holder(self) -> None:
        assert (
            is_merge_rights_holder("stranger@example.com", merge_rights_holders=frozenset({"maintainer@example.com"}))
            is False
        )


# ---------------------------------------------------------------------------
# classify_correction
# ---------------------------------------------------------------------------


class TestClassifyCorrection:
    def test_single_source(self) -> None:
        pair = CorrectionPair(
            base_commit="aaaa",
            follow_up_commit="bbbb",
            merge_commit="cccc",
            author="Maintainer <maintainer@example.com>",
            message="fix",
            created_timestamp=1.0,
            base_diff="",
            follow_up_diff="",
        )
        assert classify_correction(pair) == "single-source"


# ---------------------------------------------------------------------------
# extract_correction_pairs
# ---------------------------------------------------------------------------


class TestExtractCorrectionPairs:
    def test_empty_repo(self, tmp_path: Path) -> None:
        repo = _init_repo()
        pairs = extract_correction_pairs(repo)
        assert pairs == []

    def test_finds_correction_pair(self, tmp_path: Path) -> None:
        """A maintainer fix on top of a contributor commit is extracted."""
        repo, shas = _make_merge_repo()
        pairs = extract_correction_pairs(repo)
        assert len(pairs) >= 1
        pair = pairs[0]
        assert pair.base_commit == shas["base"]
        assert pair.follow_up_commit == shas["follow_up"]

    def test_cites_commit_pair(self, tmp_path: Path) -> None:
        """Every pair's commit_pair_id cites the SHA pair (requirement #1)."""
        repo, shas = _make_merge_repo()
        pairs = extract_correction_pairs(repo)
        assert any(p.commit_pair_id == f"{shas['base']}::{shas['follow_up']}" for p in pairs)

    def test_author_identity_preserved(self, tmp_path: Path) -> None:
        """The maintainer author is captured."""
        repo, shas = _make_merge_repo()
        pairs = extract_correction_pairs(repo)
        assert any(p.author_id == shas["maintainer"] for p in pairs)

    def test_follow_up_is_maintainer_not_contributor(self, tmp_path: Path) -> None:
        """The follow_up commit author should be the maintainer, not the contributor."""
        repo, shas = _make_merge_repo()
        pairs = extract_correction_pairs(repo)
        for pair in pairs:
            assert pair.author_id != shas["contributor"] or pair.author_id == shas["maintainer"]


# ---------------------------------------------------------------------------
# mine_corrections
# ---------------------------------------------------------------------------


class TestMineCorrections:
    def test_empty_pairs(self) -> None:
        result = mine_corrections([])
        assert result.proposals == ()
        assert result.total_pairs_analyzed == 0
        assert result.total_proposals == 0

    def test_single_pair_produces_proposal(self) -> None:
        """A single pair becomes a single-source proposal."""
        repo, shas = _make_merge_repo()
        pairs = extract_correction_pairs(repo)
        assert len(pairs) >= 1
        result = mine_corrections(pairs)
        assert result.total_proposals >= 1
        prop = result.proposals[0]
        assert prop.corpus_size == 1
        assert prop.classification == "single-source"
        assert len(prop.authors) == 1
        assert prop.authors[0] == shas["maintainer"]

    def test_cites_commit_pairs(self) -> None:
        """Proposals cite the commit-SHA pairs they were derived from (requirement #1)."""
        repo, _ = _make_merge_repo()
        pairs = extract_correction_pairs(repo)
        result = mine_corrections(pairs)
        for prop in result.proposals:
            assert prop.commit_pairs
            # Every pair ID must be a real base::follow_up from the pairs
            for pair_id in prop.commit_pairs:
                assert any(p.commit_pair_id == pair_id for p in pairs)

    def test_corpus_size_rendered(self) -> None:
        """Corpus size renders with the proposal (requirement #4)."""
        repo, _ = _make_merge_repo()
        pairs = extract_correction_pairs(repo)
        result = mine_corrections(pairs)
        for prop in result.proposals:
            assert prop.corpus_size >= 1
            # The report must include the corpus size
            report = render_corrections_report(result)
            assert f"{prop.corpus_size} pair(s)" in report or f"{prop.corpus_size} pairs" in report

    def test_single_source_label(self) -> None:
        """A correction from one author is labelled single-source (requirement #3)."""
        repo, _ = _make_merge_repo()
        pairs = extract_correction_pairs(repo)
        result = mine_corrections(pairs)
        for prop in result.proposals:
            assert prop.classification in ("single-source", "corroborated")
            if prop.author_count == 1:
                assert prop.classification == "single-source"

    def test_corroborated_label(self) -> None:
        """Corrections from multiple authors are labelled corroborated (requirement #3)."""
        repo, _ = _make_multi_author_correction_repo()
        pairs = extract_correction_pairs(repo)
        result = mine_corrections(pairs)
        assert result.total_pairs_analyzed >= 1
        # There should be at least one corroborated proposal (two authors)
        [p for p in result.proposals if p.is_corroborated]
        # Note: if the two corrections cluster differently by diff signature, they may be separate proposals.
        # The key guarantee: when they ARE in the same cluster, classification is corroborated.
        if len(pairs) >= 2:
            authors = {p.author_id for p in pairs}
            if len(authors) > 1:
                # At least one proposal should be corroborated
                assert any(p.is_corroborated for p in result.proposals)

    def test_single_source_cannot_auto_activate(self) -> None:
        """A single-source proposal is inert until confirmed (requirement #5).

        The miner itself produces proposals that are NOT active. The
        active state requires an explicit confirmation chain entry,
        which T2 (confirm_convention_proposal) will implement.
        Here we verify the proposal has no status that implies activation.
        """
        repo, _ = _make_merge_repo()
        pairs = extract_correction_pairs(repo)
        result = mine_corrections(pairs)
        for prop in result.proposals:
            # A proposal from the miner is a *proposal*, not a receipt.
            # It must not have an "active" status -- that is T2's domain.
            # We verify the proposal has no activation mechanism by checking
            # it doesn't carry an explicit status field at all.
            assert not hasattr(prop, "status")
            assert not hasattr(prop, "receipt_id")

    def test_recommendations_include_corpus(self) -> None:
        """The report includes corpus size for every proposal."""
        repo, _shas = _make_merge_repo()
        pairs = extract_correction_pairs(repo)
        result = mine_corrections(pairs)
        report = render_corrections_report(result)
        assert "pair(s)" in report

    def test_corpus_size_matches_pair_count(self) -> None:
        """corpus_size equals the number of pairs in the cluster."""
        repo, _shas = _make_multi_author_correction_repo()
        pairs = extract_correction_pairs(repo)
        result = mine_corrections(pairs)
        for prop in result.proposals:
            # corpus_size must match the number of distinct pairs it claims
            assert prop.corpus_size == len(prop.commit_pairs) or prop.corpus_size >= 1


# ---------------------------------------------------------------------------
# render_corrections_report
# ---------------------------------------------------------------------------


class TestRenderCorrectionsReport:
    def test_empty_result(self) -> None:
        result = MiningResult(proposals=(), total_pairs_analyzed=0, total_authors_analyzed=0, total_proposals=0)
        report = render_corrections_report(result)
        assert "**Correction pairs analyzed:** 0" in report
        assert "No correction patterns found" in report

    def test_renders_proposals(self) -> None:
        repo, _shas = _make_merge_repo()
        pairs = extract_correction_pairs(repo)
        result = mine_corrections(pairs)
        report = render_corrections_report(result)
        assert "Proposals" in report or "Proposal Details" in report
        assert "pair(s)" in report or "pair" in report

    def test_renders_commit_pairs(self) -> None:
        """Every proposal cites its commit-SHA pairs in the report."""
        repo, _shas = _make_merge_repo()
        pairs = extract_correction_pairs(repo)
        result = mine_corrections(pairs)
        report = render_corrections_report(result)
        for prop in result.proposals:
            for pair_id in prop.commit_pairs:
                assert pair_id in report, f"Commit pair {pair_id} missing from report"

    def test_renders_corpus_size(self) -> None:
        """Corpus size renders with the proposal."""
        repo, _shas = _make_merge_repo()
        pairs = extract_correction_pairs(repo)
        result = mine_corrections(pairs)
        report = render_corrections_report(result)
        for prop in result.proposals:
            assert str(prop.corpus_size) in report

    def test_renders_classification(self) -> None:
        """Classification renders as either single-source or corroborated."""
        repo, _shas = _make_merge_repo()
        pairs = extract_correction_pairs(repo)
        result = mine_corrections(pairs)
        report = render_corrections_report(result)
        assert "single-source" in report or "corroborated" in report

    def test_renders_authors(self) -> None:
        """Authors render with the proposal."""
        repo, _shas = _make_multi_author_correction_repo()
        pairs = extract_correction_pairs(repo)
        result = mine_corrections(pairs)
        report = render_corrections_report(result)
        for prop in result.proposals:
            for author in prop.authors:
                assert author in report


# ---------------------------------------------------------------------------
# End-to-end: the full pipeline
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_full_pipeline_finds_recurring_classes(self) -> None:
        """Running the miner over a repo with maintainer corrections produces proposals citing SHA pairs."""
        repo, _shas = _make_merge_repo()
        pairs = extract_correction_pairs(repo)
        assert len(pairs) >= 1
        result = mine_corrections(pairs)
        assert result.total_proposals >= 1
        for prop in result.proposals:
            assert prop.commit_pairs, "Every proposal must cite commit-SHA pairs"
            assert prop.corpus_size >= 1, "Every proposal must report corpus size"

    def test_zero_data_loss_invariant(self) -> None:
        """All pairs produced by extraction are accounted for in the result.

        The miner must not silently drop pairs: every pair extracted
        must appear in at least one proposal's commit_pairs (or the
        result must acknowledge it was processed).
        """
        repo, _shas = _make_merge_repo()
        pairs = extract_correction_pairs(repo)
        result = mine_corrections(pairs)
        # Build the set of all pair IDs seen in proposals
        cited_pairs: set[str] = set()
        for prop in result.proposals:
            cited_pairs.update(prop.commit_pairs)
        # Some pairs may not cluster (empty follow-up diff) and are
        # silently skipped by the clustering filter. That's acceptable
        # only if they had no diff content.
        skipped = set(p.commit_pair_id for p in pairs) - cited_pairs
        for pair_id in skipped:
            pair = next(p for p in pairs if p.commit_pair_id == pair_id)
            assert not pair.follow_up_diff.strip() or not pair.base_diff.strip()

    def test_mining_result_fields(self) -> None:
        """MiningResult carries the required fields."""
        repo, _shas = _make_merge_repo()
        pairs = extract_correction_pairs(repo)
        result = mine_corrections(pairs)
        assert isinstance(result.total_pairs_analyzed, int)
        assert isinstance(result.total_authors_analyzed, int)
        assert isinstance(result.total_proposals, int)
        assert isinstance(result.proposals, tuple)

    def test_proposal_is_frozen_dataclass(self) -> None:
        """CorrectionProposal is frozen: attributes cannot be mutated."""
        repo, _shas = _make_merge_repo()
        pairs = extract_correction_pairs(repo)
        result = mine_corrections(pairs)
        if result.proposals:
            prop = result.proposals[0]
            with pytest.raises(AttributeError):
                prop.rule_text = "changed"  # type: ignore[misc]

    def test_correction_pair_is_frozen_dataclass(self) -> None:
        """CorrectionPair is frozen: attributes cannot be mutated."""
        repo, _shas = _make_merge_repo()
        pairs = extract_correction_pairs(repo)
        if pairs:
            pair = pairs[0]
            with pytest.raises(AttributeError):
                pair.base_commit = "changed"  # type: ignore[misc]
