"""Tests for the UTF-8 BOM hygiene gate (#4500).

Every case builds its own throwaway repository. None of them read the real
tree: the three fragments that motivated the gate (#4429, #4430, #4493) were
cleaned in review, so a test asserting against the real files would pass from
that moment on whether or not the check still worked.

Files are staged but never committed. ``git ls-files`` reports the index and
``git check-attr`` reads the working tree, so staging is the whole setup and
no committer identity is needed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from scripts.check_bom import BOM, check, starts_with_bom

# A fragment shaped like the ones that arrived mangled: the first line is a
# heading, which is exactly where a stray glyph shows up on a release page.
FRAGMENT = "# Fixed\n\n- A release-notes fragment.\n"


def make_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return tmp_path


def add(root: Path, name: str, content: bytes, *, gitattributes: str | None = None) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if gitattributes is not None:
        (root / ".gitattributes").write_text(gitattributes, encoding="utf-8")
        subprocess.run(["git", "add", ".gitattributes"], cwd=root, check=True)
    subprocess.run(["git", "add", "--", name], cwd=root, check=True)
    return path


class TestTheGateCatchesTheDriftItWasBuiltFor:
    def test_a_bom_prefixed_fragment_fails_and_names_the_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = make_repo(tmp_path)
        add(root, "docs/releases/4429.md", BOM + FRAGMENT.encode("utf-8"))

        assert check(root) == 1
        # Naming the path is the point. "a file has a BOM" costs a bisect.
        assert "docs/releases/4429.md" in capsys.readouterr().err

    def test_the_same_fragment_without_the_bom_passes(self, tmp_path: Path) -> None:
        root = make_repo(tmp_path)
        add(root, "docs/releases/4429.md", FRAGMENT.encode("utf-8"))

        assert check(root) == 0

    @pytest.mark.parametrize("suffix", [".md", ".py", ".yaml", ".yml", ".toml", ".json"])
    def test_every_text_suffix_in_scope_is_swept(self, tmp_path: Path, suffix: str) -> None:
        root = make_repo(tmp_path)
        add(root, f"fixture{suffix}", BOM + b"x\n")

        assert check(root) == 1


class TestByteExactFixturesStayOutOfScope:
    def test_a_minus_text_path_with_arbitrary_leading_bytes_passes(self, tmp_path: Path) -> None:
        # The demo receipt in miniature: '-text' is how .gitattributes says a
        # file's bytes are load-bearing, and a signature dies on any rewrite.
        root = make_repo(tmp_path)
        add(
            root,
            "docs/assets/demo-run/run-receipt.json",
            BOM + b'{"signature": "..."}\n',
            gitattributes="docs/assets/demo-run/run-receipt.json -text linguist-generated\n",
        )

        assert check(root) == 0

    def test_the_binary_macro_exempts_too(self, tmp_path: Path) -> None:
        # 'binary' expands to '-diff -merge -text', so the same query has to
        # cover it. Spelling the exemption two ways is a real thing people do.
        root = make_repo(tmp_path)
        add(
            root,
            "vectors/frame.json",
            BOM + b"{}\n",
            gitattributes="vectors/frame.json binary\n",
        )

        assert check(root) == 0

    def test_a_plain_text_path_is_still_caught_when_others_are_exempt(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The exemption must be per-path, not a switch that disarms the sweep.
        root = make_repo(tmp_path)
        add(
            root,
            "vectors/frame.json",
            BOM + b"{}\n",
            gitattributes="vectors/frame.json -text\n",
        )
        add(root, "docs/releases/4430.md", BOM + FRAGMENT.encode("utf-8"))

        assert check(root) == 1
        err = capsys.readouterr().err
        assert "docs/releases/4430.md" in err
        assert "vectors/frame.json" not in err


class TestTheSweepBoundary:
    def test_an_untracked_bom_file_is_not_swept(self, tmp_path: Path) -> None:
        # The gate is about what lands in the repository. A scratch file in a
        # working tree is the contributor's business.
        root = make_repo(tmp_path)
        (root / "scratch.md").write_bytes(BOM + b"# scratch\n")

        assert check(root) == 0

    def test_a_suffix_outside_the_allowlist_is_not_swept(self, tmp_path: Path) -> None:
        root = make_repo(tmp_path)
        add(root, "docs/assets/demo.cast", BOM + b"[0.0, 'o', 'x']\n")

        assert check(root) == 0

    def test_an_empty_repository_passes(self, tmp_path: Path) -> None:
        assert check(make_repo(tmp_path)) == 0


class TestTheBomProbe:
    def test_a_bare_bom_with_no_content_is_still_a_bom(self, tmp_path: Path) -> None:
        path = tmp_path / "empty-but-marked.md"
        path.write_bytes(BOM)

        assert starts_with_bom(path) is True

    def test_a_bom_later_in_the_file_is_not_a_leading_bom(self, tmp_path: Path) -> None:
        # Only the leading mark renders as a stray glyph. A U+FEFF mid-file is
        # a zero-width no-break space and a different argument entirely.
        path = tmp_path / "midway.md"
        path.write_bytes(b"# Fixed\n" + BOM + b"more\n")

        assert starts_with_bom(path) is False

    def test_a_short_file_does_not_false_positive(self, tmp_path: Path) -> None:
        # Two bytes cannot be a three-byte mark, and the read must not raise.
        path = tmp_path / "short.md"
        path.write_bytes(b"\xef\xbb")

        assert starts_with_bom(path) is False
