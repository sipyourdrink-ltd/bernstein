"""Tests for CI-failure post-mortem ingestion.

The original DLQ / postmortem coverage lives in
``tests/unit/test_incident_synthesizer.py``. This file focuses on the
new ``CIFailurePostmortem`` shape and the
``scripts/scrape_ci_postmortems`` helpers.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from bernstein.eval.incident_synthesizer import (
    CIFailurePostmortem,
    IncidentSynthesizer,
    incident_cases_dir,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def _load_scraper() -> Any:
    """Load ``scripts/scrape_ci_postmortems`` once per session.

    The scripts directory is not a package, so we import by path.
    """
    if "scrape_ci_postmortems" in sys.modules:
        return sys.modules["scrape_ci_postmortems"]
    repo_root = Path(__file__).resolve().parents[3]
    script_path = repo_root / "scripts" / "scrape_ci_postmortems.py"
    spec = importlib.util.spec_from_file_location("scrape_ci_postmortems", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["scrape_ci_postmortems"] = module
    spec.loader.exec_module(module)
    return module


SCRAPER = _load_scraper()


# ---------------------------------------------------------------------------
# CIFailurePostmortem dataclass
# ---------------------------------------------------------------------------


class TestCIFailurePostmortemDataclass:
    def test_required_fields_and_defaults(self) -> None:
        pm = CIFailurePostmortem(pr_number=1, commit_sha="abc")
        assert pm.pr_number == 1
        assert pm.commit_sha == "abc"
        assert pm.failing_test == ""
        assert pm.error_line == ""
        assert pm.fixup_commits == ()

    def test_is_hashable_and_frozen(self) -> None:
        import dataclasses

        pm = CIFailurePostmortem(pr_number=2, commit_sha="def")
        # Frozen dataclasses must be hashable.
        assert hash(pm) == hash(pm)
        with pytest.raises(dataclasses.FrozenInstanceError):
            pm.pr_number = 3  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Synthesizer dispatch on CIFailurePostmortem
# ---------------------------------------------------------------------------


class TestSynthesizeFromCiPostmortem:
    def test_one_postmortem_produces_one_case(self, tmp_path: Path) -> None:
        pm = CIFailurePostmortem(
            pr_number=1793,
            commit_sha="0123456789abcdef" * 2,
            failing_test="tests/unit/eval/test_widget.py::test_foo",
            error_line="AssertionError: expected 1 got 2",
            fixup_commits=(
                "fix(ci): retry flaky network harness",
                "fix(tests): widen widget expected output",
            ),
        )
        synth = IncidentSynthesizer(tmp_path)
        case = synth.synthesize_from_ci_postmortem(pm)
        assert case is not None
        assert case.severity == "P1"
        assert case.source_incident == f"ci-postmortem:{pm.pr_number}:{pm.commit_sha}"
        assert case.owner == "ci-fixer"
        assert "ci_failure" in case.tags
        assert "regression" in case.tags
        assert "test_failure" in case.tags
        # The fix-up commit subjects must be visible in the synthesised prompt
        # so the candidate agent has the breadcrumb to reproduce the regression.
        assert "fix(ci): retry flaky network harness" in case.prompt
        assert "tests/unit/eval/test_widget.py" in case.prompt
        assert "AssertionError" in case.prompt

    def test_empty_fixups_skipped(self, tmp_path: Path) -> None:
        pm = CIFailurePostmortem(pr_number=99, commit_sha="aaaa", fixup_commits=())
        synth = IncidentSynthesizer(tmp_path)
        assert synth.synthesize_from_ci_postmortem(pm) is None

    def test_missing_sha_skipped(self, tmp_path: Path) -> None:
        pm = CIFailurePostmortem(
            pr_number=99,
            commit_sha="",
            fixup_commits=("fix(ci): a", "fix(tests): b"),
        )
        synth = IncidentSynthesizer(tmp_path)
        assert synth.synthesize_from_ci_postmortem(pm) is None

    def test_dispatch_via_internal_seam(self, tmp_path: Path) -> None:
        pm = CIFailurePostmortem(
            pr_number=1,
            commit_sha="abc",
            fixup_commits=("fix(ci): a", "fix(tests): b"),
        )
        synth = IncidentSynthesizer(tmp_path)
        # _synthesize_eval_case is the single dispatch seam called by
        # both public methods and the iterators.
        case = synth._synthesize_eval_case(pm)
        assert case is not None
        assert case.severity == "P1"


# ---------------------------------------------------------------------------
# JSON record ingestion via sync()
# ---------------------------------------------------------------------------


def _write_record(workdir: Path, record: dict[str, Any]) -> Path:
    ci_dir = workdir / ".sdd" / "reports" / "ci_postmortems"
    ci_dir.mkdir(parents=True, exist_ok=True)
    path = ci_dir / f"pr-{record['pr_number']}-{record['commit_sha'][:12]}.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


class TestSyncIngestsCiPostmortems:
    def test_sync_emits_yaml_case(self, tmp_path: Path) -> None:
        _write_record(
            tmp_path,
            {
                "pr_number": 100,
                "commit_sha": "deadbeefcafe1234",
                "failing_test": "ruff",
                "error_line": "ruff check failed in tests/unit/test_foo.py",
                "fixup_commits": ["fix(ci): a", "fix(lint): b"],
            },
        )
        synth = IncidentSynthesizer(tmp_path)
        result = synth.sync()
        assert len(result.created) == 1
        case = result.created[0]
        assert case.source_incident == "ci-postmortem:100:deadbeefcafe1234"
        assert case.severity == "P1"

        cases_dir = incident_cases_dir(tmp_path)
        files = list(cases_dir.glob("inc-*.yaml"))
        assert len(files) == 1
        body = files[0].read_text(encoding="utf-8")
        assert "source_incident:" in body
        assert "ci-postmortem:100:deadbeefcafe1234" in body
        assert "severity: P1" in body

    def test_sync_is_idempotent_for_ci_postmortems(self, tmp_path: Path) -> None:
        _write_record(
            tmp_path,
            {
                "pr_number": 101,
                "commit_sha": "feedfacecafef00d",
                "failing_test": "",
                "error_line": "",
                "fixup_commits": ["fix(ci): a", "fix(tests): b"],
            },
        )
        synth = IncidentSynthesizer(tmp_path)
        first = synth.sync()
        second = synth.sync()
        assert len(first.created) == 1
        assert len(second.created) == 0
        assert second.skipped_duplicates >= 1

    def test_malformed_records_skipped(self, tmp_path: Path) -> None:
        ci_dir = tmp_path / ".sdd" / "reports" / "ci_postmortems"
        ci_dir.mkdir(parents=True, exist_ok=True)
        (ci_dir / "bad.json").write_text("{not json", encoding="utf-8")
        (ci_dir / "wrong.json").write_text(json.dumps({"pr_number": "not-an-int"}), encoding="utf-8")
        synth = IncidentSynthesizer(tmp_path)
        result = synth.sync()
        assert len(result.created) == 0


# ---------------------------------------------------------------------------
# scrape_ci_postmortems heuristics + idempotency
# ---------------------------------------------------------------------------


class TestFixupCommitDetection:
    """Pin down the fix-up commit regex behaviour.

    A fix-up commit is one of:
      - ``fix(ci):`` / ``fix(tests):`` / ``fix(lint):`` / ``fix(types):``
        / ``fix(typing):`` / ``fix(format):`` / ``fix(coverage):``
      - ``fixup!`` / ``!fixup`` / ``squash!``
      - ``fix ci:`` / ``fix tests:`` / ``fix lint:`` / ``fix typing:``

    The first commit of a PR is treated as the *original feature commit*
    and is never counted as a fix-up regardless of its subject.
    """

    def test_first_commit_never_counted(self) -> None:
        subjects = [
            "fix(ci): set up matrix",
            "feat: real work",
        ]
        # Even though the first subject matches the regex, it is the
        # 'original feature commit' slot by position.
        assert SCRAPER.detect_fixup_commits(subjects) == []

    def test_obvious_ci_fixup_detected(self) -> None:
        subjects = [
            "feat(eval): wire new judge",
            "fix(ci): bump runner image",
        ]
        assert SCRAPER.detect_fixup_commits(subjects) == ["fix(ci): bump runner image"]

    def test_full_pattern_set(self) -> None:
        subjects = [
            "feat: introduce widget",
            "fix(ci): rerun smoke",
            "fix(tests): adjust fixture",
            "fix(lint): satisfy ruff",
            "fix(types): mypy override",
            "fixup! widget defaults",
            "squash! prior",
            "!fixup typo",
            "fix ci: cache npm",
            "fix tests: fixture order",
            "fix typing: stub return",
        ]
        detected = SCRAPER.detect_fixup_commits(subjects)
        # All 10 trailing subjects must match.
        assert len(detected) == 10

    def test_unrelated_commits_not_detected(self) -> None:
        subjects = [
            "feat: introduce widget",
            "docs: improve readme",
            "refactor: split helper",
            "chore: bump dep",
            "feat: another feature",
        ]
        assert SCRAPER.detect_fixup_commits(subjects) == []


class TestSynthesizeRecord:
    def test_single_fixup_does_not_qualify(self) -> None:
        pr = {"number": 1, "mergeCommit": {"oid": "abc123"}}
        record = SCRAPER.synthesize_record(pr, ["feat: x", "fix(ci): one"], ["fix(ci): one"])
        # MIN_FIXUP_COMMITS = 2, so a single fixup does not qualify.
        assert record is None

    def test_two_fixups_qualify(self) -> None:
        pr = {"number": 42, "mergeCommit": {"oid": "abc123def456"}}
        subjects = ["feat: x", "fix(ci): one", "fix(tests): two"]
        fixups = SCRAPER.detect_fixup_commits(subjects)
        record = SCRAPER.synthesize_record(pr, subjects, fixups)
        assert record is not None
        assert record["pr_number"] == 42
        assert record["commit_sha"] == "abc123def456"
        assert record["fixup_commits"] == ["fix(ci): one", "fix(tests): two"]

    def test_missing_merge_commit_skipped(self) -> None:
        pr: dict[str, Any] = {"number": 7, "mergeCommit": None}
        record = SCRAPER.synthesize_record(pr, ["a", "b"], ["fix(ci): b"])
        assert record is None


class TestScraperRun:
    """End-to-end behaviour of ``scrape_ci_postmortems.run`` with a fake gh."""

    def _make_loader(self, mapping: dict[int, list[str]]) -> Callable[[int], list[str]]:
        def loader(pr_num: int) -> list[str]:
            return mapping.get(pr_num, [])

        return loader

    def test_one_fixup_pr_becomes_one_record(self, tmp_path: Path) -> None:
        pr_data = [
            {
                "number": 500,
                "mergeCommit": {"oid": "merge-sha-500"},
                "mergedAt": "",
            },
        ]
        commits = {
            500: [
                "feat(eval): add foo",
                "fix(ci): pin runner image",
                "fix(tests): widen expected output",
            ],
        }
        out_dir = tmp_path / "ci_postmortems"
        emitted = SCRAPER.run(
            repo="dummy/repo",
            since_days=0,
            out_dir=out_dir,
            cases_dir=None,
            dry_run=False,
            pr_data=pr_data,
            commits_loader=self._make_loader(commits),
        )
        assert emitted == 1
        files = list(out_dir.glob("*.json"))
        assert len(files) == 1
        record = json.loads(files[0].read_text(encoding="utf-8"))
        assert record["pr_number"] == 500
        assert record["commit_sha"] == "merge-sha-500"
        assert record["fixup_commits"] == [
            "fix(ci): pin runner image",
            "fix(tests): widen expected output",
        ]

    def test_rerun_is_idempotent(self, tmp_path: Path) -> None:
        pr_data = [
            {"number": 501, "mergeCommit": {"oid": "sha-501"}, "mergedAt": ""},
        ]
        commits = {
            501: [
                "feat: thing",
                "fix(ci): a",
                "fix(tests): b",
            ],
        }
        out_dir = tmp_path / "ci_postmortems"
        loader = self._make_loader(commits)
        first = SCRAPER.run(
            repo="dummy/repo",
            since_days=0,
            out_dir=out_dir,
            cases_dir=None,
            dry_run=False,
            pr_data=pr_data,
            commits_loader=loader,
        )
        second = SCRAPER.run(
            repo="dummy/repo",
            since_days=0,
            out_dir=out_dir,
            cases_dir=None,
            dry_run=False,
            pr_data=pr_data,
            commits_loader=loader,
        )
        assert first == 1
        assert second == 0
        assert len(list(out_dir.glob("*.json"))) == 1

    def test_no_fixups_produces_zero_records(self, tmp_path: Path) -> None:
        pr_data = [
            {"number": 600, "mergeCommit": {"oid": "sha-600"}, "mergedAt": ""},
            {"number": 601, "mergeCommit": {"oid": "sha-601"}, "mergedAt": ""},
        ]
        commits = {
            600: ["feat: a"],
            601: ["feat: b", "docs: c", "chore: d"],
        }
        out_dir = tmp_path / "ci_postmortems"
        emitted = SCRAPER.run(
            repo="dummy/repo",
            since_days=0,
            out_dir=out_dir,
            cases_dir=None,
            dry_run=False,
            pr_data=pr_data,
            commits_loader=self._make_loader(commits),
        )
        assert emitted == 0
        assert not out_dir.exists() or not list(out_dir.glob("*.json"))

    def test_multi_pr_mixed_qualification(self, tmp_path: Path) -> None:
        pr_data = [
            {"number": 700, "mergeCommit": {"oid": "sha-700"}, "mergedAt": ""},
            {"number": 701, "mergeCommit": {"oid": "sha-701"}, "mergedAt": ""},
            {"number": 702, "mergeCommit": {"oid": "sha-702"}, "mergedAt": ""},
        ]
        commits = {
            700: ["feat: a", "fix(ci): x", "fix(tests): y"],  # qualifies
            701: ["feat: b", "fix(ci): only one"],  # does not qualify (< MIN_FIXUP_COMMITS)
            702: ["feat: c", "fix(lint): u", "fix(types): v", "fix(coverage): w"],  # qualifies
        }
        out_dir = tmp_path / "ci_postmortems"
        emitted = SCRAPER.run(
            repo="dummy/repo",
            since_days=0,
            out_dir=out_dir,
            cases_dir=None,
            dry_run=False,
            pr_data=pr_data,
            commits_loader=self._make_loader(commits),
        )
        assert emitted == 2
        files = sorted(p.name for p in out_dir.glob("*.json"))
        assert any("700" in f for f in files)
        assert any("702" in f for f in files)
        assert not any("701" in f for f in files)

    def test_dedup_against_existing_yaml_case(self, tmp_path: Path) -> None:
        # Seed a YAML case that already references the same source_incident
        # key so the scraper recognises the postmortem as already promoted.
        cases_dir = tmp_path / "cases" / "incidents"
        cases_dir.mkdir(parents=True, exist_ok=True)
        (cases_dir / "inc-deadbeefcafe.yaml").write_text(
            "id: inc-deadbeefcafe\n"
            "severity: P1\n"
            'source_incident: "ci-postmortem:800:sha-800"\n'
            "owner: ci-fixer\n"
            "tags: []\n"
            "prompt: |\n"
            "  Reproduce and resolve the CI-failure regression from PR #800.\n",
            encoding="utf-8",
        )

        pr_data = [
            {"number": 800, "mergeCommit": {"oid": "sha-800"}, "mergedAt": ""},
        ]
        commits = {800: ["feat: a", "fix(ci): x", "fix(tests): y"]}
        out_dir = tmp_path / "ci_postmortems"
        emitted = SCRAPER.run(
            repo="dummy/repo",
            since_days=0,
            out_dir=out_dir,
            cases_dir=cases_dir,
            dry_run=False,
            pr_data=pr_data,
            commits_loader=self._make_loader(commits),
        )
        assert emitted == 0

    def test_gh_unavailable_exits_zero(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(SCRAPER, "_gh_available", lambda: False)
        # When pr_data is None the scraper goes through _gh_available.
        emitted = SCRAPER.run(
            repo="dummy/repo",
            since_days=30,
            out_dir=tmp_path / "ci_postmortems",
            cases_dir=None,
            dry_run=False,
        )
        assert emitted == 0


# ---------------------------------------------------------------------------
# Cross-module: scraper -> synthesizer end-to-end
# ---------------------------------------------------------------------------


class TestEndToEndFlow:
    def test_scraper_output_drives_synthesizer(self, tmp_path: Path) -> None:
        """A record emitted by the scraper must be ingested by ``sync()``."""
        pr_data = [
            {"number": 900, "mergeCommit": {"oid": "sha-900-long"}, "mergedAt": ""},
        ]
        commits = {
            900: [
                "feat(eval): new harness",
                "fix(ci): bump action",
                "fix(tests): repair fixture",
            ],
        }
        out_dir = tmp_path / ".sdd" / "reports" / "ci_postmortems"
        emitted = SCRAPER.run(
            repo="dummy/repo",
            since_days=0,
            out_dir=out_dir,
            cases_dir=None,
            dry_run=False,
            pr_data=pr_data,
            commits_loader=lambda pr_num: commits[pr_num],
        )
        assert emitted == 1
        synth = IncidentSynthesizer(tmp_path)
        result = synth.sync()
        assert len(result.created) == 1
        case = result.created[0]
        assert case.source_incident.startswith("ci-postmortem:900:")
        assert case.severity == "P1"
        # Re-run is a pure no-op end-to-end.
        again = synth.sync()
        assert len(again.created) == 0
        assert again.skipped_duplicates >= 1
