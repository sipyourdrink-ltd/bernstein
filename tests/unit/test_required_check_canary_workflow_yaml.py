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
5. Every context branch protection requires on a PR is also reportable on
   a ``merge_group`` ref, and the diff planner that decides which jobs may
   skip classifies a queued group from the group's own base SHA - so a
   green ``CI gate`` on the queue means the whole combination was built.
"""

from __future__ import annotations

import json
import os
import re
import shutil
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


def _emits_required_context(name: object) -> bool:
    """True when a job's ``name:`` can post a ``CI gate`` check run.

    The stub resolves its name from a guard verdict, so ``CI gate``
    appearing as a quoted branch of a ``${{ ... }}`` expression counts as
    an emitter. Matching only the bare literal would let a third emitter
    hide behind a template.
    """
    if not isinstance(name, str):
        return False
    if name == REQUIRED_CONTEXT:
        return True
    if "${{" not in name:
        return False
    return REQUIRED_CONTEXT in re.findall(r"'([^']*)'", name)


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
        It publishes the context only when its guard proves every changed
        path is ignored -- see
        ``tests/unit/test_ci_gate_stub_workflow_yaml.py``.

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
            if not _emits_required_context(body.get("name")):
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


def test_stub_can_emit_ci_gate_check(stub_doc: dict[str, object]) -> None:
    """The stub must keep a job able to publish the required context.

    It resolves the name from its guard verdict rather than hard-coding
    it, so the assertion is "``CI gate`` is a reachable branch of the
    name expression". That the *other* branch is not ``CI gate``, and
    that the verdict is derived honestly, is asserted in
    ``tests/unit/test_ci_gate_stub_workflow_yaml.py``.
    """
    jobs = stub_doc.get("jobs")
    assert isinstance(jobs, dict)
    job = jobs.get(REQUIRED_JOB_KEY)
    assert isinstance(job, dict), f"ci-gate-stub.yml must define a `{REQUIRED_JOB_KEY}` job."
    name = job.get("name")
    assert _emits_required_context(name), (
        f"ci-gate-stub.yml::{REQUIRED_JOB_KEY}.name must be able to resolve to "
        f"{REQUIRED_CONTEXT!r} so branch protection's required context "
        f"is satisfied on paths-ignored-only PRs; found {name!r}."
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


# A job publishes a check-run named after itself, but that is not the only
# way to publish a context, and for a required one it is the worse way: the
# check-run inherits the job's fate, so cancelling the job blocks the commit
# permanently and skipping it satisfies the gate without running it
# (#3042, #3154). A job may instead write the context explicitly through the
# Checks API, which is what `scripts/publish_required_check.py` does. Both
# mechanisms count as an emitter here.
_API_PUBLISHER = "publish_required_check.py"
_PUBLISHED_NAME_RE = re.compile(r"--name\s+(?P<name>[\w.-]+)")


def _api_published_contexts(job: dict) -> set[str]:
    """Context names a job writes via the Checks API publisher."""
    names: set[str] = set()
    for step in job.get("steps") or []:
        if not isinstance(step, dict):
            continue
        run = str(step.get("run") or "")
        if _API_PUBLISHER not in run:
            continue
        names.update(m.group("name") for m in _PUBLISHED_NAME_RE.finditer(run))
    return names


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
            # A job gated to some *other* event cannot report on the queue.
            guard = str(body.get("if") or "")
            if "github.event_name != 'merge_group'" in guard:
                continue
            published = _api_published_contexts(body)
            name = body.get("name")
            if isinstance(name, str):
                published.add(name)
            for context in published:
                emitters.setdefault(context, []).append(f"{wf_path.name}::{key}")
    return emitters


@pytest.mark.parametrize("context", BRANCH_PROTECTION_CONTEXTS)
def test_required_context_has_a_merge_group_emitter(context: str) -> None:
    """Each PR-required context must be publishable on a merge_group ref."""
    emitters = _merge_group_context_emitters()
    assert context in emitters, (
        f"No workflow publishes the required context {context!r} on a "
        "`merge_group` event. Enabling the merge queue would wedge every merge: "
        "the queue blocks forever on a required check that never reports. Add a "
        "`merge_group:` trigger and either a job whose `name:` is exactly this "
        "context, or a job that publishes it via scripts/publish_required_check.py. "
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
    the queue's ephemeral ref, so a dedicated queue-side job publishes the
    identical context name there. Without it the context cannot be required on
    the ruleset, and the two gates (PR entry vs queue merge) drift apart.

    The job must not be *named* after the context - a job's check-run inherits
    the job's fate, and for a required name both `cancelled` and `skipped` are
    unrecoverable states (#3042, #3154). It writes the context explicitly
    instead, on the merge group's own head SHA.
    """
    doc = yaml.safe_load(REVIEW_BOT_ACK_WF.read_text(encoding="utf-8"))
    assert isinstance(doc, dict)
    assert "merge_group" in _on(doc), "review-bot-ack.yml must trigger on merge_group"

    jobs = doc.get("jobs")
    assert isinstance(jobs, dict)
    queue_side = [
        key
        for key, body in jobs.items()
        if isinstance(body, dict)
        and REVIEW_BOT_ACK_CONTEXT in _api_published_contexts(body)
        and "merge_group" in str(body.get("if") or "")
        and "!=" not in str(body.get("if") or "")
    ]
    assert queue_side, (
        f"review-bot-ack.yml must keep a job gated to `github.event_name == 'merge_group'` that publishes "
        f"{REVIEW_BOT_ACK_CONTEXT!r} via scripts/publish_required_check.py, so the context reports on queued groups."
    )
    for key in queue_side:
        job = jobs[key]
        assert isinstance(job, dict)
        assert job.get("name") != REVIEW_BOT_ACK_CONTEXT, (
            f"job {key!r} must not be named after the required context; a cancelled or skipped job would then "
            "write the context itself"
        )
        env = "\n".join(str(s.get("env") or {}) for s in job.get("steps") or [] if isinstance(s, dict))
        assert "merge_group.head_sha" in env, (
            f"job {key!r} must publish the context on the merge group's own head SHA, which is the ref the queue "
            "evaluates required checks against"
        )


# ---------------------------------------------------------------------------
# Diff-planner correctness on a merge group (#2966)
#
# `determine-changes` decides which downstream jobs may legitimately skip, and
# the `ci-gate` roll-up trusts that decision. On a merge group the ref stacks
# every queued entry on top of `main`, so classifying it with the push
# heuristic (`HEAD~1...HEAD`) reads only the tail commit. A docs-only tail
# would mark the whole group docs-only, skip every Python test job, and let
# `CI gate` report green for a combination that was never built - the exact
# untested-combination hole the queue is meant to close.
#
# These tests execute the shipped classifier, not a copy of it.
# ---------------------------------------------------------------------------

MERGE_GROUP_BASE_SHA_EXPR = "github.event.merge_group.base_sha"

_SHELL_TOOLS_PRESENT = shutil.which("bash") is not None and shutil.which("git") is not None
requires_shell_tools = pytest.mark.skipif(
    not _SHELL_TOOLS_PRESENT,
    reason="needs bash and git to execute the shipped classify script",
)


def _classify_script(ci_doc: dict[str, object]) -> str:
    """Extract the `run:` body of the `classify` step in `determine-changes`."""
    jobs = ci_doc["jobs"]
    assert isinstance(jobs, dict)
    planner = jobs["determine-changes"]
    assert isinstance(planner, dict)
    steps = planner["steps"]
    assert isinstance(steps, list)
    for step in steps:
        if isinstance(step, dict) and step.get("id") == "classify":
            run = step.get("run")
            assert isinstance(run, str)
            return run
    raise AssertionError("ci.yml `determine-changes` no longer has a `classify` step")


def _classify_env(ci_doc: dict[str, object]) -> dict[str, str]:
    """The `env:` mapping of the `classify` step."""
    jobs = ci_doc["jobs"]
    assert isinstance(jobs, dict)
    planner = jobs["determine-changes"]
    assert isinstance(planner, dict)
    steps = planner["steps"]
    assert isinstance(steps, list)
    for step in steps:
        if isinstance(step, dict) and step.get("id") == "classify":
            env = step.get("env")
            assert isinstance(env, dict)
            return {str(k): str(v) for k, v in env.items()}
    raise AssertionError("ci.yml `determine-changes` no longer has a `classify` step")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=ci@example.invalid",
            "-c",
            "user.name=ci",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _commit_file(repo: Path, relpath: str, body: str, message: str) -> str:
    target = repo / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    _git(repo, "add", relpath)
    _git(repo, "commit", "-m", message)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return head.stdout.strip()


def _synthetic_merge_group(tmp_path: Path) -> tuple[Path, str]:
    """Build a two-entry merge group whose tail entry is docs-only.

    Returns the repo path and the group's `base_sha`. Entry 1 touches a
    macOS-sensitive Python module; entry 2 touches only docs. A planner that
    reads the tail alone sees a docs-only group.
    """
    repo = tmp_path / "queue-ref"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    base_sha = _commit_file(repo, "README.md", "base\n", "base commit")
    _commit_file(repo, "src/bernstein/core/tunnels/ssh.py", "x = 1\n", "entry 1: tunnels")
    _commit_file(repo, "docs/notes.md", "notes\n", "entry 2: docs only")
    return repo, base_sha


def _run_classify(
    repo: Path,
    script: str,
    *,
    event: str,
    extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Run the shipped classify script and return its `GITHUB_OUTPUT` pairs."""
    output_path = repo / "github_output.txt"
    output_path.write_text("", encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(repo),
        "EVENT_NAME": event,
        "BASE_REF": "",
        "MERGE_GROUP_BASE_SHA": "",
        "PUSH_BEFORE_SHA": "",
        "GITHUB_OUTPUT": str(output_path),
    }
    env.update(extra_env or {})
    proc = subprocess.run(
        ["bash", "-e", "-c", script],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"the classify step must never fail the planner job.\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    parsed: dict[str, str] = {}
    for line in output_path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            parsed[key.strip()] = value.strip()
    return parsed


def test_classify_step_reads_the_merge_group_base_sha(ci_doc: dict[str, object]) -> None:
    """The group's base SHA must reach the script through `env:`.

    Interpolating it straight into the `run:` body would be a template
    injection surface; reading it from the environment keeps the script
    auditable and testable.
    """
    env = _classify_env(ci_doc)
    assert any(MERGE_GROUP_BASE_SHA_EXPR in value for value in env.values()), (
        "the `classify` step must expose `github.event.merge_group.base_sha` via "
        f"`env:`; found {env!r}. Without it the planner classifies a queued group "
        "from its tail commit only."
    )


@requires_shell_tools
def test_planner_classifies_the_whole_merge_group(ci_doc: dict[str, object], tmp_path: Path) -> None:
    """A group whose tail commit is docs-only is still not a docs-only group.

    Entry 1 changes a macOS-sensitive Python module and entry 2 changes only
    docs. The planner must report the union, otherwise every Python test job
    skips and `CI gate` certifies an untested combination.
    """
    repo, base_sha = _synthetic_merge_group(tmp_path)
    outputs = _run_classify(
        repo,
        _classify_script(ci_doc),
        event="merge_group",
        extra_env={"MERGE_GROUP_BASE_SHA": base_sha},
    )
    assert outputs["docs_only"] == "false", (
        "planner reported docs_only=true for a merge group that changes "
        f"src/**.py in an earlier entry; outputs={outputs!r}. Every job in "
        "DOCS_ONLY_SKIPPABLE would skip and the gate would pass regardless."
    )
    assert outputs["python_changed"] == "true", (
        f"planner missed the Python change in the group's first entry; outputs={outputs!r}"
    )
    assert outputs["macos_sensitive"] == "true", (
        "planner missed the macOS-sensitive path in the group's first entry, so "
        f"the queue would not run the macOS cells for it; outputs={outputs!r}"
    )


@requires_shell_tools
def test_tail_only_classification_would_have_missed_it(ci_doc: dict[str, object], tmp_path: Path) -> None:
    """Pin down why the base SHA is needed rather than `HEAD~1`.

    The same tree classified from the tail commit alone reads as docs-only.
    This is correct for a single-commit push to `main` and wrong for a merge
    group; the test exists so a future simplification back to one shared
    heuristic fails loudly instead of silently skipping the test suite.
    """
    repo, _ = _synthetic_merge_group(tmp_path)
    outputs = _run_classify(repo, _classify_script(ci_doc), event="push")
    assert outputs["docs_only"] == "true", (
        "expected the tail-only (push) heuristic to see just the docs commit; "
        f"outputs={outputs!r}. If this changed, revisit the merge_group branch."
    )


@requires_shell_tools
def test_planner_fails_safe_when_the_group_base_is_missing(ci_doc: dict[str, object], tmp_path: Path) -> None:
    """No base SHA must widen the classification, never narrow it.

    A missing or unresolvable base is an infrastructure problem. Skipping test
    jobs because of one would let the queue merge an unvalidated combination,
    so the planner has to fall back to "everything changed".
    """
    repo, _ = _synthetic_merge_group(tmp_path)
    outputs = _run_classify(repo, _classify_script(ci_doc), event="merge_group")
    assert outputs["docs_only"] == "false", (
        f"planner must not report docs_only on an unresolvable base; outputs={outputs!r}"
    )
    for key in ("python_changed", "tests_changed", "gha_workflows_changed", "macos_sensitive"):
        assert outputs[key] == "true", (
            f"fail-safe classification must set {key}=true so no job skips; outputs={outputs!r}"
        )
