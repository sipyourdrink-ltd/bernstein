"""Activation log integration via ``inject_skills`` (#1720, Track 5 floor)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bernstein.core.models import Task

from bernstein.adapters import skills_injector
from bernstein.adapters.skills_injector import inject_skills, render_skill_template
from bernstein.core.skills.activation_log import ENV_VAR, activation_log_path
from bernstein.core.skills.sanitizer import sanitize_skill_body
from bernstein.core.skills.selection_rules import SELECTION_RULES_FILENAME


def _make_skill_templates(root: Path) -> Path:
    """Mirror the minimal template tree used by the existing injector tests."""
    skills_dir = root / "templates" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "bernstein-completion-protocol.md").write_text(
        "---\nname: bernstein-completion-protocol\n"
        "description: Report task completion to the orchestrator after every task.\n"
        "version: 1.2.3\n---\n\n# Completion\nComplete: {{COMPLETE_CMDS}}\n",
        encoding="utf-8",
    )
    (skills_dir / "bernstein-signal-check.md").write_text(
        "---\nname: bernstein-signal-check\n"
        "description: Poll the runtime signal directory for wake-up requests.\n"
        "version: 2.0.0\n---\n\n# Signal\nSignals at {{SESSION_ID}}\n",
        encoding="utf-8",
    )
    (skills_dir / "pytest-helper.md").write_text(
        "---\nname: pytest-helper\n"
        "description: Use for pytest regressions, failing unit tests, and test isolation work.\n"
        "trigger_keywords:\n"
        "  - pytest\n"
        "  - regression\n"
        "version: 1.0.0\n---\n\n# Pytest helper\nUse this for pytest regression fixes.\n",
        encoding="utf-8",
    )
    return root / "templates" / "roles"


def _make_task(task_id: str = "T-001") -> Task:
    return Task(id=task_id, title="Test task", description="A test task", role="backend")


def test_inject_skills_writes_activation_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each injected skill produces one activation log line per task."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    templates_dir = _make_skill_templates(tmp_path)
    workdir = tmp_path / "workdir"
    workdir.mkdir()

    inject_skills(
        workdir=workdir,
        role="security",  # role with no extra skills -> only always-inject pair
        tasks=[_make_task("T-001")],
        session_id="sec-abc",
        templates_dir=templates_dir,
    )

    log_path = activation_log_path(workdir)
    assert log_path.is_file()
    lines = [json.loads(line) for line in log_path.read_text().splitlines()]
    # Two always-inject skills, one task = two activation records. The
    # exact count guard catches duplicate-log regressions that a
    # set-equality check would silently swallow.
    assert len(lines) == 2
    skills_seen = {row["skill"] for row in lines}
    assert skills_seen == {"bernstein-completion-protocol", "bernstein-signal-check"}
    for row in lines:
        assert row["role"] == "security"
        assert row["task_id"] == "T-001"
        assert row["trigger_source"] == "role-binding"
        assert row["digest"]  # non-empty
        assert row["timestamp"].endswith("Z")


def test_inject_skills_respects_activation_log_env_opt_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting ``BERNSTEIN_SKILL_ACTIVATION_LOG=0`` suppresses log writes."""
    monkeypatch.setenv(ENV_VAR, "0")
    templates_dir = _make_skill_templates(tmp_path)
    workdir = tmp_path / "workdir"
    workdir.mkdir()

    inject_skills(
        workdir=workdir,
        role="security",
        tasks=[_make_task("T-002")],
        session_id="sec-def",
        templates_dir=templates_dir,
    )

    # Skills are still injected -> .claude/skills/ exists with the files.
    assert (workdir / ".claude" / "skills" / "bernstein-completion-protocol.md").is_file()
    # But the activation log is not created.
    assert not activation_log_path(workdir).exists()


def test_inject_skills_auto_route_is_off_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TF-IDF auto-routing is opt-in; role binding remains the default."""
    monkeypatch.delenv("BERNSTEIN_SKILLS_AUTO_ROUTE", raising=False)
    templates_dir = _make_skill_templates(tmp_path)
    workdir = tmp_path / "workdir"
    workdir.mkdir()

    inject_skills(
        workdir=workdir,
        role="security",
        tasks=[
            Task(id="T-003", title="Fix pytest regression", description="The pytest suite is failing.", role="security")
        ],
        session_id="sec-ghi",
        templates_dir=templates_dir,
    )

    assert not (workdir / ".claude" / "skills" / "pytest-helper.md").exists()


def test_inject_skills_auto_route_adds_matching_skill_when_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opt-in TF-IDF routing injects deterministic task-matched skills."""
    monkeypatch.setenv("BERNSTEIN_SKILLS_AUTO_ROUTE", "1")
    templates_dir = _make_skill_templates(tmp_path)
    workdir = tmp_path / "workdir"
    workdir.mkdir()

    inject_skills(
        workdir=workdir,
        role="security",
        tasks=[
            Task(id="T-004", title="Fix pytest regression", description="The pytest suite is failing.", role="security")
        ],
        session_id="sec-jkl",
        templates_dir=templates_dir,
    )

    assert (workdir / ".claude" / "skills" / "pytest-helper.md").is_file()
    rows = [json.loads(line) for line in activation_log_path(workdir).read_text(encoding="utf-8").splitlines()]
    auto_rows = [row for row in rows if row["skill"] == "pytest-helper"]
    assert len(auto_rows) == 1
    assert auto_rows[0]["trigger_source"] == "auto-route"


def test_no_rule_table_is_byte_identical_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Merge-deciding property of issue #3383: absent table == no rule layer.

    With no ``selection-rules.yaml`` on disk the injector must behave
    byte-identically to a build without the rule layer: the loader is
    never invoked (existence is one cheap stat), the injected file set
    and bytes are exactly what the unchanged sanitize+render pipeline
    produces for role binding alone, and the activation log carries
    only ``role-binding`` lines.
    """
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.delenv("BERNSTEIN_SKILLS_AUTO_ROUTE", raising=False)
    loader_calls = {"count": 0}
    real_loader = skills_injector.load_selection_rules

    def _counting_loader(*args: object, **kwargs: object) -> object:
        loader_calls["count"] += 1
        return real_loader(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(skills_injector, "load_selection_rules", _counting_loader)

    templates_dir = _make_skill_templates(tmp_path)
    skills_source_dir = templates_dir.parent / "skills"
    assert not (skills_source_dir / SELECTION_RULES_FILENAME).exists()
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    task = _make_task("T-100")

    inject_skills(
        workdir=workdir,
        role="security",
        tasks=[task],
        session_id="sec-noop",
        templates_dir=templates_dir,
    )

    # No rule table on disk -> the loader is never invoked.
    assert loader_calls["count"] == 0

    # Injected file set is exactly the role-derived pair - no new writes.
    out_dir = workdir / ".claude" / "skills"
    expected_names = ["bernstein-completion-protocol.md", "bernstein-signal-check.md"]
    assert sorted(p.name for p in out_dir.iterdir()) == expected_names

    # Bytes are identical to the pre-rule-layer sanitize+render pipeline.
    for name in expected_names:
        source_path = skills_source_dir / name
        expected_bytes = render_skill_template(
            sanitize_skill_body(
                source_path.read_text(encoding="utf-8"),
                skill_name=name,
                origin=str(source_path),
                source_name="templates/skills",
            ),
            session_id="sec-noop",
            tasks=[task],
        ).encode("utf-8")
        assert (out_dir / name).read_bytes() == expected_bytes

    # Activation log: one role-binding line per injected skill, nothing else.
    rows = [json.loads(line) for line in activation_log_path(workdir).read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    assert {row["skill"] for row in rows} == {"bernstein-completion-protocol", "bernstein-signal-check"}
    assert {row["trigger_source"] for row in rows} == {"role-binding"}


def test_rule_selected_skill_logs_trigger_source_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rule-selected template is injected with trigger_source ``rule``."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.delenv("BERNSTEIN_SKILLS_AUTO_ROUTE", raising=False)
    templates_dir = _make_skill_templates(tmp_path)
    skills_source_dir = templates_dir.parent / "skills"
    (skills_source_dir / SELECTION_RULES_FILENAME).write_text(
        "rules:\n  - owned_files: 'tests/*.py'\n    skills: [pytest-helper]\n",
        encoding="utf-8",
    )
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    task = Task(
        id="T-200",
        title="Refactor helpers",
        description="Cleanup",
        role="security",
        owned_files=["tests/test_api.py"],
    )

    inject_skills(
        workdir=workdir,
        role="security",
        tasks=[task],
        session_id="sec-rule",
        templates_dir=templates_dir,
    )

    assert (workdir / ".claude" / "skills" / "pytest-helper.md").is_file()
    rows = [json.loads(line) for line in activation_log_path(workdir).read_text(encoding="utf-8").splitlines()]
    rule_rows = [row for row in rows if row["skill"] == "pytest-helper"]
    assert len(rule_rows) == 1
    assert rule_rows[0]["trigger_source"] == "rule"
    assert rule_rows[0]["task_id"] == "T-200"


def test_role_binding_wins_for_template_selected_by_both(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A template selected by role map and rules keeps the role-binding trigger."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.delenv("BERNSTEIN_SKILLS_AUTO_ROUTE", raising=False)
    templates_dir = _make_skill_templates(tmp_path)
    skills_source_dir = templates_dir.parent / "skills"
    (skills_source_dir / "bernstein-test-runner.md").write_text(
        "---\nname: bernstein-test-runner\n"
        "description: Run the project test suite before completion.\n"
        "version: 1.0.0\n---\n\n# Test runner\nRun tests with uv.\n",
        encoding="utf-8",
    )
    (skills_source_dir / SELECTION_RULES_FILENAME).write_text(
        "rules:\n  - owned_files: 'src/*.py'\n    skills: [bernstein-test-runner]\n",
        encoding="utf-8",
    )
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    task = Task(
        id="T-300",
        title="Implement feature",
        description="Feature work",
        role="qa",
        owned_files=["src/feature.py"],
    )

    inject_skills(
        workdir=workdir,
        role="qa",  # qa role-binds bernstein-test-runner.md
        tasks=[task],
        session_id="qa-both",
        templates_dir=templates_dir,
    )

    assert (workdir / ".claude" / "skills" / "bernstein-test-runner.md").is_file()
    rows = [json.loads(line) for line in activation_log_path(workdir).read_text(encoding="utf-8").splitlines()]
    runner_rows = [row for row in rows if row["skill"] == "bernstein-test-runner"]
    # Exactly one activation line, and role binding wins the trigger.
    assert len(runner_rows) == 1
    assert runner_rows[0]["trigger_source"] == "role-binding"


def test_auto_route_excludes_rule_selected_templates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule-selected templates are excluded from TF-IDF scoring, trigger stays ``rule``."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.setenv("BERNSTEIN_SKILLS_AUTO_ROUTE", "1")
    templates_dir = _make_skill_templates(tmp_path)
    skills_source_dir = templates_dir.parent / "skills"
    (skills_source_dir / SELECTION_RULES_FILENAME).write_text(
        "rules:\n  - owned_files: 'tests/*.py'\n    skills: [pytest-helper]\n",
        encoding="utf-8",
    )
    captured: dict[str, list[str]] = {}
    real_auto_route = skills_injector.select_auto_route_templates

    def _spying_auto_route(*args: object, **kwargs: object) -> object:
        captured["excluded"] = list(kwargs["excluded_templates"])  # type: ignore[arg-type]
        return real_auto_route(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(skills_injector, "select_auto_route_templates", _spying_auto_route)
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    task = Task(
        id="T-400",
        title="Fix pytest regression",
        description="The pytest suite is failing.",
        role="security",
        owned_files=["tests/test_api.py"],
    )

    inject_skills(
        workdir=workdir,
        role="security",
        tasks=[task],
        session_id="sec-excl",
        templates_dir=templates_dir,
    )

    # The rule-selected template was passed to the auto-route as excluded,
    # so TF-IDF neither re-scored nor duplicated it.
    assert "pytest-helper.md" in captured["excluded"]
    assert (workdir / ".claude" / "skills" / "pytest-helper.md").is_file()
    rows = [json.loads(line) for line in activation_log_path(workdir).read_text(encoding="utf-8").splitlines()]
    helper_rows = [row for row in rows if row["skill"] == "pytest-helper"]
    assert len(helper_rows) == 1
    assert helper_rows[0]["trigger_source"] == "rule"


def test_inject_skills_auto_route_ignores_malformed_frontmatter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed skill template does not abort deterministic routing."""
    monkeypatch.setenv("BERNSTEIN_SKILLS_AUTO_ROUTE", "1")
    templates_dir = _make_skill_templates(tmp_path)
    skills_dir = templates_dir.parent / "skills"
    (skills_dir / "bad-frontmatter.md").write_text(
        "---\nname: [unterminated\n---\n\n# Bad frontmatter\npytest routing should continue.\n",
        encoding="utf-8",
    )
    workdir = tmp_path / "workdir"
    workdir.mkdir()

    inject_skills(
        workdir=workdir,
        role="security",
        tasks=[
            Task(id="T-005", title="Fix pytest regression", description="The pytest suite is failing.", role="security")
        ],
        session_id="sec-mno",
        templates_dir=templates_dir,
    )

    assert (workdir / ".claude" / "skills" / "pytest-helper.md").is_file()


def test_inject_skills_returns_audit_record_even_when_activation_log_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The injector returns an audit record for each skill regardless of the activation log setting."""
    monkeypatch.setenv(ENV_VAR, "0")
    templates_dir = _make_skill_templates(tmp_path)
    workdir = tmp_path / "workdir"
    workdir.mkdir()

    audit_records = inject_skills(
        workdir=workdir,
        role="security",
        tasks=[_make_task("T-001")],
        session_id="sec-abc",
        templates_dir=templates_dir,
    )

    # We expect two skills for the security role: the two always-inject skills.
    assert len(audit_records) == 2
    # Check that each record has the required fields.
    for record in audit_records:
        assert isinstance(record, dict)
        assert "template_name" in record
        assert "version" in record
        assert "pre_render_digest" in record
        assert "rendered_digest" in record
        assert "trigger_source" in record
        # For these skills, we expect them to be successfully injected.
        assert record["status"] == "injected"
        # We can also check one of the skills by name and version.
        if record["template_name"] == "bernstein-completion-protocol.md":
            assert record["version"] == "1.2.3"
            assert record["trigger_source"] == "role-binding"
