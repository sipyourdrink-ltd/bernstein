"""URL scheme allow-listing for outbound HTTP requests.

This module exists to lock down operator-supplied URLs passed to
``urllib.request.urlopen`` so they cannot be coerced into reading local
files (``file://``), launching FTP transfers, or being interpreted as some
other scheme that ``urllib`` happens to support out of the box.

The Semgrep rule
``python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected``
flags every dynamic ``urlopen`` call site because of exactly that risk. By
piping operator-supplied URLs through :func:`ensure_http_url` first we get a
defence-in-depth check independent of the per-call-site allow-list comments.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable
from typing import Final
from urllib.parse import urlparse

__all__ = ["UrlSchemeError", "ensure_http_url", "ensure_public_http_url"]

# Resolves a hostname to a list of textual IP addresses. Injectable so tests can
# feed hostile answers without depending on DNS.
HostResolver = Callable[[str], Iterable[str]]


class UrlSchemeError(ValueError):
    """Raised when a URL is rejected by :func:`ensure_http_url`."""


_HTTPS_ONLY: Final[frozenset[str]] = frozenset({"https"})
_HTTP_AND_HTTPS: Final[frozenset[str]] = frozenset({"http", "https"})
# Hosts that always permit plain HTTP. ``0.0.0.0`` is intentionally NOT
# included: it is the "bind-any" address, not a loopback target, and
# treating it as loopback would silently allow non-localhost HTTP in
# environments that translate ``0.0.0.0`` to a routable interface.
_LOCAL_HOSTS: Final[frozenset[str]] = frozenset({"localhost", "127.0.0.1", "::1"})


def ensure_http_url(
    url: str,
    *,
    allow_http: bool = False,
    source: str = "",
    strict: bool = False,
    resolver: HostResolver | None = None,
) -> str:
    """Validate that ``url`` has an http(s) scheme; return it unchanged.

    This is a validate-and-passthrough guard: every accept path returns
    the same value (the input ``url``), and rejection is signalled by
    raising :class:`UrlSchemeError` rather than by a sentinel return.
    That invariant return is intentional (hence the ``S3516`` waiver) and
    must not be relaxed into an always-allow.

    Args:
        url: The candidate URL string.
        allow_http: When True, accept ``http://`` URLs in addition to
            ``https://``. Even when False, plain ``http://`` is still accepted
            for localhost / loopback hosts so developers can hit local mock
            servers without flipping the flag globally.
        source: Optional human-readable label used in the error message
            (e.g. ``"jira webhook"``) for easier debugging.

    Returns:
        ``url`` if it passes validation.

    Raises:
        UrlSchemeError: If the URL is empty, unparseable, or uses any scheme
            other than the permitted ones.
    """
    if not url or not isinstance(url, str):
        raise UrlSchemeError(_msg(source, "URL is empty or not a string"))

    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if not scheme:
        raise UrlSchemeError(_msg(source, f"URL has no scheme: {url!r}"))

    allowed = _HTTP_AND_HTTPS if allow_http else _HTTPS_ONLY
    host = (parsed.hostname or "").lower()
    if scheme == "http" and host in _LOCAL_HOSTS:
        # Loopback hosts are always permitted on plain HTTP - most operator
        # toolchains expect to be able to point Bernstein at a local mock.
        return url
    if scheme not in allowed:
        raise UrlSchemeError(
            _msg(
                source,
                f"URL scheme {scheme!r} is not permitted (allowed: {sorted(allowed)!r}); url={url!r}",
            )
        )
    # Strict mode: reject internal destinations after hostname resolution.
    if strict:
        host = (parsed.hostname or "").lower()
        if not host:
            raise UrlSchemeError(_msg(source, f"URL has no host: {url!r}"))
        resolve = resolver or _default_resolver
        try:
            addresses = list(resolve(host))
        except OSError as exc:
            raise UrlSchemeError(_msg(source, f"host {host!r} could not be resolved: {exc}; url={url!r}")) from exc
        if not addresses:
            raise UrlSchemeError(_msg(source, f"host {host!r} resolved to no addresses; url={url!r}"))
        for address in addresses:
            try:
                internal = _is_internal(address)
            except ValueError as exc:
                raise UrlSchemeError(
                    _msg(source, f"host {host!r} resolved to an unparseable address {address!r}")
                ) from exc
            if internal:
                raise UrlSchemeError(
                    _msg(
                        source,
                        f"host {host!r} resolves to internal address {address!r}, "
                        f"which is not a permitted destination; url={url!r}",
                    )
                )
    return url


def _msg(source: str, body: str) -> str:
    prefix = f"{source}: " if source else ""
    return f"{prefix}{body}"


def _default_resolver(host: str) -> list[str]:
    """Resolve ``host`` to every address it answers with, v4 and v6."""
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    # sockaddr is (host, port) for v4 and (host, port, flowinfo, scope_id) for v6;
    # the address is element 0 either way, but the tuple type is str | int.
    return [str(info[4][0]) for info in infos]


def _is_internal(address: str) -> bool:
    """Is ``address`` an address a third party must not be able to aim us at?"""
    ip = ipaddress.ip_address(address)
    # ::ffff:127.0.0.1 is loopback wearing an IPv6 coat; judge the address it maps to.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped

    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified


def ensure_public_http_url(
    url: str,
    *,
    allow_http: bool = False,
    source: str = "",
    resolver: HostResolver | None = None,
) -> str:
    """Validate ``url`` for a destination supplied by third-party content.

    :func:`ensure_http_url` checks the scheme and stops there, which is the
    right level of trust for a URL an operator configured: an operator who
    points Bernstein at their own webhook is not attacking themselves. It is
    the wrong level for a URL read out of *fetched content* — an entry inside a
    catalog index we downloaded — because whoever authored that entry chooses
    the destination. A crafted entry can name ``127.0.0.1``, a cloud metadata
    service at ``169.254.169.254``, or any private range, and a scheme-only
    check waves it through.

    This is the strict sibling: scheme validation first (so the existing
    behaviour and error messages are reused verbatim), then hostname
    resolution, then rejection of loopback, private, link-local, reserved,
    multicast and unspecified destinations.

    **Every** resolved address must be public. A name answering with one public
    and one private address is rejected, since which one the HTTP client picks
    is not ours to decide.

    Resolution failure is a rejection, not a pass-through: an unresolvable host
    is not evidence of a safe host.

    This validates the URL you pass it and nothing that URL later leads to. It
    is a single pre-fetch check, so a public host that answers with a redirect
    to ``127.0.0.1`` or ``169.254.169.254`` still reaches the client unchecked,
    and a name re-resolved between this call and the connection can answer
    differently the second time. Callers handling third-party-derived URLs must
    therefore disable redirect following, or re-run this check on every hop.

    Args:
        url: The candidate URL string.
        allow_http: When True, accept ``http://`` as well as ``https://``.
            Note that the loopback exemption :func:`ensure_http_url` grants to
            plain HTTP is irrelevant here, since loopback is rejected outright.
        source: Optional human-readable label used in the error message.
        resolver: Optional hostname resolver, for tests. Defaults to
            :func:`socket.getaddrinfo`.

    Returns:
        ``url`` if it passes validation.

    Raises:
        UrlSchemeError: If the scheme is not permitted, the host is missing or
            unresolvable, or any resolved address is internal.
    """
    ensure_http_url(url, allow_http=allow_http, source=source)

    host = (urlparse(url).hostname or "").lower()
    if not host:
        raise UrlSchemeError(_msg(source, f"URL has no host: {url!r}"))

    resolve = resolver or _default_resolver
    try:
        addresses = list(resolve(host))
    except OSError as exc:
        raise UrlSchemeError(_msg(source, f"host {host!r} could not be resolved: {exc}; url={url!r}")) from exc

    if not addresses:
        raise UrlSchemeError(_msg(source, f"host {host!r} resolved to no addresses; url={url!r}"))

    for address in addresses:
        try:
            internal = _is_internal(address)
        except ValueError as exc:
            raise UrlSchemeError(_msg(source, f"host {host!r} resolved to an unparseable address {address!r}")) from exc

        if internal:
            raise UrlSchemeError(
                _msg(
                    source,
                    f"host {host!r} resolves to internal address {address!r}, "
                    f"which is not a permitted destination; url={url!r}",
                )
            )

    return url
