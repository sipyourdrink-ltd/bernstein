"""Tests for bernstein.core.pr_size_governor — PR Size Governor."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from bernstein.core.git_ops import GitResult, PullRequestResult
from bernstein.core.pr_size_governor import (
    MAX_PR_LINES,
    PRChunk,
    SplitPlan,
    SplitResult,
    _build_pr_body,
    _file_to_module,
    _parse_python_imports,
    build_dependency_order,
    count_diff_lines_per_file,
    execute_split,
    plan_split,
    split_pr_if_needed,
)

REPO = Path("/fake/repo")


def _git_ok(stdout: str = "") -> GitResult:
    return GitResult(returncode=0, stdout=stdout, stderr="")


def _git_fail(stderr: str = "error") -> GitResult:
    return GitResult(returncode=1, stdout="", stderr=stderr)


def _pr_ok(url: str = "https://github.com/org/repo/pull/1") -> PullRequestResult:
    return PullRequestResult(success=True, pr_url=url)


def _pr_fail(error: str = "gh error") -> PullRequestResult:
    return PullRequestResult(success=False, pr_url="", error=error)


# ---------------------------------------------------------------------------
# count_diff_lines_per_file
# ---------------------------------------------------------------------------


@patch("bernstein.core.git.pr_size_governor.run_git")
def test_count_diff_lines_empty_output(mock_run: MagicMock) -> None:
    mock_run.return_value = _git_ok(stdout="")
    result = count_diff_lines_per_file(REPO)
    assert result == {}


@patch("bernstein.core.git.pr_size_governor.run_git")
def test_count_diff_lines_git_failure(mock_run: MagicMock) -> None:
    mock_run.return_value = _git_fail()
    result = count_diff_lines_per_file(REPO)
    assert result == {}


@patch("bernstein.core.git.pr_size_governor.run_git")
def test_count_diff_lines_parses_numstat(mock_run: MagicMock) -> None:
    mock_run.return_value = _git_ok(stdout="10\t5\tsrc/foo.py\n3\t1\tsrc/bar.py\n")
    result = count_diff_lines_per_file(REPO, base_ref="main", head_ref="HEAD")
    assert result == {"src/foo.py": 15, "src/bar.py": 4}


@patch("bernstein.core.git.pr_size_governor.run_git")
def test_count_diff_lines_binary_file_counted_as_zero(mock_run: MagicMock) -> None:
    mock_run.return_value = _git_ok(stdout="-\t-\timage.png\n5\t0\tscript.py\n")
    result = count_diff_lines_per_file(REPO)
    assert result["image.png"] == 0
    assert result["script.py"] == 5


@patch("bernstein.core.git.pr_size_governor.run_git")
def test_count_diff_lines_skips_malformed_lines(mock_run: MagicMock) -> None:
    mock_run.return_value = _git_ok(stdout="bad line\n10\t2\tgood.py\n")
    result = count_diff_lines_per_file(REPO)
    assert result == {"good.py": 12}


# ---------------------------------------------------------------------------
# _parse_python_imports
# ---------------------------------------------------------------------------


def test_parse_python_imports_import_statement() -> None:
    src = "import os\nimport sys\n"
    assert _parse_python_imports(src) == {"os", "sys"}


def test_parse_python_imports_from_statement() -> None:
    src = "from pathlib import Path\nfrom collections import OrderedDict\n"
    assert _parse_python_imports(src) == {"pathlib", "collections"}


def test_parse_python_imports_dotted_keeps_top_level() -> None:
    src = "import os.path\nfrom bernstein.core import models\n"
    assert _parse_python_imports(src) == {"os", "bernstein"}


def test_parse_python_imports_empty_source() -> None:
    assert _parse_python_imports("") == set()


def test_parse_python_imports_ignores_comments() -> None:
    src = "# import os\nx = 1\n"
    assert _parse_python_imports(src) == set()


# ---------------------------------------------------------------------------
# _file_to_module
# ---------------------------------------------------------------------------


def test_file_to_module_strips_extension() -> None:
    assert _file_to_module("src/bernstein/core/foo.py") == "foo"


def test_file_to_module_test_file() -> None:
    assert _file_to_module("tests/unit/test_foo.py") == "test_foo"


def test_file_to_module_no_extension() -> None:
    assert _file_to_module("Makefile") == "Makefile"


# ---------------------------------------------------------------------------
# build_dependency_order
# ---------------------------------------------------------------------------


def test_build_dependency_order_empty() -> None:
    assert build_dependency_order([], REPO) == []


def test_build_dependency_order_no_deps(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("import os\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("import sys\n", encoding="utf-8")
    result = build_dependency_order(["a.py", "b.py"], tmp_path)
    assert set(result) == {"a.py", "b.py"}
    assert len(result) == 2


def test_build_dependency_order_simple_dep(tmp_path: Path) -> None:
    # b.py imports a → a must come before b
    (tmp_path / "a.py").write_text("def helper(): pass\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("import a\n\nfoo = a.helper()\n", encoding="utf-8")
    result = build_dependency_order(["b.py", "a.py"], tmp_path)
    assert result.index("a.py") < result.index("b.py")


def test_build_dependency_order_from_import_dep(tmp_path: Path) -> None:
    (tmp_path / "utils.py").write_text("def util(): pass\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("from utils import util\n", encoding="utf-8")
    result = build_dependency_order(["main.py", "utils.py"], tmp_path)
    assert result.index("utils.py") < result.index("main.py")


def test_build_dependency_order_cycle_does_not_crash(tmp_path: Path) -> None:
    (tmp_path / "alpha.py").write_text("import beta\n", encoding="utf-8")
    (tmp_path / "beta.py").write_text("import alpha\n", encoding="utf-8")
    result = build_dependency_order(["alpha.py", "beta.py"], tmp_path)
    assert set(result) == {"alpha.py", "beta.py"}


def test_build_dependency_order_non_python_first(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text("import helper\n", encoding="utf-8")
    result = build_dependency_order(["README.md", "mod.py"], tmp_path)
    # Non-python files have no deps → appear first
    assert result.index("README.md") < result.index("mod.py")


def test_build_dependency_order_missing_file_graceful(tmp_path: Path) -> None:
    # File listed but doesn't exist on disk — should not crash
    result = build_dependency_order(["missing.py"], tmp_path)
    assert result == ["missing.py"]


# ---------------------------------------------------------------------------
# plan_split
# ---------------------------------------------------------------------------


@patch("bernstein.core.git.pr_size_governor.run_git")
def test_plan_split_no_files_no_diff(mock_run: MagicMock) -> None:
    # No files provided, diff returns nothing
    mock_run.return_value = _git_ok(stdout="")
    plan = plan_split(REPO, files=[])
    assert plan.needs_split is False
    assert plan.chunks == []
    assert plan.total_lines == 0


@patch("bernstein.core.git.pr_size_governor.run_git")
def test_plan_split_under_limit_no_split(mock_run: MagicMock, tmp_path: Path) -> None:
    # 3 files, 100 lines total — well under 400
    mock_run.return_value = _git_ok(stdout="50\t10\ta.py\n30\t10\tb.py\n")
    plan = plan_split(tmp_path, files=["a.py", "b.py"], max_lines=400)
    assert plan.needs_split is False
    assert len(plan.chunks) == 1
    assert plan.chunks[0].part_number == 1
    assert plan.chunks[0].base_branch == "main"
    assert plan.total_lines == 100


@patch("bernstein.core.git.pr_size_governor.run_git")
def test_plan_split_over_limit_splits(mock_run: MagicMock, tmp_path: Path) -> None:
    # Two files, each with 300 lines → total 600, needs split into 2 chunks
    mock_run.return_value = _git_ok(stdout="200\t100\ta.py\n200\t100\tb.py\n")
    for f in ("a.py", "b.py"):
        (tmp_path / f).write_text("x = 1\n", encoding="utf-8")
    plan = plan_split(tmp_path, files=["a.py", "b.py"], max_lines=400)
    assert plan.needs_split is True
    assert plan.total_lines == 600
    assert len(plan.chunks) == 2


@patch("bernstein.core.git.pr_size_governor.run_git")
def test_plan_split_chunks_chain_bases(mock_run: MagicMock, tmp_path: Path) -> None:
    # 3 chunks → p2 targets p1, p3 targets p2
    numstat = "200\t100\ta.py\n200\t100\tb.py\n200\t100\tc.py\n"
    mock_run.return_value = _git_ok(stdout=numstat)
    for f in ("a.py", "b.py", "c.py"):
        (tmp_path / f).write_text("x = 1\n", encoding="utf-8")
    plan = plan_split(
        tmp_path,
        files=["a.py", "b.py", "c.py"],
        max_lines=400,
        task_branch="bernstein/task-abc",
    )
    assert len(plan.chunks) == 3
    assert plan.chunks[0].base_branch == "main"
    assert plan.chunks[0].branch_name == "bernstein/task-abc-p1"
    assert plan.chunks[1].base_branch == "bernstein/task-abc-p1"
    assert plan.chunks[1].branch_name == "bernstein/task-abc-p2"
    assert plan.chunks[2].base_branch == "bernstein/task-abc-p2"


@patch("bernstein.core.git.pr_size_governor.run_git")
def test_plan_split_single_oversized_file_own_chunk(mock_run: MagicMock, tmp_path: Path) -> None:
    # One file with 500 lines → single chunk (cannot split further)
    mock_run.return_value = _git_ok(stdout="300\t200\tbig.py\n")
    (tmp_path / "big.py").write_text("x = 1\n", encoding="utf-8")
    plan = plan_split(tmp_path, files=["big.py"], max_lines=400)
    # total 500 > 400 but only one file → needs_split=True, one chunk
    assert plan.needs_split is True
    assert len(plan.chunks) == 1
    assert plan.chunks[0].files == ["big.py"]


@patch("bernstein.core.git.pr_size_governor.run_git")
def test_plan_split_auto_detects_files_from_diff(mock_run: MagicMock, tmp_path: Path) -> None:
    # files=[] → reads from diff --name-only, then --numstat
    mock_run.side_effect = [
        _git_ok(stdout="auto.py\n"),  # diff --name-only
        _git_ok(stdout="50\t10\tauto.py\n"),  # diff --numstat
    ]
    plan = plan_split(tmp_path, files=[])
    assert plan.chunks[0].files == ["auto.py"]
    assert plan.total_lines == 60


# ---------------------------------------------------------------------------
# execute_split
# ---------------------------------------------------------------------------


@patch("bernstein.core.git.pr_size_governor.run_git")
def test_execute_split_empty_plan(mock_run: MagicMock) -> None:
    plan = SplitPlan(chunks=[], total_lines=0, needs_split=False)
    result = execute_split(REPO, plan)
    assert result.success is True
    assert result.pr_urls == []
    assert result.chunk_count == 0
    mock_run.assert_not_called()


@patch("bernstein.core.git.pr_size_governor.run_git")
def test_execute_split_no_split_needed(mock_run: MagicMock) -> None:
    chunk = PRChunk(
        files=["a.py"],
        line_count=50,
        branch_name="bernstein/task-x-p1",
        base_branch="main",
        part_number=1,
    )
    plan = SplitPlan(chunks=[chunk], total_lines=50, needs_split=False)
    result = execute_split(REPO, plan)
    assert result.success is True
    assert result.chunk_count == 0
    assert "no split needed" in result.error
    mock_run.assert_not_called()


@patch("bernstein.core.git.pr_size_governor.create_github_pr")
@patch("bernstein.core.git.pr_size_governor.push_branch")
@patch("bernstein.core.git.pr_size_governor.run_git")
def test_execute_split_creates_prs_for_each_chunk(
    mock_run: MagicMock,
    mock_push: MagicMock,
    mock_pr: MagicMock,
) -> None:
    chunk1 = PRChunk(
        files=["a.py"],
        line_count=300,
        branch_name="bernstein/task-abc-p1",
        base_branch="main",
        part_number=1,
    )
    chunk2 = PRChunk(
        files=["b.py"],
        line_count=300,
        branch_name="bernstein/task-abc-p2",
        base_branch="bernstein/task-abc-p1",
        part_number=2,
    )
    plan = SplitPlan(chunks=[chunk1, chunk2], total_lines=600, needs_split=True)

    mock_run.return_value = _git_ok(stdout="main")
    mock_push.return_value = _git_ok()
    mock_pr.side_effect = [
        _pr_ok("https://github.com/org/repo/pull/1"),
        _pr_ok("https://github.com/org/repo/pull/2"),
    ]

    result = execute_split(REPO, plan, pr_title_prefix="feat")

    assert result.success is True
    assert result.chunk_count == 2
    assert result.pr_urls == [
        "https://github.com/org/repo/pull/1",
        "https://github.com/org/repo/pull/2",
    ]
    assert mock_push.call_count == 2
    assert mock_pr.call_count == 2


@patch("bernstein.core.git.pr_size_governor.create_github_pr")
@patch("bernstein.core.git.pr_size_governor.push_branch")
@patch("bernstein.core.git.pr_size_governor.run_git")
def test_execute_split_branch_creation_failure_returns_error(
    mock_run: MagicMock,
    mock_push: MagicMock,
    mock_pr: MagicMock,
) -> None:
    chunk = PRChunk(
        files=["a.py"],
        line_count=300,
        branch_name="bernstein/task-abc-p1",
        base_branch="main",
        part_number=1,
    )
    plan = SplitPlan(chunks=[chunk], total_lines=300, needs_split=True)

    # rev-parse ok, then both checkout -B attempts fail, then branch -D ok
    mock_run.side_effect = [
        _git_ok(stdout="main"),  # rev-parse --abbrev-ref
        _git_fail("branch exists"),  # first checkout -B
        _git_ok(),  # branch -D
        _git_fail("still fails"),  # second checkout -B
        _git_ok(stdout="main"),  # _restore checkout
    ]

    result = execute_split(REPO, plan)

    assert result.success is False
    assert "Cannot create branch" in result.error
    mock_push.assert_not_called()
    mock_pr.assert_not_called()


@patch("bernstein.core.git.pr_size_governor.create_github_pr")
@patch("bernstein.core.git.pr_size_governor.push_branch")
@patch("bernstein.core.git.pr_size_governor.run_git")
def test_execute_split_push_failure_returns_error(
    mock_run: MagicMock,
    mock_push: MagicMock,
    mock_pr: MagicMock,
) -> None:
    chunk = PRChunk(
        files=["a.py"],
        line_count=300,
        branch_name="bernstein/task-abc-p1",
        base_branch="main",
        part_number=1,
    )
    plan = SplitPlan(chunks=[chunk], total_lines=300, needs_split=True)

    mock_run.return_value = _git_ok(stdout="main\ndiff --git a/a.py\n+++ added line")
    mock_push.return_value = _git_fail("push rejected")

    result = execute_split(REPO, plan)

    assert result.success is False
    assert "Push failed" in result.error
    mock_pr.assert_not_called()


@patch("bernstein.core.git.pr_size_governor.create_github_pr")
@patch("bernstein.core.git.pr_size_governor.push_branch")
@patch("bernstein.core.git.pr_size_governor.run_git")
def test_execute_split_pr_failure_logged_but_marked_unsuccessful(
    mock_run: MagicMock,
    mock_push: MagicMock,
    mock_pr: MagicMock,
) -> None:
    chunk = PRChunk(
        files=["a.py"],
        line_count=300,
        branch_name="bernstein/task-abc-p1",
        base_branch="main",
        part_number=1,
    )
    plan = SplitPlan(chunks=[chunk], total_lines=300, needs_split=True)

    mock_run.return_value = _git_ok(stdout="main\n+diff line\n")
    mock_push.return_value = _git_ok()
    mock_pr.return_value = _pr_fail("gh: not found")

    result = execute_split(REPO, plan)

    # PR creation failed → chunk_count=0, success=False (0 != 1 total)
    assert result.success is False
    assert result.chunk_count == 0


@patch("bernstein.core.git.pr_size_governor.create_github_pr")
@patch("bernstein.core.git.pr_size_governor.push_branch")
@patch("bernstein.core.git.pr_size_governor.run_git")
def test_execute_split_restores_original_branch(
    mock_run: MagicMock,
    mock_push: MagicMock,
    mock_pr: MagicMock,
) -> None:
    chunk = PRChunk(
        files=["a.py"],
        line_count=300,
        branch_name="bernstein/task-abc-p1",
        base_branch="main",
        part_number=1,
    )
    plan = SplitPlan(chunks=[chunk], total_lines=300, needs_split=True)

    diff_output = "+diff content here\n"
    mock_run.side_effect = [
        _git_ok(stdout="feature-branch"),  # rev-parse --abbrev-ref
        _git_ok(),  # checkout -B
        _git_ok(stdout=diff_output),  # diff base head
        _git_ok(),  # apply
        _git_ok(),  # add
        _git_ok(),  # commit
        _git_ok(stdout="feature-branch"),  # _restore checkout
    ]
    mock_push.return_value = _git_ok()
    mock_pr.return_value = _pr_ok()

    execute_split(REPO, plan)

    # Last run_git call must restore the original branch
    last_call_args = mock_run.call_args_list[-1]
    assert last_call_args[0][0][:2] == ["checkout", "feature-branch"]


# ---------------------------------------------------------------------------
# _build_pr_body
# ---------------------------------------------------------------------------


def test_build_pr_body_middle_chunk_mentions_next() -> None:
    chunk1 = PRChunk(
        files=["a.py"],
        line_count=100,
        branch_name="bernstein/task-p1",
        base_branch="main",
        part_number=1,
    )
    chunk2 = PRChunk(
        files=["b.py"],
        line_count=100,
        branch_name="bernstein/task-p2",
        base_branch="bernstein/task-p1",
        part_number=2,
    )
    plan = SplitPlan(chunks=[chunk1, chunk2], total_lines=200, needs_split=True)
    body = _build_pr_body(chunk1, plan, prefix="")
    assert "Next:" in body
    assert "p2" in body


def test_build_pr_body_final_chunk_says_final() -> None:
    chunk = PRChunk(
        files=["c.py"],
        line_count=100,
        branch_name="bernstein/task-p1",
        base_branch="main",
        part_number=1,
    )
    plan = SplitPlan(chunks=[chunk], total_lines=100, needs_split=True)
    body = _build_pr_body(chunk, plan, prefix="")
    assert "final part" in body


def test_build_pr_body_includes_prefix() -> None:
    chunk = PRChunk(
        files=["a.py"],
        line_count=50,
        branch_name="bernstein/task-p1",
        base_branch="main",
        part_number=1,
    )
    plan = SplitPlan(chunks=[chunk], total_lines=50, needs_split=True)
    body = _build_pr_body(chunk, plan, prefix="## My Task\n\nSome context")
    assert "## My Task" in body
    assert "a.py" in body


def test_build_pr_body_lists_files() -> None:
    chunk = PRChunk(
        files=["src/foo.py", "src/bar.py"],
        line_count=200,
        branch_name="bernstein/task-p1",
        base_branch="main",
        part_number=1,
    )
    plan = SplitPlan(chunks=[chunk], total_lines=200, needs_split=True)
    body = _build_pr_body(chunk, plan, prefix="")
    assert "src/foo.py" in body
    assert "src/bar.py" in body


# ---------------------------------------------------------------------------
# split_pr_if_needed
# ---------------------------------------------------------------------------


@patch("bernstein.core.git.pr_size_governor.execute_split")
@patch("bernstein.core.git.pr_size_governor.plan_split")
def test_split_pr_if_needed_returns_none_when_no_split(
    mock_plan: MagicMock,
    mock_exec: MagicMock,
) -> None:
    mock_plan.return_value = SplitPlan(
        chunks=[PRChunk(files=["a.py"], line_count=100, branch_name="b-p1", base_branch="main", part_number=1)],
        total_lines=100,
        needs_split=False,
    )

    result = split_pr_if_needed(REPO, task_id="abc", task_title="feat: something")

    assert result is None
    mock_exec.assert_not_called()


@patch("bernstein.core.git.pr_size_governor.execute_split")
@patch("bernstein.core.git.pr_size_governor.plan_split")
def test_split_pr_if_needed_calls_execute_when_split_needed(
    mock_plan: MagicMock,
    mock_exec: MagicMock,
) -> None:
    chunk1 = PRChunk(files=["a.py"], line_count=300, branch_name="b-p1", base_branch="main", part_number=1)
    chunk2 = PRChunk(files=["b.py"], line_count=300, branch_name="b-p2", base_branch="b-p1", part_number=2)
    mock_plan.return_value = SplitPlan(chunks=[chunk1, chunk2], total_lines=600, needs_split=True)
    mock_exec.return_value = SplitResult(
        pr_urls=["https://github.com/org/repo/pull/1", "https://github.com/org/repo/pull/2"],
        chunk_count=2,
        success=True,
    )

    result = split_pr_if_needed(REPO, task_id="abc", task_title="feat: big change")

    assert result is not None
    assert result.success is True
    assert result.chunk_count == 2
    mock_exec.assert_called_once()


@patch("bernstein.core.git.pr_size_governor.execute_split")
@patch("bernstein.core.git.pr_size_governor.plan_split")
def test_split_pr_if_needed_uses_task_id_for_branch_prefix(
    mock_plan: MagicMock,
    mock_exec: MagicMock,
) -> None:
    mock_plan.return_value = SplitPlan(chunks=[], total_lines=0, needs_split=False)

    split_pr_if_needed(REPO, task_id="xyz789")

    _, kwargs = mock_plan.call_args
    assert kwargs.get("task_branch") == "bernstein/task-xyz789"


@patch("bernstein.core.git.pr_size_governor.execute_split")
@patch("bernstein.core.git.pr_size_governor.plan_split")
def test_split_pr_if_needed_fallback_branch_prefix(
    mock_plan: MagicMock,
    mock_exec: MagicMock,
) -> None:
    mock_plan.return_value = SplitPlan(chunks=[], total_lines=0, needs_split=False)

    split_pr_if_needed(REPO)

    _, kwargs = mock_plan.call_args
    assert kwargs.get("task_branch") == "bernstein/split"


@patch("bernstein.core.git.pr_size_governor.execute_split")
@patch("bernstein.core.git.pr_size_governor.plan_split")
def test_split_pr_if_needed_passes_max_lines(
    mock_plan: MagicMock,
    mock_exec: MagicMock,
) -> None:
    mock_plan.return_value = SplitPlan(chunks=[], total_lines=0, needs_split=False)

    split_pr_if_needed(REPO, max_lines=200)

    _, kwargs = mock_plan.call_args
    assert kwargs.get("max_lines") == 200


# ---------------------------------------------------------------------------
# MAX_PR_LINES constant
# ---------------------------------------------------------------------------


def test_max_pr_lines_is_400() -> None:
    assert MAX_PR_LINES == 400
