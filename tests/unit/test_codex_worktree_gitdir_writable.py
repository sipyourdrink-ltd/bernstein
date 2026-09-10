"""A codex agent in a linked worktree must be able to commit.

Finding Q (2026-09-03): a linked worktree's `.git` is a FILE pointing at
`<repo>/.git/worktrees/<id>`, outside the workspace root, so codex's
`workspace-write` sandbox refused `index.lock` and no codex agent could ever
commit. Reproduced verbatim outside pytest against codex-cli 0.152.0:
`fatal: Unable to create '.../.git/worktrees/test/index.lock': Operation not
permitted`, and fixed by `--add-dir <git-common-dir>`.

These pin the argv contract. The sandbox behaviour itself is verified by a real
codex run, not here - a mocked test would pass against the broken build.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bernstein.adapters.codex import _worktree_gitdir_roots


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _run(["git", "init", "-b", "main"], root)
    _run(["git", "config", "user.email", "t@e.com"], root)
    _run(["git", "config", "user.name", "T"], root)
    _run(["git", "config", "commit.gpgsign", "false"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _run(["git", "add", "-A"], root)
    _run(["git", "commit", "-m", "seed"], root)
    return root


def test_a_linked_worktree_yields_the_common_git_dir(repo: Path) -> None:
    wt = repo / ".sdd" / "worktrees" / "sess"
    wt.parent.mkdir(parents=True)
    _run(["git", "worktree", "add", "-b", "agent/sess", str(wt)], repo)

    roots = _worktree_gitdir_roots(wt)

    assert roots, "a linked worktree needs a writable git dir"
    assert Path(roots[0]).resolve() == (repo / ".git").resolve()
    # The per-worktree dir is nested under the common dir, so one root covers both.
    assert len(roots) == 1
    assert (Path(roots[0]) / "worktrees" / "sess").exists()


def test_a_normal_checkout_is_a_no_op(repo: Path) -> None:
    """The argv must be unchanged for an ordinary repo."""
    assert _worktree_gitdir_roots(repo) == []


def test_a_non_repository_is_a_no_op(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _worktree_gitdir_roots(plain) == []
    assert _worktree_gitdir_roots(tmp_path / "missing") == []


def _spawn_capturing_argv(
    wt: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    host_isolation: str | None = None,
) -> list[str]:
    """Spawn against a fake `codex` and return the argv it was handed."""
    from bernstein.core.models import ModelConfig

    from bernstein.adapters import codex as codex_mod

    captured: dict[str, list[str]] = {}
    real_popen = subprocess.Popen

    class _Proc:
        pid = 4242

        def poll(self) -> None:
            return None

    def _fake_popen(cmd: list[str], **kw: object) -> object:
        if not cmd or "codex" not in str(cmd[0]):
            return real_popen(cmd, **kw)  # type: ignore[arg-type]
        captured["cmd"] = list(cmd)
        return _Proc()

    monkeypatch.setattr(codex_mod.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(codex_mod, "build_worker_cmd", lambda cmd, **_k: list(cmd))

    adapter = codex_mod.CodexAdapter()
    if host_isolation is not None:
        adapter.host_isolation = host_isolation
    adapter.spawn(
        prompt="p",
        workdir=wt,
        model_config=ModelConfig(model="gpt-5.6-sol", effort="high"),
        session_id="resolver-abc12345",
    )
    return captured["cmd"]


def test_a_bypassed_sandbox_adds_no_writable_roots(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A declared container/vm host drops the vendor sandbox (#5341), and with
    no workspace root to extend the roots would be inert argv. The bypass
    invocation must stay exactly what it is today."""
    wt = repo / ".sdd" / "worktrees" / "sess3"
    wt.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "-b", "agent/sess3", str(wt)], repo)

    cmd = _spawn_capturing_argv(wt, monkeypatch, host_isolation="container")

    assert "--dangerously-bypass-approvals-and-sandbox" in cmd, cmd
    assert "--add-dir" not in cmd, cmd


def test_the_spawn_argv_carries_add_dir_for_a_worktree(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End of the contract: the flag reaches `codex exec`, repeatable form."""
    from bernstein.adapters import codex as codex_mod

    wt = repo / ".sdd" / "worktrees" / "sess2"
    wt.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "-b", "agent/sess2", str(wt)], repo)

    captured: dict[str, list[str]] = {}
    real_popen = subprocess.Popen

    class _Proc:
        pid = 4242

        def poll(self) -> None:
            return None

    def _fake_popen(cmd: list[str], **kw: object) -> object:
        # `_worktree_gitdir_roots` shells out to git through subprocess.run,
        # which goes through Popen too - let those run for real.
        if not cmd or "codex" not in str(cmd[0]):
            return real_popen(cmd, **kw)  # type: ignore[arg-type]
        captured["cmd"] = list(cmd)
        return _Proc()

    monkeypatch.setattr(codex_mod.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(codex_mod, "build_worker_cmd", lambda cmd, **_k: list(cmd))

    from bernstein.core.models import ModelConfig

    codex_mod.CodexAdapter().spawn(
        prompt="p",
        workdir=wt,
        model_config=ModelConfig(model="gpt-5.6-sol", effort="high"),
        session_id="resolver-abc12345",
    )

    cmd = captured["cmd"]
    assert "--add-dir" in cmd, cmd
    assert str((repo / ".git").resolve()) in [str(Path(c).resolve()) for c in cmd if c.startswith("/")]
    assert cmd.index("--add-dir") > cmd.index("workspace-write")
