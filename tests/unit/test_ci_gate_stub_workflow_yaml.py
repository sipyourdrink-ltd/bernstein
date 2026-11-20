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


def test_classifier_uses_event_base_and_collects_around_pagination(stub_doc: dict) -> None:
    steps = stub_doc["jobs"]["classify"]["steps"]
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["ref"] == "${{ github.event.pull_request.base.sha }}"
    collect = next(step for step in steps if step.get("name") == "Collect changed paths")
    for ref in ("base", "head"):
        assert collect["env"]["EXPECTED_" + ref.upper()] == f"${{{{ github.event.pull_request.{ref}.sha }}}}"
    assert [line.strip() for line in collect["run"].splitlines() if line.strip().startswith("gh api ")] == [
        'gh api "$pr" > stub-before.json',
        'gh api --paginate --slurp "$pr/files?per_page=100" > stub-pages.json',
        'gh api "$pr" > stub-after.json',
    ]
    assert 'cd "$RUNNER_TEMP"' in collect["run"]
    guard = next(step for step in steps if step.get("id") == "guard")
    assert guard["run"] == 'python3 scripts/ci_gate_stub_guard.py --changed-paths "$RUNNER_TEMP/stub-paths.txt"'
    real = yaml.safe_load(CI.read_text(encoding="utf-8"))
    assert set(stub_doc[True]["pull_request"]["types"]) == set(real[True]["pull_request"]["types"])
    assert "merge_group" not in stub_doc[True]


def _collect_paths(
    stub_doc: dict,
    tmp_path: Path,
    pages: object,
    count: int,
    *,
    before_fields: dict | None = None,
    after_fields: dict | None = None,
    merge_bases: dict[str, str] | None = None,
) -> tuple[int, list[str]]:
    base, head = "a" * 40, "b" * 40
    snapshot = {"base": {"sha": base}, "head": {"sha": head}, "changed_files": count}
    for name, data in (
        ("stub-before.json", snapshot | (before_fields or {})),
        ("stub-after.json", snapshot | (after_fields or {})),
        ("stub-pages.json", pages),
    ):
        (tmp_path / name).write_text(json.dumps(data), encoding="utf-8")
    step = next(step for step in stub_doc["jobs"]["classify"]["steps"] if step.get("name") == "Collect changed paths")
    program = re.search(r"^python3 - <<'PY'\n(.+?)^PY$", step["run"], re.MULTILINE | re.DOTALL)
    assert program
    script = tmp_path / "collect.py"
    script.write_text(program[1], encoding="utf-8")
    endpoints = {
        f"repos/test/repo/compare/{ref}...{head}?per_page=1": value for ref, value in (merge_bases or {}).items()
    }

    def compare(args: list[str], *, text: bool) -> str:
        assert text and args == ["gh", "api", args[2], "--jq", ".merge_base_commit.sha"]
        assert args[2] in endpoints, f"Unexpected or repeated compare request {args[2]}"
        return endpoints.pop(args[2]) + "\n"

    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path)
        patch.setenv("EXPECTED_BASE", base)
        patch.setenv("EXPECTED_HEAD", head)
        patch.setenv("REPO", "test/repo")
        patch.setattr(subprocess, "check_output", compare)
        try:
            runpy.run_path(str(script), run_name="__main__")
        except SystemExit as error:
            assert error.code
            assert not (tmp_path / "stub-paths.txt").exists()
            return 1, []
    assert not endpoints
    return 0, _MOD.read_changed_paths(tmp_path / "stub-paths.txt")


def test_complete_docs_only_pages_can_emit(stub_doc: dict, tmp_path: Path, patterns: list[str]) -> None:
    files = [{"filename": f"docs/file-{n}.md", "status": "modified"} for n in range(101)]
    status, paths = _collect_paths(stub_doc, tmp_path, [files[:100], files[100:]], 101)
    assert status == 0
    assert paths == [entry["filename"] for entry in files]
    assert all_paths_ignored(paths, patterns)


def test_rename_out_of_source_cannot_emit(stub_doc: dict, tmp_path: Path, patterns: list[str]) -> None:
    change = {"filename": "docs/old.py", "previous_filename": "src/old.py", "status": "renamed"}
    status, paths = _collect_paths(stub_doc, tmp_path, [[change]], 1)
    assert status == 0
    assert paths == ["docs/old.py", "src/old.py"]
    assert not all_paths_ignored(paths, patterns)


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("fields", [{"head": {"sha": "c" * 40}}, {"base": {"sha": "d" * 40}}, {"changed_files": 2}])
def test_changed_snapshot_has_no_verdict(stub_doc: dict, tmp_path: Path, when: str, fields: dict) -> None:
    files = [[{"filename": "docs/a.md", "status": "modified"}]]
    status, paths = _collect_paths(stub_doc, tmp_path, files, 1, **{f"{when}_fields": fields})
    assert status != 0
    assert not paths


@pytest.mark.parametrize(
    ("event_fork", "observed_fork", "valid"),
    [("c" * 40, "c" * 40, True), ("d" * 40, "c" * 40, False), ("", "", False), ("z" * 40, "z" * 40, False)],
    ids=["same-three-dot-diff", "changed-diff", "missing-fork", "invalid-sha"],
)
def test_stable_advanced_base_needs_equivalent_diff(
    stub_doc: dict, tmp_path: Path, patterns: list[str], event_fork: str, observed_fork: str, valid: bool
) -> None:
    advanced = "e" * 40
    snapshot = {"base": {"sha": advanced}}
    files = [[{"filename": "docs/a.md", "status": "modified"}]]
    status, paths = _collect_paths(
        stub_doc,
        tmp_path,
        files,
        1,
        before_fields=snapshot,
        after_fields=snapshot,
        merge_bases={"a" * 40: event_fork, advanced: observed_fork},
    )
    assert (status == 0) is valid
    assert all_paths_ignored(paths, patterns) is valid


@pytest.mark.parametrize(
    ("pages", "count"),
    [
        ([], 0),
        ([[{"filename": "docs/a.md", "status": "added"}]], 3001),
        ([[{"filename": "docs/a.md", "status": "added"}]] * 2, 2),
        ([{"filename": "docs/a.md", "status": "added"}], 1),
        ([[{"filename": "docs/a.md", "status": "renamed"}]], 1),
        ([[{"filename": "docs/a.md", "status": "unknown"}]], 1),
        ([[{"filename": "docs/a.md", "previous_filename": "docs/a.md", "status": "renamed"}]], 1),
    ],
    ids=["empty", "truncated", "duplicate", "malformed-page", "missing-old-name", "unknown-status", "same-old-name"],
)
def test_incomplete_file_evidence_has_no_verdict(stub_doc: dict, tmp_path: Path, pages: list, count: int) -> None:
    status, paths = _collect_paths(stub_doc, tmp_path, pages, count)
    assert status != 0
    assert not paths


@pytest.mark.parametrize("key", ["filename", "previous_filename"])
@pytest.mark.parametrize("path", [" docs/a.md", "docs/a.md\r", "docs/a\nsrc/b.py", "docs/a\u2028src/b.py", "docs/a\0b"])
def test_ambiguous_filename_has_no_verdict(stub_doc: dict, tmp_path: Path, key: str, path: str) -> None:
    change = {"filename": "docs/b.md", "previous_filename": "docs/a.md", "status": "renamed", key: path}
    status, paths = _collect_paths(stub_doc, tmp_path, [[change]], 1)
    assert status != 0
    assert not paths


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
