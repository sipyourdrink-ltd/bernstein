"""The merge queue cannot merge, or cannot wedge, on a required context.

Enabling the queue makes ``merge_group`` the event that decides whether
``main`` advances. Two symmetric failure modes follow, and neither one
reports as a red check anywhere:

*Wedge.* A required context that cannot report on a ``merge_group`` ref
leaves the queue waiting forever for a check that will never arrive. The
pull requests sit in the queue, nothing is red, and nothing merges.

*Blind pass.* A job the ``CI gate`` roll-up needs, which skips on
``merge_group`` and lands in a tolerance bucket it does not belong in,
makes the gate green for a combination that was never built - the exact
hole the queue exists to close.

Both are decided by static structure, so they are asserted statically
here. The roll-up's tolerance sets are read out of the shipped
``ci.yml`` rather than restated, so this file cannot drift away from the
code it guards.

See ``docs/operations/merge-queue.md``.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - dev env should have pyyaml
    pytest.skip("pyyaml not installed", allow_module_level=True)

WORKFLOWS = Path(".github/workflows")
CI_WF = WORKFLOWS / "ci.yml"
RUNBOOK = Path("docs/operations/merge-queue.md")

GATE_JOB_KEY = "ci-gate"
# Job `if:` expressions that reference these are event-shape dependent:
# their truth value changes with `github.event_name`, so they may resolve
# differently on a queued group than on the pull request.
EVENT_SHAPE_MARKERS = ("github.event_name", "github.event.pull_request")

# Top-level directory whose change can move the shipped SPA bundle. A
# `pull_request` lane that can run on a change under it can also fail on
# one, which is the property #4010 turned into a red `main`.
WEB_ROOT = "web"
# A `paths` entry starting with either of these matches any diff, so the
# lane runs on a `web/**` change too.
MATCH_ANYTHING = ("*", "**")

#: Lanes that can fail on a `web/**` change and carry an unfiltered
#: `merge_group` trigger, so branch protection *can* require them.
#:
#: A name here is a claim about the file, not an exemption from it:
#: `test_queue_reporting_web_lanes_can_actually_be_required` re-reads the
#: workflow and fails if the trigger is missing or filtered.
QUEUE_REPORTING_WEB_LANES = {
    "spa-bundle-freshness.yml": (
        "The bundle gate. #4010 merged with it red because the lane was advisory, and main "
        "went red behind eleven queue entries. #4028 added the merge_group trigger so that "
        "`shipped bundle matches the lockfile` can be made a required context."
    ),
}

#: Lanes that can run on a `web/**` change and stay advisory on purpose.
#:
#: This bucket exists because #4010's bill was for an *unwritten* deferral.
#: The reason the bundle gate could not be required lived in a comment in
#: its own header, and nothing failed when the comment stopped describing
#: an acceptable state. A deferral that has to be typed here, next to the
#: assertion it suppresses, is one somebody can find and re-decide.
ADVISORY_BY_DECISION = {
    "typecheck-ts.yml": (
        "`tsc --noEmit` over the TypeScript packages, `web/` among them. Same shape as the "
        "bundle gate and not fixed by #4028, whose scope is one lane; raised on that thread "
        "rather than folded in silently."
    ),
    "pr-policy.yml": (
        "Consolidated per-PR policy lane (text hygiene, main-red guard, andon gate, "
        "pre-merge autosync). Its own header already records that none of these is a "
        "required check."
    ),
    "trufflehog.yml": ("Secret scanning. Advisory as configured today; nothing about #4028 changed it."),
    "pr-observability-summary.yml": (
        "Posts a summary on the pull request behind a `deep-review` label. It reports, it "
        "does not gate, so requiring it is not the missing piece."
    ),
    "dependabot-auto-merge.yml": (
        "Automation that merges Dependabot PRs once the required checks pass. It consumes "
        "the gate rather than being part of it."
    ),
}


def _load(path: Path) -> dict[str, Any]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), f"{path} is not a mapping"
    return doc


def _on(doc: dict[str, Any]) -> dict[str, Any]:
    # PyYAML 1.1 parses a bare ``on:`` key as the boolean True.
    on = doc.get(True, doc.get("on"))
    assert isinstance(on, dict), "workflow must declare a mapping of triggers"
    return on


@pytest.fixture(scope="module")
def ci_doc() -> dict[str, Any]:
    return _load(CI_WF)


@pytest.fixture(scope="module")
def ci_jobs(ci_doc: dict[str, Any]) -> dict[str, Any]:
    jobs = ci_doc.get("jobs")
    assert isinstance(jobs, dict)
    return jobs


@pytest.fixture(scope="module")
def rollup_source(ci_jobs: dict[str, Any]) -> str:
    """The Python heredoc the ``ci-gate`` job runs to fold `needs.*.result`."""
    gate = ci_jobs.get(GATE_JOB_KEY)
    assert isinstance(gate, dict), f"ci.yml must keep a `{GATE_JOB_KEY}` job"
    for step in gate.get("steps") or []:
        run = str(step.get("run", ""))
        if "<<'PY'" not in run:
            continue
        body = run.split("<<'PY'", 1)[1]
        return body.split("\nPY", 1)[0]
    raise AssertionError(f"`{GATE_JOB_KEY}` no longer runs an inline Python roll-up")


@pytest.fixture(scope="module")
def rollup_constants(rollup_source: str) -> dict[str, Any]:
    """Module-level literal assignments inside the roll-up script.

    Parsed rather than restated: a tolerance bucket renamed or emptied in
    ``ci.yml`` must change what these tests assert, not slip past them.
    """
    tree = ast.parse(rollup_source)
    out: dict[str, Any] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or not target.id.isupper():
            continue
        try:
            out[target.id] = ast.literal_eval(node.value)
        except ValueError:
            continue
    return out


@pytest.fixture(scope="module")
def queue_required_contexts() -> tuple[str, ...]:
    """Contexts the runbook tells the operator to require on the queue."""
    match = re.search(r"<<'JSON'\n(.*?)\nJSON\n", RUNBOOK.read_text(encoding="utf-8"), re.DOTALL)
    assert match, "merge-queue.md must keep the copy-pasteable ruleset payload"
    payload = json.loads(match.group(1))
    for rule in payload.get("rules", []):
        if rule.get("type") == "required_status_checks":
            checks = rule["parameters"]["required_status_checks"]
            return tuple(c["context"] for c in checks)
    raise AssertionError("enable payload declares no required_status_checks rule")


def _triggers(doc: Mapping[Any, Any]) -> dict[str, Any]:
    """The workflow's trigger mapping, or an empty one if it has none.

    Looked up under both keys because PyYAML 1.1 parses a bare ``on:`` as
    the boolean ``True``. Unlike ``_on`` this tolerates a workflow with no
    triggers rather than asserting, because the callers below scan every
    file in the directory and a malformed one is not their subject.
    """
    on = doc.get(True, doc.get("on"))
    return on if isinstance(on, dict) else {}


def _triggers_on_merge_group(doc: dict[str, Any]) -> bool:
    return "merge_group" in _triggers(doc)


def _names_web(entry: str) -> bool:
    """Whether a ``paths`` / ``paths-ignore`` entry covers a ``web/`` change."""
    head = str(entry).lstrip("!").split("/", 1)[0]
    return head == WEB_ROOT or head in MATCH_ANYTHING


def _can_run_on_a_web_change(doc: dict[str, Any]) -> bool:
    """Whether a ``pull_request`` event over a ``web/**`` diff starts this lane.

    Negated entries are skipped on both sides, which is the conservative
    direction in each case: an excluded pattern is not read as adding
    coverage under ``paths``, and not read as removing it under
    ``paths-ignore``. A lane that lands in scope by mistake has to be
    written down, which is cheap; one that escapes is the defect.
    """
    on = _triggers(doc)
    if "pull_request" not in on:
        return False
    trigger = on.get("pull_request")
    if not isinstance(trigger, dict):
        # Bare ``pull_request:`` - every pull request, `web/**` included.
        return True

    paths = trigger.get("paths")
    if isinstance(paths, list):
        return any(_names_web(e) for e in paths if not str(e).startswith("!"))

    ignored = trigger.get("paths-ignore")
    if isinstance(ignored, list):
        return not any(_names_web(e) for e in ignored if not str(e).startswith("!"))

    return True


def _emits_any(path: Path, doc: dict[str, Any], contexts: tuple[str, ...]) -> bool:
    """Whether the workflow publishes one of ``contexts`` as a check run.

    Unlike ``test_every_queue_required_context_has_a_merge_group_emitter``
    this does not care which event the lane runs on: the question here is
    whether the workflow is part of the required gate at all.
    """
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return False
    text = path.read_text(encoding="utf-8")
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        if any(job.get("name") == c or f"--name {c}" in text for c in contexts):
            return True
    return False


def _web_triggerable_lanes() -> list[Path]:
    out: list[Path] = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        try:
            doc = _load(path)
        except (AssertionError, yaml.YAMLError):
            continue
        if _can_run_on_a_web_change(doc):
            out.append(path)
    return out


# The scan below is only as good as the predicate that feeds it, and the
# predicate's interesting cases are not all present in the tree on any given
# day - exactly one workflow uses `paths-ignore` under `pull_request`, and it
# is exempt for an unrelated reason, so a bug in that branch would change
# nothing today and everything the moment a second one appeared. These
# assert the function's behaviour directly rather than through whatever
# `.github/workflows` happens to contain.
@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("web/**", True),
        ("web/package-lock.json", True),
        ("**", True),
        ("*", True),
        ("!web/**", True),  # `lstrip` first: callers decide what negation means
        ("src/**", False),
        ("docs/**", False),
        # A prefix, not a path segment. `webhooks/**` is a different tree and
        # a substring test would wrongly claim the lane guards the bundle.
        ("webhooks/**", False),
        ("src/bernstein/gui/static/**", False),
    ],
)
def test_names_web_matches_on_a_path_segment(entry: str, expected: bool) -> None:
    assert _names_web(entry) is expected


@pytest.mark.parametrize(
    ("trigger", "expected"),
    [
        # No `pull_request` trigger at all - not this test's subject.
        ({"push": {"branches": ["main"]}}, False),
        # Bare `pull_request:` - runs on every PR, `web/**` among them.
        ({"pull_request": None}, True),
        ({"pull_request": {"types": ["opened"]}}, True),
        # An allow-list decides it.
        ({"pull_request": {"paths": ["web/**"]}}, True),
        ({"pull_request": {"paths": ["src/**", "web/**"]}}, True),
        ({"pull_request": {"paths": ["src/**"]}}, False),
        # A negated entry is not coverage, so this lane never sees `web/`.
        ({"pull_request": {"paths": ["!web/**", "src/**"]}}, False),
        # A deny-list decides it the other way round.
        ({"pull_request": {"paths-ignore": ["docs/**"]}}, True),
        ({"pull_request": {"paths-ignore": ["web/**"]}}, False),
        # A negated deny-list entry re-includes, so coverage stands.
        ({"pull_request": {"paths-ignore": ["!web/**"]}}, True),
    ],
)
def test_can_run_on_a_web_change_reads_both_filter_shapes(trigger: dict[str, Any], expected: bool) -> None:
    assert _can_run_on_a_web_change({"on": trigger}) is expected


def test_can_run_on_a_web_change_reads_the_yaml_true_key() -> None:
    """PyYAML 1.1 turns a bare ``on:`` into ``True``; both spellings count."""
    assert _can_run_on_a_web_change({True: {"pull_request": {"paths": ["web/**"]}}}) is True
    assert _can_run_on_a_web_change({}) is False


def test_emits_any_discriminates(tmp_path: Path) -> None:
    """The gate-emitter exemption must not wave everything through.

    If this predicate ever answered ``True`` unconditionally, the scan below
    would pass with an empty allow-list and assert nothing at all.
    """
    wf = tmp_path / "w.yml"
    wf.write_text("jobs:\n  a:\n    name: CI gate\n", encoding="utf-8")
    assert _emits_any(wf, {"jobs": {"a": {"name": "CI gate"}}}, ("CI gate",)) is True
    assert _emits_any(wf, {"jobs": {"a": {"name": "something else"}}}, ("CI gate",)) is False
    assert _emits_any(wf, {}, ("CI gate",)) is False


def test_the_bundle_gate_is_not_exempt_as_a_gate_emitter(
    queue_required_contexts: tuple[str, ...],
) -> None:
    """It is accounted for by the allow-list, not by publishing `CI gate`.

    Pins the reason the lane is in the first dict. If it ever started
    emitting a required context the entry would become dead weight, and
    worse, the canary in
    ``tests/unit/test_required_check_canary_workflow_yaml.py`` would already
    be failing for a different reason.
    """
    path = WORKFLOWS / "spa-bundle-freshness.yml"
    assert _can_run_on_a_web_change(_load(path)) is True
    assert _emits_any(path, _load(path), queue_required_contexts) is False


def test_no_web_triggerable_lane_is_advisory_only(
    queue_required_contexts: tuple[str, ...],
) -> None:
    """Every lane that can fail on a ``web/**`` change is accounted for.

    This is the assertion #4010 did not have. `SPA bundle freshness` went
    red on that pull request, `CI gate` - the only required context - went
    green, branch protection was satisfied, and the queue merged it. The
    lane existed precisely to stop that and could only annotate it.

    A lane in scope has to be one of three things, and the third is the
    point: part of the required gate, on the way to being required, or a
    deferral somebody typed out on purpose. What is no longer available is
    the fourth state, which is where the bundle gate sat - advisory, for a
    reason recorded only in a comment, with nothing failing when that
    reason expired.
    """
    unaccounted: list[str] = []
    for path in _web_triggerable_lanes():
        if path.name in QUEUE_REPORTING_WEB_LANES or path.name in ADVISORY_BY_DECISION:
            continue
        if _emits_any(path, _load(path), queue_required_contexts):
            continue
        unaccounted.append(path.name)

    assert not unaccounted, (
        f"these lanes can run - and so can fail - on a `web/**` change, but they neither publish a "
        f"required context nor appear in QUEUE_REPORTING_WEB_LANES or ADVISORY_BY_DECISION: "
        f"{sorted(unaccounted)}. That is the #4010 shape: red lane, green gate, merged anyway. Give the "
        f"lane a `merge_group` trigger and list it in the first dict, or record why it stays advisory in "
        f"the second."
    )


def test_queue_reporting_web_lanes_can_actually_be_required() -> None:
    """The allow-list above must not be able to lie about a file.

    Listing a lane in ``QUEUE_REPORTING_WEB_LANES`` is a claim that a
    maintainer can safely add it to branch protection. If the trigger is
    absent, or filtered, that claim is false and flipping the switch wedges
    the queue instead of gating the pull request - the failure mode in
    ``docs/operations/merge-queue.md``'s troubleshooting table, row 1.
    """
    for name, reason in QUEUE_REPORTING_WEB_LANES.items():
        path = WORKFLOWS / name
        assert path.is_file(), f"QUEUE_REPORTING_WEB_LANES names {name}, which does not exist"
        assert reason.strip(), f"{name} needs a reason, not an empty string"
        trigger = _on(_load(path)).get("merge_group", "__absent__")
        assert trigger != "__absent__", (
            f"{name} is listed as requirable but declares no `merge_group` trigger. A required context "
            f"that cannot report on a queued ref wedges the queue."
        )
        assert trigger in (None, {}), (
            f"{name}'s `merge_group` trigger carries a filter ({trigger!r}). `paths` is not evaluated for "
            f"`merge_group` at all, so a filter there is either ignored or a mistake - and a queue batch "
            f"stacks several pull requests, so the batch can carry a `web/**` change the tested entry does not."
        )


def _stale_bucket_entries(bucket: Mapping[str, str], in_scope: set[str]) -> list[str]:
    """Bucket names that no longer describe a lane the scan looks at.

    Split out from the test so it can be exercised on a bucket that has
    actually gone stale. Doing it only against the shipped constants would
    assert nothing until the day one of them rots, which is the day the
    assertion is supposed to have been protecting something.
    """
    return sorted(name for name in bucket if name not in in_scope)


@pytest.mark.parametrize(
    ("bucket", "in_scope", "expected"),
    [
        ({"a.yml": "reason"}, {"a.yml"}, []),
        ({"a.yml": "reason"}, {"a.yml", "b.yml"}, []),
        # The lane's trigger changed and it left the scan's scope: the entry
        # is now suppressing an assertion about nothing.
        ({"a.yml": "reason"}, {"b.yml"}, ["a.yml"]),
        ({"a.yml": "reason", "b.yml": "reason"}, set(), ["a.yml", "b.yml"]),
        ({}, {"a.yml"}, []),
    ],
)
def test_stale_bucket_entries_flags_a_lane_that_left_scope(
    bucket: dict[str, str], in_scope: set[str], expected: list[str]
) -> None:
    assert _stale_bucket_entries(bucket, in_scope) == expected


def test_lane_buckets_stay_in_sync_with_the_tree() -> None:
    """Stale entries in either bucket are silent holes, so they are errors."""
    overlap = set(QUEUE_REPORTING_WEB_LANES) & set(ADVISORY_BY_DECISION)
    assert not overlap, f"{sorted(overlap)} is both requirable and advisory-by-decision; pick one"

    in_scope = {path.name for path in _web_triggerable_lanes()}
    for bucket, label in (
        (QUEUE_REPORTING_WEB_LANES, "QUEUE_REPORTING_WEB_LANES"),
        (ADVISORY_BY_DECISION, "ADVISORY_BY_DECISION"),
    ):
        for name in bucket:
            assert (WORKFLOWS / name).is_file(), f"{label} names {name}, which does not exist"
        stale = _stale_bucket_entries(bucket, in_scope)
        assert not stale, (
            f"{label} names {stale}, which can no longer run on a `web/**` change. The trigger changed; "
            f"drop the entry rather than leaving it to suppress an assertion about nothing."
        )

    for bucket in (QUEUE_REPORTING_WEB_LANES, ADVISORY_BY_DECISION):
        for name, reason in bucket.items():
            assert reason.strip(), f"{name} needs a reason, not an empty string"


def test_ci_merge_group_trigger_carries_no_filter(ci_doc: dict[str, Any]) -> None:
    """``ci.yml`` must run on every merge group, unconditionally.

    ``paths`` / ``paths-ignore`` are evaluated for ``push``,
    ``pull_request`` and ``pull_request_target`` only, so ``ci.yml``'s
    ``paths-ignore`` list does not apply to a queued group and
    ``ci-gate-stub.yml`` correctly has no ``merge_group`` trigger. That
    makes the unfiltered trigger load-bearing: any filter added here
    would stop ``CI gate`` from reporting on the groups it excludes, and
    the queue would wait on a context that can never arrive.
    """
    trigger = _on(ci_doc).get("merge_group", "__absent__")
    assert trigger != "__absent__", "ci.yml must declare a `merge_group` trigger or the queue has no `CI gate`"
    assert trigger in (None, {}), (
        f"ci.yml's `merge_group` trigger carries a filter ({trigger!r}). Every merge group must run CI: a group "
        "the filter excludes never publishes `CI gate`, and the queue waits on it forever."
    )


def test_every_queue_required_context_has_a_merge_group_emitter(
    queue_required_contexts: tuple[str, ...],
) -> None:
    """Each context required on the queue is published from a merge group.

    A context required by the ruleset but emitted only from
    ``pull_request`` is the wedge: the entry can never satisfy it.
    """
    assert queue_required_contexts, "the queue must require at least one context"
    emitters: dict[str, list[str]] = {c: [] for c in queue_required_contexts}
    for path in sorted(WORKFLOWS.glob("*.yml")):
        try:
            doc = _load(path)
        except (AssertionError, yaml.YAMLError):
            continue
        if not _triggers_on_merge_group(doc):
            continue
        text = path.read_text(encoding="utf-8")
        jobs = doc.get("jobs")
        if not isinstance(jobs, dict):
            continue
        for key, job in jobs.items():
            if not isinstance(job, dict):
                continue
            for context in queue_required_contexts:
                # Either the job's own name is the context, or the job
                # publishes it explicitly through the Checks API.
                if job.get("name") == context or f"--name {context}" in text:
                    emitters[context].append(f"{path.name}::{key}")

    missing = sorted(c for c, found in emitters.items() if not found)
    assert not missing, (
        f"contexts {missing} are required on the merge queue but no workflow emits them on a `merge_group` "
        f"event. The queue would wait on them forever. Found: { {k: v for k, v in emitters.items() if v} }"
    )


def test_macos_tolerance_covers_the_merge_group_event(
    rollup_constants: dict[str, Any],
) -> None:
    """The roll-up must tolerate the macOS skip on a queued group.

    The macOS jobs gate on ``push``, on a ``macos-needed`` label, or on
    the planner's ``macos_sensitive`` verdict. On a merge group there is
    no pull request payload, so the label branch cannot fire and only the
    planner's verdict can start them. Without ``merge_group`` in the
    tolerated-skip events, every non-macOS-sensitive group fails
    ``CI gate`` and nothing ever merges.
    """
    events = rollup_constants.get("MACOS_SKIP_EVENTS")
    assert events, "the roll-up no longer declares MACOS_SKIP_EVENTS"
    assert "merge_group" in events, (
        f"MACOS_SKIP_EVENTS is {events!r}. A queued group cannot set the `macos-needed` label branch, so the "
        "macOS cells skip and `CI gate` fails on every group whose diff is not macOS-sensitive."
    )


def test_event_gated_required_jobs_declare_their_merge_group_tolerance(
    ci_jobs: dict[str, Any],
    rollup_constants: dict[str, Any],
) -> None:
    """No job the gate needs may gate itself off a merge group silently.

    A job in ``ci-gate``'s ``needs`` whose ``if:`` depends on the event
    shape resolves differently on a queued group than on the pull
    request. Two outcomes, both bad and neither visible as a red check:
    the roll-up flags the skip and the queue can never merge anything, or
    the skip lands in a tolerance bucket that was never meant to cover it
    and ``CI gate`` passes without the job.

    So the rule is explicit: gate a required job on the event, and name
    it in one of the roll-up's tolerance buckets in the same change.
    """
    gate = ci_jobs[GATE_JOB_KEY]
    tolerated: set[str] = set()
    for name in ("DOCS_ONLY_SKIPPABLE", "MACOS_GATED", "PUSH_ONLY"):
        bucket = rollup_constants.get(name)
        assert bucket, f"the roll-up no longer declares {name}"
        tolerated |= set(bucket)

    undeclared: list[str] = []
    for key in gate.get("needs") or []:
        job = ci_jobs.get(key)
        assert isinstance(job, dict), f"ci-gate needs `{key}`, which is not a job in ci.yml"
        condition = " ".join(str(job.get("if", "")).split())
        if not any(marker in condition for marker in EVENT_SHAPE_MARKERS):
            continue
        if key not in tolerated:
            undeclared.append(f"{key} (if: {condition})")

    assert not undeclared, (
        "these jobs are required by `CI gate` and gate themselves on the event shape, but no tolerance bucket in "
        f"the roll-up names them: {undeclared}. On a merge_group ref they may skip; an undeclared skip is flagged "
        "by the roll-up and wedges the queue. Add the job to DOCS_ONLY_SKIPPABLE, MACOS_GATED or PUSH_ONLY with "
        "the reason its skip is safe, or drop the event condition."
    )
