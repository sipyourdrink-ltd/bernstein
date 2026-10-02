"""Tests for volunteer adapter selection — local-first default posture."""

from __future__ import annotations

import http.client
from pathlib import Path
from unittest.mock import patch

import pytest

from bernstein.core.volunteer.adapter_selection import (
    LOCAL_ADAPTER_ID,
    EndpointRef,
    discover_local_endpoint,
    select_adapter_for_volunteer,
)
from bernstein.core.volunteer.runner import _validate_volunteer_auth_basis


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
            assert result == LOCAL_ADAPTER_ID

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
                assert result == LOCAL_ADAPTER_ID
        finally:
            if original is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = original

    def test_certified_local_endpoint_returns_local_with_new_signature(self) -> None:
        """When a local endpoint is certified for the role, the function returns the local adapter id."""
        with patch(
            "bernstein.core.volunteer.adapter_selection.certified_roles_for_endpoint", autospec=True
        ) as mock_cert:
            mock_cert.return_value = {"backend"}
            candidate = EndpointRef(base_url="http://192.168.1.20:8000/v1", model="test-model")
            result = select_adapter_for_volunteer(
                role="backend",
                explicit_adapter=None,
                workdir=Path("/tmp"),
                local_endpoint=candidate,
            )
            assert result == LOCAL_ADAPTER_ID

    def test_the_selected_adapter_is_registered_and_passes_the_auth_basis_gate(self) -> None:
        """The selected id must be a real adapter the volunteer gate accepts."""
        from bernstein.adapters.registry import get_adapter

        assert get_adapter(LOCAL_ADAPTER_ID) is not None
        assert _validate_volunteer_auth_basis(LOCAL_ADAPTER_ID) is None

    def test_a_certified_but_hosted_endpoint_is_not_selected_as_local(self) -> None:
        """Certification proves capability, not locality."""
        with patch(
            "bernstein.core.volunteer.adapter_selection.certified_roles_for_endpoint", autospec=True
        ) as mock_cert:
            mock_cert.return_value = {"backend"}
            for url in ("https://api.openai.com/v1", "http://93.184.216.34:8000/v1"):
                result = select_adapter_for_volunteer(
                    role="backend",
                    explicit_adapter=None,
                    workdir=Path("."),
                    local_endpoint=EndpointRef(base_url=url, model="m"),
                )
                assert result is None, url


class _Probe:
    """Records what the /models probe would have sent."""

    def __init__(self, *, body: bytes = b'{"data": [{"id": "m1"}]}', exc: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self._body = body
        self._exc = exc

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float):
        self.calls.append((url, dict(headers)))
        if self._exc is not None:
            raise self._exc
        return 200, self._body


class TestDiscoverLocalEndpoint:
    """The probe is credentialed, so it is limited to local hosts and never raises."""

    def _discover(self, probe: _Probe, environ: dict[str, str]):
        with patch("bernstein.core.endpoints.conformance._default_transport", probe):
            return discover_local_endpoint(environ)

    def test_no_base_url_means_no_probe(self) -> None:
        probe = _Probe()
        assert self._discover(probe, {"OPENAI_API_KEY": "sk-secret"}) is None
        assert probe.calls == []

    def test_a_local_host_is_probed_and_resolved(self) -> None:
        probe = _Probe()
        ref = self._discover(probe, {"OPENAI_BASE_URL": "http://127.0.0.1:11434/v1/"})
        assert ref == EndpointRef(base_url="http://127.0.0.1:11434/v1", model="m1")
        assert len(probe.calls) == 1

    @pytest.mark.parametrize(
        "url",
        ["http://93.184.216.34:8000/v1", "http://llm.example.com/v1", "https://api.openai.com/v1"],
    )
    def test_the_api_key_is_never_sent_to_a_non_local_host(self, url: str) -> None:
        probe = _Probe()
        ref = self._discover(probe, {"OPENAI_BASE_URL": url, "OPENAI_API_KEY": "sk-secret"})
        assert ref is None
        assert probe.calls == []

    @pytest.mark.parametrize(
        "exc",
        [
            http.client.IncompleteRead(b"x", 99),
            http.client.BadStatusLine("SSH-2.0-OpenSSH_9.6"),
            ConnectionResetError("reset"),
            TimeoutError("slow"),
        ],
    )
    def test_a_broken_endpoint_yields_no_candidate_instead_of_raising(self, exc: Exception) -> None:
        probe = _Probe(exc=exc)
        assert self._discover(probe, {"OPENAI_BASE_URL": "http://localhost:11434/v1"}) is None

    def test_a_body_that_is_not_utf8_yields_no_candidate_instead_of_raising(self) -> None:
        probe = _Probe(body=b"\xff\xfe not json")
        assert self._discover(probe, {"OPENAI_BASE_URL": "http://localhost:11434/v1"}) is None
