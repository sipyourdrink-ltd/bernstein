"""Issue #4578: default branch must come from the repository, not the checkout.

``bernstein agents-md sync`` used to fall back to the checked-out branch
name when no conventional ``main``/``master`` ref and no ``origin/HEAD``
were present. On a feature branch (or a single-branch CI clone) that wrote
the wrong branch into all five mirrors, and the mirror-drift guard then
failed forever with a message telling the contributor to run the very
command that caused it.

The default branch is a property of the repository. These tests use real
temporary git repositories (per the issue: faking the ref resolution
proves nothing) and pin the acceptance behaviour:

* the rendered line is stable across checked-out branches;
* ``origin/HEAD`` wins over the checked-out branch;
* a detached HEAD still renders the repository default;
* an unresolvable default fails loudly instead of writing a guess;
* an explicit configuration value resolves otherwise-unresolvable repos.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bernstein.core.knowledge.agents_md_generator import (
    DefaultBranchUnresolvedError,
    GenerateOptions,
    generate,
)


def _clone_with_default(tmp_path: Path, name: str, default: str) -> tuple[Path, Path]:
    """Create ``(bare_origin, working_clone)`` whose origin/HEAD → ``default``.

    A real clone is used because ``git clone`` is what sets up
    ``refs/remotes/origin/HEAD`` in the first place; hand-crafting the ref
    would test the probe against an environment no real checkout has.
    """
    origin = tmp_path / f"{name}-origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", default, str(origin)], check=True)
    clone = tmp_path / name
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    (clone / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=clone, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=clone, check=True)
    subprocess.run(["git", "add", "-A"], cwd=clone, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=clone, check=True)
    subprocess.run(["git", "push", "-q", "origin", default], cwd=clone, check=True)
    # ``git clone`` of an empty repository leaves ``origin/HEAD`` unset, and
    # pushing does not create it either. Re-point it explicitly so the probe
    # under test sees the same ref layout a real clone of a populated repo
    # would have.
    subprocess.run(["git", "remote", "set-head", "origin", "-a"], cwd=clone, check=True)
    return origin, clone


def _git_workflow_section(repo: Path) -> str | None:
    """Return the git-workflow section body (or None) for ``repo``."""
    sections = generate(repo)
    for sec in sections:
        if sec.key == "git-workflow":
            return sec.body
    return None


def test_default_branch_line_is_stable_across_checked_out_branches(tmp_path: Path) -> None:
    """Issue acceptance: same repo on ``main`` vs ``feature/x`` renders identically."""
    _origin, clone = _clone_with_default(tmp_path, "stable", "main")
    subprocess.run(["git", "checkout", "-q", "-b", "feature/x"], cwd=clone, check=True)

    on_feature = _git_workflow_section(clone)

    subprocess.run(["git", "checkout", "-q", "main"], cwd=clone, check=True)
    on_main = _git_workflow_section(clone)

    assert on_feature == on_main == "Default branch: `main`."


def test_default_branch_is_read_from_the_repository_not_from_head(tmp_path: Path) -> None:
    """Issue acceptance: origin/HEAD → ``trunk`` renders ``trunk`` on a feature branch."""
    _origin, clone = _clone_with_default(tmp_path, "trunkrepo", "trunk")
    subprocess.run(["git", "checkout", "-q", "-b", "feature/x"], cwd=clone, check=True)

    assert _git_workflow_section(clone) == "Default branch: `trunk`."


def test_detached_head_still_renders_the_repository_default(tmp_path: Path) -> None:
    """Issue acceptance: no branch name at all must not degrade the output."""
    _origin, clone = _clone_with_default(tmp_path, "detached", "main")
    subprocess.run(["git", "checkout", "-q", "--detach"], cwd=clone, check=True)

    body = _git_workflow_section(clone)
    assert body is not None
    assert body == "Default branch: `main`."


def test_unresolvable_default_fails_instead_of_writing_the_checked_out_branch(tmp_path: Path) -> None:
    """Open-decision resolution: fail the command, never write a guess.

    A checkout on ``feature/x`` with no origin and no conventional refs has
    no repository property to report. Rendering ``main`` (the suggested
    alternative) would be the same class of bug for a repo whose default is
    ``trunk``, so the generator must refuse.
    """
    repo = tmp_path / "noorigin"
    repo.mkdir(parents=True)
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "feature/x"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    with pytest.raises(DefaultBranchUnresolvedError):
        generate(repo)


def test_configured_default_branch_resolves_an_otherwise_unresolvable_repo(tmp_path: Path) -> None:
    """An explicit pin is the escape hatch the error message points operators at."""
    repo = tmp_path / "pinned"
    repo.mkdir(parents=True)
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "feature/x"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    sections = generate(repo, GenerateOptions(default_branch="trunk"))
    bodies = {sec.key: sec.body for sec in sections}
    assert bodies["git-workflow"] == "Default branch: `trunk`."


def test_explicit_configuration_overrides_origin_head(tmp_path: Path) -> None:
    """A user pin beats git probes - operators win over repository defaults."""
    _origin, clone = _clone_with_default(tmp_path, "override", "main")

    sections = generate(clone, GenerateOptions(default_branch="release/v3"))
    bodies = {sec.key: sec.body for sec in sections}
    assert bodies["git-workflow"] == "Default branch: `release/v3`."


def test_git_workflow_section_omitted_outside_a_git_tree(tmp_path: Path) -> None:
    """Non-git directories still render fine with the section simply absent."""
    repo = tmp_path / "plain"
    repo.mkdir(parents=True)
    (repo / "README.md").write_text("x\n", encoding="utf-8")

    assert _git_workflow_section(repo) is None
