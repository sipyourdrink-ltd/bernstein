"""Structural assertions on the required-check name canary.

The canary in ``.github/workflows/required-check-canary.yml`` defends the
single `CI gate` required context configured in branch protection on
`main`. These tests guard the canary itself plus the in-tree invariants
the canary asserts at workflow-run time, so that a refactor cannot
weaken the canary AND drift the required context in the same PR.

Invariants exercised here:

1. ``ci.yml`` exposes a ``ci-gate`` job whose ``name:`` is exactly
   ``CI gate``.
2. ``ci.yml`` exposes a ``test-macos`` job whose ``name:`` is the
   literal string ``Test (macos-latest, Python 3.13)`` (no ``${{ ... }}``
   template, which would resolve to a different string when the job is
   skipped via the gate condition).
3. Exactly two files under ``.github/workflows/*.yml`` emit a check-run
   named ``CI gate``: ``ci.yml`` (real aggregator) and
   ``ci-gate-stub.yml`` (synthetic emitter for PRs whose diff is fully
   paths-ignored by ci.yml - see PR opening this allow-list). No other
   workflow may emit this check name.
4. The canary workflow file itself exists and is wired to the
   ``pull_request``/``schedule``/``workflow_dispatch`` triggers, with
   every action SHA-pinned and the verify step asserting the same set
   of invariants.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - dev env should have pyyaml
    pytest.skip("pyyaml not installed", allow_module_level=True)


CI = Path(".github/workflows/ci.yml")
CANARY = Path(".github/workflows/required-check-canary.yml")
STUB = Path(".github/workflows/ci-gate-stub.yml")
WORKFLOWS_DIR = Path(".github/workflows")

REQUIRED_CONTEXT = "CI gate"
REQUIRED_JOB_KEY = "ci-gate"
MACOS_JOB_KEY = "test-macos"
MACOS_JOB_NAME = "Test (macos-latest, Python 3.13)"
TOPOLOGY_REPORT_PATH = "docs/operations/ci-topology.md"
TOPOLOGY_REPORT_UNIGNORE = f"!{TOPOLOGY_REPORT_PATH}"

# Allow-listed `CI gate` emitters. Branch protection still depends on
# a single required-context *name*, but two workflow files now legitimately
# produce it:
#   - ci.yml::ci-gate       - real rolled-up aggregator
#   - ci-gate-stub.yml::ci-gate - synthetic success for PRs whose diff is
#     entirely contained in ci.yml's paths-ignore list, otherwise such PRs
#     are permanently BLOCKED (e.g. Renovate lockfile bumps under
#     sdk/typescript/** or packages/vscode/**).
ALLOWED_CI_GATE_EMITTERS = {
    (CI, REQUIRED_JOB_KEY),
    (STUB, REQUIRED_JOB_KEY),
}


@pytest.fixture(scope="module")
def ci_doc() -> dict[str, object]:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def canary_text() -> str:
    return CANARY.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def canary_doc(canary_text: str) -> dict[str, object]:
    return yaml.safe_load(canary_text)


# ---------------------------------------------------------------------------
# Invariants on ci.yml that branch protection depends on
# ---------------------------------------------------------------------------


def test_ci_gate_job_exists(ci_doc: dict[str, object]) -> None:
    jobs = ci_doc.get("jobs")
    assert isinstance(jobs, dict)
    job = jobs.get(REQUIRED_JOB_KEY)
    assert isinstance(job, dict), (
        f"ci.yml must keep a `{REQUIRED_JOB_KEY}` job -- it produces the `{REQUIRED_CONTEXT}` required check on `main`."
    )


def test_ci_gate_name_is_literal_required_context(ci_doc: dict[str, object]) -> None:
    jobs = ci_doc.get("jobs")
    assert isinstance(jobs, dict)
    job = jobs[REQUIRED_JOB_KEY]
    assert isinstance(job, dict)
    assert job.get("name") == REQUIRED_CONTEXT, (
        f"ci-gate.name must equal {REQUIRED_CONTEXT!r}. "
        "Branch protection's required context is keyed on this exact string."
    )


def test_test_macos_name_is_literal(ci_doc: dict[str, object]) -> None:
    jobs = ci_doc.get("jobs")
    assert isinstance(jobs, dict)
    job = jobs.get(MACOS_JOB_KEY)
    assert isinstance(job, dict)
    name = job.get("name")
    assert isinstance(name, str)
    assert "${{" not in name and "}}" not in name, (
        f"`{MACOS_JOB_KEY}.name` must NOT be templated. "
        "Skip-state check runs post the unresolved template, breaking any "
        "downstream required-context rule keyed on the literal form."
    )
    assert name == MACOS_JOB_NAME, (
        f"`{MACOS_JOB_KEY}.name` is {name!r}; expected {MACOS_JOB_NAME!r}. "
        "If the rename is intentional, update the canary expectation in the "
        "same PR."
    )


def test_ci_gate_check_run_name_emitters_are_allow_listed() -> None:
    """Only the allow-listed workflow files may emit a `CI gate` check.

    Two emitters are intentional:
      * ``ci.yml::ci-gate`` -- the real aggregator that rolls up every
        required upstream job.
      * ``ci-gate-stub.yml::ci-gate`` -- a synthetic success for PRs whose
        diff is entirely contained in ci.yml's ``paths-ignore`` list (so
        ci.yml never fires). Without this stub such PRs sit ``BLOCKED``
        on ``main`` indefinitely (Renovate lockfile bumps for
        ``sdk/typescript/**`` were the originally reported regression).

    Any additional emitter is rejected so a future refactor cannot
    weaken branch protection by silently introducing a third source
    of the required context.
    """
    seen: list[str] = []
    for wf_path in sorted(WORKFLOWS_DIR.glob("*.yml")):
        wf = yaml.safe_load(wf_path.read_text(encoding="utf-8"))
        if not isinstance(wf, dict):
            continue
        jobs = wf.get("jobs")
        if not isinstance(jobs, dict):
            continue
        for key, body in jobs.items():
            if not isinstance(body, dict):
                continue
            if body.get("name") != REQUIRED_CONTEXT:
                continue
            seen.append(f"{wf_path}:{key}")

    seen_pairs = set()
    for entry in seen:
        wf_str, key = entry.rsplit(":", 1)
        seen_pairs.add((Path(wf_str), key))

    unexpected = seen_pairs - ALLOWED_CI_GATE_EMITTERS
    missing = ALLOWED_CI_GATE_EMITTERS - seen_pairs
    assert not unexpected, (
        f"Unexpected emitters of {REQUIRED_CONTEXT!r}: {sorted(unexpected)}. "
        "Branch protection's required context is allow-listed to "
        f"{sorted(ALLOWED_CI_GATE_EMITTERS)} only."
    )
    assert not missing, (
        f"Missing expected emitters of {REQUIRED_CONTEXT!r}: {sorted(missing)}. "
        "Both ci.yml::ci-gate and ci-gate-stub.yml::ci-gate must remain."
    )


# ---------------------------------------------------------------------------
# Invariants on the canary workflow itself
# ---------------------------------------------------------------------------


def test_canary_workflow_exists() -> None:
    assert CANARY.exists(), "required-check name canary workflow is missing"


def test_canary_has_pull_request_schedule_and_dispatch_triggers(
    canary_doc: dict[str, object],
) -> None:
    # PyYAML 1.1 parses bare ``on:`` as the boolean True; tolerate both.
    on = canary_doc.get(True, canary_doc.get("on"))
    assert isinstance(on, dict)
    assert "pull_request" in on, "canary must run on PRs that touch workflow files"
    assert "schedule" in on, "canary must run on a weekly cron"
    assert "workflow_dispatch" in on, "canary must be manually runnable"


def test_canary_pull_request_paths_filtered_to_workflows(
    canary_doc: dict[str, object],
) -> None:
    on = canary_doc.get(True, canary_doc.get("on"))
    assert isinstance(on, dict)
    pr = on.get("pull_request")
    assert isinstance(pr, dict)
    paths = pr.get("paths") or []
    assert any(".github/workflows/" in p for p in paths), "canary should only fire on PRs that modify workflow files"


def test_canary_actions_pinned_to_sha(canary_text: str) -> None:
    """Every ``uses:`` must reference a 40-char SHA, not a tag."""
    uses_lines = [m.group(0) for m in re.finditer(r"uses:\s*[^\s#]+", canary_text)]
    pattern = re.compile(r"uses:\s*[\w./-]+@[0-9a-f]{40}\b")
    for line in uses_lines:
        assert pattern.match(line), f"action not pinned to 40-char SHA: {line}"


def test_canary_permissions_locked_down(canary_doc: dict[str, object]) -> None:
    # Workflow-level permissions are empty; job-level grants only `contents: read`.
    perms = canary_doc.get("permissions")
    assert perms == {} or perms == "{}"
    jobs = canary_doc.get("jobs")
    assert isinstance(jobs, dict)
    verify = jobs.get("verify")
    assert isinstance(verify, dict)
    job_perms = verify.get("permissions")
    assert isinstance(job_perms, dict)
    assert job_perms == {"contents": "read"}


def test_canary_asserts_required_context_name(canary_text: str) -> None:
    """The literal expected context names must appear in the canary env block."""
    assert f'REQUIRED_CONTEXT: "{REQUIRED_CONTEXT}"' in canary_text
    assert f'REQUIRED_JOB_KEY: "{REQUIRED_JOB_KEY}"' in canary_text
    assert f'MACOS_JOB_KEY: "{MACOS_JOB_KEY}"' in canary_text
    assert f'MACOS_JOB_NAME: "{MACOS_JOB_NAME}"' in canary_text


# ---------------------------------------------------------------------------
# Invariants on the synthetic CI gate stub
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def stub_doc() -> dict[str, object]:
    return yaml.safe_load(STUB.read_text(encoding="utf-8"))


def test_stub_workflow_exists() -> None:
    assert STUB.exists(), (
        "ci-gate-stub.yml is missing. Without it, PRs whose diff is entirely "
        "paths-ignored by ci.yml sit BLOCKED on `main` because the required "
        "`CI gate` context is never published."
    )


def test_stub_emits_ci_gate_check(stub_doc: dict[str, object]) -> None:
    jobs = stub_doc.get("jobs")
    assert isinstance(jobs, dict)
    job = jobs.get(REQUIRED_JOB_KEY)
    assert isinstance(job, dict), f"ci-gate-stub.yml must define a `{REQUIRED_JOB_KEY}` job."
    assert job.get("name") == REQUIRED_CONTEXT, (
        f"ci-gate-stub.yml::{REQUIRED_JOB_KEY}.name must equal "
        f"{REQUIRED_CONTEXT!r} so branch protection's required context "
        "is satisfied on paths-ignored-only PRs."
    )


def test_stub_paths_mirror_ci_paths_ignore(ci_doc: dict[str, object], stub_doc: dict[str, object]) -> None:
    """The stub's ``paths:`` list MUST be identical to ci.yml's
    ``pull_request.paths-ignore:`` list. Otherwise a PR could fail both
    filters and emit no `CI gate` check at all (BLOCKED forever), or
    succeed both and waste a runner.
    """
    ci_on = ci_doc.get(True, ci_doc.get("on"))
    assert isinstance(ci_on, dict)
    pr = ci_on.get("pull_request")
    assert isinstance(pr, dict)
    ci_paths_ignore = pr.get("paths-ignore")
    assert isinstance(ci_paths_ignore, list)

    stub_on = stub_doc.get(True, stub_doc.get("on"))
    assert isinstance(stub_on, dict)
    stub_pr = stub_on.get("pull_request")
    assert isinstance(stub_pr, dict)
    stub_paths = stub_pr.get("paths")
    assert isinstance(stub_paths, list)

    assert stub_paths == ci_paths_ignore, (
        "ci-gate-stub.yml `paths:` must mirror ci.yml `pull_request.paths-ignore:` exactly.\n"
        f"  ci.yml paths-ignore : {ci_paths_ignore}\n"
        f"  stub paths          : {stub_paths}\n"
        "When you add or remove an entry in one file, update the other in the same PR."
    )


def test_ci_topology_report_changes_trigger_real_ci(ci_doc: dict[str, object], stub_doc: dict[str, object]) -> None:
    """Topology report repairs must not be docs-only skipped.

    The report is generated from workflow YAML. When a workflow change lands
    with stale topology docs, the repair PR must exercise the real CI gate
    again so main gets a fresh green check on the repaired head.
    """
    ci_on = ci_doc.get(True, ci_doc.get("on"))
    assert isinstance(ci_on, dict)

    for event_name in ("push", "pull_request"):
        event = ci_on.get(event_name)
        assert isinstance(event, dict)
        paths_ignore = event.get("paths-ignore")
        assert isinstance(paths_ignore, list)
        assert TOPOLOGY_REPORT_UNIGNORE in paths_ignore, (
            f"ci.yml {event_name}.paths-ignore must unignore {TOPOLOGY_REPORT_PATH!r}. "
            "Otherwise topology repairs can merge without a fresh real CI gate on the repaired head."
        )

    stub_on = stub_doc.get(True, stub_doc.get("on"))
    assert isinstance(stub_on, dict)
    stub_pr = stub_on.get("pull_request")
    assert isinstance(stub_pr, dict)
    stub_paths = stub_pr.get("paths")
    assert isinstance(stub_paths, list)
    assert TOPOLOGY_REPORT_UNIGNORE in stub_paths, (
        "ci-gate-stub.yml must mirror the topology-report unignore so the stub "
        "does not emit CI gate for topology report repairs."
    )


# ---------------------------------------------------------------------------
# merge_group wedge guard: the CI gate roll-up must resolve to SUCCESS on a
# merge_group event, otherwise enabling a GitHub merge queue wedges every
# merge (the queue runs CI on a synthetic merge_group ref and refuses to
# merge anything until `CI gate` reports success on it).
# ---------------------------------------------------------------------------


def _ci_gate_rollup_script(ci_doc: dict[str, object]) -> str:
    """Extract the Python heredoc body from the ci-gate roll-up step.

    The ``ci-gate`` job runs an inline ``python3 - <<'PY' ... PY`` block
    that reads ``results.json`` / ``plan.json`` / ``EVENT_NAME`` and decides
    whether the rolled-up result is a pass. We lift that exact body so the
    test exercises the shipped logic rather than a copy.
    """
    jobs = ci_doc["jobs"]
    assert isinstance(jobs, dict)
    gate = jobs[REQUIRED_JOB_KEY]
    assert isinstance(gate, dict)
    steps = gate["steps"]
    assert isinstance(steps, list)
    run_bodies = [s["run"] for s in steps if isinstance(s, dict) and "run" in s]
    rollup = next((r for r in run_bodies if "results.json" in r and "plan.json" in r), None)
    assert rollup is not None, "could not locate the ci-gate roll-up step `run:` body"
    match = re.search(r"<<'PY'\n(.*?)\n\s*PY\b", rollup, re.DOTALL)
    assert match is not None, "ci-gate roll-up no longer uses a `python3 - <<'PY'` heredoc"
    return textwrap.dedent(match.group(1))


def _run_rollup(
    tmp_path: Path,
    script: str,
    *,
    event: str,
    needs: dict[str, dict[str, str]],
    plan: dict[str, str],
    event_payload: dict[str, object] | None = None,
) -> subprocess.CompletedProcess[str]:
    (tmp_path / "results.json").write_text(json.dumps(needs))
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    payload_path = tmp_path / "event.json"
    payload_path.write_text(json.dumps(event_payload or {}))
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={
            "EVENT_NAME": event,
            "GITHUB_EVENT_PATH": str(payload_path),
            "PATH": __import__("os").environ.get("PATH", ""),
        },
        capture_output=True,
        text=True,
        check=False,
    )


# A typical (non-macOS-sensitive) merge_group entry: every required job
# succeeds except the ones whose `if:` excludes merge_group. Under
# merge_group: macOS-gated jobs skip (if: only fires on push / sensitive /
# label), and PR-only jobs skip (if: pull_request).
_MERGE_GROUP_NEEDS = {
    "determine-changes": {"result": "success"},
    "repo-hygiene": {"result": "success"},
    "lint": {"result": "success"},
    "spelling": {"result": "success"},
    "actionlint": {"result": "success"},
    "lineage-gate": {"result": "success"},
    "typecheck": {"result": "success"},
    "dead-code": {"result": "success"},
    "dist-size": {"result": "success"},
    "install-smoke-pipx": {"result": "success"},
    "install-smoke-uv": {"result": "success"},
    "property-tests": {"result": "success"},
    "snapshot-tests": {"result": "success"},
    "schemathesis-smoke": {"result": "success"},
    "semgrep": {"result": "success"},
    "bandit": {"result": "success"},
    "pip-audit": {"result": "success"},
    "beartype": {"result": "success"},
    "pyright-strict-zone": {"result": "success"},
    "adapter-integration": {"result": "success"},
    "adapter-integration-macos": {"result": "skipped"},  # if: push/sensitive/label
    "test": {"result": "success"},
    "test-macos": {"result": "skipped"},  # if: push/sensitive/label
}


def test_ci_gate_rollup_passes_on_merge_group(ci_doc: dict[str, object], tmp_path: Path) -> None:
    """The shipped roll-up must PASS on a merge_group event whose only
    non-success jobs are the ones legitimately skipped under merge_group.

    If this fails, a GitHub merge queue would wedge: the first queued entry
    with a non-macOS-sensitive diff makes test-macos / adapter-integration-macos
    skip, and an intolerant gate reads that as a failure -> nothing merges.
    """
    script = _ci_gate_rollup_script(ci_doc)
    proc = _run_rollup(
        tmp_path,
        script,
        event="merge_group",
        needs=_MERGE_GROUP_NEEDS,
        plan={"docs_only": "false", "macos_sensitive": "false"},
    )
    assert proc.returncode == 0, (
        "CI gate roll-up FAILED on a merge_group event -- enabling a merge "
        "queue would wedge all merges.\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


def test_ci_gate_rollup_still_fails_on_real_failure_under_merge_group(
    ci_doc: dict[str, object], tmp_path: Path
) -> None:
    """Tolerance must not become a rubber stamp: a genuine failure (e.g.
    the ubuntu test job) must still fail the gate under merge_group.
    """
    script = _ci_gate_rollup_script(ci_doc)
    needs = dict(_MERGE_GROUP_NEEDS)
    needs["test"] = {"result": "failure"}
    proc = _run_rollup(
        tmp_path,
        script,
        event="merge_group",
        needs=needs,
        plan={"docs_only": "false", "macos_sensitive": "false"},
    )
    assert proc.returncode == 1, (
        "CI gate roll-up must FAIL when a real required job fails under "
        f"merge_group.\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


def test_ci_gate_rollup_passes_on_push(ci_doc: dict[str, object], tmp_path: Path) -> None:
    """Sanity: on a push to main the macOS jobs run (success here) and the
    PR-only jobs skip; the gate passes. Guards against a fix that breaks the
    existing push path.
    """
    script = _ci_gate_rollup_script(ci_doc)
    needs = dict(_MERGE_GROUP_NEEDS)
    needs["test-macos"] = {"result": "success"}
    needs["adapter-integration-macos"] = {"result": "success"}
    proc = _run_rollup(
        tmp_path,
        script,
        event="push",
        needs=needs,
        plan={"docs_only": "false", "macos_sensitive": "false"},
    )
    assert proc.returncode == 0, f"CI gate roll-up must pass on push.\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"


# ---------------------------------------------------------------------------
# Merge-queue required-context coverage (#2966)
#
# Every context branch protection requires on a PR must also be reportable on
# a `merge_group` ref, otherwise the queue waits forever for a check that can
# never publish. See docs/operations/merge-queue.md ::
# "Required-check coverage under `merge_group`".
# ---------------------------------------------------------------------------

REVIEW_BOT_ACK_CONTEXT = "review-bot-ack"
REVIEW_BOT_ACK_WF = Path(".github/workflows/review-bot-ack.yml")

# Mirrors `repos/sipyourdrink-ltd/bernstein/branches/main/protection`
# -> required_status_checks.contexts. Keep in sync with the canary's
# BRANCH_PROTECTION_CONTEXTS_JSON.
BRANCH_PROTECTION_CONTEXTS = (REQUIRED_CONTEXT, REVIEW_BOT_ACK_CONTEXT)


def _on(doc: dict[str, object]) -> dict[str, object]:
    # PyYAML 1.1 parses a bare ``on:`` key as the boolean True.
    on = doc.get(True, doc.get("on"))
    assert isinstance(on, dict), "workflow must have an `on:` block"
    return on


def _merge_group_context_emitters() -> dict[str, list[str]]:
    """Map check-run name -> ``file::job`` for jobs reachable on merge_group."""
    emitters: dict[str, list[str]] = {}
    for wf_path in sorted(WORKFLOWS_DIR.glob("*.yml")):
        doc = yaml.safe_load(wf_path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            continue
        on = doc.get(True, doc.get("on"))
        if not isinstance(on, dict) or "merge_group" not in on:
            continue
        jobs = doc.get("jobs")
        if not isinstance(jobs, dict):
            continue
        for key, body in jobs.items():
            if not isinstance(body, dict):
                continue
            name = body.get("name")
            if not isinstance(name, str):
                continue
            # A job gated to some *other* event cannot report on the queue.
            guard = str(body.get("if") or "")
            if "github.event_name != 'merge_group'" in guard:
                continue
            emitters.setdefault(name, []).append(f"{wf_path.name}::{key}")
    return emitters


@pytest.mark.parametrize("context", BRANCH_PROTECTION_CONTEXTS)
def test_required_context_has_a_merge_group_emitter(context: str) -> None:
    """Each PR-required context must be publishable on a merge_group ref."""
    emitters = _merge_group_context_emitters()
    assert context in emitters, (
        f"No workflow publishes the required context {context!r} on a "
        "`merge_group` event. Enabling the merge queue would wedge every merge: "
        "the queue blocks forever on a required check that never reports. Add a "
        "`merge_group:` trigger and a job whose `name:` is exactly this context. "
        f"Contexts currently reachable on merge_group: {sorted(emitters)}"
    )


def test_ci_merge_group_trigger_is_unconditional(ci_doc: dict[str, object]) -> None:
    """`ci.yml`'s merge_group trigger must carry no filters.

    This is what makes ``ci-gate-stub.yml`` safe to leave on ``pull_request``
    only. The stub exists because ci.yml's ``pull_request`` trigger is
    ``paths-ignore``-filtered, so a fully-ignored diff never publishes
    ``CI gate``. ``paths``/``paths-ignore`` are evaluated only for ``push``,
    ``pull_request`` and ``pull_request_target``, so on a merge group ci.yml
    always runs and always publishes the context - no stub needed.

    Adding any filter here (or gating the job on an event) would silently
    wedge the queue for every diff the filter excludes.
    """
    merge_group = _on(ci_doc).get("merge_group")
    assert merge_group in (None, {}), (
        "ci.yml `merge_group:` must stay unconditional (`merge_group: {}`); "
        f"found {merge_group!r}. A filtered merge_group trigger means some "
        "merge groups never publish `CI gate` and sit in the queue forever."
    )


def test_review_bot_ack_has_merge_group_passthrough() -> None:
    """The queue-side emitter for `review-bot-ack` must stay wired.

    The real gate evaluates PR review threads and has nothing to evaluate on
    the queue's ephemeral ref, so a dedicated pass-through job republishes the
    identical context name there. Without it the context cannot be required on
    the ruleset, and the two gates (PR entry vs queue merge) drift apart.
    """
    doc = yaml.safe_load(REVIEW_BOT_ACK_WF.read_text(encoding="utf-8"))
    assert isinstance(doc, dict)
    assert "merge_group" in _on(doc), "review-bot-ack.yml must trigger on merge_group"

    jobs = doc.get("jobs")
    assert isinstance(jobs, dict)
    passthrough = [
        key
        for key, body in jobs.items()
        if isinstance(body, dict)
        and body.get("name") == REVIEW_BOT_ACK_CONTEXT
        and "merge_group" in str(body.get("if") or "")
        and "!=" not in str(body.get("if") or "")
    ]
    assert passthrough, (
        f"review-bot-ack.yml must keep a job named {REVIEW_BOT_ACK_CONTEXT!r} "
        "gated to `github.event_name == 'merge_group'` so the context reports "
        "on queued groups."
    )
