"""Tests for ``bernstein.core.persistence.journal_publish`` (#1799).

Privacy default is local-only; ``publish`` is opt-in. The published
receipt redacts the listed fields per step, then re-anchors the chain
to the redacted payloads so the published bundle still verifies offline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.persistence.journal import Journal
from bernstein.core.persistence.journal_export import verify_receipt
from bernstein.core.persistence.journal_publish import (
    PublishError,
    RedactionPolicy,
    publish_receipt,
)


class TestRedactionPolicy:
    def test_default_policy_redacts_prompt_and_results(self) -> None:
        policy = RedactionPolicy.default()
        assert "prompt" in policy.redact_fields
        assert "tool_result" in policy.redact_fields

    def test_policy_with_extra_fields(self) -> None:
        policy = RedactionPolicy(redact_fields=frozenset({"prompt", "tool_call"}))
        assert "tool_call" in policy.redact_fields


class TestPublish:
    def test_publish_replaces_redacted_fields_with_placeholder(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent-1"
        journal = Journal.open(agent_dir)
        for i in range(2):
            journal.append(
                input_hash=f"a{i}",
                model="m1",
                prompt=f"SECRET prompt {i}",
                tool_call={"name": "echo"},
                tool_result={"ok": True, "stdout": "SECRET output"},
            )
        journal.close()
        receipt_path = tmp_path / "redacted.tar"

        result = publish_receipt(
            agent_dir,
            receipt_path,
            agent_id="agent-1",
            policy=RedactionPolicy.default(),
            opt_in=True,
        )

        # Re-anchored head differs from the original head.
        assert result.head_hash != result.original_head_hash
        # The receipt remains offline-verifiable against the new head.
        v = verify_receipt(receipt_path, expected_head=result.head_hash)
        assert v.ok, v.errors

    def test_publish_requires_explicit_opt_in(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent-1"
        journal = Journal.open(agent_dir)
        journal.append(input_hash="aa", model="m1", prompt="hi")
        journal.close()
        receipt_path = tmp_path / "redacted.tar"
        with pytest.raises(PublishError):
            publish_receipt(
                agent_dir,
                receipt_path,
                agent_id="agent-1",
                policy=RedactionPolicy.default(),
                opt_in=False,
            )

    def test_publish_preserves_step_count(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent-1"
        journal = Journal.open(agent_dir)
        for i in range(5):
            journal.append(input_hash=f"a{i}", model="m1", prompt=f"p{i}")
        journal.close()
        receipt_path = tmp_path / "redacted.tar"

        result = publish_receipt(
            agent_dir,
            receipt_path,
            agent_id="agent-1",
            policy=RedactionPolicy.default(),
            opt_in=True,
        )
        assert result.steps == 5

    def test_published_receipt_does_not_contain_redacted_strings(self, tmp_path: Path) -> None:
        """A naive grep of the receipt bytes must not surface the
        sensitive payload. This is the load-bearing privacy assertion."""
        agent_dir = tmp_path / "agent-1"
        journal = Journal.open(agent_dir)
        journal.append(
            input_hash="aa",
            model="m1",
            prompt="SECRET_TOKEN_DO_NOT_LEAK",
            tool_result={"stdout": "ALSO_SECRET"},
        )
        journal.close()
        receipt_path = tmp_path / "redacted.tar"

        publish_receipt(
            agent_dir,
            receipt_path,
            agent_id="agent-1",
            policy=RedactionPolicy.default(),
            opt_in=True,
        )

        blob = receipt_path.read_bytes()
        assert b"SECRET_TOKEN_DO_NOT_LEAK" not in blob
        assert b"ALSO_SECRET" not in blob


class TestPublishEffortReanchor:
    """Published receipts must re-anchor over the effort dimension too.

    Regression: the publish recompute path folded every hashed field except
    ``effort`` when re-anchoring the redacted chain, silently dropping the
    effort dimension from published receipts while the redacted rows still
    carried the ``effort`` key.
    """

    def test_effort_bearing_journal_publishes_and_verifies(self, tmp_path: Path) -> None:
        agent_dir = tmp_path / "agent-effort"
        journal = Journal.open(agent_dir)
        journal.append(input_hash="a0", model="m1", prompt="p0", effort="high")
        journal.append(input_hash="a1", model="m1", prompt="p1", effort="low")
        journal.close()

        receipt_path = tmp_path / "redacted.tar"
        result = publish_receipt(
            agent_dir,
            receipt_path,
            agent_id="agent-effort",
            policy=RedactionPolicy.default(),
            opt_in=True,
        )

        # Offline verify must reproduce the re-anchored head, which only holds
        # if the re-anchor folds effort into each recomputed step hash.
        v = verify_receipt(receipt_path, expected_head=result.head_hash)
        assert v.ok, v.errors
        assert v.steps == 2
