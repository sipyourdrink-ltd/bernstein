"""Structural assertions on ``.github/workflows/contract-drift-autofix.yml``.

These tests guard the fork-PR path. The workflow pushes the regen commit
directly to the source PR's head ref via ``git push --force-with-lease``. That
works for same-repo PRs only: on a ``pull_request`` event raised from a fork,
GitHub hands the job a read-only GITHUB_TOKEN no matter what the ``permissions``
block asks for. Pushing, commenting, and opening an issue all fail there with
``Resource not accessible by integration``.

So fork PRs get neither write fallback. They get the drift rendered into the
job summary and a failing step, which puts the actionable message
("document your new command in cli-reference.md") on the red check instead of a
GitHub permissions error that buries it.

Two layers of assertion:

* structural -- the fork-detect step, the reporting step, and the regen capture
  it reads all exist and keep their shape;
* behavioural -- :func:`_steps_that_run` replays the ``if:`` guards against a
  simulated step context, so the routing is checked for
  ``head.repo.fork == true`` and ``== false`` rather than by grepping for a
  substring that happens to be present.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
from pathlib import Path

import pytest

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - dev env should have pyyaml
    pytest.skip("pyyaml not installed", allow_module_level=True)


WORKFLOW = Path(".github/workflows/contract-drift-autofix.yml")


@pytest.fixture(scope="module")
def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def workflow(workflow_text: str) -> dict[str, object]:
    return yaml.safe_load(workflow_text)


@pytest.fixture(scope="module")
def autofix_steps(workflow: dict[str, object]) -> list[dict[str, object]]:
    jobs = workflow.get("jobs", {})
    assert isinstance(jobs, dict)
    job = jobs.get("autofix")
    assert isinstance(job, dict), "expected an 'autofix' job"
    steps = job.get("steps", [])
    assert isinstance(steps, list)
    return [s for s in steps if isinstance(s, dict)]


def test_workflow_file_exists() -> None:
    assert WORKFLOW.exists(), (
        "contract-drift-autofix workflow must live at .github/workflows/contract-drift-autofix.yml"
    )


def test_fork_detect_step_present(autofix_steps: list[dict[str, object]]) -> None:
    """A step with id 'forkcheck' must set is_fork=true|false."""
    forkcheck = next((s for s in autofix_steps if s.get("id") == "forkcheck"), None)
    assert forkcheck is not None, (
        "fork-detect step (id: forkcheck) is missing. EDGE-2 hardening requires "
        "the workflow to distinguish fork PRs (no push access) from same-repo PRs."
    )
    run = forkcheck.get("run", "")
    assert isinstance(run, str)
    assert "is_fork=true" in run and "is_fork=false" in run, (
        "forkcheck step must emit both is_fork=true and is_fork=false to GITHUB_OUTPUT"
    )


def test_inline_push_skips_forks(autofix_steps: list[dict[str, object]]) -> None:
    """The inline-push step must be gated on is_fork == 'false'."""
    push = next((s for s in autofix_steps if s.get("id") == "inline_push"), None)
    assert push is not None, "inline_push step is missing"
    cond = push.get("if", "")
    assert isinstance(cond, str)
    assert "is_fork == 'false'" in cond, (
        "inline_push must require steps.forkcheck.outputs.is_fork == 'false' to "
        "avoid attempting a push to a fork ref where GITHUB_TOKEN has no write access"
    )


def test_inline_push_uses_force_with_lease(autofix_steps: list[dict[str, object]]) -> None:
    """The inline-push step must use --force-with-lease for race safety."""
    push = next((s for s in autofix_steps if s.get("id") == "inline_push"), None)
    assert push is not None
    run = push.get("run", "")
    assert isinstance(run, str)
    assert "--force-with-lease" in run, (
        "inline push must use --force-with-lease to avoid clobbering a concurrent "
        "push from the PR author or another agent (EDGE-6 race-safety)"
    )


def test_inline_push_is_continue_on_error(autofix_steps: list[dict[str, object]]) -> None:
    """A lease conflict or branch-protection denial must NOT fail the job;
    the comment-fallback step covers that case."""
    push = next((s for s in autofix_steps if s.get("id") == "inline_push"), None)
    assert push is not None
    assert push.get("continue-on-error") is True, (
        "inline_push must continue-on-error so the PR-comment fallback can run when the push is rejected"
    )


def test_comment_fallback_fires_when_the_push_failed(
    autofix_steps: list[dict[str, object]],
) -> None:
    """The comment-fallback step must trigger when inline_push failed."""
    comment = next((s for s in autofix_steps if s.get("id") == "comment"), None)
    assert comment is not None, (
        "PR-comment fallback step (id: comment) is missing. Without it, a "
        "lease-conflict same-repo PR gets no drift signal at all."
    )
    cond = comment.get("if", "")
    assert isinstance(cond, str)
    assert "inline_push.outcome == 'failure'" in cond or "inline_push.outputs.pushed != 'true'" in cond, (
        "comment fallback must fire when inline_push failed"
    )


# ---------------------------------------------------------------------------
# Fork PRs get a read-only token: no write fallback can fire
# ---------------------------------------------------------------------------
#
# ``permissions:`` is a ceiling, not a grant. A ``pull_request`` event from a
# fork caps GITHUB_TOKEN at read for every scope, so ``issues: write`` and
# ``pull-requests: write`` on the job buy nothing there. Observed on PR #3903
# (run 31906285880): the tracking-issue fallback fired on a fork PR and died
# with ``GraphQL: Resource not accessible by integration (createIssue)``, which
# is what the contributor saw instead of the real, actionable drift
# ("registered, but neither documented in cli-reference.md nor exempt:
# receipt"). The comment fallback shares the mechanism and would fail the same
# way on the patchable branch.


def test_regen_diagnostics_are_captured_to_a_file(
    autofix_steps: list[dict[str, object]],
) -> None:
    """The regen step must persist its output for the reporting steps to read.

    The actionable lines go to the script's *stderr*; stdout carries only
    progress chatter ("running X", "nothing to add"). Capturing the combined
    stream into the job log alone is not enough -- a later step has to be able
    to read the diagnostics back to put them in front of the contributor.
    """
    regen = next((s for s in autofix_steps if s.get("id") == "regen"), None)
    assert regen is not None, "regen step is missing"
    run = regen.get("run", "")
    assert isinstance(run, str)
    assert "regen.err" in run and "2>" in run, (
        "the regen step must redirect stderr to ${RUNNER_TEMP}/regen.err. The "
        "'[regen] ...' diagnostics naming the undocumented command are written "
        "to stderr, and the fork-report step reads them back from that file."
    )
    assert 'cat "${RUNNER_TEMP}/regen.err"' in run, (
        "the captured stderr must still be replayed into the job log; "
        "redirecting it to a file must not make it invisible in the run output"
    )


def test_fork_report_step_fails_with_the_regen_output(
    autofix_steps: list[dict[str, object]],
) -> None:
    """Fork PRs must fail on the drift itself, not on a permissions error."""
    report = next((s for s in autofix_steps if s.get("id") == "fork_report"), None)
    assert report is not None, (
        "fork-report step (id: fork_report) is missing. Fork PRs cannot reach "
        "any write fallback, so without it the job's only visible failure is "
        "'Resource not accessible by integration'."
    )
    cond = report.get("if", "")
    assert isinstance(cond, str)
    assert "is_fork == 'true'" in cond, "fork_report must be gated on the fork branch"

    run = report.get("run", "")
    assert isinstance(run, str)
    assert "GITHUB_STEP_SUMMARY" in run, "fork_report must render the drift into the job summary"
    assert "::error" in run, "fork_report must emit an error annotation so the message shows on the check"
    assert "regen.err" in run, "fork_report must render the captured '[regen] ...' diagnostics"
    assert re.search(r"^\s*exit 1\s*$", run, re.MULTILINE), (
        "fork_report must fail the job; a green check on unresolved drift tells the contributor there is nothing to do"
    )
    assert report.get("continue-on-error") is not True, (
        "fork_report must not continue-on-error -- failing the job is the point"
    )


def test_write_fallbacks_are_gated_off_forks(autofix_steps: list[dict[str, object]]) -> None:
    """Both write fallbacks must require a same-repo PR.

    Each one calls an API that a fork PR's read-only token cannot reach, and a
    403 there is strictly worse than not trying: it replaces the drift message
    with a GitHub permissions error.
    """
    for step_id, api in (("issue_fallback", "issues.create"), ("comment", "issues.createComment")):
        step = next((s for s in autofix_steps if s.get("id") == step_id), None)
        assert step is not None, f"{step_id} step is missing"
        cond = step.get("if", "")
        assert isinstance(cond, str)
        assert "is_fork == 'false'" in cond, (
            f"{step_id} calls {api}, which needs a write-scoped token. It must be "
            "gated on steps.forkcheck.outputs.is_fork == 'false'."
        )


def test_issue_body_carries_the_regen_diagnostics(
    autofix_steps: list[dict[str, object]],
) -> None:
    """The same-repo tracking issue must say *what* drifted, not just that it did."""
    step = next((s for s in autofix_steps if s.get("id") == "issue_fallback"), None)
    assert step is not None
    run = step.get("run", "")
    assert isinstance(run, str)
    assert "regen.err" in run, (
        "the tracking issue must quote the captured '[regen] ...' diagnostics. "
        "Listing possible reasons without the actual one makes the reader open "
        "the run log to find what the bot already knew."
    )


# ---------------------------------------------------------------------------
# Behavioural: run the report script under the runner's real shell
# ---------------------------------------------------------------------------

# GitHub runs ``run:`` blocks under ``bash --noprofile --norc -eo pipefail``,
# not plain bash. The difference is not cosmetic here: ``VAR="$(grep ...)"``
# takes grep's exit status, grep exits 1 when it matches nothing, and ``-e``
# then aborts the step -- writing no summary and no annotation, which is the
# silent failure this step exists to prevent. Running the script by hand under
# plain bash hides that entirely, so exercise it under the real flags.
_RUNNER_SHELL = ("bash", "--noprofile", "--norc", "-eo", "pipefail")


def _fork_report_script(steps: list[dict[str, object]]) -> str:
    step = next((s for s in steps if s.get("id") == "fork_report"), None)
    assert step is not None, "fork_report step is missing"
    run = step.get("run")
    assert isinstance(run, str)
    return run


def _run_fork_report(
    script: str,
    tmp_path: Path,
    *,
    regen_err: str,
    patch: str | None,
    regen_changed: str,
    reverify_status: str,
) -> tuple[int, str, str]:
    """Execute the step script and return (exit code, summary, annotations)."""
    runner_temp = tmp_path / "runner_temp"
    runner_temp.mkdir()
    (runner_temp / "regen.err").write_text(regen_err, encoding="utf-8")
    if patch is not None:
        (runner_temp / "contract-drift.patch").write_text(patch, encoding="utf-8")
    summary = tmp_path / "summary.md"
    summary.touch()
    script_file = tmp_path / "fork_report.sh"
    script_file.write_text(script, encoding="utf-8")

    completed = subprocess.run(
        [*_RUNNER_SHELL, str(script_file)],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": os.environ.get("PATH", ""),
            "RUNNER_TEMP": str(runner_temp),
            "GITHUB_STEP_SUMMARY": str(summary),
            "REGEN_CHANGED": regen_changed,
            "REVERIFY_STATUS": reverify_status,
            "LOC_DELTA": "4",
            "RUN_URL": "https://example.invalid/runs/1",
        },
    )
    return completed.returncode, summary.read_text(encoding="utf-8"), completed.stdout


# The two stderr lines run 31906285880 actually produced.
_PR_3903_STDERR = (
    "[regen] UNDOCUMENTED_EXEMPTIONS: registered, but neither documented in "
    "cli-reference.md nor exempt: receipt\n"
    "[regen] This fixture does not patch itself: an exemption records a decision, "
    "and a bot-written one records none.\n"
)


@pytest.mark.parametrize(
    ("label", "regen_err", "patch", "regen_changed", "reverify_status"),
    [
        # PR #3903: regen declined to patch, so it wrote diagnostics and no diff.
        ("unpatchable", _PR_3903_STDERR, None, "false", ""),
        # Regen patched cleanly, but the bot cannot push to a fork head ref.
        ("patchable", "", "--- a/x\n+++ b/x\n+line\n", "true", "0"),
        # Nothing on stderr and no patch: the step still has to say something.
        ("silent", "", None, "", ""),
    ],
)
def test_fork_report_script_always_reports_and_fails(
    autofix_steps: list[dict[str, object]],
    tmp_path: Path,
    label: str,
    regen_err: str,
    patch: str | None,
    regen_changed: str,
    reverify_status: str,
) -> None:
    """Under the runner's shell, every branch writes a summary and exits 1."""
    code, summary, stdout = _run_fork_report(
        _fork_report_script(autofix_steps),
        tmp_path,
        regen_err=regen_err,
        patch=patch,
        regen_changed=regen_changed,
        reverify_status=reverify_status,
    )

    assert code == 1, f"[{label}] the step must fail so the check goes red on the drift"
    assert summary.strip(), (
        f"[{label}] the job summary is empty. A red check with nothing in the "
        "summary is the failure this step was added to remove."
    )
    assert "Contract drift" in summary, f"[{label}] summary must name what failed"
    annotations = [ln for ln in stdout.splitlines() if ln.startswith("::error")]
    assert len(annotations) == 1, f"[{label}] expected exactly one error annotation, got {annotations}"
    assert annotations[0].partition("::")[2].strip(), f"[{label}] the annotation body must not be empty"


def test_fork_report_surfaces_the_undocumented_command(
    autofix_steps: list[dict[str, object]],
    tmp_path: Path,
) -> None:
    """The contributor must read the drift, not a GitHub permissions error.

    Pins the specific message from run 31906285880 end to end: the command name
    reaches the summary, and the annotation -- the line rendered on the check
    itself -- carries the actionable half rather than a generic pointer.
    """
    _, summary, stdout = _run_fork_report(
        _fork_report_script(autofix_steps),
        tmp_path,
        regen_err=_PR_3903_STDERR,
        patch=None,
        regen_changed="false",
        reverify_status="",
    )

    assert "nor exempt: receipt" in summary, "the undocumented command must appear in the summary"
    assert "cli-reference.md" in summary, "the summary must point at the file to edit"
    assert "Resource not accessible" not in summary

    annotation = next(ln for ln in stdout.splitlines() if ln.startswith("::error"))
    assert "nor exempt: receipt" in annotation, (
        "the check annotation must carry the drift itself; a contributor who "
        "reads only the red check line has to learn what to fix from it"
    )


def test_fork_report_renders_the_patch_when_regen_produced_one(
    autofix_steps: list[dict[str, object]],
    tmp_path: Path,
) -> None:
    """A fork PR cannot be pushed to or commented on, so the patch goes here."""
    patch = "--- a/tests/unit/test_api_v1_routing.py\n+++ b/tests/unit/test_api_v1_routing.py\n+    '/healthz',\n"
    _, summary, _ = _run_fork_report(
        _fork_report_script(autofix_steps),
        tmp_path,
        regen_err="",
        patch=patch,
        regen_changed="true",
        reverify_status="0",
    )

    assert "/healthz" in summary, "the regen patch must be rendered for the contributor to apply"
    assert "regen_contract_drift.py --fixture all" in summary, "and the command that reproduces it"


# ---------------------------------------------------------------------------
# Behavioural: replay the step guards for fork == true and fork == false
# ---------------------------------------------------------------------------

_STEP_OUTPUT_REF = re.compile(r"steps\.([A-Za-z_][\w-]*)\.outputs\.([A-Za-z_][\w-]*)")
_STEP_OUTCOME_REF = re.compile(r"steps\.([A-Za-z_][\w-]*)\.outcome")
_EXPR_RESIDUE = re.compile(r"\b(?:and|or|not)\b|==|!=|[()\s]")


def _eval_if(expr: str, state: dict[str, dict[str, object]]) -> bool:
    """Evaluate the slice of GitHub expression syntax these guards use.

    Only ``steps.*`` references, single-quoted literals, ``&&``/``||``/``!``,
    and ``==``/``!=`` appear in this workflow's step guards. Anything left over
    after substitution is rejected rather than guessed at, so a guard that
    later starts reading ``github.event.*`` fails this test loudly instead of
    evaluating to something arbitrary.
    """

    def _outputs(match: re.Match[str]) -> str:
        step, key = match.group(1), match.group(2)
        outputs = state.get(step, {}).get("outputs", {})
        assert isinstance(outputs, dict)
        # A step that did not run contributes '' for every output, exactly as
        # the real steps context does.
        return repr(outputs.get(key, ""))

    def _outcome(match: re.Match[str]) -> str:
        return repr(state.get(match.group(1), {}).get("outcome", "skipped"))

    py = _STEP_OUTCOME_REF.sub(_outcome, _STEP_OUTPUT_REF.sub(_outputs, expr))
    py = py.replace("&&", " and ").replace("||", " or ")
    py = re.sub(r"!(?!=)", " not ", py)

    residue = _EXPR_RESIDUE.sub("", re.sub(r"'[^']*'", "", py)).strip()
    assert not residue, f"unsupported tokens {residue!r} in condition {expr!r}"
    return bool(eval(py, {"__builtins__": {}}, {}))


def _steps_that_run(
    steps: list[dict[str, object]],
    results: dict[str, dict[str, object]],
) -> list[str]:
    """Replay every step guard in order and return the labels GitHub would run.

    ``results`` maps a step id to the outputs and outcome it produces *when it
    runs*; a step whose guard is false is recorded as skipped with empty
    outputs, which is what downstream guards read.

    Step failure is not modelled: no guard in this workflow uses a status
    function, so a failing step ends the job and everything after it is
    skipped. :func:`test_fork_pr_stops_at_the_report_step` pins that the
    reporting step is the last one eligible to run on a fork, which is what
    makes the omission safe.
    """
    state: dict[str, dict[str, object]] = {}
    ran: list[str] = []
    for step in steps:
        step_id = step.get("id")
        label = step_id or step.get("name") or "<unnamed>"
        cond = step.get("if")
        runs = cond is None or _eval_if(str(cond), state)
        if isinstance(step_id, str):
            state[step_id] = (
                results.get(step_id, {"outputs": {}, "outcome": "success"})
                if runs
                else {"outputs": {}, "outcome": "skipped"}
            )
        if runs:
            assert isinstance(label, str)
            ran.append(label)
    return ran


def _results(
    *,
    drift: bool,
    is_fork: bool,
    regen_changed: bool = False,
    reverify_ok: bool = False,
    pushed: bool = True,
) -> dict[str, dict[str, object]]:
    """Build the step-context outputs for one end-to-end scenario."""
    return {
        "forkcheck": {"outputs": {"is_fork": "true" if is_fork else "false"}, "outcome": "success"},
        "drift": {
            "outputs": {"drift": "true" if drift else "false", "status": "1" if drift else "0"},
            "outcome": "success",
        },
        "regen": {"outputs": {"regen_status": "0" if regen_changed else "1"}, "outcome": "success"},
        "verify": {"outputs": {"changed": "true" if regen_changed else "false"}, "outcome": "success"},
        "reverify": {"outputs": {"status": "0" if reverify_ok else "1"}, "outcome": "success"},
        "inline_push": {
            "outputs": {"pushed": "true"} if pushed else {},
            "outcome": "success" if pushed else "failure",
        },
    }


def test_fork_pr_with_unpatchable_drift_reports_instead_of_opening_an_issue(
    autofix_steps: list[dict[str, object]],
) -> None:
    """The exact shape of PR #3903 / run 31906285880.

    ``receipt`` was registered but neither documented nor exempt, regen refused
    to patch it (an exemption records a decision, and a bot-written one records
    none), so no diff was produced. The job then tried ``gh issue create``
    against a read-only token and failed on the permission error, hiding the
    one line the contributor needed.
    """
    ran = _steps_that_run(autofix_steps, _results(drift=True, is_fork=True))

    assert "fork_report" in ran, "a fork PR with unpatchable drift must reach the reporting step"
    assert "issue_fallback" not in ran, (
        "the tracking-issue fallback must not run on a fork PR: createIssue 403s "
        "on the read-only token and the permission error replaces the drift message"
    )
    assert "comment" not in ran, "createComment 403s on a fork PR for the same reason"
    assert "inline_push" not in ran, "there is no push access to a fork head ref"


def test_fork_pr_with_patchable_drift_also_reports_instead_of_commenting(
    autofix_steps: list[dict[str, object]],
) -> None:
    """The patchable branch on a fork hits the same read-only token."""
    ran = _steps_that_run(
        autofix_steps,
        _results(drift=True, is_fork=True, regen_changed=True, reverify_ok=True),
    )

    assert "fork_report" in ran, "a fork PR with a working patch must still get the patch rendered somewhere"
    assert "comment" not in ran
    assert "issue_fallback" not in ran
    assert "inline_push" not in ran


def test_fork_pr_stops_at_the_report_step(autofix_steps: list[dict[str, object]]) -> None:
    """Nothing is eligible to run after the fork report, so its exit 1 loses nothing.

    ``fork_report`` fails the job, and no guard here uses ``always()`` or
    ``failure()``. This pins the assumption that makes that safe: on a fork, the
    report is the last eligible step in either drift shape.
    """
    for regen_changed, reverify_ok in ((False, False), (True, True)):
        ran = _steps_that_run(
            autofix_steps,
            _results(drift=True, is_fork=True, regen_changed=regen_changed, reverify_ok=reverify_ok),
        )
        assert ran[-1] == "fork_report", (
            f"steps run after fork_report for changed={regen_changed}: {ran[ran.index('fork_report') + 1 :]}. "
            "fork_report fails the job, so anything after it would be skipped rather than run."
        )


def test_same_repo_pr_with_unpatchable_drift_still_opens_the_issue(
    autofix_steps: list[dict[str, object]],
) -> None:
    """The write path is unchanged where the token can actually write."""
    ran = _steps_that_run(autofix_steps, _results(drift=True, is_fork=False))

    assert "issue_fallback" in ran, "same-repo PRs keep the tracking-issue fallback"
    assert "fork_report" not in ran, "the fork report must not fire on a same-repo PR"
    assert "comment" not in ran, "regen produced no patch, so there is nothing to comment"


def test_same_repo_pr_with_patchable_drift_pushes_inline(
    autofix_steps: list[dict[str, object]],
) -> None:
    ran = _steps_that_run(
        autofix_steps,
        _results(drift=True, is_fork=False, regen_changed=True, reverify_ok=True),
    )

    assert "inline_push" in ran, "the primary path must still commit the regen to the source PR head ref"
    assert "comment" not in ran, "no comment when the push succeeded"
    assert "issue_fallback" not in ran
    assert "fork_report" not in ran


def test_same_repo_pr_falls_back_to_a_comment_when_the_push_fails(
    autofix_steps: list[dict[str, object]],
) -> None:
    """Branch protection or a lease conflict still routes to the PR comment."""
    ran = _steps_that_run(
        autofix_steps,
        _results(drift=True, is_fork=False, regen_changed=True, reverify_ok=True, pushed=False),
    )

    assert "inline_push" in ran
    assert "comment" in ran, "a rejected push must still surface the patch on the PR"
    assert "issue_fallback" not in ran
    assert "fork_report" not in ran


def test_clean_pr_runs_no_reporting_step(autofix_steps: list[dict[str, object]]) -> None:
    """No drift means no regen, no report, no issue -- on a fork or otherwise."""
    for is_fork in (True, False):
        ran = _steps_that_run(autofix_steps, _results(drift=False, is_fork=is_fork))
        for step_id in ("regen", "verify", "reverify", "inline_push", "comment", "issue_fallback", "fork_report"):
            assert step_id not in ran, f"{step_id} ran on a PR with no drift (is_fork={is_fork})"


def test_step_actions_are_sha_pinned(autofix_steps: list[dict[str, object]]) -> None:
    """Every ``uses: <action>`` must be pinned to a 40-char SHA, never a tag.
    Tags are mutable; a malicious tag re-point would compromise the autofix bot.
    """
    import re

    sha_pattern = re.compile(r"@[0-9a-f]{40}(\s|$)")
    for step in autofix_steps:
        uses = step.get("uses")
        if not isinstance(uses, str):
            continue
        assert sha_pattern.search(uses), (
            f"action {uses!r} is not SHA-pinned. EDGE-3 hardening requires every "
            "third-party action to be pinned to a 40-char SHA. Pin via "
            "`uses: owner/action@<sha40> # <tag>`."
        )


def test_permissions_minimum_required(workflow: dict[str, object]) -> None:
    """The ``autofix`` job needs contents:write (to push) and
    pull-requests:write (to comment). issues:write is needed for the
    tracking-issue fallback.

    These are asserted on the job rather than on the workflow top level: the
    top level grants read only, so any job added to this file later starts
    without write and has to ask for it explicitly.
    """
    top = workflow.get("permissions", {})
    assert isinstance(top, dict)
    assert top.get("contents") == "read", "top level must stay read-only; grant write per job"
    assert "write" not in top.values(), f"no write scope belongs at the top level, found {top}"

    jobs = workflow.get("jobs", {})
    assert isinstance(jobs, dict)
    job = jobs.get("autofix")
    assert isinstance(job, dict)
    perms = job.get("permissions", {})
    assert isinstance(perms, dict), "autofix must declare its own permissions"
    assert perms.get("contents") == "write", "needs contents:write to push regen commit"
    assert perms.get("pull-requests") == "write", "needs pull-requests:write for the comment-fallback path"
    assert perms.get("issues") == "write", "needs issues:write for the tracking-issue fallback"


def test_recursion_guard_on_bot_author(workflow: dict[str, object]) -> None:
    """The job-level ``if:`` must filter out bot-authored PRs so the workflow
    cannot trigger itself in a loop."""
    jobs = workflow.get("jobs", {})
    assert isinstance(jobs, dict)
    job = jobs.get("autofix")
    assert isinstance(job, dict)
    cond = job.get("if", "")
    assert isinstance(cond, str)
    assert "github-actions[bot]" in cond, "missing recursion guard: PRs authored by github-actions[bot] must be skipped"


_SELECTOR = re.compile(r"(tests/[\w./-]+\.py)((?:::[A-Za-z_]\w*)+)")

# pytest's default collection rules (pyproject.toml sets no overrides). A name
# that resolves in the AST but does not match these is still ``not found`` at
# collection time, so name existence alone is not the property under test.
_COLLECTED_CLASS = "Test"
_COLLECTED_FUNCTION = "test"


def _resolve_node_id(repo_root: Path, file_part: str, node_path: list[str]) -> bool:
    """Return True if ``node_path`` names a node pytest would actually collect.

    Walks the AST rather than importing, and applies pytest's default
    ``python_classes`` / ``python_functions`` prefixes at each step: a private
    helper such as ``_documented_commands_from_docs`` exists in the file but is
    never collected, and selecting it fails exactly like a renamed test.
    """
    tests_root = (repo_root / "tests").resolve()
    try:
        target = (repo_root / file_part).resolve()
        # ``file_part`` is workflow text, so it can carry ``..`` or point at a
        # symlink. Only a real file under tests/ is ever read.
        target.relative_to(tests_root)
        if not target.is_file() or not target.name.startswith("test_"):
            return False
        source = target.read_text(encoding="utf-8")
    except (ValueError, OSError):
        return False
    try:
        scope: list[ast.stmt] = list(ast.parse(source).body)
    except SyntaxError:
        return False
    for index, name in enumerate(node_path):
        match = next(
            (
                node
                for node in scope
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and node.name == name
            ),
            None,
        )
        if match is None:
            return False
        if isinstance(match, ast.ClassDef):
            if not name.startswith(_COLLECTED_CLASS):
                return False
            scope = list(match.body)
            continue
        # A function must be the last segment and must be collectable.
        return index == len(node_path) - 1 and name.startswith(_COLLECTED_FUNCTION)
    return False


# The two steps that actually execute the contract detectors: ``drift`` probes,
# and ``reverify`` re-runs the same node ids after regen. Both are named here
# rather than discovered, because "no step invokes pytest any more" is one of
# the states this test has to fail on.
_PROBE_STEP_IDS = ("drift", "reverify")


def _pytest_probe_steps(steps: list[dict[str, object]]) -> dict[str, set[str]]:
    """Map each probe step id to the node ids its ``run:`` command selects.

    Keyed per step rather than flattened. Both probes select the same three node
    ids, so a combined list stays non-empty and fully resolvable after a
    deletion from either one -- the boundary between the commands is what makes
    the deletion visible.

    Reading the raw file instead of the parsed ``run:`` blocks would be looser
    still: the same node id also appears in the header comment and in the
    tracking-issue body string.
    """
    probes: dict[str, set[str]] = {}
    for step_id in _PROBE_STEP_IDS:
        step = next((s for s in steps if s.get("id") == step_id), None)
        assert step is not None, (
            f"contract-drift-autofix.yml has no step with id {step_id!r}. "
            "The drift probe and its post-regen re-run are what select the contract detectors by node id."
        )
        run = step.get("run")
        assert isinstance(run, str) and "pytest" in run, (
            f"step {step_id!r} no longer invokes pytest, so it cannot probe for contract drift."
        )
        probes[step_id] = {f"{file_part}{node_part}" for file_part, node_part in _SELECTOR.findall(run)}
    return probes


def test_selected_test_node_ids_resolve(autofix_steps: list[dict[str, object]]) -> None:
    """Every ``tests/...::node`` the workflow *runs* must be collectable.

    The drift probe runs ``pytest <file>::<node>`` for the three contract
    detectors. pytest exits non-zero with ``ERROR: not found`` when a node id
    no longer resolves, and the workflow reads any non-zero exit as drift -- so
    renaming a selected test makes the probe report drift unconditionally and
    stop detecting the real thing. Observed on this branch before the name was
    restored (run 31303712728).

    Every pytest step is checked separately, and all of them must select the
    same node ids: the reverify step re-runs exactly what the drift step
    probed, and a probe that silently lost a detector is the same failure in a
    quieter form.
    """
    repo_root = WORKFLOW.resolve().parent.parent.parent
    probes = _pytest_probe_steps(autofix_steps)

    empty = sorted(step for step, selectors in probes.items() if not selectors)
    assert not empty, (
        f"pytest step(s) {empty} in contract-drift-autofix.yml select no test node id. "
        "A probe that selects nothing cannot detect drift."
    )

    selected = {step: sorted(selectors) for step, selectors in probes.items()}
    assert len({tuple(v) for v in selected.values()}) == 1, (
        f"pytest steps in contract-drift-autofix.yml no longer probe the same node ids: {selected}. "
        "The reverify step must re-run exactly what the drift step probed."
    )

    unresolved = []
    for node_id in sorted({n for selectors in probes.values() for n in selectors}):
        file_part, *node_path = node_id.split("::")
        if not _resolve_node_id(repo_root, file_part, node_path):
            unresolved.append(node_id)
    assert not unresolved, (
        "contract-drift-autofix.yml runs test node ids pytest cannot collect: "
        f"{sorted(set(unresolved))}. pytest would exit 'ERROR: not found' and the workflow "
        "would report drift on every run. Restore the name or update the workflow."
    )
