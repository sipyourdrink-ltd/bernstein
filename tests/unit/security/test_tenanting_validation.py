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


class TestTryNormalizeTenantIdForStoredData:
    """Reading history must not abort on one unattributable row.

    Archive reads and isolation checks scan records that predate these
    rules. A row whose tenant id no longer normalizes is one the reader
    cannot attribute - it must be skipped so the scan still returns a
    result for the rows it can read.
    """

    @pytest.mark.parametrize("tenant_id", NON_SEGMENT_IDS)
    def test_unusable_stored_value_returns_none(self, tenant_id: str) -> None:
        assert tenanting.try_normalize_tenant_id(tenant_id) is None

    @pytest.mark.parametrize("tenant_id", ["tenant-a", "Tenant_1.2", "acme"])
    def test_valid_stored_value_normalizes(self, tenant_id: str) -> None:
        assert tenanting.try_normalize_tenant_id(tenant_id) == tenant_id

    @pytest.mark.parametrize("raw", ["", "   ", None])
    def test_blank_stored_value_still_defaults(self, raw: str | None) -> None:
        assert tenanting.try_normalize_tenant_id(raw) == DEFAULT_TENANT_ID

    def test_none_result_matches_no_valid_tenant(self) -> None:
        """Callers filter by equality, so an unusable row must never match."""
        assert tenanting.try_normalize_tenant_id("../../etc") != normalize_tenant_id("tenant-a")
        assert tenanting.try_normalize_tenant_id("../../etc") != DEFAULT_TENANT_ID

    def test_it_does_not_raise_for_any_input(self) -> None:
        for tenant_id in [*NON_SEGMENT_IDS, "A" * 500, "CON", "acme."]:
            tenanting.try_normalize_tenant_id(tenant_id)


class TestPlatformPortableIdentifiers:
    """One identifier must not name two different things across platforms."""

    @pytest.mark.parametrize("tenant_id", ["acme.", "tenant..", "a."])
    def test_trailing_dot_is_rejected(self, tenant_id: str) -> None:
        """Windows strips it, so `acme.` and `acme` would share a directory."""
        with pytest.raises(tenanting.InvalidTenantIdError):
            normalize_tenant_id(tenant_id)

    @pytest.mark.parametrize(
        "tenant_id",
        ["CON", "con", "Con", "NUL", "PRN", "AUX", "COM1", "com9", "LPT1", "lpt9", "CON.txt", "nul.log"],
    )
    def test_windows_reserved_device_name_is_rejected(self, tenant_id: str) -> None:
        with pytest.raises(tenanting.InvalidTenantIdError):
            normalize_tenant_id(tenant_id)

    @pytest.mark.parametrize("tenant_id", ["console", "context", "commerce", "lptx", "connect", "auxiliary"])
    def test_names_merely_starting_with_a_reserved_stem_are_allowed(self, tenant_id: str) -> None:
        """Only the exact device names are reserved, not every prefix match."""
        assert normalize_tenant_id(tenant_id) == tenant_id


class TestInvalidTenantIdErrorContract:
    """The refusal type is what keeps request surfaces returning 4xx.

    `resolve_tenant_scope` normalizes a caller-supplied tenant selector, and
    the routes that call it already translate `LookupError` into a 404. The
    refusal must therefore stay catchable as `LookupError`, or an
    unvalidated selector surfaces as an unhandled 500 instead.

    It must equally NOT be an `OSError`: `cost_tracker`, `metric_collector`,
    `audit_multitenant`, `tenant_isolation_verify`, and `task_store_core`
    all catch `OSError` around filesystem work that also normalizes tenant
    IDs, and a refusal must not be swallowed there.
    """

    def test_refusal_is_a_value_error(self) -> None:
        with pytest.raises(ValueError):
            normalize_tenant_id("../../etc")

    def test_refusal_is_a_lookup_error(self) -> None:
        with pytest.raises(LookupError):
            normalize_tenant_id("../../etc")

    def test_refusal_is_not_an_os_error(self) -> None:
        with pytest.raises(tenanting.InvalidTenantIdError) as excinfo:
            normalize_tenant_id("../../etc")
        assert not isinstance(excinfo.value, OSError)

    def test_containment_failure_uses_the_same_type(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        sdd_dir = tmp_path / ".sdd"
        sdd_dir.mkdir()
        monkeypatch.setattr(tenanting, "normalize_tenant_id", lambda raw: str(raw))
        with pytest.raises(tenanting.InvalidTenantIdError):
            tenanting.tenant_paths(sdd_dir, "../escape")


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


class TestTryNormalizeRejectsNonStrings:
    """A stored tenant id that is not a string is unreadable, not coercible.

    The values come out of JSON, so the field holds whatever was written.
    Coercing before validating turns `true` into `"True"` and `123` into
    `"123"`, both of which satisfy the identifier rules, and the row is then
    attributed to a tenant nothing wrote it for.
    """

    @pytest.mark.parametrize("stored", [True, False, 123, 1.5, ["a"], {"id": "a"}, ("a",)])
    def test_non_string_values_are_unattributable(self, stored: object) -> None:
        assert tenanting.try_normalize_tenant_id(stored) is None

    def test_coercing_first_would_have_produced_a_valid_looking_id(self) -> None:
        """The bug this rules out, stated as the assertion that used to hold."""
        assert tenanting.is_valid_tenant_id(str(True))
        assert tenanting.try_normalize_tenant_id(True) is None

    def test_absent_value_still_means_the_default_tenant(self) -> None:
        assert tenanting.try_normalize_tenant_id(None) == tenanting.DEFAULT_TENANT_ID

    def test_valid_string_is_unaffected(self) -> None:
        assert tenanting.try_normalize_tenant_id("  team-a  ") == "team-a"

    def test_malformed_string_is_still_unattributable(self) -> None:
        assert tenanting.try_normalize_tenant_id("../escape") is None
