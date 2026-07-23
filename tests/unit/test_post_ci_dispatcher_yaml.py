"""Structural assertions on ``.github/workflows/post-ci-dispatcher.yml``.

The dispatcher consolidates five sibling ``workflow_run: CI completed``
listeners (auto-release, auto-heal, bernstein-ci-fix, bisect-on-red,
telegram-notify) into a single boot that calls each child via
``workflow_call``. The acceptance criteria are encoded as tests here so
the consolidation cannot silently regress.
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
    "telegram-notify",
)


# Each child must declare the exact set of repo secrets it consumes so the
# dispatcher can forward only those (zizmor `secrets-inherit`: blanket
# `secrets: inherit` would otherwise leak every repository secret to every
# called workflow). GITHUB_TOKEN is auto-provided and never appears here.
EXPECTED_CHILD_SECRETS: dict[str, frozenset[str]] = {
    "telegram-notify": frozenset({"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}),
    "auto-release": frozenset({"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}),
    "auto-heal": frozenset({"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}),
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
        ".github/workflows/telegram-notify.yml",
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
