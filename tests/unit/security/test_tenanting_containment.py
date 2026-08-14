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


class TestContainmentRoutedThroughPathContainment:
    """`tenanting.py` delegates its realpath check to `path_containment`.

    `tenant_paths` and `_tenant_metrics_anchor` must call
    `bernstein.core.security.path_containment.contained_path` for the
    containment check itself rather than re-deriving it locally (#3693: the
    same shape #3692 removed elsewhere). Patching that name out from under
    `tenanting` and asserting it fires is what proves the call site actually
    routes through it, as opposed to merely producing an equivalent result
    via independent logic.
    """

    def test_tenant_paths_delegates_the_containment_check_to_contained_path(
        self, sdd_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bernstein.core.security import path_containment, tenanting

        calls: list[tuple[object, ...]] = []

        def spy_contained_path(base: Path, *segments: str, label: str = "identifier") -> Path:
            calls.append((base, *segments))
            raise path_containment.PathContainmentError("forced for test")

        monkeypatch.setattr(tenanting, "contained_path", spy_contained_path)

        with pytest.raises(InvalidTenantIdError):
            tenanting.tenant_paths(sdd_dir, "acme")
        assert calls == [(sdd_dir, "acme")]

    def test_tenant_metrics_dir_delegates_the_containment_check_to_contained_path(
        self, sdd_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bernstein.core.security import path_containment, tenanting

        metrics = sdd_dir / "metrics"
        metrics.mkdir()
        calls: list[tuple[object, ...]] = []

        def spy_contained_path(base: Path, *segments: str, label: str = "identifier") -> Path:
            calls.append((base, *segments))
            raise path_containment.PathContainmentError("forced for test")

        monkeypatch.setattr(tenanting, "contained_path", spy_contained_path)

        with pytest.raises(InvalidTenantIdError):
            tenanting.tenant_metrics_dir(metrics, "acme")
        assert calls == [(sdd_dir, "acme", "metrics")]

    def test_tenant_paths_refuses_an_escaping_id_without_touching_the_filesystem(
        self, sdd_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bernstein.core.security import tenanting

        outside = tmp_path / "outside"
        outside.mkdir()
        (sdd_dir / "acme").symlink_to(outside, target_is_directory=True)
        monkeypatch.setattr(tenanting, "normalize_tenant_id", lambda raw: str(raw))

        with pytest.raises(InvalidTenantIdError):
            tenanting.tenant_paths(sdd_dir, "acme")
        assert list(outside.iterdir()) == []

    def test_ensure_tenant_layout_refuses_an_escaping_id_before_any_mkdir(
        self, sdd_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bernstein.core.security import tenanting

        outside = tmp_path / "outside"
        outside.mkdir()
        (sdd_dir / "acme").symlink_to(outside, target_is_directory=True)
        monkeypatch.setattr(tenanting, "normalize_tenant_id", lambda raw: str(raw))

        with pytest.raises(InvalidTenantIdError):
            tenanting.ensure_tenant_layout(sdd_dir, "acme")
        assert list(outside.iterdir()) == []

    def test_tenant_metrics_dir_refuses_an_escaping_id_via_symlinked_tenant_segment(
        self, sdd_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bernstein.core.security import tenanting

        metrics = sdd_dir / "metrics"
        metrics.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (sdd_dir / "acme").symlink_to(outside, target_is_directory=True)
        monkeypatch.setattr(tenanting, "normalize_tenant_id", lambda raw: str(raw))

        with pytest.raises(InvalidTenantIdError):
            tenanting.tenant_metrics_dir(metrics, "acme")
        assert not (outside / "metrics").exists()


def test_layout_is_created_when_the_sdd_directory_does_not_exist_yet(tmp_path: Path) -> None:
    """A first run in an empty project must create the layout, not refuse it.

    The anchored walk never creates its own root, by design. `.sdd` is that
    root, so leaving its creation to the walk made the very first
    `ensure_tenant_layout` in a fresh directory raise `FileNotFoundError` from
    the anchor's `open` - a missing base reported as if it were a containment
    refusal.
    """
    sdd_dir = tmp_path / ".sdd"
    assert not sdd_dir.exists()

    paths = ensure_tenant_layout(sdd_dir, "tenant-a")

    assert paths.backlog_dir.is_dir()
    assert paths.metrics_dir.is_dir()
    assert paths.root.resolve().parent == sdd_dir.resolve()


@needs_anchoring
def test_a_symlinked_sdd_directory_is_still_the_operator_s_choice(tmp_path: Path) -> None:
    """`.sdd` may be a symlink; everything below it may not.

    The anchor root is the caller's own base - operator configuration, and the
    only place in the layout a link is legitimate. Creating it when absent must
    not turn into following a link *below* it, which the walk still refuses.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    sdd_dir = tmp_path / ".sdd"
    sdd_dir.symlink_to(elsewhere, target_is_directory=True)

    paths = ensure_tenant_layout(sdd_dir, "tenant-a")

    assert (elsewhere / "tenant-a" / "backlog").is_dir()
    assert paths.backlog_dir.is_dir()


# --- the full data layout ---------------------------------------------------
#
# `ensure_tenant_data_layout` adds `runtime`, `runtime/wal` and `audit` below
# the tenant root. Those carry the WAL and the audit chain, so a link at any of
# them redirects exactly the writes that are supposed to be tamper-evident.


@needs_anchoring
def test_data_layout_refuses_a_runtime_directory_linked_at_a_sibling_tenant(tmp_path: Path) -> None:
    """The case the containment check passes and the walk still has to catch.

    `.sdd/acme/runtime -> .sdd/other/runtime` resolves under `.sdd`, so the
    derived path is contained by every measure available at derivation time.
    Following it would put one tenant's WAL inside another's subtree.
    """
    from bernstein.core.security.tenant_isolation import ensure_tenant_data_layout

    sdd_dir = tmp_path / ".sdd"
    victim = ensure_tenant_data_layout(sdd_dir, "other")

    attacker_root = sdd_dir / "acme"
    attacker_root.mkdir(parents=True)
    (attacker_root / "runtime").symlink_to(victim.runtime_dir, target_is_directory=True)

    with pytest.raises(OSError):
        ensure_tenant_data_layout(sdd_dir, "acme")

    assert not (victim.runtime_dir / "wal" / "wal").exists()


@needs_anchoring
def test_data_layout_refuses_a_linked_audit_directory(tmp_path: Path) -> None:
    """The audit chain is the one directory a redirect must never survive."""
    from bernstein.core.security.tenant_isolation import ensure_tenant_data_layout

    sdd_dir = tmp_path / ".sdd"
    outside = tmp_path / "outside"
    outside.mkdir()
    (sdd_dir / "acme").mkdir(parents=True)
    (sdd_dir / "acme" / "audit").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        ensure_tenant_data_layout(sdd_dir, "acme")


def test_data_layout_is_created_on_a_first_run(tmp_path: Path) -> None:
    """The ordinary path still works, `.sdd` absent included."""
    from bernstein.core.security.tenant_isolation import ensure_tenant_data_layout

    paths = ensure_tenant_data_layout(tmp_path / ".sdd", "acme")

    assert paths.wal_dir.is_dir()
    assert paths.audit_dir.is_dir()
    assert paths.runtime_dir.is_dir()
    assert paths.backlog_dir.is_dir()
