"""Decision-provenance projection over verified approval cards (issue #2917).

Every test pins one property of the projection, not a code path:

1. a verified issue/resolve pair is returned with the chain anchors of both
   source events, so the projection has something to link to at all,
2. no decision record exists without a verifying source event -- the
   load-bearing property: a mutated envelope removes the record, it does not
   annotate it,
3. the plain-language statement names the approving human identity and links
   the approval receipt,
4. every field the record prints is reachable from a referenced event,
5. `bernstein compliance decisions` reports the approver and the tool over a
   real audit directory,
6. the same command emits no record when the chain fails verification.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.commands.compliance_cmd import compliance_group
from bernstein.core.approval.card import build_card
from bernstein.core.approval.card_gate import ApprovalCardGate
from bernstein.core.approval.card_verify import verify_approval_cards
from bernstein.core.compliance.decision_record import (
    build_decision_records,
    render_decision_records,
)
from bernstein.core.security.audit_chain import AuditChainStore

_KEY = b"deterministic-test-key-2917"


def _seed(audit_dir: Path, *, approver: str = "alice@example.com") -> str:
    """Write one issued/resolved approval-card pair and return its card hash."""
    chain = AuditChainStore(audit_dir, key=_KEY)
    gate = ApprovalCardGate(chain)
    issued = gate.issue(
        build_card(
            approval_id="ap-1",
            tool_name="Edit",
            tool_args={"file_path": "src/app.py", "new_string": "x = 1"},
            reasoning="Add the constant the new endpoint reads.",
            created_at=1_000.0,
            ttl_seconds=600.0,
        ),
    )
    gate.resolve(card_hash=issued.card_hash, decision="approve", approver=approver, now=1_100.0)
    return issued.card_hash


def _mutate_stored_envelope(audit_dir: Path) -> None:
    """Rewrite the issued event's stored envelope in place, as a tamperer would."""
    for path in sorted(audit_dir.glob("*.jsonl")):
        lines = path.read_text(encoding="utf-8").splitlines()
        changed = False
        for index, line in enumerate(lines):
            entry = json.loads(line)
            envelope = entry.get("details", {}).get("envelope")
            if isinstance(envelope, dict) and "action" in envelope:
                envelope["action"]["tool_name"] = "Bash"
                lines[index] = json.dumps(entry)
                changed = True
        if changed:
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return
    msg = "no issued approval-card envelope found to mutate"
    raise AssertionError(msg)


# ---------------------------------------------------------------------------
# 1. The verifier hands back what it reconstructed
# ---------------------------------------------------------------------------


def test_verified_cards_are_returned_with_their_chain_anchors(tmp_path: Path) -> None:
    audit_dir = tmp_path / "audit"
    card_hash = _seed(audit_dir)

    result = verify_approval_cards(audit_dir, key=_KEY)

    assert result.ok, result.errors
    assert [record.card_hash for record in result.records] == [card_hash]
    record = result.records[0]
    assert record.approver == "alice@example.com"
    assert record.decision == "approve"
    # Both halves of the pair are anchored, not just the settlement.
    assert record.issued_hmac
    assert record.resolved_hmac
    assert record.issued_hmac != record.resolved_hmac


# ---------------------------------------------------------------------------
# 2. No decision record exists without a verifying source event (load-bearing)
# ---------------------------------------------------------------------------


def test_no_decision_record_exists_without_a_verifying_source_event(tmp_path: Path) -> None:
    audit_dir = tmp_path / "audit"
    _seed(audit_dir)
    assert build_decision_records(verify_approval_cards(audit_dir, key=_KEY))

    _mutate_stored_envelope(audit_dir)

    result = verify_approval_cards(audit_dir, key=_KEY)
    assert not result.ok
    # Not emitted-with-a-warning: not emitted.
    assert build_decision_records(result) == []
    assert result.records == ()


# ---------------------------------------------------------------------------
# 3. The statement names the approver and links the receipt
# ---------------------------------------------------------------------------


def test_decision_statement_names_the_approver_and_links_the_receipt(tmp_path: Path) -> None:
    audit_dir = tmp_path / "audit"
    card_hash = _seed(audit_dir, approver="bob@example.com")

    (record,) = build_decision_records(verify_approval_cards(audit_dir, key=_KEY))

    statement = record.statement()
    assert "bob@example.com" in statement
    assert "Edit" in statement
    # The receipt the reviewer follows to check the claim.
    assert card_hash in statement


# ---------------------------------------------------------------------------
# 4. Every printed field is reachable from a referenced event
# ---------------------------------------------------------------------------


def test_every_rendered_field_is_reachable_from_a_referenced_event(tmp_path: Path) -> None:
    audit_dir = tmp_path / "audit"
    _seed(audit_dir)

    (record,) = build_decision_records(verify_approval_cards(audit_dir, key=_KEY))

    events = AuditChainStore(audit_dir, key=_KEY).query()
    referenced = [
        event for event in events if event.hmac in {record.source_events["issued"], record.source_events["resolved"]}
    ]
    assert len(referenced) == 2
    haystack = json.dumps([{"details": event.details, "hmac": event.hmac} for event in referenced], sort_keys=True)
    for field, value in record.to_dict().items():
        # ``schema`` names the projection's own shape and ``source_events``
        # carries the anchors themselves; every remaining field is a claim
        # about the run and must be backed by one of the referenced events.
        if field in {"schema", "source_events"}:
            continue
        for atom in value if isinstance(value, list) else [value]:
            assert json.dumps(atom) in haystack or str(atom) in haystack, f"{field} is not backed by a source event"


# ---------------------------------------------------------------------------
# 5 & 6. The CLI over a real audit directory
# ---------------------------------------------------------------------------


def _audit_key_file(tmp_path: Path) -> Path:
    key_path = tmp_path / "audit.key"
    key_path.write_bytes(_KEY)
    key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return key_path


def _run_decisions(audit_dir: Path, key_path: Path, *extra: str) -> object:
    env = dict(os.environ, BERNSTEIN_AUDIT_KEY_PATH=str(key_path))
    return CliRunner().invoke(
        compliance_group,
        ["decisions", "--audit-dir", str(audit_dir), *extra],
        env=env,
        catch_exceptions=False,
    )


def test_decisions_command_reports_the_approved_tool_and_approver(tmp_path: Path) -> None:
    audit_dir = tmp_path / "audit"
    card_hash = _seed(audit_dir, approver="carol@example.com")

    result = _run_decisions(audit_dir, _audit_key_file(tmp_path))

    assert result.exit_code == 0, result.output
    assert "carol@example.com" in result.output
    assert "Edit" in result.output
    assert card_hash[:16] in result.output


def test_decisions_command_emits_nothing_when_the_chain_fails_verification(tmp_path: Path) -> None:
    audit_dir = tmp_path / "audit"
    _seed(audit_dir, approver="carol@example.com")
    _mutate_stored_envelope(audit_dir)

    result = _run_decisions(audit_dir, _audit_key_file(tmp_path))

    assert result.exit_code != 0
    assert "carol@example.com" not in result.output


# ---------------------------------------------------------------------------
# Rendering is a projection of the records, not an independent read
# ---------------------------------------------------------------------------


def test_rendering_no_records_states_that_none_were_verified(tmp_path: Path) -> None:
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir(parents=True)

    assert "no verified" in render_decision_records([]).lower()
