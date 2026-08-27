"""Shared fixtures for chaos tests."""

import pytest


@pytest.fixture(autouse=True)
def _auth_disabled_for_chaos_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable auth for chaos tests that use respx.mock to proxy test_client requests."""
    monkeypatch.setenv("BERNSTEIN_AUTH_DISABLED", "1")
