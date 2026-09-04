"""CLI tests for ``bernstein govern audit-compliance`` over the compliance namespace (#5075)."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.governance_cmd import govern_group
from bernstein.core.govern.compliance_checks import iter_compliance_checks

if TYPE_CHECKING:
    from pathlib import Path

#: Words that would turn a report of what was read into an assertion of conformance.
_CONFORMANCE_CLAIMS = re.compile(r"\b(compliant|conformant|certified|certification|attests?)\b", re.IGNORECASE)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A minimal install: a state directory and one configuration declaration."""
    (tmp_path / ".sdd").mkdir()
    (tmp_path / "bernstein.yaml").write_text("auth:\n  method: oidc\n", encoding="utf-8")
    return tmp_path


def _run(args: list[str]) -> str:
    result = CliRunner().invoke(govern_group, args)
    assert result.exit_code == 0, result.output
    return result.output


def test_govern_audit_list_names_every_registered_compliance_check(project: Path) -> None:
    """``--list`` prints the ids and areas the audit would run, without running them."""
    output = _run(["audit-compliance", "--workdir", str(project), "--list"])
    for spec in iter_compliance_checks():
        assert spec.check_id in output
        assert spec.area in output


def test_govern_audit_only_cmp_reports_every_registered_check(project: Path) -> None:
    """``--only CMP`` is the whole compliance namespace, one finding per registered check."""
    payload = json.loads(_run(["audit-compliance", "--workdir", str(project), "--only", "CMP", "--json-output"]))
    reported = [row["check_id"] for row in payload["checks"]]
    assert reported == sorted(spec.check_id for spec in iter_compliance_checks())


def test_govern_audit_reports_counts_with_named_denominators_and_no_score(project: Path) -> None:
    """Every number is a fraction of a named denominator; there is no score and no grade."""
    payload = json.loads(_run(["audit-compliance", "--workdir", str(project), "--json-output"]))
    counts = payload["counts"]
    assert set(counts) == {"measured_pass", "measured_fail", "declared", "not_measurable"}
    assert sum(counts.values()) == payload["checks_run"]
    assert "score" not in payload
    assert "grade" not in payload


def test_govern_audit_profile_marks_required_ids_and_emits_no_conformance_claim(project: Path) -> None:
    """A profile selects which ids are required and never claims the install conforms."""
    text = _run(["audit-compliance", "--workdir", str(project), "--profile", "soc2"])
    assert not _CONFORMANCE_CLAIMS.search(text), text

    payload = json.loads(_run(["audit-compliance", "--workdir", str(project), "--profile", "soc2", "--json-output"]))
    assert payload["profile"] == "soc2"
    required = {row["check_id"] for row in payload["checks"] if row["required"]}
    assert required
    assert required < {row["check_id"] for row in payload["checks"]}
    assert "conformance" not in json.dumps(payload).lower()


def test_govern_audit_skip_removes_only_the_named_id(project: Path) -> None:
    """Skipping is a filter on what runs, not a suppression of what was found."""
    target = iter_compliance_checks()[0].check_id
    payload = json.loads(_run(["audit-compliance", "--workdir", str(project), "--skip", target, "--json-output"]))
    reported = {row["check_id"] for row in payload["checks"]}
    assert target not in reported
    assert len(reported) == len(iter_compliance_checks()) - 1


def test_govern_audit_rejects_an_unknown_selector(project: Path) -> None:
    """A selector that matches no registered id fails loudly instead of auditing nothing."""
    result = CliRunner().invoke(govern_group, ["audit-compliance", "--workdir", str(project), "--only", "XYZ"])
    assert result.exit_code != 0
    assert "XYZ" in result.output
