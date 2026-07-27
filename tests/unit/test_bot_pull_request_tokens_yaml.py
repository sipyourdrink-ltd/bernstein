"""Every workflow that opens a pull request must use a triggering token.

A pull request created with the Actions token (``secrets.GITHUB_TOKEN`` or the
equivalent ``github.token``) does not trigger workflows. Neither required
context ever reports on it, so branch protection holds it at BLOCKED while the
status rollup reads SUCCESS. Nothing is red, nothing is pending, and the only
way out is an operator closing it by hand - and a closed pull request cannot be
revived by force-pushing its branch, so the next fire opens a fresh one.

The damage is not the churn. An automation lane whose pull requests can never
merge is a regeneration that never lands: the artefact it exists to refresh
goes stale while the lane keeps reporting that it ran. That is the same shape
as a check that reports without checking.

This module enumerates the pull-request-opening steps **by discovery** rather
than from a list. A new automation lane that copies the old shape is caught by
the same assertion that catches the current ones, without anyone remembering to
extend a fixture.

Four step shapes count as opening a pull request:

* ``uses: peter-evans/create-pull-request@...`` - the token is ``with.token``;
* a ``run:`` block invoking ``gh pr create`` - the token is ``GH_TOKEN`` (or
  ``GITHUB_TOKEN``) resolved from the step, then the job, then the workflow;
* a ``run:`` block POSTing to the ``.../pulls`` collection through ``gh api``,
  which is the same call one layer down and takes the same token;
* ``uses: actions/github-script@...`` whose script calls ``pulls.create`` -
  the token is ``with.github-token``, which **defaults to the Actions token**,
  so omitting it is the failure rather than a neutral choice.

The last two shapes match nothing in the tree today. They are here because the
guard is only worth having if it survives the next lane, and a lane that opens
its pull request through the API instead of the porcelain is not an exotic
hypothetical: ``issue_to_pr.py`` already opens pull requests with
``gh api -X POST repos/{repo}/pulls`` in library code. Nothing stops that call
from being lifted into a workflow, and a guard that only knows two spellings
would report success while the lane it was written to protect went unchecked.

The sweep covers composite actions under ``.github/actions`` as well as
``.github/workflows``. A composite action has ``runs.steps`` rather than
``jobs.<id>.steps``, so a pull-request step hidden behind ``uses: ./.github/
actions/<name>`` is invisible to a workflow-only sweep while being just as
capable of opening a pull request with the wrong token.

Comment lines are stripped from ``run:`` blocks before matching, so a workflow
that only *describes* ``gh pr create`` in a comment - ``auto-release.yml``
explains why it stopped calling it - is not mistaken for one that calls it.

Opening the pull request with a triggering token is necessary but not
sufficient. The branch has to be pushed with one too: a push made with the
Actions token emits no ``pull_request: synchronize``, so a lane that opens
correctly and then re-pushes incorrectly leaves an open pull request whose
checks describe an older commit. That is worse than the original bug, because
the rollup reads green against the wrong SHA rather than reading empty. The
push credential is resolved the way git resolves it: an explicit
``http.https://github.com/.extraheader`` if the step sets one, otherwise the
credential ``actions/checkout`` persisted, which is ``github.token`` unless the
step passed something else.

The last assertion here is the one that is easiest to get wrong. A reusable
workflow (``on: workflow_call``) sees only the secrets its caller passed:
``secrets.SOMETHING`` that was never declared and forwarded evaluates to the
empty string rather than raising. A ``||`` fallback then swallows it and the
lane silently degrades to the Actions token - the fix present in the file, and
absent at runtime. So a declared preference is only real when the callee
declares the secret and every caller forwards it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - dev env should have pyyaml
    pytest.skip("pyyaml not installed", allow_module_level=True)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW_DIR = _REPO_ROOT / ".github" / "workflows"
_ACTION_DIR = _REPO_ROOT / ".github" / "actions"

_CREATE_PR_ACTION = "peter-evans/create-pull-request"
_CREATE_PR_COMMAND = re.compile(r"\bgh\s+pr\s+create\b")

# `gh api -X POST .../pulls` is `gh pr create` one layer down. The negative
# lookahead keeps reads of a single pull request (`/pulls/123`, `/pulls/123/
# files`) out, and the POST requirement keeps `commits/<sha>/pulls` out, which
# is a collection-shaped path that `bisect-on-red.yml` only ever GETs.
_GH_API_COMMAND = re.compile(r"\bgh\s+api\b")
_GH_API_POST = re.compile(r"(?:^|\s)(?:-X\s*|--method[= ]\s*)POST\b")
_PULLS_COLLECTION = re.compile(r"/pulls(?![/\w])")

# `actions/github-script` exposes an authenticated Octokit. Its `github-token`
# input defaults to the Actions token, so a step that omits it opens the pull
# request with exactly the token this module exists to forbid.
_GITHUB_SCRIPT_ACTION = "actions/github-script"
_GITHUB_SCRIPT_CREATE_PR = re.compile(r"\bpulls\s*\.\s*create\s*\(")
_GITHUB_SCRIPT_DEFAULT_TOKEN = "${{ github.token }}"

# The branch push. Either the step configures git's auth header explicitly, or
# it relies on the credential `actions/checkout` persisted.
_GIT_PUSH = re.compile(r"\bgit\s+push\b")
_EXTRAHEADER_TOKEN_VAR = re.compile(r"x-access-token:%s['\"]?\s+[\"']?\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")
_CHECKOUT_ACTION = "actions/checkout"

# The Actions token under either spelling. Both are suppressed as a workflow
# trigger; `github.token` is not a safe alternative to `secrets.GITHUB_TOKEN`.
_ACTIONS_TOKEN = re.compile(r"secrets\.GITHUB_TOKEN|github\.token")

# Token env vars `gh` reads, in the order it prefers them.
_GH_TOKEN_KEYS = ("GH_TOKEN", "GITHUB_TOKEN")


def _strip_comments(script: str) -> str:
    """Drop whole-line shell comments so prose cannot look like a call."""
    return "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))


def _as_env(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items()}  # type: ignore[union-attr]


def _resolve_gh_token(step: dict[str, Any], job: dict[str, Any], workflow: dict[str, Any]) -> str | None:
    """Return the token expression `gh` would use inside ``step``."""
    for scope in (step, job, workflow):
        env = _as_env(scope.get("env"))
        for key in _GH_TOKEN_KEYS:
            if key in env:
                return env[key]
    return None


def _workflow_files() -> list[Path]:
    return sorted(p for p in _WORKFLOW_DIR.glob("*.yml") if p.is_file())


def _action_files() -> list[Path]:
    """Composite actions, which carry ``runs.steps`` instead of ``jobs``."""
    if not _ACTION_DIR.is_dir():
        return []
    found = [p for suffix in ("yml", "yaml") for p in _ACTION_DIR.glob(f"*/action.{suffix}")]
    return sorted(p for p in found if p.is_file())


def _step_containers(document: dict[str, Any]) -> list[tuple[str, dict[str, Any], list[Any]]]:
    """Return ``(container_name, container, steps)`` for a workflow or action."""
    containers: list[tuple[str, dict[str, Any], list[Any]]] = []
    jobs = document.get("jobs")
    if isinstance(jobs, dict):
        for job_name, job in jobs.items():
            if isinstance(job, dict) and isinstance(job.get("steps"), list):
                containers.append((str(job_name), job, job["steps"]))
    runs = document.get("runs")
    if isinstance(runs, dict) and isinstance(runs.get("steps"), list):
        containers.append(("runs", runs, runs["steps"]))
    return containers


def _step_label(step: dict[str, Any], index: int) -> str:
    return str(step.get("name") or step.get("id") or f"step[{index}]")


def _openers_in_document(name: str, document: Any) -> list[tuple[str, str, str, str | None]]:
    """Return ``(file, container, step, token_expression)`` for every PR opener."""
    if not isinstance(document, dict):
        return []
    found: list[tuple[str, str, str, str | None]] = []
    for container_name, container, steps in _step_containers(document):
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            label = _step_label(step, index)
            uses = str(step.get("uses", ""))
            if _CREATE_PR_ACTION in uses:
                with_block = step.get("with")
                token = None
                if isinstance(with_block, dict):
                    raw_token = with_block.get("token")
                    token = None if raw_token is None else str(raw_token)
                found.append((name, container_name, label, token))
                continue
            if _GITHUB_SCRIPT_ACTION in uses:
                with_block = step.get("with")
                script = with_block.get("script") if isinstance(with_block, dict) else None
                if isinstance(script, str) and _GITHUB_SCRIPT_CREATE_PR.search(script):
                    raw_token = with_block.get("github-token") if isinstance(with_block, dict) else None
                    token = _GITHUB_SCRIPT_DEFAULT_TOKEN if raw_token is None else str(raw_token)
                    found.append((name, container_name, label, token))
                continue
            script = step.get("run")
            if not isinstance(script, str):
                continue
            body = _strip_comments(script)
            if _CREATE_PR_COMMAND.search(body) or _opens_pull_request_over_the_api(body):
                found.append(
                    (name, container_name, label, _resolve_gh_token(step, container, document)),
                )
    return found


def _opens_pull_request_over_the_api(body: str) -> bool:
    """True for ``gh api`` calls that POST to the ``.../pulls`` collection."""
    return bool(
        _GH_API_COMMAND.search(body) and _GH_API_POST.search(body) and _PULLS_COLLECTION.search(body),
    )


def _pr_opening_steps() -> list[tuple[str, str, str, str | None]]:
    """Return ``(file, container, step, token_expression)`` across the sweep."""
    found: list[tuple[str, str, str, str | None]] = []
    for path in _workflow_files() + _action_files():
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        found.extend(_openers_in_document(_sweep_name(path), document))
    return found


def _sweep_name(path: Path) -> str:
    """Workflows are named by file; composite actions by directory."""
    if path.name.startswith("action."):
        return f"{path.parent.name}/{path.name}"
    return path.name


def test_workflow_directory_is_readable() -> None:
    assert _workflow_files(), "no workflow files discovered; the sweep would pass vacuously"


def test_discovery_finds_the_known_pull_request_lanes() -> None:
    """Guard the discovery itself: a broken matcher would pass everything."""
    lanes = {workflow for workflow, _job, _step, _token in _pr_opening_steps()}
    for expected in (
        "adapter-conformance-canary.yml",
        "auto-heal.yml",
        "bernstein-ci-fix.yml",
        "bernstein-issues-decompose.yml",
        "ci-topology-heal.yml",
        "coverage-ratchet-weekly.yml",
        "coverage-ratchet.yml",
        "docs-observability-snapshot.yml",
        "nightly-drift-sweep.yml",
        "review-bot-sweep.yml",
    ):
        assert expected in lanes, f"{expected} opens a pull request but discovery missed it"


def test_comment_only_mentions_are_not_counted_as_openers() -> None:
    """``auto-release.yml`` explains why it no longer calls ``gh pr create``."""
    raw = (_WORKFLOW_DIR / "auto-release.yml").read_text(encoding="utf-8")
    assert "gh pr create" in raw, "fixture drifted: auto-release.yml no longer mentions the command"
    lanes = {workflow for workflow, _job, _step, _token in _pr_opening_steps()}
    assert "auto-release.yml" not in lanes


@pytest.mark.parametrize(
    ("workflow", "job", "step", "token"),
    _pr_opening_steps(),
    ids=lambda value: str(value),
)
def test_pull_request_is_opened_with_a_triggering_token(
    workflow: str,
    job: str,
    step: str,
    token: str | None,
) -> None:
    assert token is not None, (
        f"{workflow} job {job!r} step {step!r} opens a pull request without naming a token. "
        "The default is the Actions token, which does not trigger workflows, so the pull "
        "request can never collect its required contexts."
    )

    without_actions_token = _ACTIONS_TOKEN.sub("", token)
    assert "secrets." in without_actions_token, (
        f"{workflow} job {job!r} step {step!r} opens a pull request with the Actions token only. "
        "A pull request created that way does not trigger workflows, so branch protection holds "
        "it at BLOCKED forever while the rollup reads SUCCESS. Prefer a configured PAT with a "
        "GITHUB_TOKEN fallback, e.g. "
        "${{ secrets.BERNSTEIN_AUTOSYNC_TOKEN || secrets.GITHUB_TOKEN }}."
    )

    actions_token = _ACTIONS_TOKEN.search(token)
    if actions_token is not None:
        assert "secrets." in token[: actions_token.start()], (
            f"{workflow} job {job!r} step {step!r} lists the Actions token before the triggering "
            "token. The Actions token may only be the fallback, never the preferred value."
        )


def _secret_names(token: str) -> set[str]:
    """Return the secret names a token expression reads."""
    return set(re.findall(r"secrets\.([A-Za-z_][A-Za-z0-9_]*)", token)) - {"GITHUB_TOKEN"}


def _declared_call_secrets(workflow: dict[str, Any]) -> set[str] | None:
    """Return declared ``workflow_call`` secrets, or None if not reusable."""
    triggers = workflow.get("on", workflow.get(True))
    if not isinstance(triggers, dict) or "workflow_call" not in triggers:
        return None
    call = triggers.get("workflow_call")
    declared = call.get("secrets") if isinstance(call, dict) else None
    return set(declared) if isinstance(declared, dict) else set()


def test_reusable_workflows_declare_the_secrets_their_pr_steps_read() -> None:
    """An undeclared secret reads as empty, so the `||` fallback hides the bug."""
    missing: list[str] = []
    for path in _workflow_files():
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(workflow, dict):
            continue
        declared = _declared_call_secrets(workflow)
        if declared is None:
            continue
        for name, job, step, token in _pr_opening_steps():
            if name != path.name or token is None:
                continue
            for secret in sorted(_secret_names(token) - declared):
                missing.append(f"{path.name} job {job!r} step {step!r} reads secrets.{secret}")
    assert not missing, (
        "these reusable workflows read a secret they do not declare under "
        "on.workflow_call.secrets, so it evaluates to empty at runtime and the "
        f"pull request is opened with the Actions token after all: {missing}"
    )


def test_callers_forward_the_secrets_reusable_pr_lanes_declare() -> None:
    """Declaring the secret is only half of it; the caller has to pass it."""
    reusable: dict[str, set[str]] = {}
    for path in _workflow_files():
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(workflow, dict):
            continue
        declared = _declared_call_secrets(workflow)
        if not declared:
            continue
        needed = {
            secret
            for name, _job, _step, token in _pr_opening_steps()
            if name == path.name and token is not None
            for secret in _secret_names(token)
        }
        if needed & declared:
            reusable[path.name] = needed & declared

    assert reusable, "no reusable pull-request lane found; this guard would pass vacuously"

    missing: list[str] = []
    for path in _workflow_files():
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        jobs = workflow.get("jobs") if isinstance(workflow, dict) else None
        if not isinstance(jobs, dict):
            continue
        for job_name, job in jobs.items():
            if not isinstance(job, dict):
                continue
            uses = str(job.get("uses", ""))
            callee = uses.rsplit("/", 1)[-1].split("@", 1)[0]
            if callee not in reusable:
                continue
            passed = job.get("secrets")
            if passed == "inherit":
                continue
            forwarded = set(passed) if isinstance(passed, dict) else set()
            for secret in sorted(reusable[callee] - forwarded):
                missing.append(f"{path.name} job {job_name!r} calls {callee} without forwarding secrets.{secret}")
    assert not missing, (
        "these callers do not forward a secret the callee's pull-request step reads, "
        f"so it evaluates to empty inside the reusable workflow: {missing}"
    )


# ---------------------------------------------------------------------------
# Discovery durability: the shapes a future lane could use to slip past
# ---------------------------------------------------------------------------


def _load(text: str) -> Any:
    return yaml.safe_load(text)


def test_github_script_pull_request_creation_is_discovered() -> None:
    """`github-token` defaults to the Actions token, so omitting it is the bug."""
    document = _load(
        """
jobs:
  propose:
    steps:
      - name: Open PR via github-script
        uses: actions/github-script@v9
        with:
          script: |
            await github.rest.pulls.create({
              owner: context.repo.owner,
              repo: context.repo.repo,
              head: 'bot/branch',
              base: 'main',
              title: 'chore: regenerate',
            });
"""
    )
    openers = _openers_in_document("synthetic.yml", document)
    assert openers, "a github-script step calling pulls.create opens a pull request"
    assert openers[0][2] == "Open PR via github-script"
    with pytest.raises(AssertionError):
        test_pull_request_is_opened_with_a_triggering_token(*openers[0])


def test_github_script_pull_request_creation_reads_its_declared_token() -> None:
    document = _load(
        """
jobs:
  propose:
    steps:
      - name: Open PR via github-script
        uses: actions/github-script@v9
        with:
          github-token: ${{ secrets.BERNSTEIN_AUTOSYNC_TOKEN || secrets.GITHUB_TOKEN }}
          script: |
            await github.rest.pulls.create({head: 'b', base: 'main', title: 't'});
"""
    )
    openers = _openers_in_document("synthetic.yml", document)
    assert len(openers) == 1
    test_pull_request_is_opened_with_a_triggering_token(*openers[0])


def test_github_script_that_only_comments_is_not_an_opener() -> None:
    """`issues.createComment` is the common shape and must not be swept up."""
    document = _load(
        """
jobs:
  notify:
    steps:
      - uses: actions/github-script@v9
        with:
          script: |
            await github.rest.issues.createComment({issue_number: 1, body: 'hi'});
"""
    )
    assert _openers_in_document("synthetic.yml", document) == []


def test_gh_api_post_to_the_pulls_collection_is_discovered() -> None:
    """The porcelain is not the only spelling; `issue_to_pr.py` uses this one."""
    document = _load(
        """
jobs:
  propose:
    steps:
      - name: Open PR through the REST API
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          gh api -X POST "repos/${REPO}/pulls" --input payload.json
"""
    )
    openers = _openers_in_document("synthetic.yml", document)
    assert openers, "gh api -X POST .../pulls opens a pull request"
    with pytest.raises(AssertionError):
        test_pull_request_is_opened_with_a_triggering_token(*openers[0])


@pytest.mark.parametrize(
    "script",
    [
        'gh api "repos/${REPO}/pulls/${PR_NUMBER}" --jq .merged',
        'gh api "repos/${REPO}/commits/${SHA}/pulls" --jq ".[0].number"',
        'gh api --paginate "repos/${REPO}/pulls/${PR_NUMBER}/files"',
        'gh api -X POST "repos/${REPO}/issues/${N}/comments" -f body=hi',
    ],
    ids=["read-one", "read-for-commit", "read-files", "post-elsewhere"],
)
def test_gh_api_calls_that_do_not_open_a_pull_request_are_not_swept_up(script: str) -> None:
    """A false positive would demand a PAT of a lane that only reads."""
    document = _load("jobs:\n  j:\n    steps:\n      - run: PLACEHOLDER\n")
    document["jobs"]["j"]["steps"][0]["run"] = script
    assert _openers_in_document("synthetic.yml", document) == []


def test_composite_action_pull_request_step_is_discovered() -> None:
    """`runs.steps`, not `jobs.<id>.steps` - a workflow-only sweep misses it."""
    document = _load(
        """
name: open-pr
runs:
  using: composite
  steps:
    - name: Open PR
      shell: bash
      env:
        GH_TOKEN: ${{ github.token }}
      run: gh pr create --fill
"""
    )
    openers = _openers_in_document("open-pr/action.yml", document)
    assert openers, "a composite action step can open a pull request too"
    assert openers[0][1] == "runs"
    with pytest.raises(AssertionError):
        test_pull_request_is_opened_with_a_triggering_token(*openers[0])


def test_the_sweep_reads_the_composite_action_directory() -> None:
    """Anti-vacuity: the action sweep must actually resolve files."""
    assert _action_files(), "no composite action discovered; the action sweep is vacuous"


def test_discovery_matches_exactly_the_known_pull_request_lanes() -> None:
    """Exact set, so a widened matcher cannot quietly add a false positive."""
    lanes = {name for name, _c, _s, _t in _pr_opening_steps()}
    assert lanes == {
        "adapter-conformance-canary.yml",
        "auto-heal.yml",
        "bernstein-ci-fix.yml",
        "bernstein-issues-decompose.yml",
        "ci-topology-heal.yml",
        "coverage-ratchet-weekly.yml",
        "coverage-ratchet.yml",
        "docs-observability-snapshot.yml",
        "nightly-drift-sweep.yml",
        "review-bot-sweep.yml",
    }


# ---------------------------------------------------------------------------
# The branch push, which the pull-request token alone does not cover
# ---------------------------------------------------------------------------


def _checkout_credential(step: dict[str, Any]) -> tuple[str | None, str]:
    """Return the credential ``actions/checkout`` persists, and how it got it."""
    with_block = step.get("with")
    with_block = with_block if isinstance(with_block, dict) else {}
    if with_block.get("persist-credentials") is False:
        return None, "checkout ran with persist-credentials: false and the push sets no extraheader"
    raw_token = with_block.get("token")
    if raw_token is None:
        return "${{ github.token }}", "checkout persisted its default credential"
    return str(raw_token), "checkout persisted the token it was given"


def _push_steps_in_document(name: str, document: Any) -> list[tuple[str, str, str, str | None, str]]:
    """Resolve the credential every branch push in a PR-opening container uses."""
    if not isinstance(document, dict):
        return []
    found: list[tuple[str, str, str, str | None, str]] = []
    for container_name, container, steps in _step_containers(document):
        commands = [
            _strip_comments(step["run"])
            for step in steps
            if isinstance(step, dict) and isinstance(step.get("run"), str)
        ]
        if not any(_CREATE_PR_COMMAND.search(body) or _opens_pull_request_over_the_api(body) for body in commands):
            continue
        checkout: tuple[str | None, str] = (None, "the container runs no actions/checkout")
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            if _CHECKOUT_ACTION in str(step.get("uses", "")):
                checkout = _checkout_credential(step)
            script = step.get("run")
            if not isinstance(script, str):
                continue
            body = _strip_comments(script)
            if not _GIT_PUSH.search(body):
                continue
            label = _step_label(step, index)
            explicit = _EXTRAHEADER_TOKEN_VAR.search(body)
            if explicit is not None:
                variable = explicit.group(1)
                env = _as_env(step.get("env")) or {}
                resolved = (
                    env.get(variable)
                    or _as_env(container.get("env")).get(variable)
                    or _as_env(document.get("env")).get(variable)
                )
                found.append(
                    (name, container_name, label, resolved, f"push sets an extraheader from ${variable}"),
                )
                continue
            credential, source = checkout
            found.append((name, container_name, label, credential, source))
    return found


def _branch_push_steps() -> list[tuple[str, str, str, str | None, str]]:
    found: list[tuple[str, str, str, str | None, str]] = []
    for path in _workflow_files() + _action_files():
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        found.extend(_push_steps_in_document(_sweep_name(path), document))
    return found


def test_branch_push_discovery_covers_every_gh_pr_create_lane() -> None:
    """Anti-vacuity: every porcelain lane pushes a branch, so all must appear."""
    porcelain = {
        name
        for name, _c, _s, _t in _pr_opening_steps()
        if name
        in {
            "adapter-conformance-canary.yml",
            "auto-heal.yml",
            "bernstein-ci-fix.yml",
            "bernstein-issues-decompose.yml",
            "nightly-drift-sweep.yml",
        }
    }
    pushed = {name for name, _c, _s, _cred, _src in _branch_push_steps()}
    assert porcelain, "no gh pr create lane discovered; this guard would pass vacuously"
    assert porcelain <= pushed, f"lanes that open a pull request without a discovered push: {porcelain - pushed}"


@pytest.mark.parametrize(
    ("workflow", "job", "step", "credential", "source"),
    _branch_push_steps(),
    ids=lambda value: str(value),
)
def test_branch_push_uses_a_triggering_token(
    workflow: str,
    job: str,
    step: str,
    credential: str | None,
    source: str,
) -> None:
    assert credential is not None, (
        f"{workflow} job {job!r} step {step!r} pushes the pull request branch with no resolvable "
        f"credential ({source}). A push with no credential fails outright; a push with the Actions "
        "token emits no `pull_request: synchronize`, so the open pull request keeps the checks of "
        "an older commit."
    )
    without_actions_token = _ACTIONS_TOKEN.sub("", credential)
    assert "secrets." in without_actions_token, (
        f"{workflow} job {job!r} step {step!r} pushes the pull request branch with the Actions "
        f"token only ({source}). The pull request is opened with a triggering token but every "
        "later push is invisible, so the rollup reports green against a superseded SHA - worse "
        "than the empty rollup this lane was fixed to avoid."
    )
    actions_token = _ACTIONS_TOKEN.search(credential)
    if actions_token is not None:
        assert "secrets." in credential[: actions_token.start()], (
            f"{workflow} job {job!r} step {step!r} lists the Actions token before the triggering "
            "token when pushing the branch. The Actions token may only be the fallback."
        )
