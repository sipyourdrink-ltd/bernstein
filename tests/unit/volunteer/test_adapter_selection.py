"""Tests for volunteer adapter selection — local-first default posture."""

from __future__ import annotations

from unittest.mock import patch

from bernstein.core.volunteer.adapter_selection import select_adapter_for_volunteer


class TestAdapterSelection:
    """Adapter selection tests for volunteer mode's local-first posture."""

    def test_no_explicit_choice_and_a_certified_local_endpoint_available_selects_local(self) -> None:
        """When no adapter is chosen and a local endpoint is certified, select local."""
        with patch("bernstein.core.volunteer.adapter_selection.certified_roles_for_endpoint") as mock_cert:
            mock_cert.return_value = ["backend", "qa"]  # local endpoint certified for needed role
            result = select_adapter_for_volunteer(role="backend", explicit_adapter=None)
            assert result == "local"

    def test_an_explicit_provider_choice_is_honored_even_when_local_is_available(self) -> None:
        """Explicit choice overrides local-first default."""
        with patch("bernstein.core.volunteer.adapter_selection.certified_roles_for_endpoint") as mock_cert:
            mock_cert.return_value = ["backend"]  # local available
            result = select_adapter_for_volunteer(role="backend", explicit_adapter="claude")
            assert result == "claude"

    def test_no_explicit_choice_and_no_local_endpoint_available_falls_back_to_whatever_is_configured(
        self,
    ) -> None:
        """When no local endpoint and no explicit choice, fall back with warning."""
        with patch("bernstein.core.volunteer.adapter_selection.certified_roles_for_endpoint") as mock_cert:
            mock_cert.return_value = []  # no local endpoint certified
            # Should return None and caller logs warning
            result = select_adapter_for_volunteer(role="backend", explicit_adapter=None)
            assert result is None

    def test_the_selection_function_never_reads_provider_credentials_to_decide(self) -> None:
        """Selection must not depend on incidental environment like ANTHROPIC_API_KEY."""
        import os

        # Set a provider key
        original = os.environ.get("ANTHROPIC_API_KEY")
        try:
            os.environ["ANTHROPIC_API_KEY"] = "sk-test-fake"
            with patch("bernstein.core.volunteer.adapter_selection.certified_roles_for_endpoint") as mock_cert:
                mock_cert.return_value = []
                result = select_adapter_for_volunteer(role="backend", explicit_adapter=None)
                # Result should be None regardless of env vars
                assert result is None
                # Now with local certified
                mock_cert.return_value = ["backend"]
                result = select_adapter_for_volunteer(role="backend", explicit_adapter=None)
                assert result == "local"
        finally:
            if original is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = original
