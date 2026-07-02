"""Unit tests for the A2A signed-card conformance self-check.

Covers:

* A well-formed signed card passes every check.
* A tampered card body fails the signature check (and the report).
* An expired card fails the expiry check while its signature stays
  valid (proving the two checks are independent).
* A card missing a required field fails the required-fields check.
* The CLI ``bernstein interop a2a conformance`` exits 0 on a good card
  and non-zero on a tampered one.
"""

from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.commands.interop_cmd import interop_group
from bernstein.core.interop.a2a_card import (
    CardPolicies,
    SignedCapabilityCard,
    issue_capability_card,
)
from bernstein.core.interop.a2a_conformance import check_card_conformance


def _issue(*, ttl_seconds: int = 3600, now: float | None = None) -> SignedCapabilityCard:
    policies = CardPolicies(
        cost_cap_usd=5.0,
        redaction_tier="standard",
        sandbox_profile="container",
    )
    signed, _key = issue_capability_card(
        issuer="acme",
        name="acme-orchestrator",
        description="test",
        advertised_tools=["task_orchestration"],
        policies=policies,
        ttl_seconds=ttl_seconds,
        now=now,
    )
    return signed


def _check_by_name(report, name: str) -> bool:
    for check in report.checks:
        if check.name == name:
            return check.passed
    raise AssertionError(f"no check named {name!r}")


def test_wellformed_card_passes_all_checks() -> None:
    report = check_card_conformance(_issue())
    assert report.ok is True
    assert all(c.passed for c in report.checks)
    assert report.issuer == "acme"
    assert report.fingerprint.startswith("sha256:")


def test_tampered_card_fails_signature() -> None:
    signed = _issue()
    tampered_card = dataclasses.replace(signed.card, issuer="evil")
    tampered = dataclasses.replace(signed, card=tampered_card)

    report = check_card_conformance(tampered)
    assert report.ok is False
    assert _check_by_name(report, "signature") is False


def test_expired_card_fails_expiry_but_keeps_signature() -> None:
    # Issue a card that was valid for an hour but a long time ago.
    signed = _issue(ttl_seconds=3600, now=time.time() - 10_000)
    report = check_card_conformance(signed)
    assert report.ok is False
    assert _check_by_name(report, "expiry") is False
    # The signature is still cryptographically valid; only expiry failed.
    assert _check_by_name(report, "signature") is True


def test_missing_required_field_fails() -> None:
    signed = _issue()
    empty_kid_card = dataclasses.replace(signed.card, kid="")
    broken = dataclasses.replace(signed, card=empty_kid_card)
    report = check_card_conformance(broken)
    assert report.ok is False
    assert _check_by_name(report, "required_fields") is False


def test_cli_conformance_passes(tmp_path: Path) -> None:
    card_path = tmp_path / "card.json"
    card_path.write_text(_issue().to_json())
    runner = CliRunner()
    result = runner.invoke(interop_group, ["a2a", "conformance", "--card", str(card_path)])
    assert result.exit_code == 0, result.output
    assert "passes all conformance checks" in result.output


def test_cli_conformance_fails_on_tampered_card(tmp_path: Path) -> None:
    signed = _issue()
    tampered = dataclasses.replace(signed, card=dataclasses.replace(signed.card, issuer="evil"))
    card_path = tmp_path / "card.json"
    card_path.write_text(tampered.to_json())
    runner = CliRunner()
    result = runner.invoke(interop_group, ["a2a", "conformance", "--card", str(card_path)])
    assert result.exit_code == 1, result.output


def test_cli_conformance_json_output(tmp_path: Path) -> None:
    card_path = tmp_path / "card.json"
    card_path.write_text(_issue().to_json())
    runner = CliRunner()
    result = runner.invoke(
        interop_group,
        ["a2a", "conformance", "--card", str(card_path)],
        obj={"JSON": True},
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert len(payload["checks"]) == 5
