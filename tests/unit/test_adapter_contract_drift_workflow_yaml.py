"""Trigger, header and npm install checks for the adapter contract drift workflow.

Covers ``.github/workflows/adapter-contract-drift.yml``.

The per-PR trigger was dropped on purpose. The matrix is 16 jobs per fire
against the same runner pool that pull-request verdicts queue behind, so
pre-merge validation moved to the merge queue's ephemeral branch and the
daily cron kept surfacing upstream CLI releases. Two things can undo that
quietly:

* someone adds ``pull_request`` back to make one branch validate sooner,
  and the CI bill returns without the decision being revisited;
* the trigger list changes and the comment block above ``on:`` does not,
  leaving the file's own description of when it runs wrong - which is how
  the header came to advertise a PR trigger that had already been removed.

So both halves are pinned here: the exact set of triggers, and the claim
the header makes about them.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "adapter-contract-drift.yml"
CLI_DIR = REPO_ROOT / ".github" / "adapter-clis"

#: Every trigger this workflow may declare, and nothing else.
#:
#: ``merge_group`` is the pre-merge lane, ``schedule`` catches upstream CLI
#: releases independent of any merge, and ``workflow_dispatch`` covers an
#: operator re-check. ``pull_request`` is absent by decision, not omission.
EXPECTED_TRIGGERS = frozenset({"schedule", "workflow_dispatch"})

#: A header bullet, e.g. ``#   * Manual dispatch for operator-driven re-runs.``
BULLET = re.compile(r"^#\s+\*\s+(?P<text>.*)$")

#: A claim that this workflow runs per pull request.
#:
#: Matched against the normalised bullet text, so ``pull_request``,
#: ``pull-request`` and ``pull request`` all reach this as one spelling.
PR_CLAIM = re.compile(r"\bpull requests?\b|\bprs?\b")


def _doc() -> dict[str, Any]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{WORKFLOW.name} is not a mapping"
    return cast("dict[str, Any]", data)


def _triggers() -> dict[str, Any]:
    doc = _doc()
    # PyYAML 1.1 parses a bare ``on:`` key as the boolean True.
    triggers = doc.get(True, doc.get("on"))
    assert isinstance(triggers, dict), "workflow must declare a mapping of triggers"
    return cast("dict[str, Any]", triggers)


def _header() -> list[str]:
    """The comment lines above the ``on:`` key."""
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    header = []
    for line in lines:
        if line.startswith("on:"):
            break
        header.append(line)
    else:  # pragma: no cover - a workflow with no triggers fails earlier
        raise AssertionError(f"{WORKFLOW.name} has no top-level `on:` key")
    return header


def _trigger_bullets() -> list[str]:
    """The bullets under the header's ``Triggers:`` heading.

    Scoped to that section on purpose: the prose above it discusses the
    aggregator's issue handling and the absence of a batched auto-PR, so a
    search for "PR" across the whole header would answer about the wrong
    sentence. Wrapped continuation lines are folded into the bullet they
    belong to.
    """
    lines = _header()
    start = next((i for i, line in enumerate(lines) if line.strip() == "# Triggers:"), None)
    assert start is not None, "the header must document when this workflow runs, under `# Triggers:`"

    bullets: list[str] = []
    for line in lines[start + 1 :]:
        match = BULLET.match(line)
        if match is not None:
            bullets.append(match.group("text").strip())
            continue
        if not line.startswith("#"):
            break
        continuation = line.lstrip("#").strip()
        if bullets and continuation:
            bullets[-1] = f"{bullets[-1]} {continuation}"
    assert bullets, "the `Triggers:` section must list the triggers"
    return bullets


def _normalise(text: str) -> str:
    """Lowercase, with ``_`` and ``-`` read as spaces.

    ``merge_group``, ``merge-queue`` and ``merge group`` are the same claim
    written three ways; the assertions below should not care which one the
    header happens to use.
    """
    return re.sub(r"[_\-]+", " ", text).lower()


def test_workflow_file_exists() -> None:
    assert WORKFLOW.is_file()


def _install_script() -> str:
    steps = _doc()["jobs"]["check"]["steps"]
    return next(step["run"] for step in steps if step.get("name") == "Install upstream CLI")


def test_npm_branch_installs_from_the_committed_lockfile() -> None:
    npm_branch = _install_script().split("npm)", 1)[1].split(";;", 1)[0]
    assert "npm ci --prefix .github/adapter-clis --no-audit --no-fund" in npm_branch
    assert '--script-shell="$GITHUB_WORKSPACE/.github/adapter-clis/lifecycle-shell.sh"' in npm_branch
    assert not re.search(r"\bnpm\s+(?:install|i|add)\b|@latest|--ignore-scripts", npm_branch)
    assert (CLI_DIR / "package-lock.json").is_file()


def test_npm_matrix_contracts_match_the_locked_packages() -> None:
    packages = json.loads((CLI_DIR / "package-lock.json").read_text(encoding="utf-8"))["packages"]
    dependencies = packages[""]["dependencies"]
    assert json.loads((CLI_DIR / "package.json").read_text(encoding="utf-8"))["dependencies"] == dependencies

    contracted = set()
    for adapter in _doc()["jobs"]["check"]["strategy"]["matrix"]["adapter"]:
        path = REPO_ROOT / "tests" / "contract" / "contracts" / f"{adapter}.yaml"
        contract = yaml.safe_load(path.read_text(encoding="utf-8"))
        install = contract["install"]
        if install["method"] != "npm":
            continue
        package, _, version = install["spec"].rpartition("@")
        assert package in dependencies, f"{adapter}: {install['spec']} is missing from the CLI lock"
        locked = packages[f"node_modules/{package}"]
        assert version == dependencies[package] == locked["version"], adapter
        assert contract["binary"] in locked["bin"], adapter
        contracted.add(package)
    assert contracted == set(dependencies)


@pytest.mark.skipif(os.name != "posix", reason="The adapter contract job runs on Linux")
def test_npm_lifecycle_shell_only_runs_hooks_in_the_selected_tree(tmp_path: Path) -> None:
    cli = tmp_path / "cli"
    dependency = tmp_path / "optional dependency"
    unrelated = tmp_path / "cli-other"
    env = {**os.environ, "ADAPTER_LIFECYCLE_PATHS": f"{cli}\n{dependency}"}
    for directory, expected_rc in [(cli, 23), (dependency, 23), (unrelated, 0), (tmp_path, 0)]:
        directory.mkdir(exist_ok=True)
        result = subprocess.run([CLI_DIR / "lifecycle-shell.sh", "-c", "exit 23"], cwd=directory, env=env, check=False)
        assert result.returncode == expected_rc, directory.name


@pytest.mark.skipif(
    os.name != "posix" or not all(shutil.which(tool) for tool in ("bash", "npm", "jq")),
    reason="The Linux npm workflow step needs bash, npm and jq",
)
@pytest.mark.parametrize("mode", [0o755, 0o644, None], ids=["executable", "not-executable", "missing"])
def test_npm_step_selects_hooks_and_exports_the_local_binary(tmp_path: Path, mode: int | None) -> None:
    cli_dir = tmp_path / ".github" / "adapter-clis"
    bin_dir = cli_dir / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    # sh is also available on the ambient PATH, including when the local binary is missing.
    binary = bin_dir / "sh"
    if mode is not None:
        binary.touch()
        binary.chmod(mode)
    manifest = {"name": "fixture", "dependencies": {"@example/cli": "1.0.0", "sibling": "1.0.0"}}
    packages = {
        "": manifest,
        "node_modules/@example/cli": {"version": "1.0.0", "dependencies": {"helper": "1.0.0"}},
        "node_modules/helper": {"version": "1.0.0", "dependencies": {"leaf": "1.0.0"}},
        "node_modules/leaf": {"version": "1.0.0"},
        "node_modules/sibling": {"version": "1.0.0"},
    }
    (cli_dir / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
    (cli_dir / "package-lock.json").write_text(
        json.dumps({"lockfileVersion": 3, "packages": packages}), encoding="utf-8"
    )
    github_path = tmp_path / "github-path"
    github_path.touch()
    env = {
        **os.environ,
        "METHOD": "npm",
        "SPEC": "@example/cli@1.0.0",
        "BINARY": "sh",
        "GITHUB_WORKSPACE": str(tmp_path),
        "GITHUB_PATH": str(github_path),
        "npm_config_cache": str(tmp_path / "npm-cache"),
    }
    # Run the real lock-only query. Stand in for the install and record the hook scope it receives.
    stub = """
npm() {
  case "$1" in
    query) command npm --offline "$@" ;;
    ci) printf '%s\\n' "$ADAPTER_LIFECYCLE_PATHS" > hooks ;;
    *) return 99 ;;
  esac
}
"""
    result = subprocess.run(["bash", "-c", stub + _install_script()], cwd=tmp_path, env=env, capture_output=True)
    assert result.returncode == (0 if mode == 0o755 else 1), result.stderr
    assert set((tmp_path / "hooks").read_text(encoding="utf-8").splitlines()) == {
        str(cli_dir / path) for path in ("node_modules/@example/cli", "node_modules/helper", "node_modules/leaf")
    }
    assert github_path.read_text(encoding="utf-8").splitlines() == ([str(bin_dir)] if mode == 0o755 else [])


def test_the_trigger_set_is_exactly_the_one_that_was_decided() -> None:
    """Adding ``pull_request`` back is the regression this locks out."""
    declared = frozenset(_triggers())
    assert declared == EXPECTED_TRIGGERS, (
        f"triggers are {sorted(declared)}, expected {sorted(EXPECTED_TRIGGERS)}. "
        "Both per-change triggers were dropped to cut CI load - the matrix is "
        "16 jobs per fire against the pool that PR verdicts queue behind, and "
        "only `CI gate` is required in the merge queue, so this lane never "
        "gated a queue merge while competing with ci.yml on every entry and "
        "reshuffle. Upstream drift is a function of upstream releases, not of "
        "what a change touches. If either needs to come back, say so here and "
        "in the header comment rather than letting the file and its "
        "description disagree"
    )


def test_no_pull_request_trigger() -> None:
    """The exact-set assertion states this; this one names it."""
    assert "pull_request" not in _triggers(), (
        "pre-merge validation runs on the merge queue's ephemeral branch, not on every pull-request push"
    )


def test_the_header_does_not_advertise_a_trigger_that_was_removed() -> None:
    """The header outlived the trigger once already."""
    offenders = [bullet for bullet in _trigger_bullets() if PR_CLAIM.search(_normalise(bullet))]
    assert not offenders, (
        f"the header still describes a pull-request trigger: {offenders}. "
        "This workflow does not run per pull request; a comment that says "
        "otherwise is what sends someone looking for a check that never fires"
    )


def test_the_header_describes_the_merge_queue_trigger() -> None:
    """A trigger nobody documented is a trigger nobody expects to pay for."""
    bullets = _trigger_bullets()
    assert any("merge group" in _normalise(bullet) or "merge queue" in _normalise(bullet) for bullet in bullets), (
        f"no header bullet mentions the merge-queue trigger: {bullets}. "
        "`merge_group` has no paths filter, so the full 16-job matrix runs "
        "pre-merge - the header is where that is stated"
    )


@pytest.mark.parametrize(
    "bullet",
    [
        pytest.param("On PRs touching adapters or contracts.", id="prs-abbreviated"),
        pytest.param("On pull requests touching adapters.", id="pull-requests-spelled"),
        pytest.param("On pull_request for immediate validation.", id="event-name"),
        pytest.param("On every PR push.", id="pr-singular"),
    ],
)
def test_a_reintroduced_pr_claim_is_caught_however_it_is_spelled(bullet: str) -> None:
    """The check is on the claim, not on one wording of it."""
    assert PR_CLAIM.search(_normalise(bullet)), f"{bullet!r} claims a PR trigger and must be rejected"


@pytest.mark.parametrize(
    "bullet",
    [
        pytest.param("On merge group for pre-merge adapter contract validation.", id="merge-group"),
        pytest.param("On merge_group, the pre-merge lane.", id="event-name"),
        pytest.param("On the merge-queue branch before a merge.", id="merge-queue"),
    ],
)
def test_the_merge_queue_trigger_is_recognised_however_it_is_spelled(bullet: str) -> None:
    """Normalisation is what lets one assertion accept all three spellings."""
    normalised = _normalise(bullet)
    assert "merge group" in normalised or "merge queue" in normalised
    assert not PR_CLAIM.search(normalised), "a merge-queue bullet must not read as a PR claim"


VPS_NODE_WORKFLOWS = (
    ".github/workflows/adapter-contract-drift.yml",
    ".github/workflows/adapter-conformance-canary.yml",
)


@pytest.mark.parametrize("rel_path", VPS_NODE_WORKFLOWS)
def test_self_hosted_vps_workflows_pin_node_24(rel_path: str) -> None:
    """Self-hosted VPS runner container fails Node 26 with exit 127; keep on Node 24."""
    doc = yaml.safe_load((REPO_ROOT / rel_path).read_text(encoding="utf-8"))
    setup_node_versions = [
        str(step.get("with", {}).get("node-version", "")).strip()
        for job in doc["jobs"].values()
        if isinstance(job, dict)
        for step in job.get("steps", [])
        if isinstance(step, dict) and str(step.get("uses", "")).startswith("actions/setup-node@")
    ]
    assert setup_node_versions == ["24"], f"{rel_path}: expected Node 24 pin on VPS runner, got {setup_node_versions}"


def test_renovate_caps_node_only_in_self_hosted_vps_workflows() -> None:
    """Renovate must hold Node below 25 strictly for the two self-hosted VPS workflows."""
    config = json.loads((REPO_ROOT / "renovate.json").read_text(encoding="utf-8"))
    rules = config.get("packageRules", [])
    matching = [
        rule
        for rule in rules
        if set(rule.get("matchFileNames", ())) == set(VPS_NODE_WORKFLOWS)
        and "github-actions" in rule.get("matchManagers", ())
        and {"node", "actions/node-versions"} <= set(rule.get("matchPackageNames", ()))
    ]
    assert len(matching) == 1, f"expected one VPS Node cap packageRule in renovate.json, found {len(matching)}"
    assert matching[0].get("allowedVersions") == "<25"
