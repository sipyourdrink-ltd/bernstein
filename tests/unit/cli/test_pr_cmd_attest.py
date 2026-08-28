"""The description-attestation step of ``bernstein pr``.

``attest_pr_description`` is covered in
``tests/unit/test_pr_description_from_change.py``, but it is covered by
handing it a diff the test made itself. The CLI wrapper that reads the diff
off the branch was not covered anywhere, so a wrong call into it survived
every existing test: the wrapper passed ``_diff_bytes``' whole
``(bytes, error)`` tuple to a hasher that wants bytes, and ``bernstein pr``
died with ``TypeError: object supporting the buffer API required``.

The command dies *after* the pull request is open and after the run has
already passed its verification gate, so the caller reads a failure for work
that succeeded, and the run's claim on its issue is withdrawn.

These tests drive the wrapper against a real git repository, because the
defect lived in what it did with the real ``_diff_bytes`` result.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bernstein.cli.commands.pr_cmd import _attest_description, _diff_bytes
from bernstein.core.integrations.pr_gen import SessionSummary, build_provenance

PR_URL = "https://github.com/sipyourdrink-ltd/bernstein/pull/4485"
ISSUE_BODY = "The description must be anchored to the diff it describes."


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository with a ``main`` and a branch that changed one file."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "keys.py").write_text("def derive() -> None: ...\n", encoding="utf-8")
    _git(tmp_path, "add", "keys.py")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "checkout", "-qb", "work")
    (tmp_path / "keys.py").write_text("def derive() -> int:\n    return 1\n", encoding="utf-8")
    _git(tmp_path, "commit", "-aqm", "change")
    return tmp_path


def _summary(repo: Path) -> SessionSummary:
    diff, error = _diff_bytes(repo, "main", "work")
    assert not error and diff, "the fixture must produce a real diff to hash"
    return SessionSummary(
        session_id="T-1",
        goal="anchor the description",
        branch="work",
        base_branch="main",
        journal_head="sha256:deadbeef",
        provenance=build_provenance(diff=diff, journal_head="sha256:deadbeef"),
    )


def test_attesting_a_real_branch_does_not_raise(repo: Path) -> None:
    """The regression itself: the wrapper hashed a tuple, not the diff."""
    _attest_description(
        summary=_summary(repo),
        pr_url=PR_URL,
        description="body",
        issue_body=ISSUE_BODY,
        cwd=repo,
    )


def test_attesting_writes_a_receipt(repo: Path) -> None:
    """Not raising is not enough -- the step must reach its own output.

    A wrapper that swallowed everything would pass the test above while
    anchoring nothing, so this one asserts the receipt exists.
    """
    _attest_description(
        summary=_summary(repo),
        pr_url=PR_URL,
        description="body",
        issue_body=ISSUE_BODY,
        cwd=repo,
    )

    receipts = list((repo / ".sdd" / "lineage").rglob("*")) if (repo / ".sdd").exists() else []
    assert any(p.is_file() for p in receipts), "attestation produced no receipt"


def test_an_unexpected_error_inside_attestation_does_not_fail_the_command(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The step promises it never fails the command. It must mean any error.

    The handler used to name three exception types, so the one that actually
    happened -- a ``TypeError`` -- walked straight past it and killed a
    publish. A guard whose whole purpose is "this step is optional" cannot
    be selective about which failures it treats as optional.
    """

    def boom(**_: object) -> None:
        raise TypeError("object supporting the buffer API required")

    monkeypatch.setattr("bernstein.cli.commands.pr_cmd.build_provenance", boom)

    _attest_description(
        summary=_summary(repo),
        pr_url=PR_URL,
        description="body",
        issue_body=ISSUE_BODY,
        cwd=repo,
    )

    assert "could not anchor" in capsys.readouterr().err
