"""Tests for the powered-by badge helper."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.cli.badge import (
    BadgeVariant,
    _badge_already_present,
    _insertion_index,
    get_variant,
    inject_badge,
    list_variants,
)


class TestVariants:
    def test_four_variants_present(self) -> None:
        variants = list_variants()
        assert len(variants) == 4
        names = {v.name for v in variants}
        assert names == {"signed", "audited-by", "orchestrated-by", "crew-managed-by"}

    def test_get_variant_lookup(self) -> None:
        v = get_variant("orchestrated-by")
        assert isinstance(v, BadgeVariant)
        assert v.name == "orchestrated-by"

    def test_get_variant_raises_on_unknown(self) -> None:
        with pytest.raises(KeyError):
            get_variant("nonexistent")

    def test_url_contains_shields(self) -> None:
        v = get_variant("signed")
        url = v.url()
        assert url.startswith("https://img.shields.io/badge/")
        assert "bernstein" in url

    def test_markdown_contains_link_and_alt(self) -> None:
        v = get_variant("signed")
        md = v.markdown()
        assert md.startswith("[![")
        assert "bernstein.run" in md
        assert "utm_source=badge" in md


class TestPresenceDetection:
    def test_empty_readme_returns_false(self) -> None:
        assert _badge_already_present("") is False

    def test_no_shields_returns_false(self) -> None:
        assert _badge_already_present("# foo\n\nplain text") is False

    def test_unrelated_shields_badge_returns_false(self) -> None:
        text = "[![CI](https://img.shields.io/badge/ci-passing-green)](#)"
        assert _badge_already_present(text) is False

    def test_signed_by_badge_returns_true(self) -> None:
        text = (
            "[![signed by bernstein](https://img.shields.io/badge/signed_by-bernstein-FBBF24)](https://bernstein.run/)"
        )
        assert _badge_already_present(text) is True


class TestInsertionIndex:
    def test_inserts_after_h1_when_no_badge_stack(self) -> None:
        text = "# my project\n\nbody\n"
        idx = _insertion_index(text)
        assert text[:idx] == "# my project\n"

    def test_inserts_at_top_when_no_h1_no_badges(self) -> None:
        text = "plain body\n"
        idx = _insertion_index(text)
        assert idx == 0

    def test_appends_to_existing_badge_stack(self) -> None:
        text = (
            "# proj\n"
            "[![CI](https://img.shields.io/badge/ci-passing-green)](#)\n"
            "[![PyPI](https://img.shields.io/pypi/v/example)](#)\n"
            "\nbody\n"
        )
        idx = _insertion_index(text)
        before, after = text[:idx], text[idx:]
        # Insertion must follow the LAST badge line.
        assert before.endswith("[![PyPI](https://img.shields.io/pypi/v/example)](#)\n")
        assert after.startswith("\nbody")


class TestInjectBadge:
    def test_writes_when_missing(self, tmp_path: Path) -> None:
        readme = tmp_path / "README.md"
        readme.write_text("# myproj\n\nDoes a thing.\n", encoding="utf-8")

        v = get_variant("signed")
        changed = inject_badge(readme, v)
        assert changed is True

        new_text = readme.read_text(encoding="utf-8")
        assert "img.shields.io/badge/signed_by-bernstein" in new_text
        assert "Does a thing." in new_text

    def test_idempotent_when_already_present(self, tmp_path: Path) -> None:
        readme = tmp_path / "README.md"
        readme.write_text(
            "# proj\n\n"
            "[![signed by bernstein]"
            "(https://img.shields.io/badge/signed_by-bernstein-FBBF24)]"
            "(https://bernstein.run/)\n",
            encoding="utf-8",
        )

        v = get_variant("signed")
        changed = inject_badge(readme, v)
        assert changed is False

    def test_returns_false_when_readme_missing(self, tmp_path: Path) -> None:
        v = get_variant("signed")
        assert inject_badge(tmp_path / "README.md", v) is False

    def test_appends_after_existing_badge_stack(self, tmp_path: Path) -> None:
        readme = tmp_path / "README.md"
        readme.write_text(
            "# proj\n"
            "[![CI](https://img.shields.io/badge/ci-passing-green)](#)\n"
            "[![PyPI](https://img.shields.io/pypi/v/example)](#)\n\n"
            "body\n",
            encoding="utf-8",
        )
        v = get_variant("audited-by")
        changed = inject_badge(readme, v)
        assert changed is True

        new_text = readme.read_text(encoding="utf-8")
        # The new badge must appear after the existing PyPI badge but before "body".
        ci_pos = new_text.find("/ci-passing-green")
        pypi_pos = new_text.find("/pypi/v/example")
        bernstein_pos = new_text.find("/audited_by-bernstein")
        body_pos = new_text.find("\nbody\n")
        assert ci_pos != -1 < pypi_pos < bernstein_pos < body_pos


@pytest.mark.parametrize(
    ("original", "terminator"),
    [
        (b"# proj\n\nDoes a thing.\n", b"\n"),
        (b"# proj\r\n\r\nDoes a thing.\r\n", b"\r\n"),
        (b"# proj\r\n\nDoes a thing.\n", b"\r\n"),
        (b"intro\n# proj\r\nbody\n", b"\r\n"),
    ],
    ids=["lf", "crlf", "mixed-crlf-heading", "mixed-lf-first"],
)
def test_inserting_badge_preserves_existing_line_endings(tmp_path: Path, original: bytes, terminator: bytes) -> None:
    """Existing bytes stay as they were; the badge line takes the insertion point's terminator (#8059)."""
    readme = tmp_path / "README.md"
    readme.write_bytes(original)
    variant = get_variant("signed")

    assert inject_badge(readme, variant) is True
    written = readme.read_bytes()

    badge_line = variant.markdown().encode("utf-8") + terminator
    heading_end = original.index(b"# proj") + len(b"# proj") + len(terminator)
    assert written == original[:heading_end] + badge_line + original[heading_end:]

    assert inject_badge(readme, variant) is False
    assert readme.read_bytes() == written


def test_prepended_badge_takes_the_first_line_terminator(tmp_path: Path) -> None:
    """With no heading or badge stack, the prepended line copies the first line's terminator."""
    readme = tmp_path / "README.md"
    readme.write_bytes(b"body\r\nmore\n")
    variant = get_variant("signed")

    assert inject_badge(readme, variant) is True
    assert readme.read_bytes() == variant.markdown().encode("utf-8") + b"\r\nbody\r\nmore\n"
