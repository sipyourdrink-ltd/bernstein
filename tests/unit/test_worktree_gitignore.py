"""Regression tests for #3017: agent worktrees must locally exclude the
merge guard's full deny list -- plus the orchestrator's own generated
``CLAUDE.md`` -- at creation time, scoped to that ONE worktree only.

Bernstein orchestrates agents against arbitrary target repositories, most of
which have no idea ``.sdd/``, ``attestations/``, ``auth/``,
``bernstein.yaml``, ``.env``, or ``.claude/mcp.json`` are orchestrator-owned
runtime/control state. An agent following its own "finish with ``git add -A
&& git commit``" instruction would otherwise stage all of it -- and the
reap-and-merge preflight's forbidden-path guard then refuses the *entire*
commit, diverting real work to the graveyard instead of merging it.

The exclude list is derived directly from the merge guard's own deny list
(``bernstein.core.git.git_pr._MERGE_DENY_PREFIXES`` /
``_MERGE_DENY_EXACT``) so the two can never drift apart, plus ``/CLAUDE.md``
added on top: the orchestrator generates a session-specific ``CLAUDE.md`` at
the root of *every* worktree (``worktree_claude_md.write_claude_md``), so
that exact path is always a duplicate/decoy file, never a genuine
target-repo deliverable.

Mechanism: a bernstein-owned exclude file lives inside this worktree's own
per-worktree git dir (``.git/worktrees/<id>/``, never the tracked tree) and
is wired up via ``git config --worktree core.excludesFile``. This was the
SECOND design tried in this PR, after ``.git/info/exclude`` was proven
(empirically, see below) to be read from the *shared* common git dir, not
per-worktree -- which would have silently changed what the operator's own
main checkout picks up on ``git add -A`` (a freshly-created
``bernstein.yaml`` seed config, for example, would stop being staged with
no warning). ``core.excludesFile`` scoped with ``--worktree`` (which
requires the one-time, idempotent ``extensions.worktreeConfig`` flag) is
git's native way to scope a setting to exactly one worktree, confirmed to
leave every other worktree of the same clone -- and the main checkout --
completely unaffected.

These tests run against a real git repository (not mocks) so ``git add -A``,
``git status``, and the merge-preflight guard reflect true on-disk/index
behaviour.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from bernstein.core.models import Scope, Task

import bernstein.core.git.git_pr as git_pr
import bernstein.core.git.worktree as worktree_mod
from bernstein.core.git.worktree import WorktreeManager
from bernstein.core.git.worktree_claude_md import write_claude_md


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _git_dir(worktree_path: Path) -> Path:
    """Resolve the *per-worktree* git dir, mirroring the implementation."""
    out = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-dir"],
        cwd=worktree_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return Path(out)


def _configured_excludes_file(worktree_path: Path) -> Path | None:
    """Read whatever ``core.excludesFile`` resolves to for *worktree_path*."""
    result = subprocess.run(
        ["git", "config", "--worktree", "--get", "core.excludesFile"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip())


def _config_value(repo: Path, *args: str) -> str:
    """Read a config value, returning ``""`` when the key is not set."""
    result = subprocess.run(
        ["git", "config", *args],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _local_exclude_lines(worktree_path: Path) -> set[str]:
    exclude_path = _configured_excludes_file(worktree_path)
    if exclude_path is None or not exclude_path.exists():
        return set()
    return {line.strip() for line in exclude_path.read_text(encoding="utf-8").splitlines()}


def _staged(worktree_path: Path) -> list[str]:
    return [
        line.strip() for line in _git(worktree_path, "diff", "--cached", "--name-only").splitlines() if line.strip()
    ]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A plain target repository with no bernstein-aware ignore rules.

    This mirrors the real-world case: bernstein spawns agents into arbitrary
    client repos that have never heard of ``.sdd/``, ``attestations/``,
    ``auth/``, or ``.claude/`` -- and that DO legitimately track their own
    ``bernstein.yaml`` seed config in the main checkout.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("# repo\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "initial")
    return root


def _drop_full_deny_list_runtime_state(worktree_path: Path, session_id: str) -> None:
    """Write one artefact per entry in the merge guard's deny list
    (``.sdd/``, ``attestations/``, ``auth/``, ``bernstein.yaml``, ``.env``,
    ``.claude/mcp.json``), mirroring the real e2e evidence in #3017 plus the
    other deny-listed prefixes the original fix missed."""
    runtime_dir = worktree_path / ".sdd" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / f"{session_id}.log").write_text("agent log line\n", encoding="utf-8")

    attestations_dir = worktree_path / "attestations"
    attestations_dir.mkdir(parents=True, exist_ok=True)
    (attestations_dir / "ed25519-signing-key.pem").write_text("KEY\n", encoding="utf-8")

    auth_dir = worktree_path / "auth"
    auth_dir.mkdir(parents=True, exist_ok=True)
    (auth_dir / "agent_identity_jwt_secret").write_text("SECRET\n", encoding="utf-8")

    (worktree_path / "bernstein.yaml").write_text("token: x\n", encoding="utf-8")
    (worktree_path / ".env").write_text("SECRET=1\n", encoding="utf-8")

    claude_dir = worktree_path / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "mcp.json").write_text("{}\n", encoding="utf-8")


def _make_task() -> Task:
    return Task(
        id="T-001",
        title="Implement feature",
        description="Write the code.",
        role="backend",
        scope=Scope.MEDIUM,
        priority=2,
        owned_files=[],
    )


def test_create_writes_worktree_scoped_excludes_not_a_tracked_gitignore(tmp_path: Path, repo: Path) -> None:
    """``WorktreeManager.create`` must configure a worktree-scoped
    ``core.excludesFile`` covering the full deny-list-derived exclude set --
    and must NOT create or modify a tracked ``.gitignore`` in the worktree's
    working tree, since that would itself be staged and committed into the
    target repo. Must also NOT touch the shared repo-level ``.git/config``
    beyond the one-time ``extensions.worktreeConfig`` flag."""
    mgr = WorktreeManager(repo_root=repo)

    worktree_path = mgr.create("sess-excludes")

    # No tracked .gitignore was created -- the target repo had none, and
    # this fix must never introduce one.
    assert not (worktree_path / ".gitignore").exists()

    lines = _local_exclude_lines(worktree_path)
    assert "/.sdd/" in lines
    assert "/attestations/" in lines
    assert "/auth/" in lines
    assert "/bernstein.yaml" in lines
    assert "/.env" in lines
    assert "/.claude/mcp.json" in lines
    assert "/CLAUDE.md" in lines

    # Must not blanket-ignore the whole .claude/ tree -- that would drop a
    # legitimate .claude/ deliverable (e.g. a skill or command).
    assert ".claude/" not in lines
    assert "/.claude/" not in lines

    # The exclude file lives inside THIS worktree's own git dir, not the
    # shared/common one.
    exclude_path = _configured_excludes_file(worktree_path)
    assert exclude_path is not None
    assert str(_git_dir(worktree_path)) in str(exclude_path)

    # The shared repo config gained only the (idempotent, harmless)
    # extensions.worktreeConfig flag -- never the excludesFile setting
    # itself, which must stay worktree-scoped.
    shared_config = (repo / ".git" / "config").read_text(encoding="utf-8")
    assert "worktreeConfig = true" in shared_config
    assert "excludesFile" not in shared_config


def test_create_leaves_target_repo_tracked_tree_unchanged(tmp_path: Path, repo: Path) -> None:
    """Worktree creation must not stage, modify, or introduce any file in
    the target repo's working tree / index. ``git status`` right after
    ``create()`` must be completely clean."""
    mgr = WorktreeManager(repo_root=repo)

    worktree_path = mgr.create("sess-clean-tree")

    status = _git(worktree_path, "status", "--porcelain")
    assert status == "", f"worktree creation must leave the tracked tree untouched, got: {status!r}"


def test_git_add_dash_a_does_not_stage_full_deny_list_paths(tmp_path: Path, repo: Path) -> None:
    """The actual bug, now covering the guard's *full* deny list: an agent
    running ``git add -A`` in its worktree must never stage ``.sdd/*``,
    ``attestations/*``, ``auth/*``, ``bernstein.yaml``, ``.env``, or
    ``.claude/mcp.json`` -- only its real work."""
    mgr = WorktreeManager(repo_root=repo)
    session_id = "sess-add-a"
    worktree_path = mgr.create(session_id)

    # The agent's actual deliverable.
    src_dir = worktree_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "feature.py").write_text("def feature():\n    return 42\n", encoding="utf-8")

    _drop_full_deny_list_runtime_state(worktree_path, session_id)

    _git(worktree_path, "add", "-A")
    staged = _staged(worktree_path)

    denied_staged = [p for p in staged if git_pr._is_forbidden_for_merge(p)]
    assert denied_staged == [], f"deny-listed paths must never be staged, got: {denied_staged}"
    assert "src/feature.py" in staged, "the agent's real work must still be staged"


def test_generated_session_claude_md_is_not_staged(tmp_path: Path, repo: Path) -> None:
    """Regression for the blocker an earlier review round caught: the
    orchestrator itself generates a session ``CLAUDE.md`` at the worktree
    root via ``write_claude_md`` (real header: "This file was
    auto-generated by Bernstein for this agent session."). That file must
    never be staged by ``git add -A`` -- it is never a target-repo
    deliverable, it's always bernstein's own control file at that exact
    path."""
    mgr = WorktreeManager(repo_root=repo)
    session_id = "sess-claude-md"
    worktree_path = mgr.create(session_id)

    # Exercise the real production code path, not a hand-rolled stand-in.
    write_claude_md(
        worktree_path,
        [_make_task()],
        session_id=session_id,
        role="backend",
        workdir=repo,
    )
    generated = (worktree_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert "auto-generated by Bernstein" in generated

    src_dir = worktree_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "feature.py").write_text("def feature():\n    return 42\n", encoding="utf-8")

    _git(worktree_path, "add", "-A")
    staged = _staged(worktree_path)

    assert "CLAUDE.md" not in staged, "the orchestrator-generated session CLAUDE.md must not be staged"
    assert "src/feature.py" in staged


def test_git_add_dash_a_still_stages_non_runtime_claude_dir_deliverable(tmp_path: Path, repo: Path) -> None:
    """A non-runtime file elsewhere under ``.claude/`` (a skill or command
    the agent was actually tasked to add) must still be staged -- only
    ``.claude/mcp.json`` specifically is excluded."""
    mgr = WorktreeManager(repo_root=repo)
    session_id = "sess-claude-dir-deliverable"
    worktree_path = mgr.create(session_id)

    _drop_full_deny_list_runtime_state(worktree_path, session_id)

    commands_dir = worktree_path / ".claude" / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)
    (commands_dir / "deploy.md").write_text("# /deploy command\n", encoding="utf-8")

    _git(worktree_path, "add", "-A")
    staged = _staged(worktree_path)

    assert ".claude/commands/deploy.md" in staged, "a .claude/ deliverable must not be silently dropped"
    assert ".claude/mcp.json" not in staged


def test_normal_work_commit_passes_merge_preflight_forbidden_path_guard(tmp_path: Path, repo: Path) -> None:
    """End-to-end regression for #3017: with the full deny-list of runtime
    state on disk but correctly excluded, the staged set that reaches the
    reap-and-merge preflight's forbidden-path guard (defect 28) must be
    clean -- the guard must not refuse a legitimately-finishing agent's
    commit."""
    mgr = WorktreeManager(repo_root=repo)
    session_id = "sess-preflight"
    worktree_path = mgr.create(session_id)

    src_dir = worktree_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "feature.py").write_text("def feature():\n    return 42\n", encoding="utf-8")

    _drop_full_deny_list_runtime_state(worktree_path, session_id)

    _git(worktree_path, "add", "-A")

    # This is the exact guard invoked by the reap-and-merge preflight
    # (bernstein.core.git.git_pr._verify_merge_staging_is_safe) against the
    # staged set. Empty list == safe to commit/merge.
    forbidden = git_pr._verify_merge_staging_is_safe(worktree_path, f"agent/{session_id}")
    assert forbidden == [], f"merge-preflight forbidden-path guard must not trip on runtime paths, got: {forbidden}"


def test_operator_main_checkout_unaffected_after_worktree_lifecycle(tmp_path: Path, repo: Path) -> None:
    """The scenario this review round was raised to prevent: writing the
    exclude set to a *shared* location (``.git/info/exclude``) would have
    silently changed what the operator's own main checkout picks up on
    ``git add -A`` -- e.g. a freshly-created ``bernstein.yaml`` seed config
    would stop being staged with no warning.

    After an agent worktree is created (which configures a worktree-scoped
    excludesFile covering ``bernstein.yaml``, among others) and then
    destroyed, the operator's MAIN checkout must still stage a
    newly-created ``bernstein.yaml`` normally -- proving the exclusion
    never leaked beyond the one worktree it was configured for."""
    mgr = WorktreeManager(repo_root=repo)
    session_id = "sess-isolation"
    worktree_path = mgr.create(session_id)

    # Sanity: the worktree itself does exclude bernstein.yaml.
    (worktree_path / "bernstein.yaml").write_text("in_worktree: true\n", encoding="utf-8")
    assert _git(worktree_path, "status", "--porcelain") == ""

    mgr.cleanup(session_id)

    # The operator, in their MAIN checkout, creates a fresh bernstein.yaml
    # (their own seed config) and stages everything.
    (repo / "bernstein.yaml").write_text("operator_seed_config: true\n", encoding="utf-8")
    _git(repo, "add", "-A")
    staged = _staged(repo)

    assert "bernstein.yaml" in staged, (
        "the exclusion configured for the agent worktree must not leak into "
        "the operator's main checkout -- their own bernstein.yaml must "
        "stage normally"
    )


# ---------------------------------------------------------------------------
# Best-effort contract: the exclusion is a degradation, never a blocker.
#
# ``git config --worktree`` needs ``extensions.worktreeConfig``. Per
# git-config(1), ``extensions.*`` keys are an error unless
# ``core.repositoryFormatVersion`` is 1 -- with an explicit carve-out for
# ``worktreeConfig``, which is documented as "respected regardless of the
# core.repositoryFormatVersion setting". The implementation therefore never
# bumps the format version, and a git too old to know the extension simply
# ignores an unknown extension at format version 0 (it is only fatal at
# version 1). Either way the worktree must still be created; only the
# exclusion goes missing.
# ---------------------------------------------------------------------------


def _fail_git_config_when(monkeypatch: pytest.MonkeyPatch, matches: Callable[[list[str]], bool]) -> None:
    """Make ``git config`` calls whose args satisfy *matches* report failure.

    Only the matching invocation is forced to fail; every other ``git
    config`` call still runs for real against the real repository, so the
    surrounding behaviour under test is genuine.
    """
    real = worktree_mod._run_git_config

    def _patched(worktree_path: Path, args: list[str]) -> bool:
        if matches(args):
            return False
        return real(worktree_path, args)

    monkeypatch.setattr(worktree_mod, "_run_git_config", _patched)


_EXCLUDE_SETUP_FAILURE_MODES: dict[str, Callable[[pytest.MonkeyPatch], None]] = {
    # ``git rev-parse --git-dir`` unavailable or unparseable (no git binary
    # on PATH, a git too old for ``--path-format=absolute``, a timeout).
    "git_dir_unresolvable": lambda mp: mp.setattr(worktree_mod, "_resolve_git_dir", lambda _path: None),
    # The one-time ``extensions.worktreeConfig`` write is refused: a
    # read-only or unwritable repository config, a repository format the
    # local git refuses to write extensions into, a locked config file.
    "extension_write_refused": lambda mp: _fail_git_config_when(
        mp, lambda args: any(arg.startswith("extensions.") for arg in args)
    ),
    # The worktree-scoped write itself is refused: a git predating
    # ``git config --worktree`` (added in 2.20), or one that declines the
    # extension for this repository format.
    "worktree_scoped_write_refused": lambda mp: _fail_git_config_when(mp, lambda args: "--worktree" in args),
    # The exclude file cannot be written into the per-worktree git dir.
    # Redirecting the filename through a directory that does not exist
    # produces a real ``OSError`` from a real filesystem call.
    "exclude_file_unwritable": lambda mp: mp.setattr(
        worktree_mod, "_LOCAL_EXCLUDES_FILENAME", "no-such-dir/bernstein-local-excludes"
    ),
}


@pytest.mark.parametrize("failure_mode", sorted(_EXCLUDE_SETUP_FAILURE_MODES))
def test_create_succeeds_when_local_exclude_setup_fails(
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    """Every failure in the exclude setup degrades, it never blocks.

    The original contract is best-effort: a missing exclusion costs the
    agent a graveyard diversion on the paths the merge guard denies, which
    is the behaviour that existed before this fix. Failing worktree
    creation instead would take the agent offline entirely -- strictly
    worse than the bug being fixed. So whatever goes wrong while
    configuring the exclusion, ``create()`` must still return a usable
    worktree.
    """
    _EXCLUDE_SETUP_FAILURE_MODES[failure_mode](monkeypatch)

    mgr = WorktreeManager(repo_root=repo)
    session_id = f"sess-degraded-{failure_mode.replace('_', '-')}"

    worktree_path = mgr.create(session_id)

    # The worktree exists, is a real checkout, and git works inside it.
    assert worktree_path.is_dir()
    assert (worktree_path / "README.md").read_text(encoding="utf-8") == "# repo\n"
    assert _git(worktree_path, "rev-parse", "--abbrev-ref", "HEAD").strip() == f"agent/{session_id}"
    assert _git(worktree_path, "status", "--porcelain") == ""

    # The exclusion is genuinely absent -- this asserts the failure path was
    # really taken, so the test cannot pass for the wrong reason.
    assert _local_exclude_lines(worktree_path) == set()
    (worktree_path / "bernstein.yaml").write_text("runtime: true\n", encoding="utf-8")
    src_dir = worktree_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "feature.py").write_text("def feature():\n    return 42\n", encoding="utf-8")
    _git(worktree_path, "add", "-A")
    staged = _staged(worktree_path)
    assert "bernstein.yaml" in staged, "degraded mode means the exclusion is missing, not silently still applied"
    assert "src/feature.py" in staged, "the agent can still do its actual work"

    # The worktree still tears down cleanly.
    _git(worktree_path, "reset", "-q")
    mgr.cleanup(session_id)

    # A half-applied setup must not damage the repository for anyone else.
    # ``_git`` runs with ``check=True``, so a repository the local git
    # refuses to read (the way an unrecognised extension behaves at
    # repository format version 1) would raise here rather than assert.
    # The operator's own main checkout still stages their own files.
    (repo / "operator-note.md").write_text("operator file\n", encoding="utf-8")
    _git(repo, "add", "operator-note.md")
    assert "operator-note.md" in _staged(repo)


def test_create_never_raises_the_repository_format_version(tmp_path: Path, repo: Path) -> None:
    """The extension is enabled without touching ``repositoryFormatVersion``.

    This is what keeps the failure mode above benign. ``extensions.*`` keys
    are only fatal to a git that does not recognise them when
    ``core.repositoryFormatVersion`` is 1; at version 0 an unrecognised
    extension is ignored outright. Because the fix leaves the version at
    whatever the target repo already had, a git too old to know
    ``worktreeConfig`` keeps reading and writing the repository normally --
    it just never gets the exclusion. Bumping the version here would be the
    regression: it would make the whole repository unreadable to that git.
    """
    assert _git(repo, "config", "--get", "core.repositoryformatversion").strip() == "0"

    mgr = WorktreeManager(repo_root=repo)
    worktree_path = mgr.create("sess-format-version")

    assert _git(repo, "config", "--get", "core.repositoryformatversion").strip() == "0"
    assert _git(worktree_path, "config", "--get", "core.repositoryformatversion").strip() == "0"

    # ...and at format version 0 the extension is still honoured, so the
    # exclusion really is active rather than quietly inert.
    assert _git(repo, "config", "--get", "extensions.worktreeConfig").strip() == "true"
    (worktree_path / "bernstein.yaml").write_text("runtime: true\n", encoding="utf-8")
    assert _git(worktree_path, "status", "--porcelain") == ""


def test_git_config_worktree_is_refused_without_the_extension(tmp_path: Path, repo: Path) -> None:
    """Anchor the simulated refusal above to real git behaviour.

    A worktree that never had ``extensions.worktreeConfig`` enabled makes
    real git reject ``git config --worktree`` outright. That is the exact
    condition the ``worktree_scoped_write_refused`` case stands in for, so
    the degradation test is modelled on something git actually does.
    """
    plain_worktree = tmp_path / "plain-worktree"
    _git(repo, "worktree", "add", "-q", str(plain_worktree), "-b", "plain")

    result = subprocess.run(
        ["git", "config", "--worktree", "core.excludesFile", str(tmp_path / "excludes")],
        cwd=plain_worktree,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "worktreeconfig" in result.stderr.lower()
    assert _configured_excludes_file(plain_worktree) is None


# ---------------------------------------------------------------------------
# ``extensions.worktreeConfig`` is repository-wide and permanent, and it
# changes the scope of two other keys. Per git-worktree(1): "in this file,
# the exception for core.bare and core.worktree is gone". With the extension
# off, those two keys in the shared config apply to the main worktree only;
# with it on, they apply to every linked worktree of the clone. Bernstein
# must not flip that switch on a repository that keeps either key there --
# doing so breaks worktrees it does not own, and outlives the session.
# ---------------------------------------------------------------------------


def _bare_clone_with_operator_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """A bare repo (``core.bare = true``) that already has a linked worktree."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.email", "test@example.com")
    _git(seed, "config", "user.name", "Test")
    (seed / "README.md").write_text("# repo\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "initial")

    bare = tmp_path / "bare.git"
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
    assert _git(bare, "config", "--get", "core.bare").strip() == "true"

    operator_worktree = tmp_path / "operator-worktree"
    _git(bare, "worktree", "add", "-q", str(operator_worktree), "-b", "operator")
    return bare, operator_worktree


def test_create_does_not_break_other_worktrees_of_a_bare_clone(tmp_path: Path) -> None:
    """A bare repo with linked worktrees is an ordinary git setup, and
    ``core.bare = true`` lives in its shared config. Enabling
    ``extensions.worktreeConfig`` there makes ``core.bare`` apply to every
    linked worktree, so all of them -- including the operator's own,
    created long before bernstein ran -- start failing with "this operation
    must be run in a work tree". Creating an agent worktree must not do
    that to them."""
    bare, operator_worktree = _bare_clone_with_operator_worktree(tmp_path)
    mgr = WorktreeManager(repo_root=bare)

    worktree_path = mgr.create("sess-bare-clone")

    # The operator's pre-existing worktree still works.
    assert _git(operator_worktree, "status", "--porcelain") == ""
    (operator_worktree / "operator-note.md").write_text("operator file\n", encoding="utf-8")
    _git(operator_worktree, "add", "operator-note.md")
    assert "operator-note.md" in _staged(operator_worktree)

    # So does the agent's own worktree.
    assert _git(worktree_path, "status", "--porcelain") == ""

    # The flag was never written, so nothing is left behind for the next
    # git command anyone runs against this clone.
    assert _config_value(bare, "--get", "extensions.worktreeConfig") == ""
    assert _local_exclude_lines(worktree_path) == set()


def test_create_does_not_redirect_a_submodule_checkouts_worktree(tmp_path: Path) -> None:
    """A checkout that is itself a git submodule always carries
    ``core.worktree`` in its shared config. Enabling
    ``extensions.worktreeConfig`` re-scopes that key onto the agent's linked
    worktree, which then resolves its working tree to the git dir itself:
    ``git status`` reports the repository internals as untracked and the
    real tracked files as deleted, and ``git add -A && git commit`` -- the
    exact instruction agents are given -- commits git internals. The agent's
    worktree must keep pointing at itself."""
    sub = tmp_path / "sub"
    sub.mkdir()
    _git(sub, "init", "-q", "-b", "main")
    _git(sub, "config", "user.email", "test@example.com")
    _git(sub, "config", "user.name", "Test")
    (sub / "library.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(sub, "add", "-A")
    _git(sub, "commit", "-q", "-m", "initial")

    super_repo = tmp_path / "super"
    super_repo.mkdir()
    _git(super_repo, "init", "-q", "-b", "main")
    _git(super_repo, "config", "user.email", "test@example.com")
    _git(super_repo, "config", "user.name", "Test")
    (super_repo / "top.md").write_text("# top\n", encoding="utf-8")
    _git(super_repo, "add", "-A")
    _git(super_repo, "commit", "-q", "-m", "initial")
    _git(super_repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "vendor")
    _git(super_repo, "commit", "-q", "-m", "add submodule")

    vendor = super_repo / "vendor"
    assert _git(vendor, "config", "--local", "--get", "core.worktree").strip() != ""

    mgr = WorktreeManager(repo_root=vendor)
    worktree_path = mgr.create("sess-submodule")

    toplevel = Path(_git(worktree_path, "rev-parse", "--path-format=absolute", "--show-toplevel").strip())
    assert toplevel.resolve() == worktree_path.resolve(), (
        "the agent worktree must resolve its own working tree, not the submodule git dir"
    )

    (worktree_path / "agent_work.py").write_text("def work():\n    return 1\n", encoding="utf-8")
    _git(worktree_path, "add", "-A")
    assert _staged(worktree_path) == ["agent_work.py"], "only the agent's own file may be staged"

    assert _config_value(vendor, "--local", "--get", "extensions.worktreeConfig") == ""
    assert _local_exclude_lines(worktree_path) == set()


def test_core_bare_false_is_not_treated_as_a_hazard(tmp_path: Path, repo: Path) -> None:
    """``git init`` writes ``core.bare = false`` into every ordinary
    repository's shared config. Re-scoping *that* onto linked worktrees
    changes nothing, so the common case must still get its exclusion --
    otherwise the guard above would disable the fix everywhere."""
    assert _git(repo, "config", "--local", "--get", "core.bare").strip() == "false"

    mgr = WorktreeManager(repo_root=repo)
    worktree_path = mgr.create("sess-bare-false")

    assert "/bernstein.yaml" in _local_exclude_lines(worktree_path)
