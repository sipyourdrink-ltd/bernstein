"""Tests for tenant identifier validation and derived-path containment.

A tenant ID is used directly as a filesystem path segment: `tenant_paths`
joins it onto the `.sdd` directory and `ensure_tenant_layout` creates the
result. An identifier is therefore only meaningful if it names a single
directory entry, so `normalize_tenant_id` validates that shape and
`tenant_paths` independently asserts the derived root stays under the
`.sdd` directory it was built from.

The two checks are deliberately redundant: the second holds even if the
identifier rule is later relaxed, so the layout invariant does not depend
on the spelling of a regex.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bernstein.core.security import tenanting
from bernstein.core.security.tenanting import (
    DEFAULT_TENANT_ID,
    ensure_tenant_layout,
    normalize_tenant_id,
    tenant_paths,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.ci


# Identifiers that do not name a single path segment. Each would otherwise
# be joined onto `.sdd` verbatim.
NON_SEGMENT_IDS = [
    "..",
    "../../etc",
    "../sibling",
    "a/b",
    "/abs/path",
    "/",
    "a\\b",
    "..\\..\\windows",
    ".",
    "./relative",
    "tenant\x00null",
    "tenant\nnewline",
    "tenant\rcarriage",
    "tenant\ttab",
    "tenant id with spaces",
    "-leading-dash",
    ".leading-dot",
    "_leading-underscore",
    "tenant@host",
    "tenant:colon",
    "tenant*glob",
    "~",
]


class TestNormalizeTenantIdAcceptsLegitimateIds:
    """Ordinary identifiers keep working exactly as before."""

    @pytest.mark.parametrize(
        "tenant_id",
        [
            "tenant-a",
            "Tenant_1.2",
            "acme",
            DEFAULT_TENANT_ID,
            "t",
            "0",
            "a-b_c.d",
            "A" * 64,
        ],
    )
    def test_valid_id_passes_through_unchanged(self, tenant_id: str) -> None:
        assert normalize_tenant_id(tenant_id) == tenant_id

    def test_surrounding_whitespace_is_still_stripped(self) -> None:
        assert normalize_tenant_id("  acme  ") == "acme"

    def test_id_longer_than_the_limit_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="tenant"):
            normalize_tenant_id("A" * 65)


class TestBlankTenantIdStillDefaults:
    """Absent/blank input keeps its existing meaning: the default tenant.

    This is the documented contract for unauthenticated and
    tenant-unaware call sites, so validation must not turn it into an
    error.
    """

    @pytest.mark.parametrize("raw", ["", "   ", "\t\n", None])
    def test_blank_maps_to_default_tenant(self, raw: str | None) -> None:
        assert normalize_tenant_id(raw) == DEFAULT_TENANT_ID


class TestNormalizeTenantIdRejectsNonSegmentIds:
    """An identifier that is not a single path segment is refused."""

    @pytest.mark.parametrize("tenant_id", NON_SEGMENT_IDS)
    def test_non_segment_id_is_rejected(self, tenant_id: str) -> None:
        with pytest.raises(ValueError, match="tenant"):
            normalize_tenant_id(tenant_id)

    def test_rejection_message_does_not_echo_control_characters(self) -> None:
        """The message is logged; keep raw control bytes out of it."""
        with pytest.raises(ValueError) as excinfo:
            normalize_tenant_id("tenant\nnewline")
        assert "\n" not in str(excinfo.value)


class TestTenantPathsContainment:
    """Derived tenant roots stay under the `.sdd` directory."""

    @pytest.mark.parametrize("tenant_id", NON_SEGMENT_IDS)
    def test_non_segment_id_never_yields_a_root_outside_sdd(self, tenant_id: str, tmp_path: Path) -> None:
        sdd_dir = tmp_path / ".sdd"
        sdd_dir.mkdir()
        try:
            paths = tenant_paths(sdd_dir, tenant_id)
        except ValueError:
            return  # Refused outright, which satisfies the invariant.
        # If it ever returns, the resolved root must still be contained.
        assert paths.root.resolve().is_relative_to(sdd_dir.resolve())

    def test_valid_id_yields_a_contained_root(self, tmp_path: Path) -> None:
        sdd_dir = tmp_path / ".sdd"
        sdd_dir.mkdir()
        paths = tenant_paths(sdd_dir, "tenant-a")
        assert paths.root.resolve().is_relative_to(sdd_dir.resolve())
        assert paths.root.resolve() == (sdd_dir / "tenant-a").resolve()

    def test_containment_holds_independently_of_the_identifier_rule(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The path assert is load-bearing on its own.

        With identifier validation stubbed out, `tenant_paths` must still
        refuse a traversing segment, so the layout invariant does not rest
        solely on the regex.
        """
        sdd_dir = tmp_path / ".sdd"
        sdd_dir.mkdir()
        monkeypatch.setattr(tenanting, "normalize_tenant_id", lambda raw: str(raw))
        with pytest.raises(ValueError, match="tenant"):
            tenanting.tenant_paths(sdd_dir, "../escape")


class TestEnsureTenantLayoutCreatesNothingOutside:
    """A refused identifier must leave the filesystem untouched."""

    @pytest.mark.parametrize("tenant_id", ["../../etc", "..", "a/b", "/abs/path"])
    def test_non_segment_id_creates_no_directory_outside_sdd(self, tenant_id: str, tmp_path: Path) -> None:
        sdd_dir = tmp_path / ".sdd"
        sdd_dir.mkdir()
        outside_before = sorted(p.name for p in tmp_path.iterdir())

        with pytest.raises(ValueError, match="tenant"):
            ensure_tenant_layout(sdd_dir, tenant_id)

        # Nothing new next to `.sdd`, and nothing new inside it.
        assert sorted(p.name for p in tmp_path.iterdir()) == outside_before
        assert list(sdd_dir.iterdir()) == []

    def test_valid_id_creates_the_layout_inside_sdd(self, tmp_path: Path) -> None:
        sdd_dir = tmp_path / ".sdd"
        sdd_dir.mkdir()
        paths = ensure_tenant_layout(sdd_dir, "tenant-a")

        assert paths.backlog_dir.is_dir()
        assert paths.metrics_dir.is_dir()
        assert paths.root.resolve().is_relative_to(sdd_dir.resolve())
