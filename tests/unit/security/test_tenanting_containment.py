"""Tenant layout writes land under the `.sdd` directory they were derived from.

`test_tenanting_validation` covers the identifier rule and the containment
assert on a derived path. Those answer where the layout points at the moment
it is derived. This module covers the other half: where a *write* lands, which
is a different question whenever the directory can change between the two.

Three cases the derived-path check cannot decide on its own:

* A tenant directory linked to a sibling tenant passes it -- the link does
  resolve under `.sdd` -- and would alias one tenant's writes onto another's.
* A directory *below* the tenant root was never checked at all.
* A directory replaced after the check is a different directory by the time
  the write happens.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bernstein.core.persistence.anchored_write import ANCHORED_WRITE_SUPPORTED, anchored_append
from bernstein.core.security.tenanting import (
    InvalidTenantIdError,
    ensure_tenant_layout,
    tenant_metrics_dir,
    tenant_metrics_target,
    tenant_paths,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.ci

needs_anchoring = pytest.mark.skipif(not ANCHORED_WRITE_SUPPORTED, reason="needs dir_fd and O_NOFOLLOW")


@pytest.fixture
def sdd_dir(tmp_path: Path) -> Path:
    """Return a fresh `.sdd` directory."""
    path = tmp_path / ".sdd"
    path.mkdir()
    return path


class TestAnchorTracksTheLayout:
    """The anchored form names the same directories as the path form."""

    def test_anchor_matches_the_derived_paths(self, sdd_dir: Path) -> None:
        paths = tenant_paths(sdd_dir, "acme")
        assert paths.anchor.path == paths.root
        assert paths.anchor.child("backlog").path == paths.backlog_dir
        assert paths.anchor.child("metrics").path == paths.metrics_dir

    def test_layout_is_created_where_the_paths_say(self, sdd_dir: Path) -> None:
        paths = ensure_tenant_layout(sdd_dir, "acme")
        assert paths.backlog_dir.is_dir()
        assert paths.metrics_dir.is_dir()
        assert paths.root == sdd_dir / "acme"


class TestPreExistingRootSymlink:
    """A tenant root that is already a link is refused, wherever it points."""

    def test_root_linked_outside_sdd_is_refused(self, sdd_dir: Path, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (sdd_dir / "acme").symlink_to(outside, target_is_directory=True)

        with pytest.raises((InvalidTenantIdError, OSError)):
            ensure_tenant_layout(sdd_dir, "acme")
        assert list(outside.iterdir()) == []

    @needs_anchoring
    def test_root_linked_to_a_sibling_tenant_is_refused(self, sdd_dir: Path) -> None:
        """The case the containment assert cannot catch on its own.

        `.sdd/acme -> .sdd/beta` resolves under `.sdd`, so a check that only
        asks whether the derived root stays inside the tenant base is
        satisfied by it. Creating the layout through the walk is what refuses
        it, and the point of the test is that one tenant cannot be pointed at
        another's subtree.
        """
        beta = ensure_tenant_layout(sdd_dir, "beta")
        (sdd_dir / "acme").symlink_to(beta.root, target_is_directory=True)

        with pytest.raises(OSError):
            ensure_tenant_layout(sdd_dir, "acme")
        assert list(beta.backlog_dir.iterdir()) == []


@needs_anchoring
class TestChildSymlink:
    """A directory below the tenant root is layout, never a link."""

    def test_linked_backlog_dir_is_refused(self, sdd_dir: Path, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (sdd_dir / "acme").mkdir()
        (sdd_dir / "acme" / "backlog").symlink_to(outside, target_is_directory=True)

        with pytest.raises(OSError):
            ensure_tenant_layout(sdd_dir, "acme")
        assert list(outside.iterdir()) == []

    def test_linked_metrics_dir_is_refused(self, sdd_dir: Path, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (sdd_dir / "acme").mkdir()
        (sdd_dir / "acme" / "metrics").symlink_to(outside, target_is_directory=True)

        with pytest.raises(OSError):
            ensure_tenant_layout(sdd_dir, "acme")
        assert list(outside.iterdir()) == []


@needs_anchoring
class TestReplacementAfterTheLayoutExists:
    """A directory swapped after creation is refused at the write."""

    def test_tenant_root_replaced_before_the_append(self, sdd_dir: Path, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        paths = ensure_tenant_layout(sdd_dir, "acme")

        # Everything the derived-path check could see was true when it ran.
        import shutil

        shutil.rmtree(paths.root)
        (sdd_dir / "acme").symlink_to(outside, target_is_directory=True)

        with pytest.raises(OSError):
            with anchored_append(paths.anchor.child("backlog"), "tasks.jsonl") as handle:
                handle.write("{}\n")
        assert not (outside / "backlog").exists()

    def test_backlog_dir_replaced_before_the_append(self, sdd_dir: Path, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        paths = ensure_tenant_layout(sdd_dir, "acme")

        paths.backlog_dir.rmdir()
        paths.backlog_dir.symlink_to(outside, target_is_directory=True)

        with pytest.raises(OSError):
            with anchored_append(paths.anchor.child("backlog"), "tasks.jsonl") as handle:
                handle.write("{}\n")
        assert not (outside / "tasks.jsonl").exists()


class TestMetricsDirContract:
    """`tenant_metrics_dir` carries the contract `tenant_paths` already had."""

    def test_shared_metrics_dir_yields_a_tenant_sibling(self, sdd_dir: Path) -> None:
        metrics = sdd_dir / "metrics"
        metrics.mkdir()
        assert tenant_metrics_dir(metrics, "acme") == sdd_dir / "acme" / "metrics"

    def test_other_base_hangs_the_tenant_off_it(self, sdd_dir: Path) -> None:
        base = sdd_dir / "other"
        base.mkdir()
        assert tenant_metrics_dir(base, "acme") == base / "acme"

    def test_target_and_path_forms_agree(self, sdd_dir: Path) -> None:
        metrics = sdd_dir / "metrics"
        metrics.mkdir()
        assert tenant_metrics_target(metrics, "acme").path == tenant_metrics_dir(metrics, "acme")

    @pytest.mark.parametrize("tenant_id", ["..", "../escape", "a/b", "/abs"])
    def test_non_segment_id_is_refused(self, sdd_dir: Path, tenant_id: str) -> None:
        metrics = sdd_dir / "metrics"
        metrics.mkdir()
        with pytest.raises(InvalidTenantIdError):
            tenant_metrics_dir(metrics, tenant_id)
        with pytest.raises(InvalidTenantIdError):
            tenant_metrics_target(metrics, tenant_id)

    def test_containment_holds_independently_of_the_identifier_rule(
        self, sdd_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The assert is load-bearing on its own, as in `tenant_paths`."""
        from bernstein.core.security import tenanting

        metrics = sdd_dir / "metrics"
        metrics.mkdir()
        monkeypatch.setattr(tenanting, "normalize_tenant_id", lambda raw: str(raw))
        with pytest.raises(InvalidTenantIdError):
            tenanting.tenant_metrics_dir(metrics, "..")
