"""Tests for per-task Claude Code skill injection."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from bernstein.core.models import Task

from bernstein import _BUNDLED_TEMPLATES_DIR
from bernstein.adapters.skills_injector import (
    ROLE_SKILL_MAP,
    inject_skills,
    render_skill_template,
)

# A curl POST to a /tasks/<id>/complete endpoint on a single line - the
# fragile, unauthenticated, loopback-hardcoded shape issue #3035 calls out.
_RAW_CURL_COMPLETE = re.compile(r"curl[^\n]*/tasks/\S*?/complete", re.IGNORECASE)


def _make_task(id: str = "T-001", title: str = "Test task") -> Task:
    return Task(id=id, title=title, description="A test task", role="backend")


class TestRenderSkillTemplate:
    def test_replaces_session_id(self) -> None:
        content = "Check signals at .sdd/runtime/signals/{{SESSION_ID}}/WAKEUP"
        result = render_skill_template(content, session_id="backend-abc123")
        assert "backend-abc123" in result
        assert "{{SESSION_ID}}" not in result

    def test_replaces_complete_cmds_with_task_complete_cli(self) -> None:
        """Issue #3035 - completion commands use the `task complete` CLI front
        door, not a raw curl (which carries no auth header and hardcodes
        127.0.0.1:8052 - see TestNoRawCurlInRealInjectedSkills below).
        """
        tasks = [_make_task(id="T-001", title="Fix bug")]
        result = render_skill_template("{{COMPLETE_CMDS}}", tasks=tasks)
        assert "T-001" in result
        assert "bernstein task complete" in result
        assert _RAW_CURL_COMPLETE.search(result) is None

    def test_replaces_complete_cmds_for_multiple_tasks(self) -> None:
        tasks = [
            _make_task(id="T-001", title="First task"),
            _make_task(id="T-002", title="Second task"),
        ]
        result = render_skill_template("{{COMPLETE_CMDS}}", tasks=tasks)
        assert "T-001" in result
        assert "T-002" in result

    def test_replaces_task_ids(self) -> None:
        tasks = [_make_task(id="T-001"), _make_task(id="T-002")]
        result = render_skill_template("Tasks: {{TASK_IDS}}", tasks=tasks)
        assert "T-001" in result
        assert "T-002" in result

    def test_no_tasks_produces_placeholder_comment(self) -> None:
        result = render_skill_template("{{COMPLETE_CMDS}}", tasks=[])
        assert "No task IDs available" in result

    def test_empty_template_unchanged(self) -> None:
        result = render_skill_template("", session_id="s-1")
        assert result == ""

    def test_unknown_placeholders_left_intact(self) -> None:
        result = render_skill_template("{{UNKNOWN_TOKEN}}", session_id="s-1")
        assert "{{UNKNOWN_TOKEN}}" in result


class TestInjectSkills:
    def _make_skills_dir(self, tmp_path: Path) -> Path:
        """Create a minimal templates/skills/ directory."""
        skills_dir = tmp_path / "templates" / "skills"
        skills_dir.mkdir(parents=True)

        (skills_dir / "bernstein-completion-protocol.md").write_text(
            "---\nname: bernstein-completion-protocol\n"
            "description: Report task completion\n"
            "whenToUse: When finished\n---\n"
            "Complete tasks: {{COMPLETE_CMDS}}\n",
            encoding="utf-8",
        )
        (skills_dir / "bernstein-signal-check.md").write_text(
            "---\nname: bernstein-signal-check\n"
            "description: Check signals\n"
            "whenToUse: Periodically\n---\n"
            "Signals at {{SESSION_ID}}\n",
            encoding="utf-8",
        )
        (skills_dir / "bernstein-test-runner.md").write_text(
            "---\nname: bernstein-test-runner\n"
            "description: Run tests\n"
            "whenToUse: When testing\n---\n"
            "Run tests with uv.\n",
            encoding="utf-8",
        )
        (skills_dir / "bernstein-commit-protocol.md").write_text(
            "---\nname: bernstein-commit-protocol\n"
            "description: Commit conventions\n"
            "whenToUse: When committing\n---\n"
            "Use main branch.\n",
            encoding="utf-8",
        )
        return tmp_path / "templates" / "roles"  # templates_dir (roles subdirectory)

    def test_creates_skills_directory(self, tmp_path: Path) -> None:
        templates_dir = self._make_skills_dir(tmp_path)
        workdir = tmp_path / "workdir"
        workdir.mkdir()

        inject_skills(
            workdir=workdir,
            role="backend",
            tasks=[_make_task()],
            session_id="backend-abc",
            templates_dir=templates_dir,
        )

        assert (workdir / ".claude" / "skills").is_dir()

    def test_always_injects_completion_protocol(self, tmp_path: Path) -> None:
        templates_dir = self._make_skills_dir(tmp_path)
        workdir = tmp_path / "workdir"
        workdir.mkdir()

        inject_skills(workdir=workdir, role="docs", tasks=[], session_id="s-1", templates_dir=templates_dir)

        assert (workdir / ".claude" / "skills" / "bernstein-completion-protocol.md").exists()

    def test_always_injects_signal_check(self, tmp_path: Path) -> None:
        templates_dir = self._make_skills_dir(tmp_path)
        workdir = tmp_path / "workdir"
        workdir.mkdir()

        inject_skills(workdir=workdir, role="qa", tasks=[], session_id="s-2", templates_dir=templates_dir)

        assert (workdir / ".claude" / "skills" / "bernstein-signal-check.md").exists()

    def test_backend_role_gets_test_runner(self, tmp_path: Path) -> None:
        templates_dir = self._make_skills_dir(tmp_path)
        workdir = tmp_path / "workdir"
        workdir.mkdir()

        inject_skills(workdir=workdir, role="backend", tasks=[], session_id="s-3", templates_dir=templates_dir)

        assert (workdir / ".claude" / "skills" / "bernstein-test-runner.md").exists()

    def test_backend_role_gets_commit_protocol(self, tmp_path: Path) -> None:
        templates_dir = self._make_skills_dir(tmp_path)
        workdir = tmp_path / "workdir"
        workdir.mkdir()

        inject_skills(workdir=workdir, role="backend", tasks=[], session_id="s-4", templates_dir=templates_dir)

        assert (workdir / ".claude" / "skills" / "bernstein-commit-protocol.md").exists()

    def test_qa_role_gets_test_runner_only(self, tmp_path: Path) -> None:
        templates_dir = self._make_skills_dir(tmp_path)
        workdir = tmp_path / "workdir"
        workdir.mkdir()

        inject_skills(workdir=workdir, role="qa", tasks=[], session_id="s-5", templates_dir=templates_dir)

        skills_dir = workdir / ".claude" / "skills"
        assert (skills_dir / "bernstein-test-runner.md").exists()
        assert not (skills_dir / "bernstein-commit-protocol.md").exists()

    def test_session_id_rendered_in_signal_check(self, tmp_path: Path) -> None:
        templates_dir = self._make_skills_dir(tmp_path)
        workdir = tmp_path / "workdir"
        workdir.mkdir()

        inject_skills(workdir=workdir, role="qa", tasks=[], session_id="qa-deadbeef", templates_dir=templates_dir)

        content = (workdir / ".claude" / "skills" / "bernstein-signal-check.md").read_text()
        assert "qa-deadbeef" in content

    def test_task_ids_rendered_in_completion_protocol(self, tmp_path: Path) -> None:
        templates_dir = self._make_skills_dir(tmp_path)
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        tasks = [_make_task(id="T-99", title="My task")]

        inject_skills(workdir=workdir, role="backend", tasks=tasks, session_id="s-6", templates_dir=templates_dir)

        content = (workdir / ".claude" / "skills" / "bernstein-completion-protocol.md").read_text()
        assert "T-99" in content

    def test_missing_templates_dir_skips_gracefully(self, tmp_path: Path) -> None:
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        # templates_dir points to a directory with no sibling skills/
        templates_dir = tmp_path / "templates" / "roles"

        # Should not raise
        inject_skills(workdir=workdir, role="backend", tasks=[], session_id="s-7", templates_dir=templates_dir)

        assert not (workdir / ".claude" / "skills").exists()

    def test_unknown_role_gets_only_always_inject_skills(self, tmp_path: Path) -> None:
        templates_dir = self._make_skills_dir(tmp_path)
        workdir = tmp_path / "workdir"
        workdir.mkdir()

        inject_skills(workdir=workdir, role="unknown_role", tasks=[], session_id="s-8", templates_dir=templates_dir)

        skills_dir = workdir / ".claude" / "skills"
        assert (skills_dir / "bernstein-completion-protocol.md").exists()
        assert (skills_dir / "bernstein-signal-check.md").exists()
        assert not (skills_dir / "bernstein-test-runner.md").exists()

    def test_skills_have_valid_frontmatter(self, tmp_path: Path) -> None:
        """Injected skills must have name, description, and whenToUse frontmatter."""
        templates_dir = self._make_skills_dir(tmp_path)
        workdir = tmp_path / "workdir"
        workdir.mkdir()

        inject_skills(workdir=workdir, role="backend", tasks=[], session_id="s-9", templates_dir=templates_dir)

        skills_dir = workdir / ".claude" / "skills"
        for skill_file in skills_dir.iterdir():
            content = skill_file.read_text()
            assert content.startswith("---"), f"{skill_file.name} missing frontmatter"
            assert "name:" in content, f"{skill_file.name} missing 'name' field"
            assert "description:" in content, f"{skill_file.name} missing 'description' field"
            assert "whenToUse:" in content, f"{skill_file.name} missing 'whenToUse' field"


class TestGitExcludeRegistration:
    """Injected skills must be excluded from git so agents never commit them.

    See https://github.com/sipyourdrink-ltd/bernstein/issues/2187 - every
    worker's rendered copy differs (session id, task ids), so committing
    them causes a merge conflict on every worker merge back to the shared
    work branch.
    """

    def _make_skills_dir(self, tmp_path: Path) -> Path:
        skills_dir = tmp_path / "templates" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "bernstein-completion-protocol.md").write_text(
            "---\nname: bernstein-completion-protocol\n"
            "description: Report task completion\n"
            "whenToUse: When finished\n---\n"
            "Complete tasks: {{COMPLETE_CMDS}}\n",
            encoding="utf-8",
        )
        (skills_dir / "bernstein-signal-check.md").write_text(
            "---\nname: bernstein-signal-check\n"
            "description: Check signals\n"
            "whenToUse: Periodically\n---\n"
            "Signals at {{SESSION_ID}}\n",
            encoding="utf-8",
        )
        return tmp_path / "templates" / "roles"

    def _init_git_repo(self, path: Path) -> None:
        subprocess.run(["git", "init", "-q"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)

    def test_injection_adds_exclude_entries(self, tmp_path: Path) -> None:
        templates_dir = self._make_skills_dir(tmp_path)
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        self._init_git_repo(workdir)

        inject_skills(
            workdir=workdir,
            role="docs",
            tasks=[],
            session_id="s-1",
            templates_dir=templates_dir,
        )

        exclude_file = workdir / ".git" / "info" / "exclude"
        assert exclude_file.exists()
        content = exclude_file.read_text()
        assert ".claude/skills/bernstein-completion-protocol.md" in content
        assert ".claude/skills/bernstein-signal-check.md" in content

    def test_reinjection_does_not_duplicate_exclude_entries(self, tmp_path: Path) -> None:
        templates_dir = self._make_skills_dir(tmp_path)
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        self._init_git_repo(workdir)

        for session in ("s-1", "s-2"):
            inject_skills(
                workdir=workdir,
                role="docs",
                tasks=[],
                session_id=session,
                templates_dir=templates_dir,
            )

        exclude_file = workdir / ".git" / "info" / "exclude"
        content = exclude_file.read_text()
        assert content.count(".claude/skills/bernstein-completion-protocol.md") == 1
        assert content.count(".claude/skills/bernstein-signal-check.md") == 1

    def test_works_when_git_is_a_gitfile_worktree(self, tmp_path: Path) -> None:
        """In a git worktree, ``.git`` is a file pointing at the real gitdir."""
        templates_dir = self._make_skills_dir(tmp_path)
        main_repo = tmp_path / "main-repo"
        main_repo.mkdir()
        self._init_git_repo(main_repo)
        (main_repo / "README.md").write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=main_repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=main_repo, check=True)

        worktree_path = tmp_path / "agent-worktree"
        subprocess.run(
            ["git", "worktree", "add", "-b", "agent-branch", str(worktree_path)],
            cwd=main_repo,
            check=True,
        )

        assert (worktree_path / ".git").is_file(), "worktree .git must be a gitfile, not a dir"

        inject_skills(
            workdir=worktree_path,
            role="docs",
            tasks=[],
            session_id="s-worktree",
            templates_dir=templates_dir,
        )

        result = subprocess.run(
            ["git", "rev-parse", "--git-path", "info/exclude"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            check=True,
        )
        exclude_file = Path(result.stdout.strip())
        if not exclude_file.is_absolute():
            exclude_file = worktree_path / exclude_file

        assert exclude_file.exists()
        content = exclude_file.read_text()
        assert ".claude/skills/bernstein-completion-protocol.md" in content
        # `info/exclude` is repo-wide, resolved via the main repo's real
        # gitdir rather than a naively-assumed `worktree_path/.git/info/exclude`
        # (which does not exist - `.git` in a worktree is a file, not a dir).
        assert str(exclude_file) != str(worktree_path / ".git" / "info" / "exclude")
        assert not (worktree_path / ".git").is_dir()

    def test_injected_skills_still_readable_after_exclusion(self, tmp_path: Path) -> None:
        templates_dir = self._make_skills_dir(tmp_path)
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        self._init_git_repo(workdir)

        inject_skills(
            workdir=workdir,
            role="docs",
            tasks=[],
            session_id="s-1",
            templates_dir=templates_dir,
        )

        skill_file = workdir / ".claude" / "skills" / "bernstein-completion-protocol.md"
        assert skill_file.exists()
        assert "Complete tasks" in skill_file.read_text()

    def test_non_git_workdir_does_not_raise(self, tmp_path: Path) -> None:
        """Missing git repo must not block skill injection (best-effort)."""
        templates_dir = self._make_skills_dir(tmp_path)
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        # Deliberately not a git repo.

        inject_skills(
            workdir=workdir,
            role="docs",
            tasks=[],
            session_id="s-1",
            templates_dir=templates_dir,
        )

        assert (workdir / ".claude" / "skills" / "bernstein-completion-protocol.md").exists()
        assert not (workdir / ".git").exists()


class TestRoleSkillMap:
    def test_backend_has_test_runner_and_commit(self) -> None:
        assert "bernstein-test-runner.md" in ROLE_SKILL_MAP["backend"]
        assert "bernstein-commit-protocol.md" in ROLE_SKILL_MAP["backend"]

    def test_qa_has_test_runner(self) -> None:
        assert "bernstein-test-runner.md" in ROLE_SKILL_MAP["qa"]

    def test_docs_has_commit_protocol(self) -> None:
        assert "bernstein-commit-protocol.md" in ROLE_SKILL_MAP["docs"]


class TestRevokedSkillGuard:
    """Spawn-side kill switch: a signed revocation refuses injection (issue #2527)."""

    def _make_skills_dir(self, tmp_path: Path) -> Path:
        skills_dir = tmp_path / "templates" / "skills"
        skills_dir.mkdir(parents=True)
        for name in ("bernstein-completion-protocol", "bernstein-signal-check", "bernstein-test-runner"):
            (skills_dir / f"{name}.md").write_text(
                f"---\nname: {name}\ndescription: d\n---\nBody.\n",
                encoding="utf-8",
            )
        return tmp_path / "templates" / "roles"

    def _seed_revocation(self, workdir: Path, skill_id: str) -> None:
        from bernstein.core.skills.catalog.fetcher import SkillCatalogFetcher, default_cache_path
        from bernstein.core.skills.catalog.lockfile import CatalogLockEntry, upsert_catalog_install
        from bernstein.core.skills.catalog.revocation import RevocationEntry, sign_revocation
        from bernstein.core.skills.catalog.signature import generate_signer_keypair

        priv, pub = generate_signer_keypair()
        upsert_catalog_install(
            workdir / "skills.lock",
            CatalogLockEntry(
                id=skill_id,
                name=skill_id,
                version="1.0.0",
                manifest_url=f"github://acme/{skill_id}@v1.0.0",
                manifest_sha256="a" * 64,
                content_digest="b" * 64,
                install_id="deadbeef",
                chain_head="c" * 64,
                installed_at="2026-07-16T00:00:00Z",
            ),
            workdir=workdir,
        )
        revocation = sign_revocation(
            RevocationEntry(skill_id=skill_id, version_range="*", reason="CVE", issued_at="2026-07-16T00:00:00Z"),
            priv,
        )
        SkillCatalogFetcher(cache_path=default_cache_path()).write_cache_payload(
            {
                "version": 1,
                "generated_at": "2026-07-16T00:00:00Z",
                "signer_pubkey": pub,
                "entries": [
                    {
                        "id": skill_id,
                        "name": skill_id,
                        "version": "1.0.0",
                        "description": "d",
                        "source": {"kind": "github", "repo": f"acme/{skill_id}", "tag": "v1.0.0"},
                        "content_digest": "b" * 64,
                        "verified": True,
                    }
                ],
                "revocations": [revocation.to_dict()],
            }
        )

    def test_revoked_skill_is_not_injected_and_receipt_recorded(self, tmp_path: Path, monkeypatch: object) -> None:
        from bernstein.core.security.audit import AuditLog

        monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))  # type: ignore[attr-defined]
        templates_dir = self._make_skills_dir(tmp_path)
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        self._seed_revocation(workdir, "bernstein-test-runner")

        inject_skills(
            workdir=workdir,
            role="backend",  # backend injects bernstein-test-runner
            tasks=[],
            session_id="s-revoked",
            templates_dir=templates_dir,
        )

        skills_out = workdir / ".claude" / "skills"
        # The revoked skill is refused; an unaffected always-injected skill lands.
        assert not (skills_out / "bernstein-test-runner.md").exists()
        assert (skills_out / "bernstein-completion-protocol.md").exists()

        log = AuditLog(workdir / ".sdd" / "audit")
        events = log.query(event_type="skill.verification_refusal")
        assert any(e.details["stage"] == "spawn" for e in events)


def _all_shipped_roles() -> list[str]:
    """Every role template shipped under ``templates/roles/`` today.

    Includes, but is not limited to, the manager/backend/qa/security/
    reviewer/docs roles issue #3035 verified by name - discovering the list
    dynamically means a newly added role is covered automatically instead of
    silently falling outside this regression guard.
    """
    roles_dir = _BUNDLED_TEMPLATES_DIR / "roles"
    return sorted(p.name for p in roles_dir.iterdir() if p.is_dir() and not p.name.startswith("_"))


class TestNoRawCurlInRealInjectedSkills:
    """Issue #3035 regression guard.

    Runs the REAL ``inject_skills`` path against the shipped
    ``templates/skills/`` directory (not a fixture double built by this test
    file) so a regression in the bundled completion skill fails a test
    instead of shipping into every spawned agent's ``.claude/skills/``
    unnoticed - which is exactly what happened before this fix: the
    coherence guard added for #3021/#3015 only inspected the rendered
    *prompt* (``_render_prompt`` + ``_render_auth_section``), never the
    files ``inject_skills`` writes to disk.
    """

    def test_role_discovery_did_not_collapse(self) -> None:
        """Guard the guard, part 1: ``@pytest.mark.parametrize("role", _all_shipped_roles())``
        silently generates ZERO cases - and the parametrized test below then
        silently "passes" by not existing - if ``_all_shipped_roles()`` ever
        returns an empty (or near-empty) list, e.g. because
        ``_BUNDLED_TEMPLATES_DIR`` resolves to the wrong path. This regression
        guard is exactly the kind that must fail loudly if its input set
        collapses, so assert a floor well below the 19 roles shipped today
        rather than relying on the subset-check below to catch it indirectly.
        """
        discovered = _all_shipped_roles()
        assert len(discovered) >= 10, (
            f"role discovery found only {len(discovered)} role(s) {discovered} - "
            "the parametrized no-raw-curl sweep below would silently cover almost "
            "nothing; check _BUNDLED_TEMPLATES_DIR / 'roles' resolves correctly"
        )

    def test_at_least_the_verified_roles_are_covered(self) -> None:
        """Guard the guard, part 2: issue #3035 was verified for these roles by
        name; they must still exist and be swept by the parametrized test
        below."""
        verified = {"manager", "backend", "qa", "security", "reviewer", "docs"}
        missing = verified - set(_all_shipped_roles())
        assert not missing, f"roles #3035 verified but no longer shipped: {sorted(missing)}"

    @pytest.mark.parametrize("role", _all_shipped_roles())
    def test_injected_completion_skill_has_no_raw_curl(self, role: str, tmp_path: Path) -> None:
        templates_dir = _BUNDLED_TEMPLATES_DIR / "roles"
        workdir = tmp_path / role
        workdir.mkdir()
        tasks = [_make_task(id="T-3035", title="Fix the completion skill")]

        inject_skills(
            workdir=workdir,
            role=role,
            tasks=tasks,
            session_id=f"{role}-session",
            templates_dir=templates_dir,
        )

        skill_path = workdir / ".claude" / "skills" / "bernstein-completion-protocol.md"
        assert skill_path.exists(), f"{role}: completion protocol skill was not injected"
        content = skill_path.read_text(encoding="utf-8")

        match = _RAW_CURL_COMPLETE.search(content)
        assert match is None, (
            f"{role}: raw-curl completion still present in injected skill -> {match and match.group(0)!r}"
        )
        assert "bernstein task complete" in content, f"{role}: CLI completion instruction missing"
