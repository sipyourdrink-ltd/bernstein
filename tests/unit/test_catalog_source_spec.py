"""Tests for CatalogSourceSpec / resolve_catalog_source (issue #3972).

Mirrors the skill catalog's github|git|npm|file|directory source-kind
vocabulary (``core/skills/catalog/installer.py``) for agent catalogs. Only
``file``/``directory`` resolve in this PR; the remote kinds are valid
configuration that raises a stable ``NotImplementedError`` at load time
rather than silently doing nothing - full remote resolution (digest pinning,
lockfile, signatures) is tracked separately (#3973).
"""

from __future__ import annotations

import pytest

from bernstein.agents.catalog import (
    CatalogSourceError,
    CatalogSourceSpec,
    resolve_catalog_source,
)


class TestDirectorySource:
    def test_existing_directory_resolves_to_its_path(self, tmp_path):
        target = tmp_path / "catalog-root"
        target.mkdir()
        spec = CatalogSourceSpec(kind="directory", path=str(target))
        assert resolve_catalog_source(spec) == target

    def test_missing_directory_raises_catalog_source_error(self, tmp_path):
        missing = tmp_path / "does-not-exist"
        spec = CatalogSourceSpec(kind="directory", path=str(missing))
        with pytest.raises(CatalogSourceError, match="not found"):
            resolve_catalog_source(spec)

    def test_path_that_is_a_file_raises_catalog_source_error(self, tmp_path):
        target = tmp_path / "not-a-dir.txt"
        target.write_text("hello", encoding="utf-8")
        spec = CatalogSourceSpec(kind="directory", path=str(target))
        with pytest.raises(CatalogSourceError, match="not found"):
            resolve_catalog_source(spec)


class TestFileSource:
    def test_existing_file_resolves_to_its_path(self, tmp_path):
        target = tmp_path / "agent.md"
        target.write_text("---\nname: x\n---\nbody", encoding="utf-8")
        spec = CatalogSourceSpec(kind="file", path=str(target))
        assert resolve_catalog_source(spec) == target

    def test_missing_file_raises_catalog_source_error(self, tmp_path):
        missing = tmp_path / "missing.md"
        spec = CatalogSourceSpec(kind="file", path=str(missing))
        with pytest.raises(CatalogSourceError, match="not found"):
            resolve_catalog_source(spec)


class TestRemoteSourceKinds:
    @pytest.mark.parametrize("kind", ["github", "git", "npm"])
    def test_remote_kind_raises_not_implemented_with_stable_message(self, kind):
        """Pinned message: also asserted against by the PR's fail-before note.

        The exact wording is part of the contract - a caller (or an
        operator reading a traceback) needs to see clearly that this is
        deliberately unimplemented, not a bug, and where the remaining
        work is tracked.
        """
        spec = CatalogSourceSpec(kind=kind, repo="acme/agents", url="https://example.invalid/repo.git", package="x")
        with pytest.raises(NotImplementedError) as excinfo:
            resolve_catalog_source(spec)
        message = str(excinfo.value)
        assert kind in message
        assert "not implemented" in message
        assert "#3973" in message


class TestUnknownSourceKind:
    def test_unknown_kind_raises_catalog_source_error(self):
        spec = CatalogSourceSpec(kind="ftp", path="/tmp/whatever")
        with pytest.raises(CatalogSourceError, match="unknown catalog source kind"):
            resolve_catalog_source(spec)
