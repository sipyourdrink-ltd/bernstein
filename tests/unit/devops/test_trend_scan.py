"""Tests for the trend-scan scheduled job.

Coverage:

* score_item: keyword + boost + negative + length normalisation.
* filter_items: dedup + min_score cutoff + deterministic ordering.
* classify_gap: backlog duplicate, recently-closed, new.
* run_scan: end-to-end with a stub fetcher, including rollup file shape.
* CLI: ``bernstein trend-scan run --offline-stub`` smoke test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.trend_scan_cmd import trend_scan_group
from bernstein.core.devops.trend_scan import (
    Candidate,
    RawItem,
    SourceSpec,
    TrendScanConfig,
    classify_gap,
    filter_items,
    load_backlog_keywords,
    load_default_sources,
    run_scan,
    score_item,
)


@pytest.fixture
def spec() -> SourceSpec:
    return SourceSpec(
        name="example",
        tier=1,
        keywords=("python", "release"),
        boost_keywords=("security",),
        negative_keywords=("spam",),
        min_score=0.1,
    )


# ---------------------------------------------------------------------------
# score_item
# ---------------------------------------------------------------------------


def test_score_item_hits_required_keyword(spec: SourceSpec) -> None:
    item = RawItem(title="Python 3.13 release", url="https://example/1", ts="2026-05-19T00:00:00Z")
    score, matched = score_item(item, spec)
    assert score > 0
    assert "python" in matched
    assert "release" in matched


def test_score_item_misses_when_no_required_keyword(spec: SourceSpec) -> None:
    item = RawItem(title="Random news", url="https://example/2", ts="2026-05-19T00:00:00Z")
    score, matched = score_item(item, spec)
    assert score == 0.0
    assert matched == ()


def test_score_item_negative_keyword_zeroes_score(spec: SourceSpec) -> None:
    item = RawItem(title="Python release", url="https://example/3", ts="2026-05-19T00:00:00Z", body="spam content")
    score, _ = score_item(item, spec)
    assert score == 0.0


def test_score_item_boost_keyword_increases_score(spec: SourceSpec) -> None:
    plain = RawItem(title="Python release notes", url="https://example/4", ts="2026-05-19T00:00:00Z")
    boosted = RawItem(
        title="Python release security patch",
        url="https://example/5",
        ts="2026-05-19T00:00:00Z",
    )
    plain_score, _ = score_item(plain, spec)
    boosted_score, _ = score_item(boosted, spec)
    assert boosted_score > plain_score


def test_score_item_empty_text_returns_zero(spec: SourceSpec) -> None:
    item = RawItem(title="", url="", ts="")
    score, matched = score_item(item, spec)
    assert score == 0.0
    assert matched == ()


# ---------------------------------------------------------------------------
# filter_items
# ---------------------------------------------------------------------------


def test_filter_items_drops_below_min_score(spec: SourceSpec) -> None:
    high = RawItem(title="Python release security", url="https://example/a", ts="2026-05-19T00:00:00Z")
    low_kw = RawItem(title="Frog photo", url="https://example/b", ts="2026-05-19T00:00:00Z")
    survivors = filter_items([high, low_kw], spec)
    urls = [item.url for item, _, _ in survivors]
    assert "https://example/a" in urls
    assert "https://example/b" not in urls


def test_filter_items_dedups_by_fingerprint(spec: SourceSpec) -> None:
    one = RawItem(title="Python release", url="https://example/x", ts="2026-05-19T00:00:00Z")
    dup = RawItem(title="Python release", url="https://example/x", ts="2026-05-19T00:00:00Z")
    survivors = filter_items([one, dup], spec)
    assert len(survivors) == 1


def test_filter_items_orders_by_score_then_url(spec: SourceSpec) -> None:
    # Higher: two required + one boost keyword in one short title.
    higher = RawItem(
        title="Python release notes security",
        url="https://example/z",
        ts="2026-05-19T00:00:00Z",
    )
    # Lower: one required keyword only, longer body that depresses the score.
    lower = RawItem(
        title="Python news",
        url="https://example/a",
        ts="2026-05-19T00:00:00Z",
        body="unrelated padding word salad longer document content",
    )
    survivors = filter_items([lower, higher], spec)
    assert survivors, "expected at least one survivor"
    assert survivors[0][0].url == "https://example/z"


# ---------------------------------------------------------------------------
# classify_gap
# ---------------------------------------------------------------------------


def test_classify_gap_marks_duplicate_for_open_backlog(tmp_path: Path) -> None:
    backlog_tokens = {
        "open/some-ticket.md": {"python", "release", "patch", "operator", "rollup"},
    }
    status, refs = classify_gap(
        {"python", "release", "patch", "operator", "rollup"},
        backlog_tokens,
        [],
    )
    assert status == "duplicate"
    assert refs == ("open/some-ticket.md",)


def test_classify_gap_marks_recently_closed_from_closed_issues() -> None:
    closed = [("ISSUE-123", {"python", "release", "security", "patch", "rollup"})]
    status, refs = classify_gap(
        {"python", "release", "security", "patch", "rollup"},
        {},
        closed,
    )
    assert status == "recently-closed"
    assert refs == ("ISSUE-123",)


def test_classify_gap_new_when_no_overlap() -> None:
    status, refs = classify_gap({"python", "release"}, {}, [])
    assert status == "new"
    assert refs == ()


def test_classify_gap_prefers_open_over_closed() -> None:
    tokens = {"python", "release", "patch", "operator", "rollup"}
    backlog_tokens = {
        "open/foo.md": tokens,
        "closed/old.md": tokens,
    }
    closed = [("ISSUE-1", tokens)]
    status, refs = classify_gap(tokens, backlog_tokens, closed)
    assert status == "duplicate"
    assert refs[0].startswith("open/")


# ---------------------------------------------------------------------------
# load_backlog_keywords
# ---------------------------------------------------------------------------


def test_load_backlog_keywords_returns_empty_when_dir_missing(tmp_path: Path) -> None:
    result = load_backlog_keywords(tmp_path / "does-not-exist")
    assert result == {}


def test_load_backlog_keywords_indexes_markdown(tmp_path: Path) -> None:
    (tmp_path / "open").mkdir()
    (tmp_path / "open" / "feat-x.md").write_text("# Feature X\n\nPython release rollup.\n", encoding="utf-8")
    result = load_backlog_keywords(tmp_path)
    assert "open/feat-x.md" in result
    assert "python" in result["open/feat-x.md"]


# ---------------------------------------------------------------------------
# run_scan
# ---------------------------------------------------------------------------


def test_run_scan_writes_rollup_and_json(tmp_path: Path) -> None:
    sources = (
        SourceSpec(
            name="stub-source",
            tier=1,
            keywords=("python",),
            min_score=0.1,
        ),
    )

    def fetcher(spec: SourceSpec) -> list[RawItem]:
        return [
            RawItem(
                title="Python 3.13 RC available",
                url="https://example/py-3.13",
                ts="2026-05-19T00:00:00Z",
            ),
        ]

    config = TrendScanConfig(
        sources=sources,
        backlog_dir=tmp_path / "backlog",
        rollup_dir=tmp_path / "rollup",
    )
    result = run_scan(
        config,
        fetcher=fetcher,
        now_iso=lambda: "2026-05-19T12:00:00Z",
    )

    assert result.rollup_path.exists()
    md = result.rollup_path.read_text(encoding="utf-8")
    assert "Trend scan rollup" in md
    assert "stub-source" in md
    assert "Python 3.13 RC available" in md

    json_path = result.rollup_path.with_suffix(".json")
    assert json_path.exists()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["generated_at"] == "2026-05-19T12:00:00Z"
    assert payload["candidates"][0]["source"] == "stub-source"


def test_run_scan_empty_result_writes_no_candidates_message(tmp_path: Path) -> None:
    config = TrendScanConfig(
        sources=load_default_sources(),
        backlog_dir=tmp_path / "backlog",
        rollup_dir=tmp_path / "rollup",
    )
    result = run_scan(
        config,
        fetcher=lambda _spec: [],
        now_iso=lambda: "2026-05-19T12:00:00Z",
    )
    md = result.rollup_path.read_text(encoding="utf-8")
    assert "No candidates passed" in md
    assert result.candidates == ()


def test_run_scan_marks_duplicate_against_backlog(tmp_path: Path) -> None:
    backlog = tmp_path / "backlog" / "open"
    backlog.mkdir(parents=True)
    (backlog / "py-313-tracking.md").write_text(
        "# Python 3.13 rollup tracking\n\nrelease security patch operator backlog\n",
        encoding="utf-8",
    )

    sources = (
        SourceSpec(
            name="stub-source",
            tier=1,
            keywords=("python", "release"),
            boost_keywords=("security", "patch"),
            min_score=0.1,
        ),
    )

    def fetcher(_spec: SourceSpec) -> list[RawItem]:
        return [
            RawItem(
                title="Python 3.13 release notes",
                url="https://example/py-313",
                ts="2026-05-19T00:00:00Z",
                body="security patch operator backlog rollup tracking",
            ),
        ]

    config = TrendScanConfig(
        sources=sources,
        backlog_dir=tmp_path / "backlog",
        rollup_dir=tmp_path / "rollup",
    )
    result = run_scan(config, fetcher=fetcher, now_iso=lambda: "2026-05-19T12:00:00Z")

    assert len(result.candidates) == 1
    assert result.candidates[0].gap_status == "duplicate"
    assert any(ref.startswith("open/") for ref in result.candidates[0].related_refs)


def test_run_scan_caps_per_source(tmp_path: Path) -> None:
    sources = (SourceSpec(name="noisy", tier=1, keywords=("python",), min_score=0.0),)

    def fetcher(_spec: SourceSpec) -> list[RawItem]:
        return [
            RawItem(title=f"Python item {i}", url=f"https://example/{i}", ts="2026-05-19T00:00:00Z") for i in range(20)
        ]

    config = TrendScanConfig(
        sources=sources,
        backlog_dir=tmp_path / "backlog",
        rollup_dir=tmp_path / "rollup",
        max_candidates_per_source=3,
    )
    result = run_scan(config, fetcher=fetcher, now_iso=lambda: "2026-05-19T12:00:00Z")
    assert len(result.candidates) == 3


# ---------------------------------------------------------------------------
# Candidate.to_dict
# ---------------------------------------------------------------------------


def test_candidate_to_dict_lists_collections() -> None:
    cand = Candidate(
        source="s",
        tier=1,
        title="t",
        url="u",
        ts="2026-05-19T00:00:00Z",
        score=1.0,
        matched_keywords=("a", "b"),
        gap_status="new",
        related_refs=("ref-1",),
    )
    data = cand.to_dict()
    assert data["matched_keywords"] == ["a", "b"]
    assert data["related_refs"] == ["ref-1"]
    assert data["gap_status"] == "new"


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------


def test_cli_offline_stub_writes_empty_rollup(tmp_path: Path) -> None:
    runner = CliRunner()
    rollup_dir = tmp_path / "rollup"
    backlog_dir = tmp_path / "backlog"
    backlog_dir.mkdir()
    result = runner.invoke(
        trend_scan_group,
        [
            "run",
            "--tier",
            "all",
            "--rollup-dir",
            str(rollup_dir),
            "--backlog-dir",
            str(backlog_dir),
            "--offline-stub",
        ],
    )
    assert result.exit_code == 0, result.output
    files = list(rollup_dir.glob("rollup-*.md"))
    assert len(files) == 1
    md = files[0].read_text(encoding="utf-8")
    assert "No candidates passed" in md


def test_cli_without_fetcher_exits_naming_the_flag(tmp_path: Path) -> None:
    runner = CliRunner()
    rollup_dir = tmp_path / "rollup"
    backlog_dir = tmp_path / "backlog"
    backlog_dir.mkdir()
    result = runner.invoke(
        trend_scan_group,
        [
            "run",
            "--tier",
            "all",
            "--rollup-dir",
            str(rollup_dir),
            "--backlog-dir",
            str(backlog_dir),
        ],
    )
    assert result.exit_code == 2, result.output
    assert "--fetcher-cmd" in result.output
    assert not list(rollup_dir.glob("rollup-*.md"))


def test_cli_unknown_tier_returns_error(tmp_path: Path) -> None:
    runner = CliRunner()
    backlog_dir = tmp_path / "backlog"
    backlog_dir.mkdir()
    # Build a sources file with only tier 2 sources, then ask for tier 1.
    sources_path = tmp_path / "sources.json"
    sources_path.write_text(
        json.dumps([{"name": "only-2", "tier": 2, "keywords": ["python"]}]),
        encoding="utf-8",
    )
    result = runner.invoke(
        trend_scan_group,
        [
            "run",
            "--tier",
            "1",
            "--rollup-dir",
            str(tmp_path / "rollup"),
            "--backlog-dir",
            str(backlog_dir),
            "--sources",
            str(sources_path),
            "--offline-stub",
        ],
    )
    assert result.exit_code == 2
