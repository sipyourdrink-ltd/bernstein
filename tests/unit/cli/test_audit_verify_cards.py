"""``bernstein audit verify`` includes resolved approval cards (issue #2511).

A resolved approval must be re-checkable offline: a mutated stored envelope, a
decision echoing an unknown ``card_hash``, or a decision after expiry must make
``bernstein audit verify`` fail. When no cards exist the check is a silent
no-op that does not affect the exit code.
"""

from __future__ import annotations

import json

from bernstein.cli.commands import audit_cmd
from bernstein.core.approval.card import build_card
from bernstein.core.approval.card_gate import ApprovalCardGate
from bernstein.core.security.audit_chain import (
    EVENT_APPROVAL_CARD_ISSUED,
    AuditChainStore,
)

_KEY = b"k" * 32


def _seed(audit_dir) -> str:
    chain = AuditChainStore(audit_dir, key=_KEY)
    gate = ApprovalCardGate(chain)
    card = build_card(
        approval_id="ap-1",
        tool_name="Edit",
        tool_args={"file_path": "src/app.py", "new_string": "x = 1"},
        reasoning="Add a constant.",
        created_at=1_000.0,
        ttl_seconds=600.0,
    )
    issued = gate.issue(card)
    gate.resolve(card_hash=issued.card_hash, decision="approve", approver="U7", now=1_100.0)
    return issued.card_hash


def test_verify_approval_cards_passes_for_intact(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(audit_cmd, "AUDIT_DIR", tmp_path)
    _seed(tmp_path)
    assert audit_cmd._verify_approval_cards() is True


def test_verify_approval_cards_noop_when_absent(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(audit_cmd, "AUDIT_DIR", tmp_path)
    assert audit_cmd._verify_approval_cards() is True


def test_verify_approval_cards_fails_for_tampered_envelope(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(audit_cmd, "AUDIT_DIR", tmp_path)
    _seed(tmp_path)

    log_file = next(iter(sorted(tmp_path.glob("*.jsonl"))))
    out: list[str] = []
    for line in log_file.read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        if entry.get("event_type") == EVENT_APPROVAL_CARD_ISSUED:
            entry["details"]["envelope"]["rollback"]["procedure"] = "rewritten after the fact"
        out.append(json.dumps(entry, sort_keys=True))
    log_file.write_text("\n".join(out) + "\n", encoding="utf-8")

    assert audit_cmd._verify_approval_cards() is False
