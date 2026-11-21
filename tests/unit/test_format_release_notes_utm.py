"""Tests for ``scripts/format_release_notes.py`` UTM tagging.

Covers the per-release UTM helper and rendered-output snapshot:

- snapshot of ``format_notes`` for a dummy ``v0.0.0`` release
- idempotency: re-running ``_with_release_utm`` does not double-tag
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# ``scripts/`` is not on ``sys.path`` (pyproject.toml sets pythonpath=["src"]),
# so load the script as a module via importlib. Using a dedicated module name
# under a ``_scripts_`` namespace keeps it isolated from any future ``scripts``
# package without polluting top-level imports.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "format_release_notes.py"


def _load_release_notes_module():
    spec = importlib.util.spec_from_file_location("_scripts_format_release_notes", _SCRIPT_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - import sanity
        msg = f"could not load {_SCRIPT_PATH}"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def fmt():
    """Return the loaded ``format_release_notes`` module."""
    return _load_release_notes_module()


# --- TestWithReleaseUtm ---


class TestWithReleaseUtm:
    """Direct tests for the ``_with_release_utm`` helper."""

    def test_tags_bare_url(self, fmt) -> None:
        body = "See https://bernstein.run for details."
        out = fmt._with_release_utm(body, "0.0.0")
        assert "https://bernstein.run?utm_source=github.com&utm_medium=release-note&utm_campaign=v0.0.0" in out

    def test_tags_url_with_path(self, fmt) -> None:
        body = "Sponsors: https://bernstein.run/sponsors live now."
        out = fmt._with_release_utm(body, "1.2.3")
        assert "https://bernstein.run/sponsors?utm_source=github.com&utm_medium=release-note&utm_campaign=v1.2.3" in out

    def test_tags_url_with_existing_query(self, fmt) -> None:
        body = "Try https://bernstein.run/path?ref=blog now."
        out = fmt._with_release_utm(body, "0.0.0")
        # Existing query string must be preserved; UTM params appended via &.
        assert (
            "https://bernstein.run/path?ref=blog&utm_source=github.com&utm_medium=release-note&utm_campaign=v0.0.0"
            in out
        )

    def test_strips_leading_v_from_version(self, fmt) -> None:
        body = "https://bernstein.run/x"
        out_v = fmt._with_release_utm(body, "v0.0.0")
        out_plain = fmt._with_release_utm(body, "0.0.0")
        assert out_v == out_plain
        assert "utm_campaign=v0.0.0" in out_v
        assert "utm_campaign=vv0.0.0" not in out_v

    def test_does_not_tag_other_domains(self, fmt) -> None:
        body = "Repo: https://github.com/sipyourdrink-ltd/bernstein"
        out = fmt._with_release_utm(body, "0.0.0")
        assert out == body

    def test_idempotent_on_already_tagged_url(self, fmt) -> None:
        """Re-running on a tagged URL must NOT double-tag (idempotency)."""
        once = fmt._with_release_utm("https://bernstein.run/sponsors", "0.0.0")
        twice = fmt._with_release_utm(once, "0.0.0")
        assert once == twice
        # Sanity: only one utm_source segment present.
        assert twice.count("utm_source=") == 1
        assert twice.count("utm_campaign=v0.0.0") == 1

    def test_idempotent_across_versions_does_not_overwrite(self, fmt) -> None:
        """Already-tagged URL keeps its original campaign even if version differs."""
        original = fmt._with_release_utm("https://bernstein.run", "0.0.0")
        # A subsequent render at a different version must leave the existing
        # utm_campaign untouched (no double-tag, no overwrite).
        rerun = fmt._with_release_utm(original, "9.9.9")
        assert rerun == original
        assert "utm_campaign=v9.9.9" not in rerun


# --- TestFormatNotesSnapshot ---


class TestFormatNotesSnapshot:
    """End-to-end snapshot of ``format_notes`` body for a dummy release."""

    def test_dummy_v0_0_0_snapshot(self, fmt) -> None:
        commits = [
            "feat: ship sponsors page link https://bernstein.run/sponsors",
            "fix(cli): correct typo",
            "docs: update https://bernstein.run/changelog reference",
        ]
        body = fmt.format_notes(
            version="0.0.0",
            prev_tag="v0.0.0-pre",
            repo="sipyourdrink-ltd/bernstein",
            commits=commits,
        )

        # Header
        assert body.startswith("## v0.0.0\n")
        # Categorised buckets
        assert "### New features\n" in body
        assert "### Bug fixes\n" in body
        assert "### Documentation\n" in body
        # bernstein.run URLs in commit subjects are UTM-tagged.
        assert (
            "https://bernstein.run/sponsors?utm_source=github.com&utm_medium=release-note&utm_campaign=v0.0.0" in body
        )
        assert (
            "https://bernstein.run/changelog?utm_source=github.com&utm_medium=release-note&utm_campaign=v0.0.0" in body
        )
        # github.com compare link is left raw - different domain.
        assert "**Full changelog:** https://github.com/sipyourdrink-ltd/bernstein/compare/v0.0.0-pre...v0.0.0" in body
        # No double-tagging anywhere in the snapshot.
        assert body.count("utm_source=") == body.count("utm_source=github.com&utm_medium=release-note")

    def test_no_user_visible_changes_still_renders(self, fmt) -> None:
        body = fmt.format_notes(
            version="0.0.0",
            prev_tag="",
            repo="sipyourdrink-ltd/bernstein",
            commits=["chore: auto-bump version"],
        )
        assert "## v0.0.0" in body
        assert "_No user-visible changes._" in body


def test_release_parameters_precede_fragment(fmt) -> None:
    """The release parameters join the query, and the ``#fragment`` stays the anchor (#8058)."""
    from urllib.parse import parse_qs, urlsplit

    release = {"utm_source": ["github.com"], "utm_medium": ["release-note"], "utm_campaign": ["v1.2.3"]}
    for link, query in (
        ("https://bernstein.run/guide#setup", release),
        ("https://bernstein.run/guide?lang=en#setup", {"lang": ["en"], **release}),
    ):
        once = fmt._with_release_utm(f"See {link} now.", "1.2.3")
        url = next(word for word in once.split() if word.startswith("https://"))
        parts = urlsplit(url)
        assert parts.fragment == "setup", url
        assert parse_qs(parts.query) == query, url
        # Formatting twice gives the same link.
        assert fmt._with_release_utm(once, "1.2.3") == once
