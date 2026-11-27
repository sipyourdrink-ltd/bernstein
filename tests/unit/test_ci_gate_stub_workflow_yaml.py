"""Structural and behavioural assertions on the `CI gate` stub.

``.github/workflows/ci-gate-stub.yml`` publishes a synthetic ``CI gate``
check so that pull requests whose diff is entirely contained in
``ci.yml``'s ``paths-ignore`` list are not blocked forever by a context
that will never run. That is the only case it may serve.

The failure this module exists to prevent
-----------------------------------------
``paths``/``paths-ignore`` filters are evaluated per file with OR
semantics, so on a *mixed* diff (some files ignored, some not) both
``ci.yml`` and the stub fire. Both published ``CI gate``; the synthetic
one completed in seconds while the real matrix was still queued, and
branch protection accepted it. A pull request merged to ``main`` that
way with no test run against its code (``CHANGELOG.md`` ignored,
``src/bernstein/core/planning/plan_loader.py`` and its test not).

No ``on:`` filter can express "every changed file is ignored", so the
stub decides in-job via ``scripts/ci_gate_stub_guard.py`` and takes its
check-run name from the verdict. Under any other verdict the check is
published under a name branch protection does not require.

Invariants exercised here:

1. The stub's emitting job resolves its ``name:`` from the guard verdict,
   and the ``CI gate`` branch of that expression is the one gated on
   "every changed path is ignored".
2. The emitting job is not gated with a verdict-dependent ``if:``.
   GitHub counts a *skipped* required check as passing, so an ``if:``
   skip would leave the hole open.
3. The guard reads its pattern list from ``ci.yml`` rather than a copy.
4. The guard returns "not all ignored" for every diff that contains a
   non-ignored path, including the exact diff that merged untested.
5. The guard still returns "all ignored" for the fully-ignored diffs the
   stub exists to unblock, so those pull requests keep merging.
"""

from __future__ import annotations

import importlib.util
import json
import re
import runpy
import subprocess
from pathlib import Path

import pytest

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - dev env should have pyyaml
    pytest.skip("pyyaml not installed", allow_module_level=True)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO_ROOT / "scripts"
_GUARD_SCRIPT = _SCRIPTS / "ci_gate_stub_guard.py"

_SPEC = importlib.util.spec_from_file_location("ci_gate_stub_guard", _GUARD_SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)

all_paths_ignored = _MOD.all_paths_ignored
is_ignored = _MOD.is_ignored
load_paths_ignore = _MOD.load_paths_ignore

CI = _REPO_ROOT / ".github/workflows/ci.yml"
STUB = _REPO_ROOT / ".github/workflows/ci-gate-stub.yml"

REQUIRED_CONTEXT = "CI gate"
EMITTER_JOB_KEY = "ci-gate"

# `${{ needs.<job>.outputs.all_ignored == 'true' && 'CI gate' || '<other>' }}`
_NAME_EXPR = re.compile(
    r"^\$\{\{\s*"
    r"needs\.(?P<job>[A-Za-z0-9_-]+)\.outputs\.all_ignored\s*==\s*'true'"
    r"\s*&&\s*'(?P<when_ignored>[^']*)'"
    r"\s*\|\|\s*'(?P<otherwise>[^']*)'"
    r"\s*\}\}$"
)

# Paths ci.yml's `paths-ignore` covers, so the real gate never reports.
IGNORED_SAMPLES = (
    "CHANGELOG.md",
    "docs/guide/getting-started.md",
    "sdk/typescript/package-lock.json",
    "packages/vscode/package.json",
    "packaging/homebrew.rb",
    "deploy/helm/values.yaml",
    "docker-compose.yaml",
    "Dockerfile",
    "examples/demo/main.py",
    ".github/CODEOWNERS",
)

# Paths ci.yml does NOT ignore, so the real gate does report. Includes the
# un-ignore negations, which a naive matcher gets backwards.
NON_IGNORED_SAMPLES = (
    "src/bernstein/core/planning/plan_loader.py",
    "tests/unit/test_plan_loader.py",
    "pyproject.toml",
    "uv.lock",
    ".github/workflows/ci.yml",
    "scripts/ci_gate_stub_guard.py",
    "README.md",
    "docs/operations/ci-topology.md",
    "docs/observability/tracing.md",
    "docs/skills/authoring.md",
    "packaging/acp-registry/bernstein/agent.json",
    "packaging/acp-registry/bernstein/icon.svg",
)

# The diff that merged untested: one ignored path, two that are not.
PR_3016_DIFF = (
    "CHANGELOG.md",
    "src/bernstein/core/planning/plan_loader.py",
    "tests/unit/test_plan_loader.py",
)


@pytest.fixture(scope="module")
def stub_doc() -> dict[str, object]:
    return yaml.safe_load(STUB.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def patterns() -> list[str]:
    return load_paths_ignore(CI)


# ---------------------------------------------------------------------------
# Workflow contract: the stub cannot name its check `CI gate` unconditionally
# ---------------------------------------------------------------------------


def test_emitter_job_name_is_gated_on_the_guard_verdict(stub_doc: dict[str, object]) -> None:
    """The `CI gate` name must hang off "every changed path is ignored".

    A literal ``name: CI gate`` here means the stub publishes the required
    context for whatever diff triggered it, mixed diffs included.
    """
    jobs = stub_doc.get("jobs")
    assert isinstance(jobs, dict)
    job = jobs.get(EMITTER_JOB_KEY)
    assert isinstance(job, dict), f"ci-gate-stub.yml must keep a `{EMITTER_JOB_KEY}` job."
    name = job.get("name")
    assert isinstance(name, str)

    assert name != REQUIRED_CONTEXT, (
        f"ci-gate-stub.yml::{EMITTER_JOB_KEY}.name is the bare literal {REQUIRED_CONTEXT!r}. "
        "The stub then satisfies branch protection for any diff that triggers it, "
        "including a mixed diff whose real CI run has not finished. Resolve the name "
        "from the guard verdict instead."
    )

    match = _NAME_EXPR.match(name)
    assert match is not None, (
        f"ci-gate-stub.yml::{EMITTER_JOB_KEY}.name must be a verdict-gated expression of the form\n"
        "  ${{ needs.<classify>.outputs.all_ignored == 'true' && 'CI gate' || '<other>' }}\n"
        f"found: {name!r}"
    )
    assert match.group("when_ignored") == REQUIRED_CONTEXT, (
        f"The `all_ignored == 'true'` branch must be {REQUIRED_CONTEXT!r}, found {match.group('when_ignored')!r}."
    )
    assert match.group("otherwise") != REQUIRED_CONTEXT, (
        "The fallback branch must NOT be the required context, otherwise the expression publishes `CI gate` either way."
    )


def test_emitter_job_depends_on_a_real_classify_job(stub_doc: dict[str, object]) -> None:
    """The job named in the verdict expression must exist and run the guard."""
    jobs = stub_doc.get("jobs")
    assert isinstance(jobs, dict)
    job = jobs[EMITTER_JOB_KEY]
    assert isinstance(job, dict)
    name = job.get("name")
    assert isinstance(name, str)
    match = _NAME_EXPR.match(name)
    assert match is not None
    classify_key = match.group("job")

    needs = job.get("needs")
    needs_list = [needs] if isinstance(needs, str) else list(needs or [])
    assert classify_key in needs_list, (
        f"`{EMITTER_JOB_KEY}` reads `needs.{classify_key}.outputs.all_ignored` "
        f"but does not declare `needs: {classify_key}`."
    )

    classify = jobs.get(classify_key)
    assert isinstance(classify, dict), f"ci-gate-stub.yml must define the `{classify_key}` job."
    outputs = classify.get("outputs")
    assert isinstance(outputs, dict)
    assert "all_ignored" in outputs, f"`{classify_key}` must expose an `all_ignored` output."

    steps = classify.get("steps")
    assert isinstance(steps, list)
    runs = " ".join(str(s.get("run", "")) for s in steps if isinstance(s, dict))
    assert "scripts/ci_gate_stub_guard.py" in runs, (
        f"`{classify_key}` must derive the verdict by running scripts/ci_gate_stub_guard.py, "
        "not by re-implementing the path matching inline."
    )


def test_emitter_job_is_not_gated_by_a_skip(stub_doc: dict[str, object]) -> None:
    """The emitter must run unconditionally.

    GitHub reports a skipped job as a *passing* required check, so gating
    the emitter with ``if: needs.classify.outputs.all_ignored == 'true'``
    would leave the hole open while looking like a fix. It also posts the
    unresolved ``${{ ... }}`` template as the check name when the job is
    skipped, which breaks the fully-ignored case too.
    """
    jobs = stub_doc.get("jobs")
    assert isinstance(jobs, dict)
    job = jobs[EMITTER_JOB_KEY]
    assert isinstance(job, dict)
    guard = str(job.get("if") or "")
    assert "all_ignored" not in guard, (
        f"`{EMITTER_JOB_KEY}.if` gates on the verdict ({guard!r}). A skipped job counts as a "
        "passing required check, so this does not close the hole. Gate the check-run "
        "*name* instead and let the job always run."
    )


def test_guard_script_reads_patterns_from_ci_yml() -> None:
    """The guard must not carry its own copy of the paths-ignore list."""
    assert _GUARD_SCRIPT.exists(), "scripts/ci_gate_stub_guard.py is missing"
    source = _GUARD_SCRIPT.read_text(encoding="utf-8")
    assert ".github/workflows/ci.yml" in source, (
        "The guard must read `on.pull_request.paths-ignore` from ci.yml. A duplicated "
        "list drifts, and a drifted list is how the stub starts vouching for code again."
    )


def test_classifier_uses_event_base_and_isolated_graph(stub_doc: dict) -> None:
    steps = stub_doc["jobs"]["classify"]["steps"]
    policy, graph = [step for step in steps if step.get("uses", "").startswith("actions/checkout@")]
    assert policy["with"]["ref"] == "${{ github.event.pull_request.base.sha }}"
    assert set(policy["with"]["sparse-checkout"].splitlines()) == {
        "/scripts/ci_gate_stub_guard.py",
        "/.github/workflows/ci.yml",
    }
    assert graph["uses"] == policy["uses"]
    assert graph["with"] == {
        "ref": "${{ github.sha }}",
        "path": "stub-event-graph",
        "fetch-depth": 0,
        "sparse-checkout-cone-mode": False,
        "sparse-checkout": "!/*",
        "persist-credentials": False,
    }
    collect = next(step for step in steps if step.get("name") == "Collect changed paths")
    for ref in ("base", "head"):
        assert collect["env"]["EXPECTED_" + ref.upper()] == f"${{{{ github.event.pull_request.{ref}.sha }}}}"
    guard = next(step for step in steps if step.get("id") == "guard")
    assert guard["run"] == 'python3 scripts/ci_gate_stub_guard.py --changed-paths "$RUNNER_TEMP/stub-paths.txt"'
    real = yaml.safe_load(CI.read_text(encoding="utf-8"))
    assert set(stub_doc[True]["pull_request"]["types"]) == set(real[True]["pull_request"]["types"])
    assert "merge_group" not in stub_doc[True]


def _git(graph: Path, *args: str, data: bytes | None = None) -> str:
    result = subprocess.run(["git", "-C", str(graph), *args], input=data, capture_output=True, check=True)
    return result.stdout.decode("ascii").strip()


@pytest.fixture
def graph(tmp_path: Path) -> Path:
    directory = tmp_path / "stub-event-graph"
    directory.mkdir()
    _git(directory, "init", "-q")
    _git(directory, "config", "user.name", "CI fixture")
    _git(directory, "config", "user.email", "fixture@example.test")
    return directory


def _commit(graph: Path, paths: list[bytes], *parents: str) -> str:
    """Write Git trees directly so the host filesystem need not support the test filenames."""
    blob = _git(graph, "hash-object", "-w", "--stdin", data=b"fixture\n").encode()

    def tree(files: list[bytes]) -> str:
        entries = []
        subtrees: dict[bytes, list[bytes]] = {}
        for path in files:
            name, slash, remainder = path.partition(b"/")
            if slash:
                subtrees.setdefault(name, []).append(remainder)
            else:
                entries.append(b"100644 blob " + blob + b"\t" + name + b"\0")
        for name, children in subtrees.items():
            entries.append(b"040000 tree " + tree(children).encode() + b"\t" + name + b"\0")
        return _git(graph, "mktree", "-z", data=b"".join(entries))

    parent_args = [arg for parent in parents for arg in ("-p", parent)]
    return _git(graph, "commit-tree", tree(paths), *parent_args, "-m", "fixture")


def _collect_paths(
    stub_doc: dict,
    graph: Path,
    base: str,
    head: str,
    *,
    live_head: str | None = None,
    stale_files: list[str] | None = None,
) -> list[str]:
    """Execute the collector with real Git objects and a local substitute for the PR API."""
    step = next(step for step in stub_doc["jobs"]["classify"]["steps"] if step.get("name") == "Collect changed paths")
    program = re.fullmatch(r"python3 - <<'PY'\n(.+?)\nPY\s*", step["run"], re.DOTALL)
    assert program
    workspace = graph.parent
    script = workspace / "collect.py"
    script.write_text(program[1], encoding="utf-8")
    real_check_output = subprocess.check_output
    api_calls = []
    pr = "repos/test/repo/pulls/12"

    def read_request(args: list[str], **kwargs):
        if args[0] == "git":
            return real_check_output(args, **kwargs)
        api_calls.append(args)
        if stale_files is not None and args[0:2] == ["gh", "api"] and any(f"{pr}/files" in arg for arg in args):
            return json.dumps([[{"filename": name, "status": "modified"} for name in stale_files]])
        assert args == ["gh", "api", pr, "--jq", ".head.sha"]
        return (head if live_head is None else live_head) + "\n"

    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(workspace)
        patch.setenv("EXPECTED_BASE", base)
        patch.setenv("EXPECTED_HEAD", head)
        patch.setenv("REPO", "test/repo")
        patch.setenv("PR_NUMBER", "12")
        patch.setenv("RUNNER_TEMP", str(workspace))
        patch.setattr(subprocess, "check_output", read_request)
        runpy.run_path(str(script), run_name="__main__")
    assert api_calls == [["gh", "api", pr, "--jq", ".head.sha"]]
    return _MOD.read_changed_paths(workspace / "stub-paths.txt")


def test_stale_same_count_file_list_cannot_authorize_mixed_diff(
    stub_doc: dict, graph: Path, patterns: list[str]
) -> None:
    base = _commit(graph, [])
    head = _commit(graph, [b"docs/a.md", b"src/b.py"], base)
    paths = _collect_paths(stub_doc, graph, base, head, stale_files=["docs/a.md", "docs/b.md"])
    assert paths == ["docs/a.md", "src/b.py"]
    assert not all_paths_ignored(paths, patterns)


def test_docs_only_pr_with_advanced_base_can_emit(stub_doc: dict, graph: Path, patterns: list[str]) -> None:
    common = _commit(graph, [])
    base = _commit(graph, [b"src/base.py"], common)
    head = _commit(graph, [b"docs/a.md"], common)
    advanced = _commit(graph, [b"src/base.py", b"src/later.py"], base)
    _git(graph, "update-ref", "refs/heads/main", advanced)
    paths = _collect_paths(stub_doc, graph, base, head)
    assert paths == ["docs/a.md"]
    assert all_paths_ignored(paths, patterns)


def test_changed_paths_have_no_api_page_limit(stub_doc: dict, graph: Path) -> None:
    base = _commit(graph, [])
    names = [f"docs/{i:04d}.md".encode() for i in range(3001)] + [b"src/last.py"]
    head = _commit(graph, names, base)
    assert _collect_paths(stub_doc, graph, base, head) == [name.decode() for name in names]


def test_rename_out_of_source_cannot_emit(stub_doc: dict, graph: Path, patterns: list[str]) -> None:
    base = _commit(graph, [b"src/old.py"])
    head = _commit(graph, [b"docs/old.py"], base)
    paths = _collect_paths(stub_doc, graph, base, head)
    assert paths == ["docs/old.py", "src/old.py"]
    assert not all_paths_ignored(paths, patterns)


def test_changed_live_head_has_no_verdict(stub_doc: dict, graph: Path) -> None:
    base = _commit(graph, [])
    head = _commit(graph, [b"docs/a.md"], base)
    with pytest.raises(SystemExit, match="PR head differs"):
        _collect_paths(stub_doc, graph, base, head, live_head=base)
    assert not (graph.parent / "stub-paths.txt").exists()


@pytest.mark.parametrize(
    "path", [b" docs/a.md", b"docs/a.md\r", b"docs/a\nsrc/b.py", "docs/a\u2028b".encode(), b"docs/\xff"]
)
def test_ambiguous_or_undecodable_filename_has_no_verdict(stub_doc: dict, graph: Path, path: bytes) -> None:
    base = _commit(graph, [])
    head = _commit(graph, [path], base)
    with pytest.raises((SystemExit, UnicodeDecodeError)):
        _collect_paths(stub_doc, graph, base, head)
    assert not (graph.parent / "stub-paths.txt").exists()


def test_missing_history_and_empty_diff_have_no_verdict(stub_doc: dict, graph: Path) -> None:
    base = _commit(graph, [])
    with pytest.raises(subprocess.CalledProcessError):
        _collect_paths(stub_doc, graph, base, "f" * 40)
    with pytest.raises(SystemExit, match="Missing or incomplete"):
        _collect_paths(stub_doc, graph, base, base)
    assert not (graph.parent / "stub-paths.txt").exists()


def test_multiple_merge_bases_have_no_verdict(stub_doc: dict, graph: Path) -> None:
    root = _commit(graph, [])
    left = _commit(graph, [b"docs/a.md"], root)
    right = _commit(graph, [b"docs/b.md"], root)
    base = _commit(graph, [b"docs/a.md", b"docs/b.md"], left, right)
    head = _commit(graph, [b"docs/c.md"], right, left)
    with pytest.raises(SystemExit, match="Expected one full commit SHA"):
        _collect_paths(stub_doc, graph, base, head)
    assert not (graph.parent / "stub-paths.txt").exists()


# ---------------------------------------------------------------------------
# Behaviour: no mixed diff may ever be classified as fully ignored
# ---------------------------------------------------------------------------


def test_pr_3016_diff_is_not_all_ignored(patterns: list[str]) -> None:
    """The regression itself: this diff must never earn a synthetic gate."""
    assert all_paths_ignored(PR_3016_DIFF, patterns) is False, (
        "The diff that merged with no test run is classified as fully ignored. "
        "The stub would publish `CI gate` for it again."
    )


@pytest.mark.parametrize("non_ignored", NON_IGNORED_SAMPLES)
@pytest.mark.parametrize("ignored", IGNORED_SAMPLES)
def test_any_non_ignored_path_defeats_the_stub(ignored: str, non_ignored: str, patterns: list[str]) -> None:
    """One non-ignored path is enough to withhold the synthetic gate.

    This is the property the required context depends on: the stub may
    speak only for diffs the real gate will never look at.
    """
    assert all_paths_ignored([ignored, non_ignored], patterns) is False, (
        f"diff {[ignored, non_ignored]} was classified as fully ignored, so the stub "
        f"would publish {REQUIRED_CONTEXT!r} while ci.yml also runs for {non_ignored!r}."
    )


@pytest.mark.parametrize("path", NON_IGNORED_SAMPLES)
def test_non_ignored_samples_are_not_ignored(path: str, patterns: list[str]) -> None:
    """Guards the matcher itself, including the `!` un-ignore entries."""
    assert is_ignored(path, patterns) is False, (
        f"{path!r} is treated as ignored, but ci.yml's paths-ignore does not cover it. "
        "The stub would vouch for a diff the real gate is about to evaluate."
    )


def test_empty_diff_is_not_all_ignored(patterns: list[str]) -> None:
    """An unreadable or empty file list must fail closed."""
    assert all_paths_ignored([], patterns) is False


def test_acp_registry_paths_not_ignored_by_ci_gate_stub(patterns: list[str]) -> None:
    """ACP registry asset edits must trigger real CI (`acp-conformance`), not the stub."""
    registry_paths = [
        "packaging/acp-registry/bernstein/agent.json",
        "packaging/acp-registry/bernstein/icon.svg",
    ]
    for path in registry_paths:
        assert is_ignored(path, patterns) is False, f"{path!r} must not be ignored by ci.yml"
    assert all_paths_ignored(registry_paths, patterns) is False

    ci_doc = yaml.safe_load(CI.read_text(encoding="utf-8"))
    ci_on = ci_doc.get(True, ci_doc.get("on"))
    assert isinstance(ci_on, dict)
    for event_name in ("push", "pull_request"):
        event = ci_on[event_name]
        assert "!packaging/acp-registry/**" in event["paths-ignore"], (
            f"ci.yml {event_name}.paths-ignore must unignore packaging/acp-registry/**"
        )


# ---------------------------------------------------------------------------
# Behaviour: the reason the stub exists is preserved
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", IGNORED_SAMPLES)
def test_ignored_samples_are_ignored(path: str, patterns: list[str]) -> None:
    assert is_ignored(path, patterns) is True, (
        f"{path!r} is not recognised as paths-ignored, so a PR touching only this file "
        "would get no `CI gate` from either emitter and sit BLOCKED forever."
    )


@pytest.mark.parametrize(
    "diff",
    [
        pytest.param(("sdk/typescript/package-lock.json",), id="renovate-sdk-lockfile"),
        pytest.param(("packages/vscode/package.json", "packages/vscode/CHANGELOG.md"), id="vscode-package"),
        pytest.param(("docs/guide/a.md", "docs/guide/b.md"), id="docs-only"),
        pytest.param(("deploy/helm/values.yaml", "docker-compose.yaml"), id="infra-only"),
    ],
)
def test_fully_ignored_diffs_still_get_the_gate(diff: tuple[str, ...], patterns: list[str]) -> None:
    """ci.yml never runs for these, so the stub must still unblock them."""
    assert all_paths_ignored(diff, patterns) is True, (
        f"diff {list(diff)} is fully paths-ignored by ci.yml, so the real `CI gate` will "
        "never report. Withholding the synthetic one leaves the PR blocked forever, which "
        "is the regression the stub was added to fix."
    )
