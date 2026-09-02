"""Unit tests for ``scripts/project_pulse.py``.

Three properties carry the weight here, because the page is published
unattended every week into an issue everyone can read:

* ``render`` is a pure function -- identical input, byte-identical output.
  The weekly job upserts one issue body, so a renderer that reorders a dict
  or formats a float differently would rewrite the body on every run and
  bury real movement in the noise.
* ``collect`` fails closed. A page built from a partly-failed collection
  would publish zeros that read as "quiet week" instead of "the query
  broke", so any API failure must abort with a non-zero exit and leave no
  output file behind.
* The rendered page carries aggregates only: no individual logins beyond the
  two documented account labels, and no e-mail addresses.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import re
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

_LOGIN_RE = re.compile(r"@([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


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


# ---------------------------------------------------------------------------
# Privacy: aggregates only
# ---------------------------------------------------------------------------


def test_rendered_page_names_no_undocumented_account() -> None:
    """No ``@login`` other than the two documented account labels."""
    page = _MOD.render(_fixture())
    found = {match.lower() for match in _LOGIN_RE.findall(page)}
    assert found <= DOCUMENTED_ACCOUNTS, f"page names undocumented account(s): {sorted(found - DOCUMENTED_ACCOUNTS)}"


def test_rendered_page_carries_no_email_address() -> None:
    page = _MOD.render(_fixture())
    assert not _EMAIL_RE.findall(page)


def test_collected_fields_stay_inside_the_allow_list() -> None:
    """The fixture -- and therefore ``collect``'s shape -- adds no field.

    A new top-level key means someone widened what gets published without
    widening the allow-list comment the page's privacy claim rests on.
    """
    expected = {
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
    assert set(_fixture()) == expected


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
    """Default-deny at the top, read + issues:write on the one job."""
    doc = _workflow()
    assert doc["permissions"] == {}
    assert doc["jobs"]["pulse"]["permissions"] == {"contents": "read", "issues": "write"}


def test_workflow_uses_only_the_built_in_token() -> None:
    """No new secret: the page is built from public data."""
    assert "secrets." not in WORKFLOW.read_text(encoding="utf-8")


def test_workflow_does_not_commit_the_generated_page() -> None:
    """Generated output is upserted into an issue and uploaded, never pushed."""
    body = WORKFLOW.read_text(encoding="utf-8")
    assert "git push" not in body
    assert "git commit" not in body
    assert "upload-artifact" in body


def test_documented_page_exists_and_is_in_the_nav() -> None:
    doc_path = _REPO_ROOT / "docs" / "project-pulse.md"
    assert doc_path.is_file()
    assert "project-pulse.md" in (_REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8")
