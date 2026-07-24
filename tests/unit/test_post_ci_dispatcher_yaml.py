"""Structural assertions on ``.github/workflows/post-ci-dispatcher.yml``.

The dispatcher consolidates the sibling ``workflow_run: CI completed``
listeners (auto-release, auto-heal, bernstein-ci-fix, bisect-on-red)
into a single boot that calls each child via ``workflow_call``. The
acceptance criteria are encoded as tests here so the consolidation
cannot silently regress.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - dev env should have pyyaml
    pytest.skip("pyyaml not installed", allow_module_level=True)


REPO_ROOT = Path(__file__).resolve().parents[2]
DISPATCHER = REPO_ROOT / ".github" / "workflows" / "post-ci-dispatcher.yml"

CHILDREN = (
    "auto-release",
    "auto-heal",
    "bernstein-ci-fix",
    "bisect-on-red",
)


# Each child must declare the exact set of repo secrets it consumes so the
# dispatcher can forward only those (zizmor `secrets-inherit`: blanket
# `secrets: inherit` would otherwise leak every repository secret to every
# called workflow). GITHUB_TOKEN is auto-provided and never appears here.
EXPECTED_CHILD_SECRETS: dict[str, frozenset[str]] = {
    "auto-release": frozenset(),
    "auto-heal": frozenset(),
    "bernstein-ci-fix": frozenset({"GEMINI_API_KEY"}),
    "bisect-on-red": frozenset(),
}


def _load(path: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(path.read_text(encoding="utf-8")))


def _on(workflow: dict[str, Any]) -> dict[str, Any]:
    # PyYAML maps unquoted ``on:`` to True under YAML 1.1; tolerate both.
    on = workflow.get(True, workflow.get("on"))
    assert isinstance(on, dict), "workflow must have an `on:` block"
    return on


@pytest.fixture(scope="module")
def dispatcher() -> dict[str, Any]:
    return _load(DISPATCHER)


def test_dispatcher_file_exists() -> None:
    assert DISPATCHER.exists(), "post-ci-dispatcher.yml must exist at the documented path"


def test_dispatcher_listens_to_workflow_run_ci_main(dispatcher: dict[str, Any]) -> None:
    """Single workflow_run: CI completed listener on main."""
    on = _on(dispatcher)
    wfr = on.get("workflow_run")
    assert isinstance(wfr, dict)
    assert wfr.get("workflows") == ["CI"]
    assert wfr.get("types") == ["completed"]
    assert wfr.get("branches") == ["main"]


def test_dispatcher_has_meta_job(dispatcher: dict[str, Any]) -> None:
    """Meta job resolves the upstream event once and exposes named outputs."""
    jobs = dispatcher["jobs"]
    assert "meta" in jobs, "meta job must exist to surface upstream metadata"
    meta = jobs["meta"]
    outputs = meta.get("outputs") or {}
    for key in ("head_sha", "head_branch", "conclusion", "run_id", "display_title", "actor_login"):
        assert key in outputs, f"meta job must expose `{key}` as an output"


@pytest.mark.parametrize("child", CHILDREN)
def test_dispatcher_calls_each_child(dispatcher: dict[str, Any], child: str) -> None:
    """Each former workflow_run listener must be invoked via workflow_call.

    Secret passthrough must be explicit per child (zizmor `secrets-inherit`):
    a blanket `secrets: inherit` is rejected. Each child's `secrets:` block
    in the dispatcher must match the documented set in
    ``EXPECTED_CHILD_SECRETS`` exactly.
    """
    jobs = dispatcher["jobs"]
    assert child in jobs, f"dispatcher missing job for `{child}`"
    job = jobs[child]
    uses = job.get("uses", "")
    assert isinstance(uses, str)
    assert uses.endswith(f"{child}.yml"), f"job `{child}` must reuse `{child}.yml`"
    secrets = job.get("secrets")
    assert child in EXPECTED_CHILD_SECRETS, (
        f"EXPECTED_CHILD_SECRETS is missing an entry for child job `{child}` "
        f"(did you forget to update EXPECTED_CHILD_SECRETS when adding `{child}` to CHILDREN?)"
    )
    expected = EXPECTED_CHILD_SECRETS[child]
    if not expected:
        assert secrets in (None, {}), (
            f"job `{child}` must not forward any repository secrets (expected empty, got {secrets!r})"
        )
        return
    assert secrets != "inherit", (
        f"job `{child}` must not use `secrets: inherit` (zizmor secrets-inherit). Forward only {sorted(expected)}."
    )
    assert isinstance(secrets, dict), (
        f"job `{child}` must declare an explicit secrets map, got {type(secrets).__name__}"
    )
    assert set(secrets.keys()) == expected, (
        f"job `{child}` secrets map mismatch: expected {sorted(expected)}, got {sorted(secrets.keys())}"
    )


def test_bernstein_ci_fix_serialised_after_auto_heal(dispatcher: dict[str, Any]) -> None:
    """bernstein-ci-fix runs only when auto-heal did NOT open a heal PR.

    Acceptance criterion: auto-heal and bernstein-ci-fix call each other
    via dispatcher (instead of both firing in parallel on the same
    failing SHA). The serialisation is implemented as needs: auto-heal
    plus a gate on auto-heal's heal_outcome output.
    """
    jobs = dispatcher["jobs"]
    ci_fix = jobs["bernstein-ci-fix"]
    needs = ci_fix.get("needs")
    if isinstance(needs, str):
        needs = [needs]
    assert isinstance(needs, list)
    assert "auto-heal" in needs, "bernstein-ci-fix must declare needs: auto-heal"
    if_cond = ci_fix.get("if", "")
    assert isinstance(if_cond, str)
    assert "needs.auto-heal" in if_cond, "bernstein-ci-fix.if must inspect needs.auto-heal to serialise the heals"


def test_dispatcher_concurrency_per_sha(dispatcher: dict[str, Any]) -> None:
    """Dispatcher owns the per-SHA concurrency group covering the fanout."""
    conc = dispatcher.get("concurrency")
    assert isinstance(conc, dict)
    group = conc.get("group", "")
    assert isinstance(group, str)
    assert "head_sha" in group, "concurrency group must key on head_sha so reruns idempotently supersede"


def test_dispatcher_workflow_permissions_minimal(dispatcher: dict[str, Any]) -> None:
    """Workflow-level permissions are empty; child jobs declare their own."""
    perms = dispatcher.get("permissions")
    assert perms == {} or perms == "{}"


@pytest.mark.parametrize(
    "child_yaml",
    [
        ".github/workflows/auto-release.yml",
        ".github/workflows/auto-heal.yml",
        ".github/workflows/bernstein-ci-fix.yml",
        ".github/workflows/bisect-on-red.yml",
    ],
)
def test_children_expose_workflow_call(child_yaml: str) -> None:
    """Each former workflow_run listener must be a workflow_call reusable.

    The file path must stay the same (so branch protection and external
    tooling that resolve workflows by file name keep working), but the
    trigger surface must move to workflow_call so the dispatcher owns
    the single workflow_run boot.
    """
    path = REPO_ROOT / child_yaml
    assert path.exists(), f"child workflow `{child_yaml}` must exist at the original path"
    data = _load(path)
    on = _on(data)
    assert "workflow_call" in on, f"`{child_yaml}` must declare on: workflow_call:"
    assert "workflow_run" not in on, f"`{child_yaml}` must NOT keep workflow_run; the dispatcher owns that trigger now"


# --- Reusable-workflow permission containment -------------------------------
#
# A called workflow inherits the *calling job's* GITHUB_TOKEN permissions.
# When the caller grants less than any callee job requests, the Actions
# validator rejects the call before scheduling anything: the whole
# dispatcher run ends as `conclusion: startup_failure` with zero
# check_runs. Nothing turns red, no check fails, no notification fires -
# every child workflow simply stops running.
#
# That has bitten this dispatcher twice. The second time, a callee job
# gained `actions: write` while the caller kept its narrower grant, and
# auto-release, auto-heal, bernstein-ci-fix and bisect-on-red were all
# silently dead for five days across 83 consecutive runs. The assertion
# below turns that class of change into a red build instead.

_PERMISSION_LEVELS = {"none": 0, "read": 1, "write": 2}

# Scopes a job declares implicitly via the `read-all` / `write-all` shorthand.
_ALL_SCOPES = "*"


def _normalise_permissions(perms: Any) -> dict[str, str] | None:
    """Map a `permissions:` block to `{scope: level}`.

    Returns ``None`` when the block is absent, which means "inherit" and
    therefore imposes no requirement of its own.
    """
    if perms is None:
        return None
    if isinstance(perms, str):
        if perms in {"read-all", "write-all"}:
            return {_ALL_SCOPES: perms.removesuffix("-all")}
        # `permissions: {}` can round-trip through YAML as the string "{}".
        return {}
    assert isinstance(perms, dict), f"unsupported permissions block: {perms!r}"
    return {str(scope): str(level) for scope, level in perms.items()}


def _required_permissions(child: str) -> dict[str, str]:
    """Union of the scopes every job in ``child.yml`` requests.

    A job without its own block falls back to the child's workflow-level
    block; when neither declares anything the job inherits the caller's
    grant and so constrains nothing.
    """
    data = _load(REPO_ROOT / ".github" / "workflows" / f"{child}.yml")
    workflow_level = _normalise_permissions(data.get("permissions"))
    required: dict[str, str] = {}
    for job in (data.get("jobs") or {}).values():
        if not isinstance(job, dict):
            continue
        declared = _normalise_permissions(job.get("permissions"))
        effective = workflow_level if declared is None else declared
        for scope, level in (effective or {}).items():
            if _PERMISSION_LEVELS.get(level, 0) > _PERMISSION_LEVELS.get(required.get(scope, "none"), 0):
                required[scope] = level
    return required


@pytest.mark.parametrize("child", CHILDREN)
def test_dispatcher_grants_cover_callee_requests(dispatcher: dict[str, Any], child: str) -> None:
    """The calling job must grant at least what every callee job requests."""
    job = (dispatcher.get("jobs") or {}).get(child)
    assert isinstance(job, dict), f"dispatcher missing job for `{child}`"
    granted = _normalise_permissions(job.get("permissions"))
    required = _required_permissions(child)
    assert granted is not None, (
        f"job `{child}` must declare an explicit `permissions:` block; the repo default "
        f"is read-only and would reject the call at boot (requires: {required})"
    )
    blanket = _PERMISSION_LEVELS.get(granted.get(_ALL_SCOPES, "none"), 0)
    missing = {
        scope: f"needs {level}, granted {granted.get(scope, 'nothing')}"
        for scope, level in required.items()
        if max(_PERMISSION_LEVELS.get(granted.get(scope, "none"), 0), blanket) < _PERMISSION_LEVELS[level]
    }
    assert not missing, (
        f"dispatcher job `{child}` grants less than `{child}.yml` requests: {missing}. "
        "The Actions validator rejects the call at boot, so the whole dispatcher run ends as "
        "conclusion: startup_failure with zero check_runs and every child silently stops running. "
        f"Widen the job's `permissions:` block to cover {required}."
    )
