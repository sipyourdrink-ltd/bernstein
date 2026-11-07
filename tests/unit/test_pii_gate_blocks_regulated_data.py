"""The ``pii_scan`` gate blocks a US SSN and a payment card number (#6191).

The gate decided its verdict on ``severity == "high"``, and both regulated-data
rules are graded ``"medium"``. So a diff holding a Social Security number or a
card number passed, listed in the gate's own detail, while a private key
blocked. The verdict now reads the finding's ``block_merge``, which the rule
table sets for every high-severity secret and for ``BLOCKING_PII_RULES``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bernstein.core.quality.quality_gates import QualityGateCheckResult, QualityGatesConfig, _run_pii_gate
from bernstein.core.security.pii_output_gate import BLOCKING_PII_RULES, scan_text

if TYPE_CHECKING:
    from pathlib import Path


def _gate_on(tmp_path: Path, body: str) -> QualityGateCheckResult:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text(body, encoding="utf-8")
    return _run_pii_gate(QualityGatesConfig(), tmp_path, ["src/a.py"])


@pytest.mark.parametrize(
    ("rule", "body"),
    [
        ("ssn", "customer_ssn = '123-45-6789'\n"),
        ("credit_card_number", "card = '4111 1111 1111 1111'\n"),
    ],
)
def test_a_regulated_data_only_file_blocks(tmp_path: Path, rule: str, body: str) -> None:
    assert [f.rule for f in scan_text(body)] == [rule], "the fixture must hold only the rule under test"

    result = _gate_on(tmp_path, body)

    assert result.blocked is True
    assert result.passed is False


def test_an_email_address_alone_is_still_a_warning(tmp_path: Path) -> None:
    """Widening to every medium finding would block on a contact address in a README."""
    result = _gate_on(tmp_path, "contact = 'jane.doe@acme-corp.io'\n")

    assert result.blocked is False
    assert result.passed is True
    assert "email_address" in result.detail


def test_a_high_severity_secret_still_blocks(tmp_path: Path) -> None:
    result = _gate_on(tmp_path, "-----BEGIN RSA PRIVATE KEY-----\n")

    assert result.blocked is True


def test_block_merge_is_what_the_rule_table_says() -> None:
    findings = scan_text("a = '123-45-6789'\nb = 'jane.doe@acme-corp.io'\nc = '-----BEGIN RSA PRIVATE KEY-----'\n")

    by_rule = {f.rule: f.block_merge for f in findings}
    assert by_rule["ssn"] is True
    assert by_rule["email_address"] is False
    assert all(by_rule[r] for r in BLOCKING_PII_RULES & by_rule.keys())
    assert all(f.block_merge for f in findings if f.severity == "high")
