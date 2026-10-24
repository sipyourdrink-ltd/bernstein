"""Mutation-kill tests for ``core/security/policy_engine.py``.

``scripts/mutmut_critical.py`` scored the ``policy_engine`` target at 27/60.
The surviving mutants sat in decisions the two existing test files reach only
through one or two examples: the decision graph's precedence and bypass rule,
when a policy directory counts as empty, the OPA-missing branch, the YAML
loader's severity and skip rules, and every comparison operator at its
boundary. Each test below pins one of those decisions to an exact value,
following the other ``*_mutation_kill.py`` files.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from bernstein.core.security import policy_engine as pe
from bernstein.core.security.policy_engine import (
    DecisionGraph,
    DecisionType,
    PermissionDecision,
    PolicyDiff,
    PolicyEngine,
    PolicyFile,
    PolicyRule,
    PolicySubject,
    RegoPolicy,
)

_SUBJECT = PolicySubject(id="T-1", title="Add login", description="Wire the form", role="backend")
_DIFF = PolicyDiff(
    diff_text="+print('x')\n",
    files=(PolicyFile(path="src/a.py", content="alpha"), PolicyFile(path="src/b.py", content="beta")),
)


def _decision(kind: DecisionType, *, immune: bool = False, reason: str = "") -> PermissionDecision:
    return PermissionDecision(kind, reason or kind.value, bypass_immune=immune)


# ---------------------------------------------------------------------------
# DecisionGraph
# ---------------------------------------------------------------------------


def test_permission_decision_defaults() -> None:
    d = PermissionDecision(DecisionType.ASK, "r")
    assert d.bypass_immune is False
    assert d.files == ()


def test_bypass_is_off_unless_asked() -> None:
    graph = DecisionGraph()
    assert graph.bypass_enabled is False
    graph.add_decision(_decision(DecisionType.ASK))
    assert graph.evaluate().type is DecisionType.ASK


def test_an_empty_graph_allows_and_says_why() -> None:
    assert DecisionGraph().evaluate() == PermissionDecision(DecisionType.ALLOW, "No rules evaluated")


def test_only_allows_falls_through_to_the_summary_allow() -> None:
    graph = DecisionGraph()
    graph.add_decision(_decision(DecisionType.ALLOW, reason="fine"))
    assert graph.evaluate() == PermissionDecision(DecisionType.ALLOW, "All checks passed or bypassed")


@pytest.mark.parametrize(
    ("first", "second", "winner"),
    [
        (DecisionType.IMMUNE, DecisionType.DENY, DecisionType.DENY),
        (DecisionType.SAFETY, DecisionType.IMMUNE, DecisionType.IMMUNE),
        (DecisionType.ASK, DecisionType.SAFETY, DecisionType.SAFETY),
        (DecisionType.SAFETY, DecisionType.DENY, DecisionType.DENY),
        (DecisionType.ASK, DecisionType.IMMUNE, DecisionType.IMMUNE),
        (DecisionType.ALLOW, DecisionType.ASK, DecisionType.ASK),
    ],
)
def test_precedence_does_not_depend_on_insertion_order(
    first: DecisionType, second: DecisionType, winner: DecisionType
) -> None:
    for order in ((first, second), (second, first)):
        graph = DecisionGraph()
        for kind in order:
            graph.add_decision(_decision(kind))
        assert graph.evaluate().type is winner, order


def test_bypass_skips_a_non_immune_block_and_returns_the_next() -> None:
    graph = DecisionGraph(bypass_enabled=True)
    graph.add_decision(_decision(DecisionType.DENY, reason="soft deny"))
    graph.add_decision(_decision(DecisionType.SAFETY, immune=True, reason="secret"))
    assert graph.evaluate().reason == "secret"


def test_bypass_leaves_an_immune_decision_in_force() -> None:
    graph = DecisionGraph(bypass_enabled=True)
    graph.add_decision(_decision(DecisionType.DENY, immune=True, reason="hard deny"))
    assert graph.evaluate().reason == "hard deny"


def test_bypass_of_everything_non_immune_allows() -> None:
    graph = DecisionGraph(bypass_enabled=True)
    graph.add_decision(_decision(DecisionType.DENY))
    graph.add_decision(_decision(DecisionType.ASK))
    assert graph.evaluate() == PermissionDecision(DecisionType.ALLOW, "All checks passed or bypassed")


def test_without_bypass_an_immune_flag_changes_nothing() -> None:
    graph = DecisionGraph(bypass_enabled=False)
    graph.add_decision(_decision(DecisionType.ASK, reason="ask"))
    assert graph.evaluate().reason == "ask"


# ---------------------------------------------------------------------------
# PolicyEngine.from_directory
# ---------------------------------------------------------------------------


def test_a_missing_directory_is_no_engine(tmp_path: Path) -> None:
    assert PolicyEngine.from_directory(tmp_path / "absent") is None


def test_an_empty_directory_is_no_engine(tmp_path: Path) -> None:
    assert PolicyEngine.from_directory(tmp_path) is None


def test_a_directory_of_only_unusable_yaml_is_no_engine(tmp_path: Path) -> None:
    (tmp_path / "x.yaml").write_text("- name: a\n  rule: ''\n", encoding="utf-8")
    assert PolicyEngine.from_directory(tmp_path) is None


def test_rego_alone_makes_an_engine(tmp_path: Path) -> None:
    (tmp_path / "deny.rego").write_text("package x\n", encoding="utf-8")
    engine = PolicyEngine.from_directory(tmp_path)
    assert engine is not None
    assert engine.yaml_rules == []
    assert engine.rego_policies == [RegoPolicy(name="deny", source_path=tmp_path / "deny.rego")]
    assert engine.policies_dir == tmp_path


def test_yaml_alone_makes_an_engine_and_both_extensions_load(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text("name: one\nrule: files_changed < 5\n", encoding="utf-8")
    (tmp_path / "b.yml").write_text("name: two\nrule: files_changed < 9\n", encoding="utf-8")
    engine = PolicyEngine.from_directory(tmp_path)
    assert engine is not None
    assert [r.name for r in engine.yaml_rules] == ["one", "two"]
    assert engine.rego_policies == []


def test_load_policy_engine_reads_sdd_policies(tmp_path: Path) -> None:
    policies = tmp_path / ".sdd" / "policies"
    policies.mkdir(parents=True)
    (policies / "a.yaml").write_text("name: one\nrule: files_changed < 5\n", encoding="utf-8")
    engine = pe.load_policy_engine(tmp_path)
    assert engine is not None and engine.policies_dir == policies


# ---------------------------------------------------------------------------
# PolicyEngine.check: YAML rules and the OPA-missing branch
# ---------------------------------------------------------------------------


def test_check_collects_only_violating_rules() -> None:
    engine = PolicyEngine(
        yaml_rules=[
            PolicyRule(name="few", rule="files_changed <= 2"),
            PolicyRule(name="tiny", rule="files_changed < 2", severity="block"),
        ]
    )
    violations = engine.check(_SUBJECT, _DIFF)
    assert [(v.policy_name, v.blocked) for v in violations] == [("tiny", True)]


def test_rego_is_skipped_and_logged_when_opa_is_missing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(pe.shutil, "which", lambda _name: None)
    called: list[RegoPolicy] = []

    def record_rego_call(policy: RegoPolicy, *_args: object) -> list[pe.PolicyViolation]:
        called.append(policy)
        return []

    monkeypatch.setattr(pe, "_evaluate_rego_policy", record_rego_call)
    engine = PolicyEngine(rego_policies=[RegoPolicy("a", Path("a.rego")), RegoPolicy("b", Path("b.rego"))])

    with caplog.at_level(logging.INFO, logger=pe.logger.name):
        assert engine.check(_SUBJECT, _DIFF) == []

    assert called == []
    assert "OPA binary not available; skipping 2 Rego policies" in caplog.text


def test_rego_runs_when_opa_is_present(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(pe.shutil, "which", lambda _name: "/usr/bin/opa")
    violation = pe.PolicyViolation(policy_name="a", source="rego", blocked=True, detail="d")
    monkeypatch.setattr(pe, "_evaluate_rego_policy", lambda *_a: [violation])
    engine = PolicyEngine(rego_policies=[RegoPolicy("a", Path("a.rego"))])

    with caplog.at_level(logging.INFO, logger=pe.logger.name):
        assert engine.check(_SUBJECT, _DIFF) == [violation]
    assert "OPA binary not available" not in caplog.text


def test_no_rego_and_no_opa_logs_nothing(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(pe.shutil, "which", lambda _name: None)
    with caplog.at_level(logging.INFO, logger=pe.logger.name):
        assert PolicyEngine().check(_SUBJECT, _DIFF) == []
    assert "OPA binary not available" not in caplog.text


# ---------------------------------------------------------------------------
# build_policy_diff and run_policy_engine
# ---------------------------------------------------------------------------


def test_build_policy_diff_reads_present_files_and_blanks_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "present.py").write_text("body", encoding="utf-8")
    (tmp_path / "adir").mkdir()

    def fake_git(_run_dir: Path, args: list[str]) -> str:
        if "--name-only" in args:
            return "present.py\n\n  gone.py  \nadir\n"
        return "DIFF"

    monkeypatch.setattr(pe, "_run_git", fake_git)
    diff = pe.build_policy_diff(tmp_path)
    assert diff.diff_text == "DIFF"
    assert diff.files == (
        PolicyFile(path="present.py", content="body"),
        PolicyFile(path="gone.py", content=""),
        PolicyFile(path="adir", content=""),
    )


@pytest.mark.parametrize(
    ("blocked_flags", "passed"),
    [((), True), ((False,), True), ((False, True), False), ((True,), False)],
)
def test_run_policy_engine_passes_only_without_a_blocking_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocked_flags: tuple[bool, ...], passed: bool
) -> None:
    violations = [
        pe.PolicyViolation(policy_name=f"p{i}", source="yaml", blocked=b, detail="d")
        for i, b in enumerate(blocked_flags)
    ]
    monkeypatch.setattr(pe, "build_policy_diff", lambda _run_dir: _DIFF)
    monkeypatch.setattr(PolicyEngine, "check", lambda _self, _s, _d: violations)
    recorded: list[str] = []
    monkeypatch.setattr(pe, "_record_policy_violation", lambda _w, _t, v: recorded.append(v.policy_name))

    class _Task:
        id = "T-1"
        title = "t"
        description = "d"
        role = "backend"

    result = pe.run_policy_engine(_Task(), tmp_path, tmp_path, PolicyEngine())  # type: ignore[arg-type]
    assert result.passed is passed
    assert result.violations == tuple(violations)
    assert recorded == [v.policy_name for v in violations]


# ---------------------------------------------------------------------------
# _load_yaml_policy_file
# ---------------------------------------------------------------------------


def _load(tmp_path: Path, text: str, name: str = "rules.yaml") -> list[PolicyRule]:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return pe._load_yaml_policy_file(path)


def test_an_unreadable_or_unparseable_file_yields_no_rules(tmp_path: Path) -> None:
    assert pe._load_yaml_policy_file(tmp_path / "absent.yaml") == []
    assert _load(tmp_path, "name: [unclosed\n") == []


def test_a_scalar_document_yields_no_rules(tmp_path: Path) -> None:
    assert _load(tmp_path, "just a string\n") == []


def test_severity_is_block_only_when_it_says_block(tmp_path: Path) -> None:
    rules = _load(
        tmp_path,
        "policies:\n"
        "  - {name: a, rule: files_changed < 1, severity: BLOCK}\n"
        "  - {name: b, rule: files_changed < 1, severity: warn}\n"
        "  - {name: c, rule: files_changed < 1, severity: blocker}\n"
        "  - {name: d, rule: files_changed < 1}\n",
    )
    assert [(r.name, r.severity) for r in rules] == [("a", "block"), ("b", "warn"), ("c", "warn"), ("d", "warn")]


def test_an_entry_needs_both_a_name_and_a_rule(tmp_path: Path) -> None:
    rules = _load(
        tmp_path,
        "- {name: '  ', rule: files_changed < 1}\n"
        "- {name: kept, rule: files_changed < 1}\n"
        "- {name: norule, rule: '   '}\n"
        "- not-a-mapping\n",
    )
    assert [r.name for r in rules] == ["kept"]


def test_a_missing_name_falls_back_to_the_file_stem(tmp_path: Path) -> None:
    (rule,) = _load(tmp_path, "rule: files_changed < 1\n", name="size-cap.yaml")
    assert rule.name == "size-cap"
    assert rule.source_path == tmp_path / "size-cap.yaml"


# ---------------------------------------------------------------------------
# Regex rules
# ---------------------------------------------------------------------------


def _violation(rule: str, *, severity: str = "warn") -> pe.PolicyViolation | None:
    return pe._evaluate_yaml_rule(PolicyRule(name="r", rule=rule, severity=severity), _SUBJECT, _DIFF)  # type: ignore[arg-type]


def test_forbidden_content_present_violates_with_the_forbidden_wording() -> None:
    v = _violation("file_content !~ /alpha/", severity="block")
    assert v is not None
    assert v.blocked is True
    assert v.detail == "Policy 'r' blocked: rule 'file_content !~ /alpha/' matched forbidden content"
    assert v.files == ("src/a.py", "src/b.py")


def test_forbidden_content_absent_passes() -> None:
    assert _violation("file_content !~ /gamma/") is None


def test_required_content_absent_violates_with_the_required_wording() -> None:
    v = _violation("task_title =~ /Remove/")
    assert v is not None
    assert v.blocked is False
    assert v.detail == "Policy 'r' blocked: rule 'task_title =~ /Remove/' required a match"
    assert v.files == ()


def test_required_content_present_passes() -> None:
    assert _violation("task_title =~ /login/") is None


@pytest.mark.parametrize(
    ("rule", "violates"),
    [
        ("file_path =~ /src\\/b\\.py/", False),
        ("file_path !~ /^src\\/a\\.py$/", True),
        ("diff_text =~ /print/", False),
        ("task_description =~ /form/", False),
        ("task_description =~ /nothing/", True),
        ("unknown_field =~ /x/", True),
    ],
)
def test_each_regex_field_reads_its_own_value(rule: str, violates: bool) -> None:
    assert (_violation(rule) is not None) is violates


# ---------------------------------------------------------------------------
# Comparison rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rule", "violates"),
    [
        ("files_changed == 2", False),
        ("files_changed == 3", True),
        ("files_changed != 2", True),
        ("files_changed != 3", False),
        ("files_changed > 2", True),
        ("files_changed > 1", False),
        ("files_changed < 2", True),
        ("files_changed < 3", False),
        ("files_changed >= 2", False),
        ("files_changed >= 3", True),
        ("files_changed <= 2", False),
        ("files_changed <= 1", True),
    ],
)
def test_every_operator_at_its_boundary(rule: str, violates: bool) -> None:
    assert (_violation(rule) is not None) is violates


def test_a_failed_comparison_names_the_rule() -> None:
    v = _violation("files_changed < 1", severity="block")
    assert v is not None
    assert v.blocked is True
    assert v.detail == "Policy 'r' failed comparison: files_changed < 1"


def test_a_non_numeric_ordering_fails_closed() -> None:
    assert _violation("task_title > 3") is not None


def test_string_equality_reads_quoted_values() -> None:
    assert _violation("task_role == 'backend'") is None
    assert _violation('task_role == "frontend"') is not None


def test_an_unknown_scalar_field_is_not_a_violation() -> None:
    assert _violation("nonsense == 1") is None


def test_unsupported_syntax_is_not_a_violation() -> None:
    assert _violation("this is not a rule") is None


@pytest.mark.parametrize(("raw", "value"), [(" 3 ", 3), ("2.5", 2.5), ("'x'", "x"), ('"7"', 7), ("abc", "abc")])
def test_coerce_scalar(raw: str, value: object) -> None:
    coerced = pe._coerce_scalar(raw)
    assert coerced == value
    assert type(coerced) is type(value)
