"""Structural assertions on the Post-CI dispatcher's routing filter.

``.github/workflows/post-ci-dispatcher.yml`` is the sole invocation path
for ``auto-release.yml``, ``auto-heal.yml``, ``bernstein-ci-fix.yml`` and
``bisect-on-red.yml``: each of those declares ``on: workflow_call:`` and
nothing else. Nothing publishes a failing check when the dispatcher stops
routing, so a filter that is one conclusion too wide disables automated
releases silently.

The dispatcher's ``meta`` job skips upstream conclusions no child can act
on. This module pins that the skipped set stays *provably* inert:

1. The set is exactly ``{cancelled, skipped}``.
2. ``success`` is not in it. Every release job in ``auto-release.yml``
   gates on ``inputs.conclusion == 'success'``.
3. No conclusion that ``auto-release.yml``'s stale-trigger alert acts on
   is in it - that list is read out of ``auto-release.yml`` rather than
   copied here, so widening one file fails the test in the other.
4. The failure routes still require exactly ``failure``, which the filter
   admits.
5. The dispatcher still calls all four children.

It also pins the DO-NOT-PRUNE header, because the coupling between this
file and the release path is invisible from ``auto-release.yml``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - dev env should have pyyaml
    pytest.skip("pyyaml not installed", allow_module_level=True)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"
_DISPATCHER = _WORKFLOWS / "post-ci-dispatcher.yml"
_AUTO_RELEASE = _WORKFLOWS / "auto-release.yml"

# Conclusions the dispatcher declines to boot a runner for.
INERT_CONCLUSIONS = {"cancelled", "skipped"}

CHILDREN = {
    "auto-release": "./.github/workflows/auto-release.yml",
    "auto-heal": "./.github/workflows/auto-heal.yml",
    "bernstein-ci-fix": "./.github/workflows/bernstein-ci-fix.yml",
    "bisect-on-red": "./.github/workflows/bisect-on-red.yml",
}


@pytest.fixture(scope="module")
def dispatcher() -> dict:
    return yaml.safe_load(_DISPATCHER.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def meta_condition(dispatcher: dict) -> str:
    return " ".join(dispatcher["jobs"]["meta"]["if"].split())


def _excluded_conclusions(condition: str) -> set[str]:
    return set(re.findall(r"github\.event\.workflow_run\.conclusion\s*!=\s*'([a-z_]+)'", condition))


def _alerting_conclusions() -> set[str]:
    """Conclusions auto-release.yml's stale-trigger alert reacts to."""
    auto_release = yaml.safe_load(_AUTO_RELEASE.read_text(encoding="utf-8"))
    condition = auto_release["jobs"]["alert-on-stale-release-trigger"]["if"]
    return set(re.findall(r'"([a-z_]+)"', condition))


def test_meta_skips_exactly_the_inert_conclusions(meta_condition: str) -> None:
    assert _excluded_conclusions(meta_condition) == INERT_CONCLUSIONS


def test_meta_never_filters_out_a_successful_ci_run(meta_condition: str) -> None:
    """The release path runs on success only; filtering it disables releases."""
    assert "success" not in _excluded_conclusions(meta_condition)
    assert "'success'" not in meta_condition


def test_filter_does_not_intersect_the_auto_release_alert_set(
    meta_condition: str,
) -> None:
    alerting = _alerting_conclusions()
    assert alerting, "could not read the alert conclusion list from auto-release.yml"
    assert not (alerting & _excluded_conclusions(meta_condition)), (
        "the dispatcher would swallow a conclusion auto-release.yml still acts on"
    )


def test_auto_release_jobs_still_gate_on_success() -> None:
    """The premise of the filter: no release job acts on cancelled/skipped.

    A job qualifies either by requiring ``success`` itself or by depending
    on a job that does - ``release`` needs ``gate``, and ``gate`` carries
    the guard.
    """
    jobs = yaml.safe_load(_AUTO_RELEASE.read_text(encoding="utf-8"))["jobs"]

    def requires_success(name: str, seen: frozenset[str] = frozenset()) -> bool:
        if name in seen:
            return False
        job = jobs[name]
        if "inputs.conclusion == 'success'" in " ".join(str(job.get("if", "")).split()):
            return True
        needs = job.get("needs") or []
        if isinstance(needs, str):
            needs = [needs]
        return bool(needs) and all(requires_success(dep, seen | {name}) for dep in needs)

    release_jobs = set(jobs) - {"alert-on-stale-release-trigger"}
    assert release_jobs
    for name in sorted(release_jobs):
        assert requires_success(name), (
            f"auto-release job {name!r} no longer requires a successful CI run, "
            "so the dispatcher's cancelled/skipped filter may now suppress real work"
        )


def test_failure_routes_require_exactly_failure(dispatcher: dict) -> None:
    for job_name in ("auto-heal", "bernstein-ci-fix", "bisect-on-red"):
        condition = " ".join(dispatcher["jobs"][job_name]["if"].split())
        assert "needs.meta.outputs.conclusion == 'failure'" in condition


def test_dispatcher_still_routes_to_every_child(dispatcher: dict) -> None:
    for job_name, path in CHILDREN.items():
        assert dispatcher["jobs"][job_name]["uses"] == path


def test_children_have_no_trigger_other_than_workflow_call() -> None:
    """If this ever stops holding, the DO-NOT-PRUNE note needs revisiting."""
    for path in CHILDREN.values():
        child = yaml.safe_load((_REPO_ROOT / path[2:]).read_text(encoding="utf-8"))
        on = child.get("on", child.get(True))
        assert set(on) == {"workflow_call"}, (
            f"{path} gained a second trigger; the dispatcher is documented as its sole invocation path"
        )


def test_sole_trigger_coupling_is_stated_at_the_top_of_the_file() -> None:
    header = _DISPATCHER.read_text(encoding="utf-8").split("jobs:", 1)[0]
    assert "DO NOT PRUNE" in header
    assert "auto-release.yml" in header
    assert "ONLY invocation path" in header


def test_upstream_trigger_is_still_a_single_workflow(dispatcher: dict) -> None:
    on = dispatcher.get("on", dispatcher.get(True))
    assert on["workflow_run"]["workflows"] == ["CI"]
    assert on["workflow_run"]["types"] == ["completed"]
