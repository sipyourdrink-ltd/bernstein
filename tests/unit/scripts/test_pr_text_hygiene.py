"""Unit tests for ``scripts/check_pr_text_hygiene.py``.

Covers the documented behaviours:

- Empty / whitespace-only title / body / branch is a pass.
- Clean technical text is a pass.
- A forbidden token in title, body, branch, or any commit message
  surfaces as a fail.
- Matching is case-insensitive.
- The script never reads PR labels (the function signature has no
  ``label`` parameter; label honouring lives in the workflow).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from collections.abc import Generator
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_pr_text_hygiene.py"


@pytest.fixture
def hygiene_module() -> Generator[ModuleType, None, None]:
    """Load scripts/check_pr_text_hygiene.py as an importable module."""
    spec = importlib.util.spec_from_file_location(
        "check_pr_text_hygiene_under_test",
        SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


@pytest.fixture
def deny_file(tmp_path: Path) -> Path:
    """Write a small deny-list JSON for the tests to use."""
    path = tmp_path / "deny.json"
    path.write_text(
        json.dumps(
            {
                "denylist": [
                    "cringe",
                    "marketing",
                    "funnel",
                    "Co-Authored-By: Claude",
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def _commit_dump(tmp_path: Path, messages: list[str]) -> Path:
    """Write commit messages to a file in the ``git log %B%n---`` format."""
    path = tmp_path / "commits.txt"
    path.write_text("\n---\n".join(messages) + "\n---\n", encoding="utf-8")
    return path


def test_empty_inputs_pass(hygiene_module: ModuleType, deny_file: Path) -> None:
    """All-empty surfaces never trigger a finding."""
    phrases = hygiene_module.load_denylist(deny_file)
    findings = hygiene_module.check_pr_text("", "", "", [], phrases)
    assert findings == []


def test_whitespace_only_body_passes(hygiene_module: ModuleType, deny_file: Path) -> None:
    """Whitespace-only body must not be flagged."""
    phrases = hygiene_module.load_denylist(deny_file)
    findings = hygiene_module.check_pr_text("", "   \n\t  \n", "", [], phrases)
    assert findings == []


def test_clean_technical_text_passes(hygiene_module: ModuleType, deny_file: Path) -> None:
    """Ordinary engineering-hygiene text must pass."""
    phrases = hygiene_module.load_denylist(deny_file)
    findings = hygiene_module.check_pr_text(
        title="ci: harden the runner egress policy",
        body=(
            "Adds an egress allow-list to the workflow so the action can run in audit mode without leaking artifacts."
        ),
        branch="feat/harden-runner-egress",
        commit_messages=[
            "ci: pin step-security/harden-runner sha\n\nAdds the v2.19.3 sha and switches mode to audit.",
        ],
        phrases=phrases,
    )
    assert findings == []


def test_forbidden_token_in_title_fails(hygiene_module: ModuleType, deny_file: Path) -> None:
    """A deny-list phrase in the title produces a finding tagged 'title'."""
    phrases = hygiene_module.load_denylist(deny_file)
    findings = hygiene_module.check_pr_text(
        title="chore: drop cringe content",
        body="",
        branch="feat/clean",
        commit_messages=[],
        phrases=phrases,
    )
    assert ("title", "cringe") in findings


def test_forbidden_token_in_branch_fails(hygiene_module: ModuleType, deny_file: Path) -> None:
    """A deny-list phrase in the branch name produces a finding tagged 'branch'."""
    phrases = hygiene_module.load_denylist(deny_file)
    findings = hygiene_module.check_pr_text(
        title="chore: rename docs",
        body="",
        branch="feat/marketing-copy-cleanup",
        commit_messages=[],
        phrases=phrases,
    )
    assert ("branch", "marketing") in findings


def test_forbidden_token_in_commit_body_fails(hygiene_module: ModuleType, deny_file: Path) -> None:
    """A deny-list phrase in any commit body produces a 'commit[i]' finding."""
    phrases = hygiene_module.load_denylist(deny_file)
    findings = hygiene_module.check_pr_text(
        title="chore: rename docs",
        body="",
        branch="feat/clean",
        commit_messages=[
            "chore: rename docs\n\nDrops references to the funnel from docs/role-prompts/.",
        ],
        phrases=phrases,
    )
    assert any(surface.startswith("commit[") and phrase == "funnel" for surface, phrase in findings)


def test_case_insensitive_match_fails(hygiene_module: ModuleType, deny_file: Path) -> None:
    """Matching ignores letter case (e.g. 'Cringe' vs 'cringe')."""
    phrases = hygiene_module.load_denylist(deny_file)
    findings = hygiene_module.check_pr_text(
        title="chore: drop Cringe content",
        body="",
        branch="feat/clean",
        commit_messages=[],
        phrases=phrases,
    )
    assert any(surface == "title" and phrase == "cringe" for surface, phrase in findings)


def test_match_inside_word_boundary(hygiene_module: ModuleType, deny_file: Path) -> None:
    """Substring semantics are intentional: 'marketing-language' is flagged."""
    phrases = hygiene_module.load_denylist(deny_file)
    findings = hygiene_module.check_pr_text(
        title="docs: drop marketing-flavoured wording",
        body="",
        branch="feat/clean",
        commit_messages=[],
        phrases=phrases,
    )
    assert ("title", "marketing") in findings


def test_attribution_trailer_in_commit_fails(hygiene_module: ModuleType, deny_file: Path) -> None:
    """The AI attribution trailer in a commit message is denied."""
    phrases = hygiene_module.load_denylist(deny_file)
    findings = hygiene_module.check_pr_text(
        title="ci: add hygiene gate",
        body="",
        branch="feat/clean",
        commit_messages=[
            "ci: add hygiene gate\n\nCo-Authored-By: Claude <noreply@anthropic.com>",
        ],
        phrases=phrases,
    )
    assert any(phrase == "Co-Authored-By: Claude" for _, phrase in findings)


def test_script_never_reads_labels(hygiene_module: ModuleType) -> None:
    """The check function must not have any 'label' parameter.

    Label-based opt-out is workflow-level; if labels ever leak into the
    script we want this test to fail loudly.
    """
    import inspect

    sig = inspect.signature(hygiene_module.check_pr_text)
    for name in sig.parameters:
        assert "label" not in name.lower(), f"check_pr_text grew a label-aware parameter: {name}"


def test_load_denylist_from_env_json(hygiene_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """JSON payload in the env var resolves to the phrase list."""
    monkeypatch.setenv(
        "PR_HYGIENE_TEST_DENYLIST",
        json.dumps({"denylist": ["foo", "bar baz"]}),
    )
    phrases = hygiene_module.load_denylist_from_env("PR_HYGIENE_TEST_DENYLIST")
    assert phrases == ["foo", "bar baz"]


def test_load_denylist_from_env_newline_form(hygiene_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Newline-separated payload also works."""
    monkeypatch.setenv("PR_HYGIENE_TEST_DENYLIST", "foo\nbar baz\n\n")
    phrases = hygiene_module.load_denylist_from_env("PR_HYGIENE_TEST_DENYLIST")
    assert phrases == ["foo", "bar baz"]


def test_load_denylist_from_env_missing_returns_empty(
    hygiene_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing or empty env var resolves to an empty list."""
    monkeypatch.delenv("PR_HYGIENE_TEST_DENYLIST", raising=False)
    assert hygiene_module.load_denylist_from_env("PR_HYGIENE_TEST_DENYLIST") == []
    monkeypatch.setenv("PR_HYGIENE_TEST_DENYLIST", "")
    assert hygiene_module.load_denylist_from_env("PR_HYGIENE_TEST_DENYLIST") == []


def test_cli_no_denylist_configured_exits_zero_with_notice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI invoked without any deny-list source exits 0 with a notice."""
    monkeypatch.delenv("PR_HYGIENE_TEST_DENYLIST", raising=False)
    commits = _commit_dump(tmp_path, ["chore: rename docs"])
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--title",
            "chore: rename docs",
            "--branch",
            "feat/clean",
            "--commit-messages-file",
            str(commits),
            "--denylist-env-var",
            "PR_HYGIENE_TEST_DENYLIST",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "no deny-list configured" in result.stdout


def test_cli_clean_run_exits_zero(tmp_path: Path, deny_file: Path) -> None:
    """End-to-end: clean inputs produce exit 0."""
    commits = _commit_dump(tmp_path, ["chore: rename docs\n\nDrops stale references."])
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--title",
            "chore: rename docs",
            "--body",
            "",
            "--branch",
            "feat/clean",
            "--commit-messages-file",
            str(commits),
            "--denylist",
            str(deny_file),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "check_pr_text_hygiene: OK" in result.stdout


def test_cli_dirty_run_exits_one_with_annotation(tmp_path: Path, deny_file: Path) -> None:
    """End-to-end: a dirty title produces exit 1 plus a GitHub annotation."""
    commits = _commit_dump(tmp_path, ["chore: rename docs"])
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--title",
            "chore: drop cringe content",
            "--body",
            "",
            "--branch",
            "feat/clean",
            "--commit-messages-file",
            str(commits),
            "--denylist",
            str(deny_file),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "::error file=title::cringe matched in title" in result.stdout


def test_load_denylist_rejects_missing_key(tmp_path: Path, hygiene_module: ModuleType) -> None:
    """A JSON file without the ``denylist`` key is rejected with a clear error."""
    path = tmp_path / "bad.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="denylist"):
        hygiene_module.load_denylist(path)


def test_load_denylist_skips_blank_entries(tmp_path: Path, hygiene_module: ModuleType) -> None:
    """Blank / whitespace-only entries are silently dropped."""
    path = tmp_path / "list.json"
    path.write_text(json.dumps({"denylist": ["cringe", "  ", ""]}), encoding="utf-8")
    phrases = hygiene_module.load_denylist(path)
    assert phrases == ["cringe"]


# --- Word-boundary regression coverage ----------------------------------
#
# Plain substring matching produced false positives: a short token could
# match inside an unrelated longer word. The boundary-aware matcher
# requires a non-alphanumeric neighbour on the left for every phrase and
# on the right as well for short (<= 4 char) tokens. These fixtures use
# neutral placeholder phrases; the point under test is the boundary logic,
# not any particular phrase.


@pytest.fixture
def boundary_deny_file(tmp_path: Path) -> Path:
    """Deny-list mixing a short acronym and longer phrases."""
    path = tmp_path / "boundary.json"
    path.write_text(
        json.dumps({"denylist": ["SEO", "cringe", "foo bar"]}),
        encoding="utf-8",
    )
    return path


def test_short_token_not_matched_inside_word(hygiene_module: ModuleType, boundary_deny_file: Path) -> None:
    """A short token must not match when embedded in a longer word.

    'closeout' contains the substring 'seo' with alphanumeric neighbours
    on both sides, so the acronym rule must not flag it.
    """
    phrases = hygiene_module.load_denylist(boundary_deny_file)
    findings = hygiene_module.check_pr_text(
        title="chore: closeout stale PRs",
        body="",
        branch="feat/clean",
        commit_messages=[],
        phrases=phrases,
    )
    assert findings == []


def test_short_token_not_matched_as_word_prefix(hygiene_module: ModuleType, boundary_deny_file: Path) -> None:
    """A short token followed by more letters must not match ('Seoul')."""
    phrases = hygiene_module.load_denylist(boundary_deny_file)
    findings = hygiene_module.check_pr_text(
        title="docs: note the author visited Seoul",
        body="",
        branch="feat/clean",
        commit_messages=[],
        phrases=phrases,
    )
    assert findings == []


def test_short_token_matched_standalone(hygiene_module: ModuleType, boundary_deny_file: Path) -> None:
    """A standalone short token is still flagged (space boundaries)."""
    phrases = hygiene_module.load_denylist(boundary_deny_file)
    findings = hygiene_module.check_pr_text(
        title="docs: add SEO playbook",
        body="",
        branch="feat/clean",
        commit_messages=[],
        phrases=phrases,
    )
    assert ("title", "SEO") in findings


def test_short_token_matched_with_hyphen_boundary(hygiene_module: ModuleType, boundary_deny_file: Path) -> None:
    """A hyphen counts as a boundary for the short-token rule ('SEO-friendly')."""
    phrases = hygiene_module.load_denylist(boundary_deny_file)
    findings = hygiene_module.check_pr_text(
        title="docs: make copy SEO-friendly",
        body="",
        branch="feat/clean",
        commit_messages=[],
        phrases=phrases,
    )
    assert ("title", "SEO") in findings


def test_long_phrase_matched_inside_word(hygiene_module: ModuleType, boundary_deny_file: Path) -> None:
    """A long phrase keeps left-boundary matching ('cringeworthy' fails)."""
    phrases = hygiene_module.load_denylist(boundary_deny_file)
    findings = hygiene_module.check_pr_text(
        title="chore: this reads cringeworthy",
        body="",
        branch="feat/clean",
        commit_messages=[],
        phrases=phrases,
    )
    assert ("title", "cringe") in findings


def test_multiword_phrase_suffixed_form_still_matched(hygiene_module: ModuleType, boundary_deny_file: Path) -> None:
    """A multi-word phrase with a suffix appended still matches (left boundary only)."""
    phrases = hygiene_module.load_denylist(boundary_deny_file)
    findings = hygiene_module.check_pr_text(
        title="chore: drop the foo barbaz wording",
        body="",
        branch="feat/clean",
        commit_messages=[],
        phrases=phrases,
    )
    assert ("title", "foo bar") in findings


def test_short_token_matched_in_commit_message(hygiene_module: ModuleType, boundary_deny_file: Path) -> None:
    """Boundary matching applies to commit messages too."""
    phrases = hygiene_module.load_denylist(boundary_deny_file)
    findings = hygiene_module.check_pr_text(
        title="chore: docs",
        body="",
        branch="feat/clean",
        commit_messages=["chore: docs\n\nAdds an SEO section to the guide."],
        phrases=phrases,
    )
    assert any(surface.startswith("commit[") and phrase == "SEO" for surface, phrase in findings)
