"""Every ``pull_request`` workflow declares a per-PR concurrency group.

A single branch push fans out across every workflow that triggers on
``pull_request``. Without a concurrency group each push leaves the
previous push's runs alive, so a PR that is pushed three times holds
three generations of runners on a pool whose free-tier ceiling is 20
concurrent jobs.

Cancelling used to look unsafe for workflows publishing a required
context. Branch protection folds *every* check-run of a required name
into its verdict, so one ``cancelled`` instance holds a PR at BLOCKED
even after a later run concludes success (#3154, #3042).

Suppressing cancellation does not fix that. A concurrency group with
``cancel-in-progress: false`` is a one-deep queue: when a run is
executing and a second is pending, a third arriving in the same group
cancels the pending one. The tombstone lands either way. What fixes it
is not naming a job after the required context, so no job state can
write it - see ``tests/unit/test_review_bot_ack_workflow_yaml.py``.

Every ``pull_request`` workflow therefore cancels superseded runs, and
``NO_CANCEL_EXCEPTIONS`` is empty. This module pins both halves: the
rule, and the closed list of exceptions. Adding a `pull_request`
workflow without a group fails here, and so does turning cancellation
off on a workflow not on the list.
"""

from __future__ import annotations

from pathlib import Path

import pytest

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - dev env should have pyyaml
    pytest.skip("pyyaml not installed", allow_module_level=True)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"

# Workflows that do NOT cancel a superseded run on a pull_request event.
# Documented in docs/operations/ci.md under "Gating vs advisory workflows".
#
# Empty on purpose. `review-bot-ack.yml` used to sit here, on the theory
# that turning cancellation off keeps a required context from being
# poisoned. It does not: with `cancel-in-progress: false` a group is a
# one-deep queue, so a third run in the same group cancels the pending one
# and the tombstone lands anyway. The durable fix was to stop naming a job
# after the required context - see
# `tests/unit/test_review_bot_ack_workflow_yaml.py` - which makes
# cancellation harmless and lets that workflow follow the ordinary rule.
#
# Before adding an entry here, check that suppressing cancellation
# actually prevents the failure you have in mind. It usually does not.
NO_CANCEL_EXCEPTIONS: set[str] = set()

# `cancel-in-progress` may be an expression rather than a literal. This one
# cancels on pull_request and preserves the per-SHA push-to-main signal, so
# it satisfies the PR-side rule.
_PR_ONLY_CANCEL = "github.event_name == 'pull_request'"

# Workflows that publish a context branch protection requires on `main`.
GATING_WORKFLOWS = {"ci.yml", "ci-gate-stub.yml", "review-bot-ack.yml"}


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(workflow: dict) -> dict:
    on = workflow.get("on", workflow.get(True)) or {}
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return dict.fromkeys(on)
    return on


def _pull_request_workflows() -> list[tuple[str, dict]]:
    found = []
    for path in sorted(_WORKFLOWS.glob("*.yml")):
        workflow = _load(path)
        if "pull_request" in _triggers(workflow):
            found.append((path.name, workflow))
    return found


def test_there_are_pull_request_workflows_to_check() -> None:
    assert len(_pull_request_workflows()) >= 15


@pytest.mark.parametrize("name, workflow", _pull_request_workflows(), ids=lambda v: v if isinstance(v, str) else "")
def test_declares_a_pr_scoped_concurrency_group(name: str, workflow: dict) -> None:
    concurrency = workflow.get("concurrency")
    assert concurrency, (
        f"{name} triggers on pull_request without a concurrency group, so each "
        "push to a PR leaves the previous push's runs holding the pool"
    )
    group = str(concurrency["group"] if isinstance(concurrency, dict) else concurrency)
    assert "github.event.pull_request.number" in group or "github.ref" in group, (
        f"{name}'s concurrency group is not keyed on the PR or its ref"
    )


@pytest.mark.parametrize("name, workflow", _pull_request_workflows(), ids=lambda v: v if isinstance(v, str) else "")
def test_cancellation_is_off_only_for_documented_exceptions(name: str, workflow: dict) -> None:
    concurrency = workflow.get("concurrency")
    cancel = concurrency.get("cancel-in-progress") if isinstance(concurrency, dict) else None
    cancels_on_pr = cancel is True or (isinstance(cancel, str) and _PR_ONLY_CANCEL in cancel)
    if name in NO_CANCEL_EXCEPTIONS:
        assert not cancels_on_pr, (
            f"{name} is listed as a no-cancel exception but cancels on pull_request; "
            "drop it from NO_CANCEL_EXCEPTIONS and from docs/operations/ci.md"
        )
        return
    assert cancels_on_pr, (
        f"{name} does not cancel a superseded pull_request run and is not a "
        "documented exception. Only workflows publishing a required context may "
        "skip cancellation - a cancelled required check-run holds the PR at "
        "BLOCKED (#3154, #3042)."
    )


def test_gating_workflows_are_the_only_required_context_emitters() -> None:
    """Guard the premise of the exception list."""
    emitters = set()
    for path in sorted(_WORKFLOWS.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        if "review-bot-ack" in text and "pull_request" in text:
            emitters.add(path.name)
    assert "review-bot-ack.yml" in emitters
    assert NO_CANCEL_EXCEPTIONS <= GATING_WORKFLOWS, (
        "a no-cancel exception was granted to a workflow that publishes no required context; advisory work must cancel"
    )


def test_ci_md_documents_the_gating_split() -> None:
    ci_md = (_REPO_ROOT / "docs" / "operations" / "ci.md").read_text(encoding="utf-8")
    assert "## Gating vs advisory workflows" in ci_md
    for name in GATING_WORKFLOWS:
        assert name in ci_md, f"{name} is gating but not named in docs/operations/ci.md"
    for name in NO_CANCEL_EXCEPTIONS:
        assert name in ci_md, f"{name} is a no-cancel exception but is undocumented"
