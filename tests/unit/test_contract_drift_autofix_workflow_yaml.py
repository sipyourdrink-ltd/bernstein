"""Structural assertions on ``.github/workflows/contract-drift-autofix.yml``.

These tests guard the fork-PR fallback path. The workflow pushes the regen
commit directly to the source PR's head ref via ``git push --force-with-lease``.
That path works for same-repo PRs only; for fork PRs the default GITHUB_TOKEN
is read-only on the head repo, so the push step fails. The workflow handles
that case by detecting the fork up front and routing to the PR-comment path
instead.

If a refactor accidentally removes the fork-detect step or the comment
fallback, drift on fork PRs would silently fail with no operator signal.
Lock the structural shape so that regression is caught at unit-test time.
"""

from __future__ import annotations

import ast
import re
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


def test_comment_fallback_fires_for_forks_or_failed_push(
    autofix_steps: list[dict[str, object]],
) -> None:
    """The comment-fallback step must trigger when is_fork == 'true' OR when
    inline_push failed (any reason)."""
    comment = next((s for s in autofix_steps if s.get("id") == "comment"), None)
    assert comment is not None, (
        "PR-comment fallback step (id: comment) is missing. Without it, fork PRs "
        "and lease-conflict same-repo PRs get no drift signal at all."
    )
    cond = comment.get("if", "")
    assert isinstance(cond, str)
    assert "is_fork == 'true'" in cond, "comment fallback must fire for fork PRs"
    assert "inline_push.outcome == 'failure'" in cond or "inline_push.outputs.pushed != 'true'" in cond, (
        "comment fallback must fire when inline_push failed"
    )


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
