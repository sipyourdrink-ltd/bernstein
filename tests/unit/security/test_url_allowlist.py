"""Tests for :func:`bernstein.core.security.ensure_http_url`.

``ensure_http_url`` is a validate-and-passthrough guard: every call site
relies on it returning its input unchanged on accept and raising
:class:`UrlSchemeError` on reject. The function is marked ``NOSONAR
python:S3516`` because every accept path returns the same value (the
input ``url``); these tests pin the *reject* paths so the suppression
can never mask a regression that turns the guard into an always-allow.
"""

from __future__ import annotations

import urllib.request
from collections.abc import Callable

import pytest

from bernstein.core.security.url_allowlist import (
    StrictHTTPRedirectHandler,
    UrlSchemeError,
    ensure_http_url,
    ensure_public_http_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/path",
        "https://example.com",
        "https://localhost:8052/health",
    ],
)
def test_https_is_accepted_and_returned_unchanged(url: str) -> None:
    assert ensure_http_url(url) == url


def test_http_rejected_by_default() -> None:
    with pytest.raises(UrlSchemeError):
        ensure_http_url("http://example.com")


def test_http_accepted_when_allow_http_set() -> None:
    url = "http://example.com/webhook"
    assert ensure_http_url(url, allow_http=True) == url


@pytest.mark.parametrize(
    "host",
    ["localhost", "127.0.0.1", "[::1]"],
)
def test_loopback_http_always_allowed(host: str) -> None:
    """Plain HTTP to loopback is permitted even without ``allow_http``.

    IPv6 literals must be bracketed in a URL authority so ``urlparse``
    extracts ``::1`` as the hostname rather than mis-splitting on the
    colons.
    """
    url = f"http://{host}:8052/mock"
    assert ensure_http_url(url) == url


def test_bind_any_host_is_not_treated_as_loopback() -> None:
    """``0.0.0.0`` is the bind-any address, not a loopback target.

    It must NOT inherit the localhost plain-HTTP exemption, otherwise a
    routable interface could receive unencrypted traffic.
    """
    with pytest.raises(UrlSchemeError):
        ensure_http_url("http://0.0.0.0:8052/x")


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/payload",
        "gopher://example.com",
        "data:text/plain;base64,AAAA",
        "jar:file:///tmp/x.jar",
    ],
)
def test_non_http_schemes_are_rejected(url: str) -> None:
    """The whole point of the guard: deny non-http(s) schemes.

    A regression that made this an always-allow guard (returning the URL
    for every input) would surface here as the missing ``UrlSchemeError``.
    """
    with pytest.raises(UrlSchemeError):
        ensure_http_url(url, allow_http=True)


def test_empty_url_rejected() -> None:
    with pytest.raises(UrlSchemeError):
        ensure_http_url("")


def test_schemeless_url_rejected() -> None:
    with pytest.raises(UrlSchemeError):
        ensure_http_url("example.com/no-scheme")


def test_error_message_includes_source_label() -> None:
    with pytest.raises(UrlSchemeError, match="jira webhook"):
        ensure_http_url("ftp://example.com", source="jira webhook")


# --- ensure_public_http_url: the strict sibling for third-party-derived URLs ---

# --- ensure_http_url strict mode tests ---


def test_strict_host_rejection_for_internal_address() -> None:
    with pytest.raises(UrlSchemeError, match="internal address"):
        ensure_http_url(
            "https://evil.example/entry.json",
            strict=True,
            resolver=_resolves_to("127.0.0.1"),
        )


def test_strict_all_public_accepted() -> None:
    url = "https://example.com/catalog/entry.json"
    assert ensure_http_url(url, strict=True, resolver=_resolves_to("93.184.216.34")) == url


def test_strict_missing_host_is_rejected() -> None:
    with pytest.raises(UrlSchemeError, match="no host"):
        ensure_http_url("https:///missing-host", strict=True)


def test_strict_unresolvable_host_is_rejected() -> None:
    def _fails(_host: str) -> list[str]:
        raise OSError("Name or service not known")

    with pytest.raises(UrlSchemeError, match="could not be resolved"):
        ensure_http_url("https://nx.example/entry.json", strict=True, resolver=_fails)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_strict_mode_rejects_the_loopback_host_the_scheme_rule_exempts(host: str) -> None:
    """The plain-HTTP exemption is a scheme exemption, not a strict-mode one.

    Every other strict test on this function uses ``https://``, which never
    reaches the loopback branch - so the branch returned ``url`` before the
    strict block ran, and ``strict=True`` was a no-op for the exact host it
    exists to reject. ``test_loopback_http_exemption_does_not_leak_into_strict_mode``
    below asserts this same invariant, but against ``ensure_public_http_url``,
    where it cannot leak: that function calls this one *without* ``strict``
    and does its own resolution afterwards.
    """
    with pytest.raises(UrlSchemeError, match="internal address"):
        ensure_http_url(
            f"http://{host}:8052/entry.json",
            allow_http=True,
            strict=True,
            resolver=_resolves_to("127.0.0.1"),
        )


def test_strict_mode_rejects_loopback_even_without_allow_http() -> None:
    """``allow_http=False`` was the shape that reached the early return."""
    with pytest.raises(UrlSchemeError, match="internal address"):
        ensure_http_url(
            "http://localhost:8052/entry.json",
            strict=True,
            resolver=_resolves_to("127.0.0.1"),
        )


def test_a_loopback_name_answering_with_a_public_address_is_still_judged_on_that() -> None:
    """Strict mode judges resolved addresses, not the spelling of the host.

    The fix must not degrade into "the string 'localhost' is banned" - the
    check is on where the name points.
    """
    url = "http://localhost:8052/entry.json"
    assert ensure_http_url(url, allow_http=True, strict=True, resolver=_resolves_to("93.184.216.34")) == url


def test_loopback_http_is_still_exempt_from_the_scheme_rule() -> None:
    """The exemption itself is intact: no strict flag, no resolution, accepted.

    This is the developer-pointing-at-a-local-mock case the exemption is for,
    and it is what makes the fix a narrowing of ``strict`` rather than a
    removal of the exemption.
    """
    url = "http://127.0.0.1:8052/health"
    assert ensure_http_url(url) == url


def test_a_non_loopback_http_url_is_still_rejected_on_scheme_before_resolving() -> None:
    """Reordering the branches must not let plain HTTP in through the side."""

    def _must_not_be_called(_host: str) -> list[str]:  # pragma: no cover - asserted by not raising
        raise AssertionError("resolved a URL that should have failed the scheme rule")

    with pytest.raises(UrlSchemeError, match="not permitted"):
        ensure_http_url("http://example.com/x", strict=True, resolver=_must_not_be_called)


def test_strict_source_label_appears_in_error() -> None:
    with pytest.raises(UrlSchemeError, match="skills_catalog.fetcher"):
        ensure_http_url(
            "https://evil.example/x",
            source="skills_catalog.fetcher",
            strict=True,
            resolver=_resolves_to("127.0.0.1"),
        )


#
# The threat these pin: a URL read out of a *fetched* catalog index is chosen by
# whoever authored that index, not by the operator. A scheme-only check lets a
# crafted entry aim the fetcher at loopback, a cloud metadata service, or any
# private range. Every reject case below is a real SSRF target.


def _resolves_to(*addresses: str) -> Callable[[str], list[str]]:
    """A resolver that answers with fixed addresses, so no DNS is needed."""
    return lambda _host: list(addresses)


def test_public_url_is_accepted_and_returned_unchanged() -> None:
    url = "https://example.com/catalog/entry.json"
    assert ensure_public_http_url(url, resolver=_resolves_to("93.184.216.34")) == url


@pytest.mark.parametrize(
    ("address", "what"),
    [
        ("127.0.0.1", "loopback"),
        ("169.254.169.254", "link-local cloud metadata"),
        ("10.0.0.5", "private 10/8"),
        ("172.16.0.5", "private 172.16/12"),
        ("192.168.1.5", "private 192.168/16"),
        ("0.0.0.0", "unspecified"),
        ("::1", "IPv6 loopback"),
        ("fe80::1", "IPv6 link-local"),
        ("fc00::1", "IPv6 unique-local"),
        ("::ffff:127.0.0.1", "IPv4-mapped loopback"),
    ],
)
def test_internal_destinations_are_rejected(address: str, what: str) -> None:
    with pytest.raises(UrlSchemeError, match="internal address"):
        ensure_public_http_url(
            "https://evil.example/entry.json",
            resolver=_resolves_to(address),
        )
    assert what  # label kept for readable parametrize ids


def test_rebinding_style_answer_is_rejected_when_any_address_is_internal() -> None:
    # One public and one private answer: which one the HTTP client picks is not
    # ours to decide, so the whole name is refused.
    with pytest.raises(UrlSchemeError, match="internal address"):
        ensure_public_http_url(
            "https://rebind.example/entry.json",
            resolver=_resolves_to("93.184.216.34", "127.0.0.1"),
        )


def test_unresolvable_host_is_rejected_not_passed_through() -> None:
    def _fails(_host: str) -> list[str]:
        raise OSError("Name or service not known")

    with pytest.raises(UrlSchemeError, match="could not be resolved"):
        ensure_public_http_url("https://nx.example/entry.json", resolver=_fails)


def test_host_resolving_to_nothing_is_rejected() -> None:
    with pytest.raises(UrlSchemeError, match="resolved to no addresses"):
        ensure_public_http_url("https://empty.example/x", resolver=_resolves_to())


def test_scheme_rules_still_apply_before_resolution() -> None:
    with pytest.raises(UrlSchemeError, match="scheme"):
        ensure_public_http_url(
            "http://example.com/entry.json",
            resolver=_resolves_to("93.184.216.34"),
        )


def test_http_allowed_when_opted_in_and_host_is_public() -> None:
    url = "http://example.com/entry.json"
    assert ensure_public_http_url(url, allow_http=True, resolver=_resolves_to("93.184.216.34")) == url


def test_loopback_http_exemption_does_not_leak_into_strict_mode() -> None:
    # ensure_http_url lets plain http through for localhost; strict mode must not.
    with pytest.raises(UrlSchemeError, match="internal address"):
        ensure_public_http_url(
            "http://localhost:8052/entry.json",
            allow_http=True,
            resolver=_resolves_to("127.0.0.1"),
        )


def test_source_label_appears_in_strict_rejection() -> None:
    with pytest.raises(UrlSchemeError, match="skills_catalog.fetcher"):
        ensure_public_http_url(
            "https://evil.example/x",
            source="skills_catalog.fetcher",
            resolver=_resolves_to("127.0.0.1"),
        )


# --- StrictHTTPRedirectHandler: redirect-aware strict URL guard ---


class _FakeFP:
    """Minimal file-like stub for ``redirect_request`` signature."""

    def read(self) -> bytes:
        return b""

    def close(self) -> None:
        pass


class _FakeHeaders(dict):
    """Minimal ``HTTPMessage`` stand-in (``dict`` subclass is sufficient)."""


def _redirect_handler(
    *,
    allow_http: bool = False,
    source: str = "",
    resolver: Callable[[str], list[str]] | None = None,
) -> StrictHTTPRedirectHandler:
    return StrictHTTPRedirectHandler(allow_http=allow_http, source=source, resolver=resolver)


def test_redirect_to_internal_address_is_rejected() -> None:
    """A 302 ``Location`` pointing at loopback must abort before connecting."""
    handler = _redirect_handler(resolver=_resolves_to("127.0.0.1"))
    req = urllib.request.Request("https://example.com/page")
    with pytest.raises(UrlSchemeError, match="internal address"):
        handler.redirect_request(req, _FakeFP(), 302, "Found", _FakeHeaders(), "https://127.0.0.1/inner")


def test_redirect_to_link_local_is_rejected() -> None:
    """Cloud-metadata link-local address via redirect must be blocked."""
    handler = _redirect_handler(resolver=_resolves_to("169.254.169.254"))
    req = urllib.request.Request("https://example.com/page")
    with pytest.raises(UrlSchemeError, match="internal address"):
        handler.redirect_request(req, _FakeFP(), 302, "Found", _FakeHeaders(), "https://169.254.169.254/meta-data/")


def test_redirect_to_private_range_is_rejected() -> None:
    """A ``192.168.x.x`` redirect target is a private-range SSRF vector."""
    handler = _redirect_handler(resolver=_resolves_to("192.168.1.5"))
    req = urllib.request.Request("https://example.com/page")
    with pytest.raises(UrlSchemeError, match="internal address"):
        handler.redirect_request(req, _FakeFP(), 302, "Found", _FakeHeaders(), "https://192.168.1.5/internal")


def test_redirect_to_public_address_succeeds() -> None:
    """A benign public→public redirect is allowed through."""
    handler = _redirect_handler(resolver=_resolves_to("93.184.216.34"))
    req = urllib.request.Request("https://example.com/page")
    result = handler.redirect_request(req, _FakeFP(), 302, "Found", _FakeHeaders(), "https://other.example.com/ok")
    assert result is not None
    assert result.full_url == "https://other.example.com/ok"


def test_redirect_http_allowed_when_opted_in_for_public_host() -> None:
    """``allow_http=True`` permits ``http://`` redirects to public hosts."""
    handler = _redirect_handler(allow_http=True, resolver=_resolves_to("93.184.216.34"))
    req = urllib.request.Request("https://example.com/page")
    result = handler.redirect_request(req, _FakeFP(), 302, "Found", _FakeHeaders(), "http://other.example.com/ok")
    assert result is not None
    assert result.full_url == "http://other.example.com/ok"


def test_redirect_http_internal_rejected_even_with_allow_http() -> None:
    """``allow_http=True`` still rejects ``http://127.0.0.1`` redirect targets."""
    handler = _redirect_handler(allow_http=True, resolver=_resolves_to("127.0.0.1"))
    req = urllib.request.Request("https://example.com/page")
    with pytest.raises(UrlSchemeError, match="internal address"):
        handler.redirect_request(req, _FakeFP(), 302, "Found", _FakeHeaders(), "http://127.0.0.1/internal")


def test_redirect_rebinding_answer_with_mixed_addresses_is_rejected() -> None:
    """One public + one private answer: which one the client picks is not ours to decide."""
    handler = _redirect_handler(resolver=_resolves_to("93.184.216.34", "10.0.0.5"))
    req = urllib.request.Request("https://example.com/page")
    with pytest.raises(UrlSchemeError, match="internal address"):
        handler.redirect_request(req, _FakeFP(), 302, "Found", _FakeHeaders(), "https://rebind.example/ok")


def test_redirect_source_label_appears_in_error() -> None:
    """The ``source`` kwarg is forwarded into the rejection message."""
    handler = _redirect_handler(
        source="volunteer.registry",
        resolver=_resolves_to("127.0.0.1"),
    )
    req = urllib.request.Request("https://example.com/page")
    with pytest.raises(UrlSchemeError, match="volunteer.registry"):
        handler.redirect_request(req, _FakeFP(), 302, "Found", _FakeHeaders(), "https://evil.example/x")


def test_redirect_non_http_scheme_rejected() -> None:
    """A ``file://`` redirect target is rejected at the scheme gate."""
    handler = _redirect_handler(resolver=_resolves_to("127.0.0.1"))
    req = urllib.request.Request("https://example.com/page")
    with pytest.raises(UrlSchemeError, match="scheme"):
        handler.redirect_request(req, _FakeFP(), 302, "Found", _FakeHeaders(), "file:///etc/passwd")
