"""Behavioural tests for ``scripts/check_context_staleness.py``.

The staleness report is a pure function of git history, so every test
builds a real synthetic git repository (``git init`` + commits with
pinned author/committer identity and dates, isolated from user and
system git config) and asserts on the script's actual output. The script
module is loaded via importlib (the ``tests/unit/test_gen_agents_md_split.py``
pattern) for its named constants; end-to-end runs go through a
subprocess so exit codes and byte-level determinism are tested for real.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_context_staleness.py"


@pytest.fixture
def staleness():
    """Load scripts/check_context_staleness.py without executing main()."""
    spec = importlib.util.spec_from_file_location("check_context_staleness_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


# ---------------------------------------------------------------------------
# Synthetic-repo helpers
# ---------------------------------------------------------------------------


def _git_env(tmp_path: Path) -> dict[str, str]:
    """Pinned identity + config isolation so host git config is inert."""
    import os

    empty_config = tmp_path / "empty-gitconfig"
    empty_config.touch()
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "Synthetic",
        "GIT_AUTHOR_EMAIL": "synthetic@example.invalid",
        "GIT_COMMITTER_NAME": "Synthetic",
        "GIT_COMMITTER_EMAIL": "synthetic@example.invalid",
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
        "GIT_CONFIG_GLOBAL": str(empty_config),
        "GIT_CONFIG_SYSTEM": str(empty_config),
    }


def _git(repo: Path, env: dict[str, str], *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _seed_repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """One commit: a context file plus modules inside and outside its scope."""
    env = _git_env(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, env, "-c", "init.defaultBranch=main", "init", "--quiet")
    _write(repo, "src/pkg/AGENTS.md", "# pkg context\n\nInvariant: mod.py stays small.\n")
    _write(repo, "src/pkg/mod.py", "".join(f"LINE_{i} = {i}\n" for i in range(10)))
    _write(repo, "src/other/util.py", "UTIL = 1\n")
    _git(repo, env, "add", "-A")
    _git(repo, env, "commit", "--quiet", "-m", "seed: context file and modules")
    return repo, env


def _run(repo: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--repo", str(repo), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _run_json(repo: Path, env: dict[str, str], *args: str) -> dict:
    proc = _run(repo, env, "--json", *args)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _big_rewrite(repo: Path, env: dict[str, str], rel: str = "src/pkg/mod.py", lines: int = 250) -> str:
    """Rewrite ``rel`` with ``lines`` fresh lines; return the commit sha."""
    _write(repo, rel, "".join(f"CHURN_{i} = {i}\n" for i in range(lines)))
    _git(repo, env, "add", "-A")
    _git(repo, env, "commit", "--quiet", "-m", f"churn: rewrite {rel}")
    return _git(repo, env, "rev-parse", "HEAD")


# ---------------------------------------------------------------------------
# Flagging behaviour
# ---------------------------------------------------------------------------


def test_churned_subtree_with_untouched_context_file_is_flagged(tmp_path: Path, staleness) -> None:
    repo, env = _seed_repo(tmp_path)
    seed_sha = _git(repo, env, "rev-parse", "HEAD")
    churn_sha = _big_rewrite(repo, env, lines=staleness.SUBTREE_LINE_THRESHOLD + 50)

    payload = _run_json(repo, env)
    assert payload["clean"] is False
    (entry,) = payload["flagged"]
    assert entry["path"] == "src/pkg/AGENTS.md"
    assert entry["last_commit"] == seed_sha
    assert entry["total_lines"] >= staleness.SUBTREE_LINE_THRESHOLD
    # The report names the exact commit that aged the file.
    assert entry["top_commits"][0]["sha"] == churn_sha

    markdown = _run(repo, env).stdout
    assert "src/pkg/AGENTS.md" in markdown
    assert seed_sha[: staleness.SHORT_SHA_LEN] in markdown
    assert churn_sha[: staleness.SHORT_SHA_LEN] in markdown


def test_churn_below_threshold_is_not_flagged(tmp_path: Path) -> None:
    repo, env = _seed_repo(tmp_path)
    _write(repo, "src/pkg/mod.py", "".join(f"LINE_{i} = {i + 1}\n" for i in range(10)))
    _git(repo, env, "add", "-A")
    _git(repo, env, "commit", "--quiet", "-m", "churn: small tweak")

    payload = _run_json(repo, env)
    assert payload["clean"] is True
    assert payload["flagged"] == []
    assert payload["files_checked"] == 1


def test_reconfirmation_only_edit_clears_the_flag(tmp_path: Path) -> None:
    repo, env = _seed_repo(tmp_path)
    _big_rewrite(repo, env)
    assert _run_json(repo, env)["clean"] is False

    # A no-op reconfirmation edit: the content review leaves a commit.
    agents = repo / "src/pkg/AGENTS.md"
    _write(repo, "src/pkg/AGENTS.md", agents.read_text(encoding="utf-8") + "\n<!-- reconfirmed -->\n")
    _git(repo, env, "add", "-A")
    _git(repo, env, "commit", "--quiet", "-m", "docs: reconfirm pkg context")

    payload = _run_json(repo, env)
    assert payload["clean"] is True
    assert payload["flagged"] == []


def test_module_add_flags_even_below_line_threshold(tmp_path: Path, staleness) -> None:
    repo, env = _seed_repo(tmp_path)
    _write(repo, "src/pkg/new_module.py", "NEW = 1\n")
    _git(repo, env, "add", "-A")
    _git(repo, env, "commit", "--quiet", "-m", "feat: add new module")

    payload = _run_json(repo, env)
    (entry,) = payload["flagged"]
    assert entry["path"] == "src/pkg/AGENTS.md"
    assert entry["modules_added"] == ["src/pkg/new_module.py"]
    assert entry["total_lines"] < staleness.SUBTREE_LINE_THRESHOLD


def test_module_removal_flags_even_below_line_threshold(tmp_path: Path, staleness) -> None:
    repo, env = _seed_repo(tmp_path)
    _git(repo, env, "rm", "--quiet", "src/pkg/mod.py")
    _git(repo, env, "commit", "--quiet", "-m", "refactor: drop module")

    payload = _run_json(repo, env)
    (entry,) = payload["flagged"]
    assert entry["path"] == "src/pkg/AGENTS.md"
    assert entry["modules_removed"] == ["src/pkg/mod.py"]
    assert entry["total_lines"] < staleness.SUBTREE_LINE_THRESHOLD


def test_sibling_directory_churn_does_not_age_the_file(tmp_path: Path) -> None:
    repo, env = _seed_repo(tmp_path)
    _big_rewrite(repo, env, rel="src/other/util.py", lines=300)

    payload = _run_json(repo, env)
    assert payload["clean"] is True
    assert payload["flagged"] == []


def test_untracked_context_files_never_enter_the_report(tmp_path: Path) -> None:
    repo, env = _seed_repo(tmp_path)
    _write(repo, "src/scratch/AGENTS.md", "# untracked scratch context\n")

    payload = _run_json(repo, env)
    assert payload["files_checked"] == 1
    assert "scratch" not in _run(repo, env).stdout


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_report_is_deterministic_for_fixed_history(tmp_path: Path) -> None:
    repo, env = _seed_repo(tmp_path)
    _big_rewrite(repo, env)

    md_runs = [_run(repo, env) for _ in range(2)]
    json_runs = [_run(repo, env, "--json") for _ in range(2)]
    assert md_runs[0].stdout == md_runs[1].stdout
    assert json_runs[0].stdout == json_runs[1].stdout
    assert [p.returncode for p in md_runs + json_runs] == [0, 0, 0, 0]


def test_rename_detection_config_cannot_change_the_report(tmp_path: Path) -> None:
    """A ``git mv`` is one removed + one added surface event, regardless of
    the repo's ``diff.renames`` setting - the verdict must not vary with
    git configuration."""
    repo, env = _seed_repo(tmp_path)
    _git(repo, env, "mv", "src/pkg/mod.py", "src/pkg/renamed.py")
    _git(repo, env, "commit", "--quiet", "-m", "refactor: rename module")

    outputs: list[str] = []
    for value in ("true", "false"):
        _git(repo, env, "config", "diff.renames", value)
        outputs.append(_run(repo, env).stdout)
        payload = _run_json(repo, env)
        (entry,) = payload["flagged"]
        assert entry["modules_removed"] == ["src/pkg/mod.py"]
        assert entry["modules_added"] == ["src/pkg/renamed.py"]
    assert outputs[0] == outputs[1]


# ---------------------------------------------------------------------------
# Exit codes and surfaces
# ---------------------------------------------------------------------------


def test_strict_flag_gates_exit_code_and_advisory_mode_never_fails(tmp_path: Path) -> None:
    repo, env = _seed_repo(tmp_path)
    assert _run(repo, env, "--strict").returncode == 0  # clean + strict

    _big_rewrite(repo, env)
    assert _run(repo, env).returncode == 0  # flagged + advisory
    assert _run(repo, env, "--strict").returncode == 1  # flagged + strict


def test_baseline_marks_only_newly_flagged(tmp_path: Path, staleness) -> None:
    env = _git_env(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, env, "-c", "init.defaultBranch=main", "init", "--quiet")
    for pkg in ("alpha", "beta"):
        _write(repo, f"src/{pkg}/AGENTS.md", f"# {pkg} context\n")
        _write(repo, f"src/{pkg}/mod.py", "".join(f"LINE_{i} = {i}\n" for i in range(10)))
    _git(repo, env, "add", "-A")
    _git(repo, env, "commit", "--quiet", "-m", "seed: two scoped context files")

    baseline_sha = _big_rewrite(repo, env, rel="src/alpha/mod.py")  # alpha flagged at baseline
    _big_rewrite(repo, env, rel="src/beta/mod.py")  # beta flagged only at head

    payload = _run_json(repo, env, "--baseline", baseline_sha)
    assert {e["path"] for e in payload["flagged"]} == {"src/alpha/AGENTS.md", "src/beta/AGENTS.md"}
    assert payload["newly_flagged"] == ["src/beta/AGENTS.md"]

    # The module API agrees with the CLI surface.
    report = staleness.compute_report(repo, "HEAD", baseline_sha)
    assert [e.context.path for e in report.newly_flagged] == ["src/beta/AGENTS.md"]


def test_committed_overlay_is_repo_scoped_and_flags_on_lines_only(tmp_path: Path, staleness) -> None:
    env = _git_env(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, env, "-c", "init.defaultBranch=main", "init", "--quiet")
    _write(repo, ".sdd/agents-md/conventions.md", "# repo conventions\n")
    _write(repo, "src/x/code.py", "X = 1\n")
    _git(repo, env, "add", "-A")
    _git(repo, env, "commit", "--quiet", "-m", "seed: committed overlay")

    # A module add anywhere must NOT flag a repo-scoped overlay.
    _write(repo, "src/x/extra.py", "Y = 2\n")
    _git(repo, env, "add", "-A")
    _git(repo, env, "commit", "--quiet", "-m", "feat: small module add")
    assert _run_json(repo, env)["clean"] is True

    # Crossing the repo-scope line threshold does flag it.
    _big_rewrite(repo, env, rel="src/x/code.py", lines=staleness.REPO_SCOPE_LINE_THRESHOLD + 100)
    payload = _run_json(repo, env)
    (entry,) = payload["flagged"]
    assert entry["path"] == ".sdd/agents-md/conventions.md"
    assert entry["repo_scoped"] is True
    assert entry["line_threshold"] == staleness.REPO_SCOPE_LINE_THRESHOLD


def test_shallow_clone_is_rejected_instead_of_reporting_wrong_numbers(tmp_path: Path) -> None:
    repo, env = _seed_repo(tmp_path)
    _big_rewrite(repo, env)
    shallow = tmp_path / "shallow"
    _git(
        tmp_path,
        env,
        "clone",
        "--quiet",
        "--depth",
        "1",
        f"file://{repo.as_posix()}",
        str(shallow),
    )

    proc = _run(shallow, env)
    assert proc.returncode == 2
    assert "shallow" in proc.stderr.lower()
