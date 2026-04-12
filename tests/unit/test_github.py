"""Unit tests for bernstein.core.github — GitHub Issues integration."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from bernstein.core.github import (
    _HASH_LABEL_PREFIX,
    _LABEL_AUTO,
    _LABEL_CLAIMED,
    _LABEL_EVOLVE,
    GitHubClient,
    GitHubIssue,
    _hash_title,
    _label_color,
)

# ---------------------------------------------------------------------------
# _hash_title
# ---------------------------------------------------------------------------


def test_hash_title_is_8_chars() -> None:
    h = _hash_title("Some proposal title")
    assert len(h) == 8
    assert h.isalnum()


def test_hash_title_case_insensitive() -> None:
    assert _hash_title("Foo Bar") == _hash_title("foo bar")
    assert _hash_title("FOO BAR") == _hash_title("foo bar")


def test_hash_title_strip_whitespace() -> None:
    assert _hash_title("  foo  ") == _hash_title("foo")


def test_hash_title_different_titles_differ() -> None:
    assert _hash_title("Add caching layer") != _hash_title("Remove dead code")


# ---------------------------------------------------------------------------
# GitHubIssue
# ---------------------------------------------------------------------------


def test_issue_is_claimed_true() -> None:
    issue = GitHubIssue(
        number=42,
        title="Improve test coverage",
        url="https://github.com/o/r/issues/42",
        labels=[_LABEL_EVOLVE, _LABEL_CLAIMED],
    )
    assert issue.is_claimed is True


def test_issue_is_claimed_false() -> None:
    issue = GitHubIssue(
        number=7,
        title="Fix import order",
        url="https://github.com/o/r/issues/7",
        labels=[_LABEL_EVOLVE],
    )
    assert issue.is_claimed is False


def test_issue_hash_label_present() -> None:
    h = _hash_title("Add caching")
    issue = GitHubIssue(
        number=10,
        title="Add caching",
        url="",
        labels=[_LABEL_EVOLVE, f"{_HASH_LABEL_PREFIX}{h}"],
    )
    assert issue.hash_label == f"{_HASH_LABEL_PREFIX}{h}"


def test_issue_hash_label_absent() -> None:
    issue = GitHubIssue(number=1, title="x", url="", labels=[_LABEL_EVOLVE])
    assert issue.hash_label is None


def test_issue_from_gh_json() -> None:
    raw = {
        "number": 99,
        "title": "Auto-tune config",
        "url": "https://github.com/o/r/issues/99",
        "labels": [
            {"name": _LABEL_EVOLVE},
            {"name": _LABEL_AUTO},
        ],
        "state": "open",
    }
    issue = GitHubIssue.from_gh_json(raw)
    assert issue.number == 99
    assert issue.title == "Auto-tune config"
    assert _LABEL_EVOLVE in issue.labels
    assert _LABEL_AUTO in issue.labels
    assert issue.state == "open"


# ---------------------------------------------------------------------------
# _label_color
# ---------------------------------------------------------------------------


def test_label_color_known_labels() -> None:
    assert _label_color(_LABEL_EVOLVE) == "0075ca"
    assert _label_color(_LABEL_CLAIMED) == "e4e669"
    assert _label_color(_LABEL_AUTO) == "cfd3d7"


def test_label_color_hash_label() -> None:
    color = _label_color(f"{_HASH_LABEL_PREFIX}abc12345")
    assert color == "d4edda"


def test_label_color_unknown() -> None:
    color = _label_color("some-random-label")
    assert color == "ededed"


# ---------------------------------------------------------------------------
# GitHubClient.available
# ---------------------------------------------------------------------------


def test_available_true_when_gh_exits_zero() -> None:
    client = GitHubClient()
    with patch("bernstein.core.git.github.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        assert client.available is True


def test_available_false_when_gh_exits_nonzero() -> None:
    client = GitHubClient()
    client._available = None  # reset cache
    with patch("bernstein.core.git.github.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1)
        assert client.available is False


def test_available_false_when_gh_not_found() -> None:
    client = GitHubClient()
    client._available = None
    with patch(
        "bernstein.core.github.subprocess.run",
        side_effect=FileNotFoundError,
    ):
        assert client.available is False


def test_available_cached_after_first_check() -> None:
    client = GitHubClient()
    client._available = None
    with patch("bernstein.core.git.github.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        # First call: checks subprocess
        assert client.available is True
        # Second call: uses cached value, no extra subprocess call
        assert client.available is True
        assert mock_run.call_count == 1


# ---------------------------------------------------------------------------
# GitHubClient.fetch_open_evolve_issues
# ---------------------------------------------------------------------------


def _mock_run_ok(data: object) -> MagicMock:
    """Return a mock subprocess.run result with returncode=0 and JSON stdout."""
    m = MagicMock()
    m.returncode = 0
    m.stdout = json.dumps(data)
    m.stderr = ""
    return m


def _mock_run_fail(stderr: str = "error") -> MagicMock:
    m = MagicMock()
    m.returncode = 1
    m.stdout = ""
    m.stderr = stderr
    return m


def test_fetch_open_evolve_issues_returns_list() -> None:
    client = GitHubClient()
    client._available = True

    raw = [
        {
            "number": 1,
            "title": "Proposal A",
            "url": "https://github.com/o/r/issues/1",
            "labels": [{"name": _LABEL_EVOLVE}],
            "state": "open",
        },
        {
            "number": 2,
            "title": "Proposal B",
            "url": "https://github.com/o/r/issues/2",
            "labels": [{"name": _LABEL_EVOLVE}, {"name": _LABEL_CLAIMED}],
            "state": "open",
        },
    ]
    with patch("bernstein.core.git.github.subprocess.run", return_value=_mock_run_ok(raw)):
        issues = client.fetch_open_evolve_issues()

    assert len(issues) == 2
    assert issues[0].number == 1
    assert issues[1].is_claimed is True


def test_fetch_open_evolve_issues_empty_when_unavailable() -> None:
    client = GitHubClient()
    client._available = False
    issues = client.fetch_open_evolve_issues()
    assert issues == []


def test_fetch_open_evolve_issues_empty_on_gh_error() -> None:
    client = GitHubClient()
    client._available = True
    with patch("bernstein.core.git.github.subprocess.run", return_value=_mock_run_fail()):
        issues = client.fetch_open_evolve_issues()
    assert issues == []


def test_fetch_open_evolve_issues_empty_on_bad_json() -> None:
    client = GitHubClient()
    client._available = True
    m = MagicMock(returncode=0, stdout="not json", stderr="")
    with patch("bernstein.core.git.github.subprocess.run", return_value=m):
        issues = client.fetch_open_evolve_issues()
    assert issues == []


# ---------------------------------------------------------------------------
# GitHubClient.find_unclaimed
# ---------------------------------------------------------------------------


def test_find_unclaimed_filters_claimed() -> None:
    client = GitHubClient()
    client._available = True

    raw = [
        {
            "number": 1,
            "title": "A",
            "url": "",
            "state": "open",
            "labels": [{"name": _LABEL_EVOLVE}],
        },
        {
            "number": 2,
            "title": "B",
            "url": "",
            "state": "open",
            "labels": [{"name": _LABEL_EVOLVE}, {"name": _LABEL_CLAIMED}],
        },
        {
            "number": 3,
            "title": "C",
            "url": "",
            "state": "open",
            "labels": [{"name": _LABEL_EVOLVE}],
        },
    ]
    with patch("bernstein.core.git.github.subprocess.run", return_value=_mock_run_ok(raw)):
        unclaimed = client.find_unclaimed()

    assert len(unclaimed) == 2
    assert all(not i.is_claimed for i in unclaimed)
    # Sorted by number ascending (oldest first)
    assert unclaimed[0].number == 1
    assert unclaimed[1].number == 3


# ---------------------------------------------------------------------------
# GitHubClient.find_by_hash
# ---------------------------------------------------------------------------


def test_find_by_hash_matches_correct_issue() -> None:
    client = GitHubClient()
    client._available = True

    title = "Optimise cache eviction"
    h = _hash_title(title)
    hash_label = f"{_HASH_LABEL_PREFIX}{h}"

    raw = [
        {
            "number": 5,
            "title": "Unrelated",
            "url": "",
            "state": "open",
            "labels": [{"name": _LABEL_EVOLVE}],
        },
        {
            "number": 6,
            "title": title,
            "url": "",
            "state": "open",
            "labels": [{"name": _LABEL_EVOLVE}, {"name": hash_label}],
        },
    ]
    with patch("bernstein.core.git.github.subprocess.run", return_value=_mock_run_ok(raw)):
        found = client.find_by_hash(title)

    assert found is not None
    assert found.number == 6


def test_find_by_hash_returns_none_when_no_match() -> None:
    client = GitHubClient()
    client._available = True

    raw = [
        {"number": 1, "title": "Other", "url": "", "state": "open", "labels": [{"name": _LABEL_EVOLVE}]},
    ]
    with patch("bernstein.core.git.github.subprocess.run", return_value=_mock_run_ok(raw)):
        found = client.find_by_hash("Non-existent proposal")

    assert found is None


# ---------------------------------------------------------------------------
# GitHubClient.create_issue
# ---------------------------------------------------------------------------


def test_create_issue_returns_issue_on_success() -> None:
    client = GitHubClient()
    client._available = True

    url = "https://github.com/owner/repo/issues/42"

    def side_effect(args, **kwargs):
        # _ensure_labels calls: gh label create x3, gh issue create
        if "label" in args and "create" in args:
            return MagicMock(returncode=0, stdout="", stderr="")
        # gh issue create
        return MagicMock(returncode=0, stdout=url, stderr="")

    with patch("bernstein.core.git.github.subprocess.run", side_effect=side_effect):
        issue = client.create_issue(title="Improve error messages", body="Some body")

    assert issue is not None
    assert issue.number == 42
    assert issue.url == url
    assert _LABEL_EVOLVE in issue.labels
    assert _LABEL_AUTO in issue.labels


def test_create_issue_returns_none_when_unavailable() -> None:
    client = GitHubClient()
    client._available = False
    issue = client.create_issue(title="X", body="Y")
    assert issue is None


def test_create_issue_returns_none_on_gh_failure() -> None:
    client = GitHubClient()
    client._available = True

    with patch("bernstein.core.git.github.subprocess.run", return_value=_mock_run_fail("permission denied")):
        issue = client.create_issue(title="X", body="Y")

    assert issue is None


# ---------------------------------------------------------------------------
# GitHubClient.claim_issue / unclaim_issue
# ---------------------------------------------------------------------------


def test_claim_issue_returns_true_on_success() -> None:
    client = GitHubClient()
    client._available = True

    def side_effect(args, **kwargs):
        # _ensure_labels + gh issue edit
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("bernstein.core.git.github.subprocess.run", side_effect=side_effect):
        result = client.claim_issue(10)

    assert result is True


def test_claim_issue_returns_false_when_unavailable() -> None:
    client = GitHubClient()
    client._available = False
    assert client.claim_issue(1) is False


def test_unclaim_issue_returns_true_on_success() -> None:
    client = GitHubClient()
    client._available = True
    with patch("bernstein.core.git.github.subprocess.run", return_value=MagicMock(returncode=0, stdout="")):
        assert client.unclaim_issue(5) is True


def test_unclaim_issue_returns_false_when_unavailable() -> None:
    client = GitHubClient()
    client._available = False
    assert client.unclaim_issue(5) is False


# ---------------------------------------------------------------------------
# GitHubClient.close_issue
# ---------------------------------------------------------------------------


def test_close_issue_posts_comment_then_closes() -> None:
    client = GitHubClient()
    client._available = True

    calls_made: list[list[str]] = []

    def side_effect(args, **kwargs):
        calls_made.append(args)
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("bernstein.core.git.github.subprocess.run", side_effect=side_effect):
        result = client.close_issue(3, comment="Applied via PR #7")

    assert result is True
    # Should have called: gh issue comment, then gh issue close
    comment_call = next((c for c in calls_made if "comment" in c), None)
    close_call = next((c for c in calls_made if "close" in c), None)
    assert comment_call is not None
    assert close_call is not None


def test_close_issue_no_comment_skips_comment_call() -> None:
    client = GitHubClient()
    client._available = True

    calls_made: list[list[str]] = []

    def side_effect(args, **kwargs):
        calls_made.append(args)
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("bernstein.core.git.github.subprocess.run", side_effect=side_effect):
        result = client.close_issue(3, comment=None)

    assert result is True
    comment_calls = [c for c in calls_made if "comment" in c]
    assert len(comment_calls) == 0


def test_close_issue_returns_false_when_unavailable() -> None:
    client = GitHubClient()
    client._available = False
    assert client.close_issue(1) is False


# ---------------------------------------------------------------------------
# GitHubClient — repo parameter forwarded
# ---------------------------------------------------------------------------


def test_repo_forwarded_to_gh_commands() -> None:
    client = GitHubClient(repo="myorg/myrepo")
    client._available = True

    raw: list[dict] = []
    captured: list[list[str]] = []

    def side_effect(args, **kwargs):
        captured.append(args)
        return MagicMock(returncode=0, stdout=json.dumps(raw), stderr="")

    with patch("bernstein.core.git.github.subprocess.run", side_effect=side_effect):
        client.fetch_open_evolve_issues()

    cmd = captured[0]
    assert "--repo" in cmd
    assert "myorg/myrepo" in cmd
