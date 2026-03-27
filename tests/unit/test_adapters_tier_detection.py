"""Tests for adapter tier detection — mocks provider responses."""

from __future__ import annotations

import os
from unittest.mock import patch

from bernstein.adapters.claude import ClaudeCodeAdapter
from bernstein.adapters.codex import CodexAdapter
from bernstein.adapters.gemini import GeminiAdapter
from bernstein.adapters.qwen import QwenAdapter
from bernstein.core.models import ApiTier, ProviderType

# --- Claude Adapter Tests ---


class TestClaudeAdapterTierDetection:
    def test_detect_tier_pro_key(self) -> None:
        adapter = ClaudeCodeAdapter()
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-api03-test-key"}):
            info = adapter.detect_tier()
            assert info is not None
            assert info.provider == ProviderType.CLAUDE
            assert info.tier == ApiTier.PRO
            assert info.is_active is True
            assert info.rate_limit is not None
            assert info.rate_limit.requests_per_minute == 1000

    def test_detect_tier_plus_key(self) -> None:
        adapter = ClaudeCodeAdapter()
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-api01-test-key"}):
            info = adapter.detect_tier()
            assert info is not None
            assert info.provider == ProviderType.CLAUDE
            assert info.tier == ApiTier.PLUS
            assert info.rate_limit is not None
            assert info.rate_limit.requests_per_minute == 100

    def test_detect_tier_free_key(self) -> None:
        adapter = ClaudeCodeAdapter()
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-other-test-key"}):
            info = adapter.detect_tier()
            assert info is not None
            assert info.provider == ProviderType.CLAUDE
            assert info.tier == ApiTier.FREE
            assert info.rate_limit is not None
            assert info.rate_limit.requests_per_minute == 20

    def test_detect_tier_no_api_key(self) -> None:
        adapter = ClaudeCodeAdapter()
        with patch.dict(os.environ, {}, clear=True):
            # Remove ANTHROPIC_API_KEY if it exists
            os.environ.pop("ANTHROPIC_API_KEY", None)
            info = adapter.detect_tier()
            assert info is None

    def test_detect_tier_rate_limits(self) -> None:
        adapter = ClaudeCodeAdapter()
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-api03-pro-key"}):
            info = adapter.detect_tier()
            assert info is not None
            assert info.rate_limit is not None
            assert info.rate_limit.requests_per_minute == 1000
            assert info.rate_limit.tokens_per_minute == 50000


# --- Gemini Adapter Tests ---


class TestGeminiAdapterTierDetection:
    def test_detect_tier_enterprise_with_gcp_project(self) -> None:
        adapter = GeminiAdapter()
        with patch.dict(
            os.environ,
            {
                "GOOGLE_API_KEY": "AIza-test-key",
                "GOOGLE_CLOUD_PROJECT": "my-enterprise-project",
            },
        ):
            info = adapter.detect_tier()
            assert info is not None
            assert info.provider == ProviderType.GEMINI
            assert info.tier == ApiTier.ENTERPRISE
            assert info.rate_limit is not None
            assert info.rate_limit.requests_per_minute == 1000

    def test_detect_tier_pro_with_api_key(self) -> None:
        adapter = GeminiAdapter()
        with patch.dict(os.environ, {"GOOGLE_API_KEY": "AIza-pro-key"}):
            info = adapter.detect_tier()
            assert info is not None
            assert info.provider == ProviderType.GEMINI
            assert info.tier == ApiTier.PRO
            assert info.rate_limit is not None
            assert info.rate_limit.requests_per_minute == 100

    def test_detect_tier_free_with_non_standard_key(self) -> None:
        adapter = GeminiAdapter()
        with patch.dict(os.environ, {"GOOGLE_API_KEY": "non-standard-key-format"}):
            info = adapter.detect_tier()
            assert info is not None
            assert info.provider == ProviderType.GEMINI
            assert info.tier == ApiTier.FREE
            assert info.rate_limit is not None
            assert info.rate_limit.requests_per_minute == 15

    def test_detect_tier_no_api_key(self) -> None:
        adapter = GeminiAdapter()
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("GOOGLE_API_KEY", None)
            os.environ.pop("GOOGLE_CLOUD_PROJECT", None)
            info = adapter.detect_tier()
            assert info is None

    def test_detect_tier_enterprise_rate_limits(self) -> None:
        adapter = GeminiAdapter()
        with patch.dict(
            os.environ,
            {
                "GOOGLE_API_KEY": "AIza-test",
                "GOOGLE_CLOUD_PROJECT": "enterprise-project",
            },
        ):
            info = adapter.detect_tier()
            assert info is not None
            assert info.rate_limit is not None
            assert info.rate_limit.requests_per_minute == 1000
            assert info.rate_limit.tokens_per_minute == 100000


# --- Codex Adapter Tests ---


class TestCodexAdapterTierDetection:
    def test_detect_tier_enterprise_with_org_id(self) -> None:
        adapter = CodexAdapter()
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "sk-test-key",
                "OPENAI_ORG_ID": "org-12345",
            },
        ):
            info = adapter.detect_tier()
            assert info is not None
            assert info.provider == ProviderType.CODEX
            assert info.tier == ApiTier.ENTERPRISE
            assert info.rate_limit is not None
            assert info.rate_limit.requests_per_minute == 500

    def test_detect_tier_pro_with_proj_key(self) -> None:
        adapter = CodexAdapter()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-proj-test-key"}):
            info = adapter.detect_tier()
            assert info is not None
            assert info.provider == ProviderType.CODEX
            assert info.tier == ApiTier.PRO
            assert info.rate_limit is not None
            assert info.rate_limit.requests_per_minute == 100

    def test_detect_tier_plus_with_standard_key(self) -> None:
        adapter = CodexAdapter()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-standard-key"}):
            info = adapter.detect_tier()
            assert info is not None
            assert info.provider == ProviderType.CODEX
            assert info.tier == ApiTier.PLUS
            assert info.rate_limit is not None
            assert info.rate_limit.requests_per_minute == 60

    def test_detect_tier_free_with_invalid_key(self) -> None:
        adapter = CodexAdapter()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "invalid-key-format"}):
            info = adapter.detect_tier()
            assert info is not None
            assert info.provider == ProviderType.CODEX
            assert info.tier == ApiTier.FREE
            assert info.rate_limit is not None
            assert info.rate_limit.requests_per_minute == 20

    def test_detect_tier_no_api_key(self) -> None:
        adapter = CodexAdapter()
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("OPENAI_API_KEY", None)
            os.environ.pop("OPENAI_ORG_ID", None)
            info = adapter.detect_tier()
            assert info is None

    def test_detect_tier_enterprise_tokens_limit(self) -> None:
        adapter = CodexAdapter()
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "sk-test",
                "OPENAI_ORG_ID": "org-123",
            },
        ):
            info = adapter.detect_tier()
            assert info is not None
            assert info.rate_limit is not None
            assert info.rate_limit.tokens_per_minute == 90000


# --- Qwen Adapter Tests ---


class TestQwenAdapterTierDetection:
    def test_detect_tier_pro_with_openrouter_paid(self) -> None:
        adapter = QwenAdapter()
        with patch("bernstein.adapters.qwen.LLMSettings") as mock_settings:
            mock_settings.return_value.openrouter_api_key_paid = "sk-or-paid-key"
            mock_settings.return_value.openrouter_api_key_free = None
            mock_settings.return_value.togetherai_user_key = None
            mock_settings.return_value.oxen_api_key = None
            mock_settings.return_value.g4f_api_key = None
            mock_settings.return_value.openai_api_key = None

            info = adapter.detect_tier()
            assert info is not None
            assert info.provider == ProviderType.QWEN
            assert info.tier == ApiTier.PRO
            assert info.rate_limit is not None
            assert info.rate_limit.requests_per_minute == 200

    def test_detect_tier_free_with_openrouter_free(self) -> None:
        adapter = QwenAdapter()
        with patch("bernstein.adapters.qwen.LLMSettings") as mock_settings:
            mock_settings.return_value.openrouter_api_key_free = "sk-or-free-key"
            mock_settings.return_value.openrouter_api_key_paid = None
            mock_settings.return_value.togetherai_user_key = None
            mock_settings.return_value.oxen_api_key = None
            mock_settings.return_value.g4f_api_key = None
            mock_settings.return_value.openai_api_key = None

            info = adapter.detect_tier()
            assert info is not None
            assert info.provider == ProviderType.QWEN
            assert info.tier == ApiTier.FREE
            assert info.rate_limit is not None
            assert info.rate_limit.requests_per_minute == 20

    def test_detect_tier_plus_with_together_ai(self) -> None:
        adapter = QwenAdapter()
        with patch("bernstein.adapters.qwen.LLMSettings") as mock_settings:
            mock_settings.return_value.togetherai_user_key = "together-key"
            mock_settings.return_value.openrouter_api_key_paid = None
            mock_settings.return_value.openrouter_api_key_free = None
            mock_settings.return_value.oxen_api_key = None
            mock_settings.return_value.g4f_api_key = None
            mock_settings.return_value.openai_api_key = None

            info = adapter.detect_tier()
            assert info is not None
            assert info.provider == ProviderType.QWEN
            assert info.tier == ApiTier.PLUS
            assert info.rate_limit is not None
            assert info.rate_limit.requests_per_minute == 60

    def test_detect_tier_pro_with_oxen(self) -> None:
        adapter = QwenAdapter()
        with patch("bernstein.adapters.qwen.LLMSettings") as mock_settings:
            mock_settings.return_value.oxen_api_key = "oxen-key"
            mock_settings.return_value.openrouter_api_key_paid = None
            mock_settings.return_value.openrouter_api_key_free = None
            mock_settings.return_value.togetherai_user_key = None
            mock_settings.return_value.g4f_api_key = None
            mock_settings.return_value.openai_api_key = None

            info = adapter.detect_tier()
            assert info is not None
            assert info.provider == ProviderType.QWEN
            assert info.tier == ApiTier.PRO
            assert info.rate_limit is not None
            assert info.rate_limit.requests_per_minute == 100

    def test_detect_tier_free_with_g4f(self) -> None:
        adapter = QwenAdapter()
        with patch("bernstein.adapters.qwen.LLMSettings") as mock_settings:
            mock_settings.return_value.g4f_api_key = "g4f-key"
            mock_settings.return_value.openrouter_api_key_paid = None
            mock_settings.return_value.openrouter_api_key_free = None
            mock_settings.return_value.togetherai_user_key = None
            mock_settings.return_value.oxen_api_key = None
            mock_settings.return_value.openai_api_key = None

            info = adapter.detect_tier()
            assert info is not None
            assert info.provider == ProviderType.QWEN
            assert info.tier == ApiTier.FREE
            assert info.rate_limit is not None
            assert info.rate_limit.requests_per_minute == 10

    def test_detect_tier_plus_with_default_openai(self) -> None:
        adapter = QwenAdapter()
        with patch("bernstein.adapters.qwen.LLMSettings") as mock_settings:
            mock_settings.return_value.openai_api_key = "sk-default-key"
            mock_settings.return_value.openrouter_api_key_paid = None
            mock_settings.return_value.openrouter_api_key_free = None
            mock_settings.return_value.togetherai_user_key = None
            mock_settings.return_value.oxen_api_key = None
            mock_settings.return_value.g4f_api_key = None

            info = adapter.detect_tier()
            assert info is not None
            assert info.provider == ProviderType.QWEN
            assert info.tier == ApiTier.PLUS
            assert info.rate_limit is not None
            assert info.rate_limit.requests_per_minute == 60

    def test_detect_tier_no_api_keys(self) -> None:
        adapter = QwenAdapter()
        with patch("bernstein.adapters.qwen.LLMSettings") as mock_settings:
            mock_settings.return_value.openrouter_api_key_paid = None
            mock_settings.return_value.openrouter_api_key_free = None
            mock_settings.return_value.togetherai_user_key = None
            mock_settings.return_value.oxen_api_key = None
            mock_settings.return_value.g4f_api_key = None
            mock_settings.return_value.openai_api_key = None

            info = adapter.detect_tier()
            assert info is None


# --- Base Adapter Tests ---


class TestBaseAdapterTierDetection:
    """Tests for the base CLIAdapter detect_tier default implementation."""

    def test_base_detect_tier_returns_none(self) -> None:
        from bernstein.adapters.base import CLIAdapter

        class TestAdapter(CLIAdapter):
            def spawn(self, *args, **kwargs):  # type: ignore
                pass

            def is_alive(self, pid: int) -> bool:
                return True

            def kill(self, pid: int) -> None:
                pass

            def name(self) -> str:
                return "Test"

        adapter = TestAdapter()
        info = adapter.detect_tier()
        assert info is None


# --- Integration Tests ---


class TestAllAdaptersTierDetection:
    """Integration tests verifying all adapters have detect_tier implemented."""

    def test_claude_adapter_has_detect_tier(self) -> None:
        adapter = ClaudeCodeAdapter()
        assert hasattr(adapter, "detect_tier")
        assert callable(adapter.detect_tier)

    def test_gemini_adapter_has_detect_tier(self) -> None:
        adapter = GeminiAdapter()
        assert hasattr(adapter, "detect_tier")
        assert callable(adapter.detect_tier)

    def test_codex_adapter_has_detect_tier(self) -> None:
        adapter = CodexAdapter()
        assert hasattr(adapter, "detect_tier")
        assert callable(adapter.detect_tier)

    def test_qwen_adapter_has_detect_tier(self) -> None:
        adapter = QwenAdapter()
        assert hasattr(adapter, "detect_tier")
        assert callable(adapter.detect_tier)

    def test_all_adapters_return_correct_provider_type(self) -> None:
        """Verify each adapter returns the correct ProviderType."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-api03-test"}):
            claude_info = ClaudeCodeAdapter().detect_tier()
            assert claude_info is not None
            assert claude_info.provider == ProviderType.CLAUDE

        with patch.dict(os.environ, {"GOOGLE_API_KEY": "AIza-test"}):
            gemini_info = GeminiAdapter().detect_tier()
            assert gemini_info is not None
            assert gemini_info.provider == ProviderType.GEMINI

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-proj-test"}):
            codex_info = CodexAdapter().detect_tier()
            assert codex_info is not None
            assert codex_info.provider == ProviderType.CODEX

        with patch("bernstein.adapters.qwen.LLMSettings") as mock_settings:
            mock_settings.return_value.openai_api_key = "sk-test"
            mock_settings.return_value.openrouter_api_key_paid = None
            mock_settings.return_value.openrouter_api_key_free = None
            mock_settings.return_value.togetherai_user_key = None
            mock_settings.return_value.oxen_api_key = None
            mock_settings.return_value.g4f_api_key = None
            qwen_info = QwenAdapter().detect_tier()
            assert qwen_info is not None
            assert qwen_info.provider == ProviderType.QWEN
