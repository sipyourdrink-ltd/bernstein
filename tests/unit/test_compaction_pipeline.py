"""Tests for compaction pipeline - pure stages, mock plugin manager."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.compaction_pipeline import (
    CompactionPipeline,
    PostCompactPayload,
    PreCompactPayload,
    _estimate_tokens,
    strip_media_blocks,
    summarize_context,
)


class TestStripMediaBlocks:
    def test_strips_markdown_images_with_data_uri(self) -> None:
        text = "Here is an image: ![screenshot](data:image/png;base64,abc123==)"
        result = strip_media_blocks(text)
        assert "[image stripped]" in result
        assert "data:image/png" not in result

    def test_strips_fenced_media_block(self) -> None:
        text = "some code\n```svg\ndata:image/svg+xml;base64,PHN2Zz4=\n```\nmore"
        result = strip_media_blocks(text)
        assert "[media block stripped]" in result

    def test_leaves_normal_text_unchanged(self) -> None:
        text = "## Code section\n\n```python\nprint('hello')\n```"
        assert strip_media_blocks(text) == text

    def test_mixed_text_and_multiple_images(self) -> None:
        """Mixed prose, code, and multiple inline images."""
        text = (
            "# Project overview\n"
            "Read the screenshot below.\n"
            "![dashboard](data:image/png;base64,aaa)\n"
            "As you can see, the tests pass.\n"
            "![chart](data:image/jpeg;base64,bbb)\n"
            "```python\ndef foo(): pass\n```\n"
        )
        result = strip_media_blocks(text)
        assert result.count("[image stripped]") == 2
        assert "data:image" not in result
        assert "```python" in result  # code blocks preserved

    def test_mixed_media_and_documents(self) -> None:
        """Mix of inline images, fenced media, and normal text."""
        text = (
            "Intro text.\n"
            "![img1](data:image/png;base64,x)\n"
            "```pdf\ndata:application/pdf;base64,y\n```\n"
            "Some normal text.\n"
            "```\nprint('ok')\n```\n"
        )
        result = strip_media_blocks(text)
        assert "[image stripped]" in result
        assert "[media block stripped]" in result
        assert "print('ok')" in result

    def test_empty_input(self) -> None:
        assert strip_media_blocks("") == ""


class TestSummarizeContext:
    def test_deterministic_summary_without_llm(self) -> None:
        text = "# Header\nsome body\n## Subheader\nmore body"
        result = summarize_context(text)
        assert "context compacted" in result
        assert "headers" in result

    def test_placeholder_with_llm_stub(self) -> None:
        # When llm_call is provided but not a real async call, returns placeholder
        result = summarize_context("test text", llm_call=lambda x: x)
        assert "delegated" in result


class TestEstimateTokens:
    def test_rough_four_chars_per_token(self) -> None:
        text = "a" * 400
        assert _estimate_tokens(text) == 100

    def test_minimum_one(self) -> None:
        assert _estimate_tokens("") == 1
        assert _estimate_tokens("abc") == 1


class TestCompactionPipeline:
    @pytest.fixture()
    def pipeline(self) -> CompactionPipeline:
        return CompactionPipeline(plugin_manager=None)

    def test_execute_without_plugins_returns_result(self, pipeline: CompactionPipeline) -> None:
        result = pipeline.execute(
            session_id="s-1",
            context_text="# Header\nsome body\n",
            tokens_before=1000,
            reason="token_budget",
        )
        assert result.correlation_id.startswith("compact-")
        assert result.tokens_before == 1000
        assert result.tokens_saved >= 0
        assert result.compacted_text != ""
        assert result.pre_hook_ok is True
        assert result.post_hook_ok is True
        assert result.reason == "token_budget"

    def test_execute_strips_media(self, pipeline: CompactionPipeline) -> None:
        context = "intro\n![img](data:image/png;base64,abc)\nconclusion"
        result = pipeline.execute(
            session_id="s-2",
            context_text=context,
            tokens_before=500,
        )
        assert "data:image" not in result.compacted_text

    def test_execute_without_strip_media(self) -> None:
        pipeline = CompactionPipeline(plugin_manager=None)
        context = "intro\n![img](data:image/png;base64,abc)\nconclusion"
        result = pipeline.execute(
            session_id="s-3",
            context_text=context,
            tokens_before=500,
            strip_media=False,
        )
        # summarize_context replaces text, so the result is a summary, not original
        assert result.compacted_text != ""
        assert result.tokens_saved >= 0

    def test_pre_compact_hook_raises_fails_safe(self) -> None:
        pm = MagicMock()
        pm.hook.on_pre_compact.side_effect = RuntimeError("hook bug")
        pipeline = CompactionPipeline(plugin_manager=pm)

        with patch("logging.Logger.warning"):
            result = pipeline.execute(
                session_id="s-4",
                context_text="test",
                tokens_before=200,
            )
        assert result.pre_hook_ok is False
        assert result.post_hook_ok is True  # post hooks still run

    def test_post_compact_hook_raises_fails_safe(self) -> None:
        pm = MagicMock()
        pm.hook.on_post_compact.side_effect = ValueError("post hook bug")
        pipeline = CompactionPipeline(plugin_manager=pm)

        with patch("logging.Logger.warning"):
            result = pipeline.execute(
                session_id="s-5",
                context_text="test",
                tokens_before=200,
            )
        assert result.pre_hook_ok is True
        assert result.post_hook_ok is False

    def test_pre_compact_hook_can_return_false(self) -> None:
        pm = MagicMock()
        pm.hook.on_pre_compact.return_value = [False]
        pipeline = CompactionPipeline(plugin_manager=pm)

        result = pipeline.execute(
            session_id="s-6",
            context_text="test",
            tokens_before=200,
        )
        assert result.pre_hook_ok is False


class TestSensitiveGateIntegration:
    """The pipeline refuses to forward credential-shaped content to the LLM."""

    PEM_BLOCK = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA7bq7wmiCPDlKcQXn3pg9qF4mUJVjcMZLxQ2R8aTnW1sBvKd0\n"
        "-----END RSA PRIVATE KEY-----"
    )

    def test_pem_block_never_reaches_summary_boundary(self) -> None:
        """Acceptance: fixture PEM content is absent from the outbound payload."""
        pipeline = CompactionPipeline(plugin_manager=None)
        context = f"# Notes\nkey file contents:\n{self.PEM_BLOCK}\nmore notes"
        summary_mock = MagicMock(return_value="[summary]")
        with patch(
            "bernstein.core.tokens.compaction_pipeline.summarize_context",
            summary_mock,
        ):
            result = pipeline.execute(
                session_id="s-gate-1",
                context_text=context,
                tokens_before=500,
            )
        assert result.gate_action == "redacted"
        summary_mock.assert_called_once()
        outbound_text = summary_mock.call_args.args[0]
        assert "PRIVATE KEY" not in outbound_text
        assert "MIIEowIBAAKCAQEA" not in outbound_text
        assert "[REDACTED:content.pem-private-key:" in outbound_text

    def test_refusal_skips_summary_and_keeps_original(self) -> None:
        pipeline = CompactionPipeline(plugin_manager=None)
        context = "$ cat .env\nAWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCyEXAMPLEKEY\n"
        summary_mock = MagicMock(return_value="[summary]")
        with patch(
            "bernstein.core.tokens.compaction_pipeline.summarize_context",
            summary_mock,
        ):
            result = pipeline.execute(
                session_id="s-gate-2",
                context_text=context,
                tokens_before=500,
            )
        summary_mock.assert_not_called()
        assert result.gate_action == "refused"
        assert result.compacted_text == context
        assert result.tokens_saved == 0

    def test_gate_can_be_disabled_per_call(self) -> None:
        pipeline = CompactionPipeline(plugin_manager=None)
        summary_mock = MagicMock(return_value="[summary]")
        with patch(
            "bernstein.core.tokens.compaction_pipeline.summarize_context",
            summary_mock,
        ):
            result = pipeline.execute(
                session_id="s-gate-3",
                context_text=f"x\n{self.PEM_BLOCK}\ny",
                tokens_before=500,
                sensitive_gate=False,
            )
        assert result.gate_action == "allow"
        assert "PRIVATE KEY" in summary_mock.call_args.args[0]

    def test_gate_events_land_in_injected_audit_chain(self, tmp_path: object) -> None:
        from pathlib import Path

        from bernstein.core.security.audit_chain import (
            EVENT_COMPACTION_SENSITIVE_GATE,
            AuditChainStore,
        )

        chain = AuditChainStore(Path(str(tmp_path)) / "audit", key=b"k" * 32)
        pipeline = CompactionPipeline(plugin_manager=None)
        result = pipeline.execute(
            session_id="s-gate-4",
            context_text=f"x\n{self.PEM_BLOCK}\ny",
            tokens_before=500,
            task_id="task-42",
            audit_chain=chain,
        )
        assert result.gate_action == "redacted"
        events = chain.query(event_type=EVENT_COMPACTION_SENSITIVE_GATE)
        assert len(events) == 1
        assert events[0].details["task_id"] == "task-42"
        assert events[0].details["action"] == "redacted"
        ok, errors = chain.verify()
        assert ok, errors

    def test_gate_result_defaults_to_allow_for_clean_context(self) -> None:
        pipeline = CompactionPipeline(plugin_manager=None)
        result = pipeline.execute(
            session_id="s-gate-5",
            context_text="# Header\nnormal notes\n",
            tokens_before=100,
        )
        assert result.gate_action == "allow"
        assert result.gate_rule_ids == ()


class TestPayloads:
    def test_pre_compact_payload_fields(self) -> None:
        payload = PreCompactPayload(
            session_id="s-1",
            context_text="hello",
            tokens_before=100,
            reason="token_budget",
            metadata={"key": "value"},
        )
        assert payload.session_id == "s-1"
        assert payload.metadata["key"] == "value"

    def test_post_compact_payload_fields(self) -> None:
        payload = PostCompactPayload(
            session_id="s-1",
            compacted_text="compact",
            tokens_before=100,
            tokens_after=50,
            correlation_id="c-1",
            reason="token_budget",
            summary="shortened",
        )
        assert payload.correlation_id == "c-1"
        assert payload.summary == "shortened"
