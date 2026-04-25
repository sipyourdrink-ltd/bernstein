"""Unit tests for :mod:`bernstein.core.preview.token_issuer`."""

from __future__ import annotations

import pytest

from bernstein.core.preview.token_issuer import IssuedAuth, PreviewTokenIssuer


def test_issue_token_mode_renders_query_string() -> None:
    """Token mode appends a ``?token=…`` query string to the URL."""
    issuer = PreviewTokenIssuer(secret="x" * 32)
    issued = issuer.issue(
        preview_id="prv-abc",
        mode="token",
        expires_in_seconds=3600,
    )
    assert issued.mode == "token"
    assert issued.token, "expected a non-empty token"
    rendered = issued.render_url("https://example.com/")
    assert "token=" in rendered
    assert rendered.startswith("https://example.com/")


def test_issue_token_preserves_existing_query() -> None:
    """An existing query string is preserved with ``&token=`` appended."""
    issuer = PreviewTokenIssuer(secret="x" * 32)
    issued = issuer.issue(preview_id="p", mode="token", expires_in_seconds=600)
    rendered = issued.render_url("https://example.com/?foo=1")
    assert rendered.startswith("https://example.com/?foo=1&token=")


def test_basic_mode_renders_user_password_in_netloc() -> None:
    """Basic mode injects user:password into the URL netloc."""
    issuer = PreviewTokenIssuer(secret="x" * 32)
    issued = issuer.issue(preview_id="p", mode="basic", expires_in_seconds=600)
    assert issued.mode == "basic"
    assert issued.basic_user and issued.basic_password
    rendered = issued.render_url("https://host.tld/")
    assert "@host.tld" in rendered
    assert issued.basic_user in rendered


def test_none_mode_returns_url_unchanged() -> None:
    """``none`` mode is a pass-through."""
    issuer = PreviewTokenIssuer(secret="x" * 32)
    issued = issuer.issue(preview_id="p", mode="none", expires_in_seconds=60)
    assert issued.mode == "none"
    assert issued.render_url("https://x.example/") == "https://x.example/"


def test_unknown_mode_raises_value_error() -> None:
    """Unknown modes are rejected up-front."""
    issuer = PreviewTokenIssuer(secret="x" * 32)
    with pytest.raises(ValueError):
        issuer.issue(preview_id="p", mode="bogus", expires_in_seconds=60)


def test_zero_expiry_raises_value_error() -> None:
    """Zero or negative expiries are rejected."""
    issuer = PreviewTokenIssuer(secret="x" * 32)
    with pytest.raises(ValueError):
        issuer.issue(preview_id="p", mode="token", expires_in_seconds=0)


def test_issued_auth_render_url_handles_empty_base() -> None:
    """An empty base URL is returned unchanged regardless of mode."""
    auth = IssuedAuth(mode="token", token="abc", expires_at_epoch=999.0)
    assert auth.render_url("") == ""
