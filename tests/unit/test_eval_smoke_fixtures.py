"""Presence + loader tests for packaged golden/smoke fixtures.

Guards two regressions:

1. ``bernstein eval run --tier smoke`` previously exited 1 with
   "No golden tasks found" because no fixture markdown files were seeded.
   The fixtures now ship inside the wheel under
   ``src/bernstein/eval/golden_data/smoke/`` so a fresh ``pip install``
   has a working smoke tier without seeding ``.sdd/``.
2. ``.sdd/`` is gitignored and CI-enforced - fixtures must NEVER live
   under ``.sdd/eval/golden/`` in the committed tree.  This test asserts
   the source-of-truth lives under the package.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import pytest

from bernstein.eval.golden import load_golden_tasks

#: Scans the source tree rather than importing it, so no diff produces an
#: import edge to this file. The marker puts it in every pull request's
#: affected slice instead of only the merge group (#5428).
pytestmark = pytest.mark.whole_tree_guard

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACKAGED_SMOKE_DIR = _REPO_ROOT / "src" / "bernstein" / "eval" / "golden_data" / "smoke"


def test_packaged_smoke_dir_exists_in_source_tree() -> None:
    assert _PACKAGED_SMOKE_DIR.is_dir(), (
        f"packaged smoke fixture dir missing: {_PACKAGED_SMOKE_DIR} - "
        "fixtures must live under src/ to ship in the wheel"
    )


def test_packaged_smoke_dir_has_at_least_one_md_file() -> None:
    md_files = sorted(_PACKAGED_SMOKE_DIR.glob("*.md"))
    assert md_files, f"no smoke .md fixtures under {_PACKAGED_SMOKE_DIR}"


def test_sdd_smoke_dir_is_not_tracked() -> None:
    """Regression guard: .sdd/eval/golden/smoke must NOT contain committed
    fixtures (Repo hygiene CI gate forbids tracked files under .sdd/).
    """
    sdd_smoke = _REPO_ROOT / ".sdd" / "eval" / "golden" / "smoke"
    if not sdd_smoke.exists():
        return  # Clean - nothing to check.
    md_files = list(sdd_smoke.glob("*.md"))
    assert not md_files, (
        f"{sdd_smoke} contains .md files - these are operator overrides "
        "and must not be committed.  Move source-of-truth fixtures into "
        "src/bernstein/eval/golden_data/smoke/ instead."
    )


def test_load_golden_tasks_falls_back_to_packaged_fixtures(tmp_path: Path) -> None:
    """When .sdd/eval/golden/<tier>/ is empty/missing, the loader must
    fall back to wheel-shipped defaults so eval works on a fresh install.
    """
    empty_overrides = tmp_path / "no-such-golden"
    tasks = load_golden_tasks(empty_overrides, tier_filter="smoke")
    assert tasks, "loader returned no tasks for smoke tier (packaged fallback broken)"
    for task in tasks:
        assert task.tier == "smoke"
        assert task.id, f"task at {task} has empty id"
        assert task.title, f"task {task.id} has empty title"
        assert task.description, f"task {task.id} has empty description"
        assert task.max_duration_s > 0, f"task {task.id} has non-positive max_duration_s"
        assert task.max_cost_usd > 0, f"task {task.id} has non-positive max_cost_usd"


def test_operator_override_wins_over_packaged_fixtures(tmp_path: Path) -> None:
    """When .sdd/eval/golden/<tier>/*.md is non-empty, on-disk overrides
    take precedence over the packaged defaults.
    """
    overrides = tmp_path / "overrides"
    tier_dir = overrides / "smoke"
    tier_dir.mkdir(parents=True)
    (tier_dir / "999-operator.md").write_text(
        "---\nid: operator-only\ntitle: Operator override\nrole: backend\n"
        "max_cost_usd: 0.42\nmax_duration_s: 99\n---\n"
        "Operator-supplied task body.\n",
        encoding="utf-8",
    )
    tasks = load_golden_tasks(overrides, tier_filter="smoke")
    ids = {t.id for t in tasks}
    assert ids == {"operator-only"}, f"override path should fully shadow packaged defaults, got ids={ids}"


def test_importlib_resources_can_locate_packaged_smoke_md() -> None:
    """The wheel-shipped fixtures must be discoverable via importlib.resources
    so `pip install bernstein` ships a runnable smoke tier without a source
    checkout.
    """
    tier_root = resources.files("bernstein.eval.golden_data").joinpath("smoke")
    assert tier_root.is_dir(), "bernstein.eval.golden_data.smoke not present as a resource"
    md_entries = [e for e in tier_root.iterdir() if e.name.endswith(".md")]
    assert md_entries, (
        "no *.md resources under bernstein.eval.golden_data.smoke - "
        "wheel packaging regression (see pyproject.toml [tool.hatch.build.targets.wheel])"
    )
