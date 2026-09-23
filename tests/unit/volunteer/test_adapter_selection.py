"""Tests for volunteer adapter selection — local-first default posture."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from bernstein.core.volunteer.adapter_selection import EndpointRef, select_adapter_for_volunteer


class TestAdapterSelection:
    """Adapter selection tests for volunteer mode's local-first posture."""

    def test_no_explicit_choice_and_a_certified_local_endpoint_available_selects_local(self) -> None:
        """When no adapter is chosen and a local endpoint is certified, select local."""
        with patch(
            "bernstein.core.volunteer.adapter_selection.certified_roles_for_endpoint", autospec=True
        ) as mock_cert:
            mock_cert.return_value = ["backend", "qa"]  # local endpoint certified for needed role
            candidate = EndpointRef(base_url="http://localhost:11434/v1", model="qwen2.5-coder:7b")
            result = select_adapter_for_volunteer(
                role="backend", explicit_adapter=None, workdir=Path("."), local_endpoint=candidate
            )
            assert result == "local"

    def test_an_explicit_provider_choice_is_honored_even_when_local_is_available(self) -> None:
        """Explicit choice overrides local-first default."""
        with patch(
            "bernstein.core.volunteer.adapter_selection.certified_roles_for_endpoint", autospec=True
        ) as mock_cert:
            mock_cert.return_value = ["backend"]  # local available
            candidate = EndpointRef(base_url="http://localhost:11434/v1", model="qwen2.5-coder:7b")
            result = select_adapter_for_volunteer(
                role="backend", explicit_adapter="claude", workdir=Path("."), local_endpoint=candidate
            )
            assert result == "claude"

    def test_no_explicit_choice_and_no_local_endpoint_available_falls_back_to_whatever_is_configured(
        self,
    ) -> None:
        """When no local endpoint candidate is present, fall back with warning."""
        result = select_adapter_for_volunteer(
            role="backend", explicit_adapter=None, workdir=Path("."), local_endpoint=None
        )
        assert result is None

    def test_the_selection_function_never_reads_provider_credentials_to_decide(self) -> None:
        """Selection must not depend on incidental environment like ANTHROPIC_API_KEY."""
        import os

        # Set a provider key
        original = os.environ.get("ANTHROPIC_API_KEY")
        try:
            os.environ["ANTHROPIC_API_KEY"] = "sk-test-fake"
            with patch(
                "bernstein.core.volunteer.adapter_selection.certified_roles_for_endpoint", autospec=True
            ) as mock_cert:
                mock_cert.return_value = []
                candidate = EndpointRef(base_url="http://localhost:11434/v1", model="qwen2.5-coder:7b")
                result = select_adapter_for_volunteer(
                    role="backend", explicit_adapter=None, workdir=Path("."), local_endpoint=candidate
                )
                # Result should be None regardless of env vars
                assert result is None

                # Now with local certified
                mock_cert.return_value = ["backend"]
                result = select_adapter_for_volunteer(
                    role="backend", explicit_adapter=None, workdir=Path("."), local_endpoint=candidate
                )
                assert result == "local"
        finally:
            if original is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = original

    def test_certified_local_endpoint_returns_local_with_new_signature(self) -> None:
        """When a local endpoint is certified for the role, the function returns 'local'."""
        with patch(
            "bernstein.core.volunteer.adapter_selection.certified_roles_for_endpoint", autospec=True
        ) as mock_cert:
            mock_cert.return_value = {"backend"}
            candidate = EndpointRef(base_url="http://example.com", model="test-model")
            result = select_adapter_for_volunteer(
                role="backend",
                explicit_adapter=None,
                workdir=Path("/tmp"),
                local_endpoint=candidate,
            )
            assert result == "local"
