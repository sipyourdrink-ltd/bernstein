"""Tests for volunteer PR submission: body, pacing, DCO, and leak scan."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from bernstein.core.git.git_basic import GitResult


@pytest.fixture
def isolated_pacing(tmp_path: Path):
    """Isolate pacing state to tmp_path so tests don't pollute ~/.bernstein."""
    with patch("bernstein.core.volunteer.submission._pacing_dir", return_value=tmp_path / "pacing"):
        yield


from bernstein.core.security.result_receipt_bundle import (
    ChainLink,
    GateResult,
    ResultBundle,
    TaskRef,
)
from bernstein.core.volunteer.submission import (
    PacingError,
    SubmissionError,
    build_volunteer_pr_body,
    submit_volunteer_pr,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_bundle(
    *,
    gates: tuple[GateResult, ...] = (
        GateResult(command="uv run pytest", exit_code=0, log="All tests passed."),
        GateResult(command="uv run ruff check", exit_code=0, log="No issues found."),
    ),
    patch: str = "--- a/foo.py\n+++ b/foo.py\n",
    issue_number: int | None = 42,
    adapter_id: str = "claude",
    model_id: str = "sonnet",
) -> ResultBundle:
    return ResultBundle(
        task=TaskRef(
            repo="https://github.com/foo/bar",
            commit_sha="abcdef1234567890",
            issue_number=issue_number,
        ),
        patch=patch,
        gates=gates,
        manifest_sha256="a" * 64,
        adapter_id=adapter_id,
        model_id=model_id,
        sandbox_profile="container",
        selection_receipt="b" * 64,
        created_at="2026-08-21T12:00:00Z",
        worker_keyid="c" * 16,
        worker_public_key_pem="-----BEGIN PUBLIC KEY-----\n-----END PUBLIC KEY-----\n",
        chain=ChainLink(anchor="genesis", length=1),
    )


# ---------------------------------------------------------------------------
# Test 1: Golden body test
# ---------------------------------------------------------------------------


def test_pr_body_golden() -> None:
    bundle = _make_bundle()
    body = build_volunteer_pr_body(
        bundle,
        adapter_id="claude",
        model_id="sonnet",
        signed_off_by="Jane Doe <jane@example.com>",
        bundle_digest="d" * 64,
    )

    expected = "\n".join(
        [
            "## Summary",
            "",
            "Automated volunteer submission via bernstein, addressing issue #42 on `https://github.com/foo/bar`.",
            "The gate results below were produced under the project's declared volunteer policy.",
            "",
            "## Gate Results",
            "",
            "| Command | Exit Code | Status |",
            "|---|---|---|",
            "| `uv run pytest` | 0 | ✅ |",
            "| `uv run ruff check` | 0 | ✅ |",
            "",
            "## Verification",
            "",
            f"- **Receipt digest:** `{'d' * 64}`",
            f"- **Manifest digest:** `{'a' * 64}`",
            "- **Verify offline:** `bernstein receipt verify bundle.json`",
            "",
            "---",
            "",
            "_Assisted-by: claude (sonnet)_",
            "Signed-off-by: Jane Doe <jane@example.com>",
        ]
    )
    assert body == expected


# ---------------------------------------------------------------------------
# Test 2: Pacing refuses a second PR while one is open
# ---------------------------------------------------------------------------


class _FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.responses: dict[str, subprocess.CompletedProcess[str]] = {}

    def __call__(self, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, stdin))
        key = " ".join(args[:2])
        if key in self.responses:
            return self.responses[key]
        return subprocess.CompletedProcess(args=["gh", *args], returncode=0, stdout="", stderr="")

    @property
    def pr_create_count(self) -> int:
        return sum(1 for args, _ in self.calls if args[:2] == ["pr", "create"])


def test_pacing_refuses_a_second_pr_while_one_is_open(tmp_path: Path, isolated_pacing) -> None:
    slug = "foo-bar"
    # Seed pacing state with an open PR
    from bernstein.core.volunteer.submission import _write_pacing

    _write_pacing(slug, "https://github.com/foo/bar/pull/1")

    runner = _FakeRunner()
    runner.responses["pr view"] = subprocess.CompletedProcess(
        args=["gh", "pr", "view"],
        returncode=0,
        stdout=json.dumps({"state": "OPEN"}),
        stderr="",
    )

    with pytest.raises(PacingError, match="already open"):
        submit_volunteer_pr(
            bundle=_make_bundle(),
            repo_url="https://github.com/foo/bar",
            branch="volunteer/task-1",
            cwd=tmp_path,
            runner=runner,
        )

    # The critical assertion: pr create was NEVER called
    assert runner.pr_create_count == 0


# ---------------------------------------------------------------------------
# Test 3: Pacing releases after the tracked PR is merged or closed
# ---------------------------------------------------------------------------


def test_pacing_releases_after_the_tracked_pr_is_merged(tmp_path: Path, isolated_pacing) -> None:
    slug = "foo-bar"
    from bernstein.core.volunteer.submission import _read_pacing, _write_pacing

    _write_pacing(slug, "https://github.com/foo/bar/pull/1")

    runner = _FakeRunner()
    runner.responses["pr view"] = subprocess.CompletedProcess(
        args=["gh", "pr", "view"],
        returncode=0,
        stdout=json.dumps({"state": "MERGED"}),
        stderr="",
    )
    runner.responses["pr create"] = subprocess.CompletedProcess(
        args=["gh", "pr", "create"],
        returncode=0,
        stdout="https://github.com/foo/bar/pull/2",
        stderr="",
    )

    # Mock DCO so we don't need real git config
    # Mock DCO and push so we don't need real git config or a real repo
    with (
        patch("bernstein.core.volunteer.submission.read_dco_line", return_value="Jane Doe <jane@example.com>"),
        patch(
            "bernstein.core.volunteer.submission.push_head_as",
            return_value=GitResult(returncode=0, stdout="", stderr=""),
        ),
    ):
        pr_url = submit_volunteer_pr(
            bundle=_make_bundle(),
            repo_url="https://github.com/foo/bar",
            branch="volunteer/task-2",
            cwd=tmp_path,
            runner=runner,
        )

    assert pr_url == "https://github.com/foo/bar/pull/2"
    # Old pacing cleared, new one written
    assert _read_pacing(slug) == "https://github.com/foo/bar/pull/2"


# ---------------------------------------------------------------------------
# Test 4: DCO trailer present and well-formed
# ---------------------------------------------------------------------------


def test_dco_trailer_is_present_and_well_formed() -> None:
    bundle = _make_bundle()
    body = build_volunteer_pr_body(
        bundle,
        adapter_id="claude",
        model_id="sonnet",
        signed_off_by="Jane Doe <jane@example.com>",
        bundle_digest="d" * 64,
    )
    # Assert the trailer line matches the standard DCO format
    assert re.search(r"^Signed-off-by: .+ <.+@.+>$", body, re.MULTILINE)


def test_submission_refuses_without_dco(tmp_path: Path, isolated_pacing) -> None:
    runner = _FakeRunner()

    with patch("bernstein.core.volunteer.submission.read_dco_line", return_value=None):
        with pytest.raises(SubmissionError, match="user.name/user.email"):
            submit_volunteer_pr(
                bundle=_make_bundle(),
                repo_url="https://github.com/foo/bar",
                branch="volunteer/task-1",
                cwd=tmp_path,
                runner=runner,
            )

    assert runner.pr_create_count == 0


# ---------------------------------------------------------------------------
# Test 5: No local paths or credential material in rendered body
# ---------------------------------------------------------------------------


def test_rendered_body_contains_no_local_paths_or_credential_material() -> None:
    bundle = _make_bundle(
        gates=(
            GateResult(
                command="uv run pytest",
                exit_code=0,
                log="Tests passed from /home/jane/project/.sdd/results",
            ),
        ),
        patch="--- a/foo.py\n+++ b/foo.py\n@@ -1,3 +1,3 @@\n-old\n+new\n",
    )
    body = build_volunteer_pr_body(
        bundle,
        adapter_id="claude",
        model_id="sonnet",
        signed_off_by="Jane Doe <jane@example.com>",
        bundle_digest="d" * 64,
    )

    # No absolute local paths
    assert not re.search(r"/(?:Users|home)/\S+", body)

    # No credential-like patterns (aws, github token, etc.)
    assert not re.search(r"(?:AKIA|ghp_|gho_|github_pat_)\S+", body)
