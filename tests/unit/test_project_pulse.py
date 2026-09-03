"""Unit tests for ``scripts/project_pulse.py``.

Four properties carry the weight here, because the page is published
unattended every week into an issue everyone can read:

* ``render`` and ``render_svg`` are pure functions -- identical input,
  byte-identical output. The weekly job upserts one issue body and pushes
  the card to a data branch, so a renderer that reorders a dict or formats a
  float differently would rewrite both on every run and bury real movement
  in the noise.
* ``collect`` fails closed. A page built from a partly-failed collection
  would publish zeros that read as "quiet week" instead of "the query
  broke", so any API failure must abort with a non-zero exit and leave no
  output file behind. The history is held to the same standard: a corrupt
  file is an error, never a silently restarted series.
* The rendered page and card carry aggregates only: no individual logins
  beyond the two documented account labels, and no e-mail addresses. The
  history is a strict subset of the same allow-list.
* The card loads nothing from anywhere: no script, no font, no image, no
  URL. It is a picture of the numbers, not a web page.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location("project_pulse", _REPO_ROOT / "scripts" / "project_pulse.py")
assert _SPEC is not None and _SPEC.loader is not None
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)

FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "ci" / "project_pulse.json"

#: The only account labels the page is allowed to name. Everything else is a
#: count. Keep in step with the allow-list at the top of the script.
DOCUMENTED_ACCOUNTS = frozenset({"chernistry", "bernstein-orchestrator"})

#: The collected fields, i.e. the allow-list as it appears on disk.
ALLOW_LIST = frozenset(
    {
        "adapters",
        "commits_main_7d",
        "days_since_last_commit",
        "distinct_outside_authors",
        "generated_at",
        "grabbable",
        "issue_close_lag_hours_median",
        "issues_closed_count",
        "latest_release",
        "merged_prs_by_author_class",
        "pr_merge_lag_hours_median",
        "pr_merged_count",
        "pr_merged_within_24h_pct",
        "readme_translations",
        "repo",
        "windows",
    }
)

_LOGIN_RE = re.compile(r"@([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_SVG_NS = "{http://www.w3.org/2000/svg}"


def _fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _history(weeks: int) -> dict[str, Any]:
    """*weeks* earlier collections, one per week, with moving numbers."""
    history: dict[str, Any] = {"weeks": []}
    for i in range(weeks, 0, -1):
        data = _fixture()
        data["generated_at"] = (
            (_MOD.datetime(2026, 9, 2, tzinfo=_MOD.UTC) - _MOD.timedelta(days=7 * i)).date().isoformat()
        )
        data["pr_merged_count"] = 900 + 13 * i
        data["pr_merge_lag_hours_median"] = 3.0 + 0.4 * i
        data["pr_merged_within_24h_pct"] = 80.0 + i
        history = _MOD.append_history(history, data)
    return history


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_render_is_byte_identical_across_calls() -> None:
    """Two renders of the same input produce the same bytes."""
    first = _MOD.render(_fixture())
    second = _MOD.render(_fixture())
    assert first.encode("utf-8") == second.encode("utf-8")


def test_render_ignores_key_insertion_order() -> None:
    """A re-ordered JSON object renders identically.

    ``collect`` writes with ``sort_keys=True``, but a hand-edited or
    re-serialised ``pulse.json`` must not change the page either.
    """
    data = _fixture()
    shuffled = dict(reversed(list(data.items())))
    assert _MOD.render(shuffled) == _MOD.render(data)


def test_render_carries_no_timestamp_beyond_the_collected_date() -> None:
    """No clock read leaks into the page; only ``generated_at`` dates it."""
    page = _MOD.render(_fixture())
    assert "2026-09-02" in page
    assert not re.search(r"\d{2}:\d{2}:\d{2}", page), "a wall-clock time would change the body on every run"


def test_card_is_byte_identical_across_calls_and_themes_differ() -> None:
    """The card is as pure as the page, in both colour schemes."""
    light = _MOD.render_svg(_fixture(), _history(7), "light")
    assert light == _MOD.render_svg(_fixture(), _history(7), "light")
    dark = _MOD.render_svg(_fixture(), _history(7), "dark")
    assert dark == _MOD.render_svg(_fixture(), _history(7), "dark")
    assert light != dark
    assert not re.search(r"\d{2}:\d{2}:\d{2}", light)


def test_headline_states_the_median_merge_lag_and_links_grabbable_issues() -> None:
    """The page answers 'will my PR be reviewed?' before anything else."""
    page = _MOD.render(_fixture())
    headline = page.split("\n\n")[1]
    assert "Median time from pull request opened to merged" in headline
    assert "is%3Aissue+is%3Aopen+label%3Aup-for-grabs+no%3Aassignee" in page


def test_absent_medians_render_as_not_available_rather_than_zero() -> None:
    """An empty window must not read as an instant review turnaround."""
    data = _fixture()
    data["pr_merge_lag_hours_median"] = None
    data["pr_merged_within_24h_pct"] = None
    data["issue_close_lag_hours_median"] = None
    page = _MOD.render(data)
    assert "n/a" in page
    assert "0.0 h" not in page
    card = _MOD.render_svg(data)
    assert "n/a" in card
    assert "0.0 h" not in card
    ET.fromstring(card)


# ---------------------------------------------------------------------------
# The page embeds the card and charts the history
# ---------------------------------------------------------------------------


def test_page_embeds_the_card_for_both_colour_schemes() -> None:
    """One ``<picture>``, light and dark, cache-busted by the collection date."""
    page = _MOD.render(_fixture())
    base = f"https://raw.githubusercontent.com/sipyourdrink-ltd/bernstein/{_MOD.DATA_BRANCH}"
    assert "<picture>" in page
    assert f'srcset="{base}/pulse-dark.svg?v=2026-09-02"' in page
    assert f'src="{base}/pulse.svg?v=2026-09-02"' in page
    assert 'media="(prefers-color-scheme: dark)"' in page


def test_page_honours_an_explicit_image_base() -> None:
    page = _MOD.render(_fixture(), image_base="https://example.invalid/cards/")
    assert 'src="https://example.invalid/cards/pulse.svg?v=2026-09-02"' in page


def test_page_without_history_has_no_trend_section() -> None:
    """A first run has nothing to chart and must not show an empty chart."""
    page = _MOD.render(_fixture())
    assert "## Trend" not in page
    assert "xychart" not in page


def test_page_with_history_charts_the_weekly_series() -> None:
    """The trend charts the last weeks, oldest to newest, current week last."""
    page = _MOD.render(_fixture(), _history(7))
    assert "## Trend" in page
    assert page.count("xychart-beta") == 2
    bars = re.search(r"bar \[([^\]]+)\]", page)
    assert bars is not None
    values = [int(v) for v in bars.group(1).split(", ")]
    assert len(values) == _MOD.TREND_WEEKS
    assert values[-1] == _fixture()["pr_merged_count"]
    assert values[0] == 900 + 13 * 7


def test_page_charts_author_classes_with_counts_only() -> None:
    page = _MOD.render(_fixture())
    assert "pie showData" in page
    assert '"Outside contributors" : 11' in page
    assert '"Maintainer" : 920' in page


# ---------------------------------------------------------------------------
# The card
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_card_is_well_formed_svg(theme: str) -> None:
    root = ET.fromstring(_MOD.render_svg(_fixture(), _history(7), theme))
    assert root.tag == f"{_SVG_NS}svg"
    assert root.get("viewBox") == f"0 0 {_MOD.CARD_WIDTH} {_MOD.CARD_HEIGHT}"
    assert root.get("role") == "img"
    assert root.find(f"{_SVG_NS}title") is not None
    assert root.find(f"{_SVG_NS}desc") is not None


def test_card_loads_nothing_and_runs_nothing() -> None:
    """A picture of the numbers: no script, no fetch, no external resource."""
    card = _MOD.render_svg(_fixture(), _history(7))
    stripped = card.replace('xmlns="http://www.w3.org/2000/svg"', "")
    assert "http://" not in stripped
    assert "https://" not in stripped
    for forbidden in ("<script", "<image", "<foreignObject", "@import", "url(", "<a ", "xlink:href"):
        assert forbidden not in card, f"card must not contain {forbidden!r}"


def test_card_sparklines_need_a_history() -> None:
    """Without earlier weeks there is no line to draw and none is drawn."""
    bare = _MOD.render_svg(_fixture())
    assert 'class="spark"' not in bare
    assert "vs last week" not in bare
    trended = _MOD.render_svg(_fixture(), _history(7))
    assert 'class="spark"' in trended
    points = re.search(r'class="spark"[^>]*points="([^"]+)"', trended)
    assert points is not None
    assert len(points.group(1).split(" ")) == _MOD.TREND_WEEKS
    assert "vs last week" in trended


def test_card_rejects_an_unknown_theme() -> None:
    with pytest.raises(_MOD.PulseError, match="theme"):
        _MOD.render_svg(_fixture(), None, "sepia")


def test_week_over_week_delta_reads_direction_against_what_is_better() -> None:
    """A shorter lag is an improvement; a higher 24-hour share is too."""
    assert _MOD._delta(3.0, 4.0, lower_is_better=True, fmt=_MOD._hours) == ("▼ 1.0 h vs last week", "green")
    assert _MOD._delta(5.0, 4.0, lower_is_better=True, fmt=_MOD._hours) == ("▲ 1.0 h vs last week", "orange")
    assert _MOD._delta(90.0, 88.0, lower_is_better=False, fmt=lambda v: f"{v:.1f} pt") == (
        "▲ 2.0 pt vs last week",
        "green",
    )
    assert _MOD._delta(4.0, 4.02, lower_is_better=True, fmt=_MOD._hours) == ("no change vs last week", "muted")
    assert _MOD._delta(None, 4.0, lower_is_better=True, fmt=_MOD._hours) == ("", "")
    assert _MOD._delta(4.0, None, lower_is_better=True, fmt=_MOD._hours) == ("", "")


# ---------------------------------------------------------------------------
# Privacy: aggregates only
# ---------------------------------------------------------------------------


def test_rendered_page_names_no_undocumented_account() -> None:
    """No ``@login`` other than the two documented account labels."""
    page = _MOD.render(_fixture(), _history(7))
    found = {match.lower() for match in _LOGIN_RE.findall(page)}
    assert found <= DOCUMENTED_ACCOUNTS, f"page names undocumented account(s): {sorted(found - DOCUMENTED_ACCOUNTS)}"


def test_rendered_page_carries_no_email_address() -> None:
    page = _MOD.render(_fixture(), _history(7))
    assert not _EMAIL_RE.findall(page)


def test_rendered_card_names_no_account_and_no_email() -> None:
    """The card's text carries no ``@login`` and no address (CSS at-rules aside)."""
    for theme in ("light", "dark"):
        card = _MOD.render_svg(_fixture(), _history(7), theme)
        text = re.sub(r"<style>.*?</style>", "", card, flags=re.S)
        assert not _LOGIN_RE.findall(text)
        assert not _EMAIL_RE.findall(text)


def test_collected_fields_stay_inside_the_allow_list() -> None:
    """The fixture -- and therefore ``collect``'s shape -- adds no field.

    A new top-level key means someone widened what gets published without
    widening the allow-list comment the page's privacy claim rests on.
    """
    assert set(_fixture()) == ALLOW_LIST


def test_history_rows_are_a_subset_of_the_allow_list() -> None:
    """The history introduces no field the page could not already show."""
    row = _MOD.history_row(_fixture())
    assert set(row) <= ALLOW_LIST
    assert set(_MOD.HISTORY_FIELDS) < ALLOW_LIST


# ---------------------------------------------------------------------------
# History: one row per collection date, capped, never silently restarted
# ---------------------------------------------------------------------------


def test_history_upsert_replaces_the_same_date_and_keeps_order() -> None:
    history = _history(3)
    dates = [row["generated_at"] for row in history["weeks"]]
    assert dates == sorted(dates)
    again = _MOD.append_history(history, _fixture())
    assert len(again["weeks"]) == 4
    twice = _MOD.append_history(again, _fixture())
    assert len(twice["weeks"]) == 4, "a re-run on the same date must replace its row, not duplicate it"
    assert twice["weeks"][-1]["generated_at"] == "2026-09-02"


def test_history_is_capped_to_the_documented_number_of_weeks() -> None:
    history: dict[str, Any] = {"weeks": []}
    for i in range(_MOD.HISTORY_KEEP_WEEKS + 5):
        data = _fixture()
        data["generated_at"] = (
            (_MOD.datetime(2020, 1, 6, tzinfo=_MOD.UTC) + _MOD.timedelta(days=7 * i)).date().isoformat()
        )
        history = _MOD.append_history(history, data)
    assert len(history["weeks"]) == _MOD.HISTORY_KEEP_WEEKS


def test_history_cli_creates_then_updates_the_file(tmp_path: Path) -> None:
    path = tmp_path / "history.json"
    assert _MOD.main(["history", str(FIXTURE), "--history", str(path)]) == 0
    assert len(json.loads(path.read_text(encoding="utf-8"))["weeks"]) == 1
    assert _MOD.main(["history", str(FIXTURE), "--history", str(path)]) == 0
    assert len(json.loads(path.read_text(encoding="utf-8"))["weeks"]) == 1


def test_history_cli_refuses_a_corrupt_file(tmp_path: Path) -> None:
    """A history that does not parse is an error, not a fresh start.

    Restarting the series on a read error would, on push, erase two years
    of weekly rows behind a green run.
    """
    path = tmp_path / "history.json"
    path.write_text("{not json", encoding="utf-8")
    assert _MOD.main(["history", str(FIXTURE), "--history", str(path)]) == 2
    assert path.read_text(encoding="utf-8") == "{not json"
    path.write_text(json.dumps({"weeks": [{"no_date": 1}]}), encoding="utf-8")
    assert _MOD.main(["history", str(FIXTURE), "--history", str(path)]) == 2


def test_render_cli_writes_the_page_and_both_cards(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "out"
    assert _MOD.main(["render", str(FIXTURE), "--svg-dir", str(out)]) == 0
    page = capsys.readouterr().out
    assert page.startswith("# Project pulse\n")
    assert page == _MOD.render(_fixture())
    assert (out / "pulse.svg").read_text(encoding="utf-8") == _MOD.render_svg(_fixture(), None, "light")
    assert (out / "pulse-dark.svg").read_text(encoding="utf-8") == _MOD.render_svg(_fixture(), None, "dark")


# ---------------------------------------------------------------------------
# Fail-closed collection
# ---------------------------------------------------------------------------


class _FailingClient:
    """HTTP layer that fails on the *n*-th call, mimicking a flaky API."""

    def __init__(self, fail_after: int = 0) -> None:
        self.calls = 0
        self._fail_after = fail_after

    def get(self, path: str, params: dict[str, str] | None = None) -> tuple[Any, dict[str, str]]:
        self.calls += 1
        if self.calls > self._fail_after:
            raise _MOD.PulseError(f"GitHub API 503 for {path}")
        return {"total_count": 0, "items": []}, {}


def test_collect_raises_on_the_first_api_failure(tmp_path: Path) -> None:
    with pytest.raises(_MOD.PulseError):
        _MOD.collect(_FailingClient(), "owner/name", _REPO_ROOT, _MOD.datetime.now(tz=_MOD.UTC))
    assert not list(tmp_path.iterdir())


def test_collect_stage_exits_non_zero_and_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI surface, not just the function: no partial page on failure."""
    out = tmp_path / "pulse.json"
    monkeypatch.setenv("GH_TOKEN", "test-token")
    monkeypatch.setattr(_MOD, "GitHubClient", lambda *_a, **_kw: _FailingClient())
    rc = _MOD.main(["collect", "--repo", "owner/name", "--out", str(out), "--repo-root", str(_REPO_ROOT)])
    assert rc == 2
    assert not out.exists(), "a failed collection must not leave a half-written pulse.json"


def test_collect_requires_a_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    out = tmp_path / "pulse.json"
    assert _MOD.main(["collect", "--repo", "owner/name", "--out", str(out)]) == 2
    assert not out.exists()


def test_search_refuses_to_truncate_at_the_api_result_cap() -> None:
    """A window wider than the API can enumerate is an error, not a short list.

    Silently returning the first 1000 of 1500 merged PRs would report a
    median computed on an arbitrary subset.
    """

    class _OverCapClient:
        def get(self, path: str, params: dict[str, str] | None = None) -> tuple[Any, dict[str, str]]:
            return {"total_count": _MOD.SEARCH_RESULT_CAP + 1, "items": []}, {}

    with pytest.raises(_MOD.PulseError, match="result API cap"):
        _MOD._search_items(_OverCapClient(), "repo:owner/name is:pr is:merged")


# ---------------------------------------------------------------------------
# Collection: pull requests
# ---------------------------------------------------------------------------

#: A trimmed recording of the ``search/issues`` response shape for a merged
#: pull request: the fields ``_collect_pull_requests`` actually reads, on
#: four items spanning all three author classes plus one lag outside the
#: 24-hour bucket. Real GitHub responses carry many more fields; keeping
#: only these is what makes it a fixture instead of a live-API snapshot.
PR_SEARCH_ITEMS = _REPO_ROOT / "tests" / "fixtures" / "ci" / "project_pulse_search_items.json"


class _RecordedSearchClient:
    """Replays recorded merged-PR items, sliced by each query's ``merged:`` range.

    ``_collect_pull_requests`` issues one search per 7-day slice of the
    30-day window. Rather than stub a single canned page, this filters the
    fixture by each item's real ``merged_at`` date against the ``start..stop``
    the query under test asks for, so the test exercises slicing plus
    aggregation together -- the same seam that shipped broken when the
    weekly workflow's token could not see pull request data at all and every
    slice came back with ``total_count: 0``.
    """

    def __init__(self, items: list[dict[str, Any]]) -> None:
        self._items = items

    def get(self, path: str, params: dict[str, str] | None = None) -> tuple[Any, dict[str, str]]:
        assert path == "search/issues"
        query = (params or {}).get("q", "")
        match = re.search(r"merged:(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})", query)
        assert match, f"query carries no merged: date range: {query}"
        start, stop = match.group(1), match.group(2)
        matched = [item for item in self._items if start <= item["pull_request"]["merged_at"][:10] <= stop]
        return {"total_count": len(matched), "items": matched}, {}


def test_collect_pull_requests_counts_by_author_class_and_lag() -> None:
    """Merged PRs are counted, classed, and timed from real-shaped search items.

    Before the fix this reproduced the incident directly: replace
    ``_RecordedSearchClient`` with one that returns ``total_count: 0`` for
    every ``is:pr`` query (what the Search API does when the caller cannot
    see pull request data) and every one of these assertions fails, exactly
    as issue #5334 published "Merged PRs (30 d): 0" while the same window's
    issue count was correct.
    """
    items = json.loads(PR_SEARCH_ITEMS.read_text(encoding="utf-8"))
    client = _RecordedSearchClient(items)
    now = _MOD.datetime(2026, 9, 2, 12, 0, tzinfo=_MOD.UTC)

    result = _MOD._collect_pull_requests(client, "sipyourdrink-ltd/bernstein", now)

    assert result["pr_merged_count"] == 4
    assert result["merged_prs_by_author_class"] == {"outside": 2, "maintainer": 1, "automation": 1}
    # Lags: outside 5.0h, maintainer 1.0h, automation 2.0h, outside 36.0h.
    assert result["pr_merge_lag_hours_median"] == 3.5
    assert result["pr_merged_within_24h_pct"] == 75.0


def test_collect_outside_authors_counts_distinct_logins_only() -> None:
    """Cardinality of outside logins, not a raw item count."""
    items = json.loads(PR_SEARCH_ITEMS.read_text(encoding="utf-8"))
    client = _RecordedSearchClient(items)
    now = _MOD.datetime(2026, 9, 2, 12, 0, tzinfo=_MOD.UTC)

    assert _MOD._collect_outside_authors(client, "sipyourdrink-ltd/bernstein", now) == 2


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("user", "expected"),
    [
        ({"login": "bernstein-orchestrator[bot]", "type": "Bot"}, "automation"),
        ({"login": "dependabot[bot]", "type": "Bot"}, "automation"),
        ({"login": "chernistry", "type": "User"}, "maintainer"),
        ({"login": "a-first-time-contributor", "type": "User"}, "outside"),
    ],
)
def test_author_class(user: dict[str, str], expected: str) -> None:
    """Any bot lands in automation, so 'outside' is never inflated by one."""
    assert _MOD._author_class(user) == expected


def test_slices_cover_the_window_without_gaps_or_overlap() -> None:
    """Sliced search windows must tile the period exactly once."""
    now = _MOD.datetime(2026, 9, 2, 11, 30, tzinfo=_MOD.UTC)
    slices = _MOD._slices(now, 30)
    assert slices[0][0] == "2026-08-03"
    assert slices[-1][1] == "2026-09-02"
    for (_, prev_end), (next_start, _) in itertools.pairwise(slices):
        expected = _MOD.datetime.strptime(prev_end, "%Y-%m-%d") + _MOD.timedelta(days=1)
        assert next_start == expected.date().isoformat()


def test_slices_do_not_depend_on_the_hour_of_the_run() -> None:
    morning = _MOD._slices(_MOD.datetime(2026, 9, 2, 3, 0, tzinfo=_MOD.UTC), 30)
    evening = _MOD._slices(_MOD.datetime(2026, 9, 2, 23, 0, tzinfo=_MOD.UTC), 30)
    assert morning == evening


# ---------------------------------------------------------------------------
# Workflow shape
# ---------------------------------------------------------------------------

WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "project-pulse.yml"


def _workflow() -> dict[str, Any]:
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), "project-pulse.yml is not a mapping"
    return doc


def test_workflow_runs_weekly_and_on_demand() -> None:
    triggers = _workflow().get(True, _workflow().get("on"))
    assert isinstance(triggers, dict)
    assert "workflow_dispatch" in triggers
    cron = str(triggers["schedule"][0]["cron"])
    assert cron.split()[-1] != "*", f"cron {cron!r} fires daily, not weekly"


def test_workflow_asks_for_no_more_than_it_needs() -> None:
    """Default-deny at the top; the one job writes the data branch and the issue, and reads pull requests.

    ``pull-requests: read`` is load-bearing, not decoration: the Search API
    silently returns ``total_count: 0`` for every ``is:pr`` query -- no
    error -- when the calling token cannot see pull request data, which is
    exactly how issue #5334 published zero for every PR metric while the
    ``is:issue`` metrics in the same run were correct.
    """
    doc = _workflow()
    assert doc["permissions"] == {}
    assert doc["jobs"]["pulse"]["permissions"] == {
        "contents": "write",
        "issues": "write",
        "pull-requests": "read",
    }


def test_workflow_uses_only_the_built_in_token() -> None:
    """No new secret: the page is built from public data."""
    assert "secrets." not in WORKFLOW.read_text(encoding="utf-8")


def test_workflow_publishes_generated_output_only_to_the_data_branch() -> None:
    """Generated output goes to the data branch and the artifact, never to main.

    The branch name is pinned in one place per side -- ``DATA_BRANCH`` in the
    workflow and in the script -- and the two must agree, or the page would
    embed a card from a branch the workflow never writes.
    """
    body = WORKFLOW.read_text(encoding="utf-8")
    assert _workflow()["env"]["DATA_BRANCH"] == _MOD.DATA_BRANCH
    assert "HEAD:refs/heads/${DATA_BRANCH}" in body
    assert "--force" not in body
    assert "HEAD:main" not in body
    assert "refs/heads/main" not in body
    assert "upload-artifact" in body


def test_documented_page_exists_embeds_the_card_and_is_in_the_nav() -> None:
    doc_path = _REPO_ROOT / "docs" / "project-pulse.md"
    assert doc_path.is_file()
    body = doc_path.read_text(encoding="utf-8")
    assert "<picture>" in body
    assert f"/{_MOD.DATA_BRANCH}/pulse.svg" in body
    assert f"`{_MOD.DATA_BRANCH}` branch" in body
    assert "project-pulse.md" in (_REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8")
