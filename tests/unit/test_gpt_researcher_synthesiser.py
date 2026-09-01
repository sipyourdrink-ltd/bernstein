"""Unit tests for GptResearcher synthesiser adapter.

These tests verify the GPT Researcher synthesiser adapter properly drives the
upstream GPT Researcher runtime while respecting the orchestrator's
environment isolation and error handling requirements.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bernstein.core.orchestration.gpt_researcher import (
    GptResearcherSynthesiser,
    GptResearcherUnavailableError,
)


def test_gpt_researcher_unavailable_error():
    """Test that importing GptResearcherSynthesiser raises UnavailableError when missing."""
    with patch.dict("sys.modules", {"gpt_researcher": None}):
        with pytest.raises(GptResearcherUnavailableError):
            GptResearcherSynthesiser()


def test_gpt_researcher_synthesiser_initializes_with_default_env():
    """Test synthesiser initializes with default environment variables."""
    with patch.dict("sys.modules", {"gpt_researcher": MagicMock()}):
        with patch("bernstein.adapters.env_isolation.build_filtered_env") as mock_build_env:
            mock_build_env.return_value = {"OPENAI_API_KEY": "test-key"}

            synthesiser = GptResearcherSynthesiser()

            assert synthesiser._extra_keys == ["OPENAI_API_KEY"]
            mock_build_env.assert_called_once_with(extra_keys=["OPENAI_API_KEY"])


def test_gpt_researcher_synthesiser_initializes_with_custom_env():
    """Test synthesiser initializes with custom extra_keys."""
    with patch.dict("sys.modules", {"gpt_researcher": MagicMock()}):
        with patch("bernstein.adapters.env_isolation.build_filtered_env") as mock_build_env:
            mock_build_env.return_value = {"OPENAI_API_KEY": "test-key", "CUSTOM_KEY": "custom-value"}

            synthesiser = GptResearcherSynthesiser(extra_keys=["OPENAI_API_KEY", "CUSTOM_KEY"])

            assert synthesiser._extra_keys == ["OPENAI_API_KEY", "CUSTOM_KEY"]
            mock_build_env.assert_called_once_with(extra_keys=["OPENAI_API_KEY", "CUSTOM_KEY"])


def test_parse_report_to_claims_basic():
    """Test parsing a basic GPT Researcher report into ClaimDraft objects."""
    with patch.dict("sys.modules", {"gpt_researcher": MagicMock()}):
        synthesiser = GptResearcherSynthesiser()

        report = {
            "claims": [
                {
                    "claim": "Python 3.13 has an optional free-threaded build.",
                    "supports": [{"content": "optional free-threaded build", "source": "https://example.com/a"}],
                },
                {
                    "claim": "Module foo is deprecated.",
                    "supports": [{"content": "deprecated and slated for removal", "source": "https://example.com/b"}],
                },
            ]
        }

        claims = synthesiser._parse_report_to_claims(report)

        assert len(claims) == 2
        assert claims[0].statement == "Python 3.13 has an optional free-threaded build."
        assert len(claims[0].spans) == 1
        assert claims[0].spans[0].quote == "optional free-threaded build"
        assert claims[0].spans[0].source_ref == "https://example.com/a"
        assert claims[0].claim_id == "c1"

        assert claims[1].statement == "Module foo is deprecated."
        assert len(claims[1].spans) == 1
        assert claims[1].spans[0].quote == "deprecated and slated for removal"
        assert claims[1].spans[0].source_ref == "https://example.com/b"
        assert claims[1].claim_id == "c2"


def test_parse_report_to_claims_empty_report():
    """Test parsing an empty report (no claims)."""
    with patch.dict("sys.modules", {"gpt_researcher": MagicMock()}):
        synthesiser = GptResearcherSynthesiser()

        report = {"claims": []}

        claims = synthesiser._parse_report_to_claims(report)

        assert len(claims) == 0


def test_parse_report_to_claims_claim_without_spans():
    """Test parsing a claim without supports (empty spans)."""
    with patch.dict("sys.modules", {"gpt_researcher": MagicMock()}):
        synthesiser = GptResearcherSynthesiser()

        report = {
            "claims": [
                {
                    "claim": "Some claim without citations",
                    "supports": [],
                },
                {
                    "claim": "Another claim",
                    "supports": [{"content": "quote", "source": "https://example.com"}],
                },
            ]
        }

        claims = synthesiser._parse_report_to_claims(report)

        assert len(claims) == 2
        assert claims[0].statement == "Some claim without citations"
        assert len(claims[0].spans) == 0
        assert claims[1].statement == "Another claim"
        assert len(claims[1].spans) == 1


def test_call_method_invokes_sync():
    """Test the synchronous __call__ method invokes the internal method correctly."""
    with patch.dict("sys.modules", {"gpt_researcher": MagicMock()}):
        with patch("bernstein.adapters.env_isolation.build_filtered_env") as mock_build_env:
            mock_build_env.return_value = {"OPENAI_API_KEY": "test-key"}

            synthesiser = GptResearcherSynthesiser()

            # Mock the sync method
            mock_sync_method = MagicMock(return_value=[])
            synthesiser._run_synthesiser_sync = mock_sync_method

            query = "Test query"
            fetched = ()

            result = synthesiser(query, fetched)

            mock_sync_method.assert_called_once_with(query, fetched)
            assert result == []


def test_environment_isolation_restricts_unrelated_keys():
    """Test that environment isolation only allows specified keys."""
    with patch.dict("sys.modules", {"gpt_researcher": MagicMock()}):
        with patch("bernstein.adapters.env_isolation.build_filtered_env") as mock_build_env:
            mock_build_env.return_value = {"OPENAI_API_KEY": "openai-secret"}

            synthesiser = GptResearcherSynthesiser()

            assert synthesiser._extra_keys == ["OPENAI_API_KEY"]


def test_gpt_researcher_error_names_package():
    """Test that GptResearcherUnavailableError names the package in its message."""
    with patch.dict("sys.modules", {"gpt_researcher": None}):
        with pytest.raises(GptResearcherUnavailableError) as exc_info:
            GptResearcherSynthesiser()
        assert "gpt-researcher" in str(exc_info.value)
