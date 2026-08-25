"""A committed text file must not start with a UTF-8 byte-order mark.

Covers ``scripts/check_bom.py``. Three pull requests in a row (#4429, #4430,
#4493) landed a release-notes fragment prefixed with ``EF BB BF``; ``Repo
hygiene`` passed all three and review caught it each time. One of those
fragments would have rendered a stray ``\\ufeff`` onto a release page.

A BOM is invisible in an editor and in a GitHub diff, which is why this is
asserted by a test rather than trusted to a reader. The exemption half matters
just as much: the demo receipt and the committed receipt vectors are
byte-exact fixtures whose signatures are over the bytes, so a check that
"helpfully" flagged their leading bytes would be a false positive nobody could
fix without invalidating the evidence.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from check_bom import BOM, find_bom_files, main

CLEAN = "# Heading\n\nBody text.\n"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=repo, capture_output=True, check=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repo with one exempt fixture path marked ``-text``."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "tests" / "fixtures" / "receipt-vectors").mkdir(parents=True)
    (tmp_path / ".gitattributes").write_text("tests/fixtures/receipt-vectors/*.json -text\n", encoding="utf-8")
    return tmp_path


def _commit(repo: Path, relpath: str, data: bytes) -> Path:
    path = repo / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"add {relpath}")
    return path


def test_bom_file_is_flagged(repo: Path) -> None:
    _commit(repo, "notes.md", BOM + CLEAN.encode("utf-8"))

    flagged = find_bom_files(repo)

    assert [p.name for p in flagged] == ["notes.md"]


def test_same_file_without_the_bom_passes(repo: Path) -> None:
    """The content is identical; only the three leading bytes differ."""
    _commit(repo, "notes.md", CLEAN.encode("utf-8"))

    assert find_bom_files(repo) == []


def test_text_marked_fixture_with_arbitrary_leading_bytes_passes(repo: Path) -> None:
    """A ``-text`` fixture is exempt even when it does start with a BOM.

    These are byte-exact signed vectors. Flagging them would be a finding no
    one could act on without invalidating the evidence they exist to pin.
    """
    _commit(repo, "tests/fixtures/receipt-vectors/vec.json", BOM + b'{"a":1}\n')

    assert find_bom_files(repo) == []


def test_untracked_bom_file_is_ignored(repo: Path) -> None:
    """The check is about what is *committed*, not what is lying around."""
    (repo / "scratch.md").write_bytes(BOM + CLEAN.encode("utf-8"))

    assert find_bom_files(repo) == []


def test_non_text_suffix_is_not_swept(repo: Path) -> None:
    _commit(repo, "blob.bin", BOM + b"\x00\x01\x02")

    assert find_bom_files(repo) == []


def test_bom_midway_through_a_file_is_not_flagged(repo: Path) -> None:
    """Only a *leading* BOM breaks a renderer; U+FEFF elsewhere is content."""
    _commit(repo, "notes.md", CLEAN.encode("utf-8") + BOM + b"tail\n")

    assert find_bom_files(repo) == []


def test_multiple_offenders_are_all_reported(repo: Path) -> None:
    _commit(repo, "a.md", BOM + b"a\n")
    _commit(repo, "b.toml", BOM + b'x = "y"\n')

    flagged = sorted(p.name for p in find_bom_files(repo))

    assert flagged == ["a.md", "b.toml"]


def test_main_exits_nonzero_and_names_the_file(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _commit(repo, "notes.md", BOM + CLEAN.encode("utf-8"))

    rc = main(["--repo-root", str(repo)])

    assert rc == 1
    assert "notes.md" in capsys.readouterr().out


def test_main_exits_zero_on_a_clean_repo(repo: Path) -> None:
    _commit(repo, "notes.md", CLEAN.encode("utf-8"))

    assert main(["--repo-root", str(repo)]) == 0
