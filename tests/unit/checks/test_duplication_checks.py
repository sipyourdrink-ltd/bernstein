"""The duplication collector reported through ``bernstein govern audit`` (#5105, slice 3).

Each collector check is a ``duplication:<name>`` check. They measure the
installed package, so they are opt-in: ``--only duplication`` runs them, and a
default audit of a clean workspace stays green.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from bernstein.cli.main import cli
from bernstein.core.checks import adapters
from bernstein.core.checks.adapters import DUPLICATION_CHECK_NAMES, DuplicationAuditAdapter
from bernstein.core.checks.contract import Verdict
from bernstein.core.checks.registry import CheckRegistry, populate_default_checks
from bernstein.core.govern.duplication_audit import collect_duplication

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_CANONICAL = 'import json\n\n\ndef sign(p):\n    return json.dumps(p, sort_keys=True, separators=(",", ":"))\n'


@pytest.fixture
def package(tmp_path: Path) -> Path:
    """A package with two inline canonical-bytes sites and no private HMAC loader."""
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "a.py").write_text(_CANONICAL, encoding="utf-8")
    (root / "b.py").write_text(_CANONICAL, encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    adapters._duplication_report.cache_clear()
    yield
    adapters._duplication_report.cache_clear()


def test_every_collector_check_has_an_adapter(package: Path) -> None:
    """A check the collector adds or drops must show up here, not vanish from the audit."""
    collected = {f.check_id for f in collect_duplication(package).findings}
    assert set(DUPLICATION_CHECK_NAMES) == collected


def test_duplication_count_above_expected_produces_a_measured_failed_finding(package: Path) -> None:
    finding = DuplicationAuditAdapter("inline-canonical-bytes-sites", package).run()

    assert finding.verdict is Verdict.FAIL
    assert finding.passed is False
    assert finding.evidence
    assert "2 found, expected at most 1" in finding.message
    assert "a.py:5" in finding.remediation
    assert "b.py:5" in finding.remediation


def test_a_count_within_expected_passes_with_evidence(package: Path) -> None:
    finding = DuplicationAuditAdapter("private-hmac-key-loaders", package).run()

    assert finding.verdict is Verdict.PASS
    assert finding.evidence
    assert "0 found, expected at most 0" in finding.message


def test_collector_reports_not_yet_measurable_for_unimplemented_checks(package: Path) -> None:
    finding = DuplicationAuditAdapter("orphan-modules", package).run()

    assert finding.verdict is Verdict.NOT_MEASURABLE
    assert finding.passed is None
    assert finding.what_would_make_it_measurable
    assert "#5100" in finding.what_would_make_it_measurable


def test_a_dropped_collector_check_is_not_measurable_not_a_pass(package: Path) -> None:
    registry = CheckRegistry()
    registry.register(DuplicationAuditAdapter("no-such-check", package))

    (finding,) = registry.run_all(only_areas=("duplication",))

    assert finding.verdict is Verdict.NOT_MEASURABLE
    assert finding.reason == "LookupError"


def test_the_collector_walks_the_tree_once_per_run(package: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Path] = []
    real = collect_duplication

    def counting(root: Path) -> object:
        calls.append(root)
        return real(root)

    monkeypatch.setattr("bernstein.core.govern.duplication_audit.collect_duplication", counting)
    registry = CheckRegistry()
    for name in DUPLICATION_CHECK_NAMES:
        registry.register(DuplicationAuditAdapter(name, package))

    registry.run_all(only_areas=("duplication",))

    assert calls == [package]


def test_duplication_checks_are_opt_in() -> None:
    registry = CheckRegistry()
    populate_default_checks(registry)

    default_run = {c.check_id for c in registry.iter_checks()}
    only_run = {c.check_id for c in registry.iter_checks(only_areas=("duplication",))}
    listed = {c.check_id for c in registry.list_checks()}

    expected = {f"duplication:{name}" for name in DUPLICATION_CHECK_NAMES}
    assert not default_run & expected, "a default audit must not run checks about the install"
    assert only_run == expected
    assert expected <= listed, "--list still shows them, so they can be found"


def _audit_json(package: Path, monkeypatch: pytest.MonkeyPatch, workdir: Path) -> str:
    monkeypatch.setattr(adapters, "_installed_package_root", lambda: package)
    adapters._duplication_report.cache_clear()
    result = CliRunner().invoke(cli, ["govern", "audit", "--workdir", str(workdir), "--only", "duplication", "--json"])
    assert result.exit_code == 1, result.output
    return result.output


def test_finding_byte_identical_across_two_runs(package: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Load-bearing: the same tree gives the same findings, byte for byte, walked afresh each time."""
    first = _audit_json(package, monkeypatch, tmp_path)
    second = _audit_json(package, monkeypatch, tmp_path)

    assert first == second
    ids = [f["check_id"] for f in json.loads(first)]
    assert sorted(ids) == sorted(f"duplication:{name}" for name in DUPLICATION_CHECK_NAMES)


def test_no_score_or_grade_in_output(package: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The collector rejects security_posture.py's A-F model, and so does its report."""
    findings = json.loads(_audit_json(package, monkeypatch, tmp_path))

    for finding in findings:
        assert not {"score", "grade", "percent", "percentage"} & set(finding)
        assert "grade" not in json.dumps(finding).lower()
