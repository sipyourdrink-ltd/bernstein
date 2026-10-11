"""Opt-in external time anchor for exported Trust Records.

A TRACE 0.2 Trust Record carries its own time in ``iat``, which the
producer writes, and TRACE verifiers reject a record older than 24 hours by
default. Nothing inside the record bounds ``iat`` from outside, so a record
that proved something today cannot be checked by a conformant verifier next
month. An anchor service closes that gap: it receives the exact record,
keeps the bytes, and later commits their digest to a clock the producer does
not control.

This module is the client half only. It is off unless the operator names an
endpoint (``bernstein trace export --anchor-endpoint URL``), it holds no
default host, and nothing else in Bernstein imports it, so no code path
depends on any particular anchor service being reachable.

Wire contract the endpoint must implement::

    POST <endpoint>
    Content-Type: application/json
    {"record": <the signed Trust Record, as a JSON object>}

    2xx  {"sha": "<64 lowercase hex>", "status": "pending" | "anchored", ...}
    4xx  {"reason_code": "<str>", ...}   (refused; optional body)

``sha`` must be the SHA-256 of the RFC 8785 (JCS) form of the record as
submitted. The client recomputes that digest locally and refuses a receipt
that names a different one, so a receipt can only ever refer to the bytes
Bernstein exported. Members beyond ``sha`` and ``status`` are kept verbatim
in :attr:`AnchorReceipt.raw` and never interpreted here.

What a receipt does and does not establish: it says the service accepted
these exact bytes and, once ``status`` is ``anchored``, that it committed
their digest to its external clock. It says nothing about whether any claim
inside the record is true; that is the verifier's appraisal, as it is for
the record's own signature.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.security.url_allowlist import UrlSchemeError, ensure_http_url

if TYPE_CHECKING:
    from collections.abc import Callable

    # (url, request body, timeout seconds) -> (HTTP status, response body)
    PostFn = Callable[[str, bytes, float], tuple[int, bytes]]

DEFAULT_ANCHOR_TIMEOUT_SECS: float = 10.0
ANCHOR_STATUSES: frozenset[str] = frozenset({"pending", "anchored"})
_MAX_RESPONSE_BYTES = 1 << 20
_SHA256_HEX = re.compile(r"\A[0-9a-f]{64}\Z")


class TimeAnchorError(Exception):
    """The record could not be anchored; the message says why."""


@dataclass(frozen=True)
class AnchorReceipt:
    """What the anchor service answered for one record.

    ``sha256`` is the locally recomputed JCS digest, which the service's
    answer was checked against. ``raw`` is the parsed response body, kept
    whole so a receipt file preserves anything the service adds (a lookup
    URL, a key thumbprint) without Bernstein vouching for it.
    """

    endpoint: str
    sha256: str
    status: str
    raw: dict[str, Any]

    def to_json(self) -> str:
        """Serialise the receipt for ``<record>.anchor.json``."""
        body = {
            "endpoint": self.endpoint,
            "record_sha256": self.sha256,
            "status": self.status,
            "response": self.raw,
        }
        return json.dumps(body, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def check_endpoint(endpoint: str) -> str:
    """Return ``endpoint`` if it is acceptable for anchoring, else raise.

    ``https://`` only; plain ``http://`` is accepted for a loopback host so a
    local test service can be used without TLS.
    """
    try:
        return ensure_http_url(endpoint, source="trace anchor endpoint")
    except UrlSchemeError as exc:
        raise TimeAnchorError(str(exc)) from exc


def record_sha256(record_json: str) -> str:
    """Return the hex SHA-256 of the RFC 8785 form of ``record_json``."""
    from bernstein.core.security.agent_card_signer import canonicalize_jcs

    try:
        record: Any = json.loads(record_json)
    except json.JSONDecodeError as exc:
        raise TimeAnchorError(f"record is not valid JSON: {exc}") from exc
    if not isinstance(record, dict):
        raise TimeAnchorError("record must be a JSON object")
    return hashlib.sha256(canonicalize_jcs(record)).hexdigest()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: a redirected POST would drop or resend the record."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _urllib_post(url: str, body: bytes, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        # ``url`` was scheme-validated by :func:`ensure_http_url` in
        # :func:`anchor_record` and is operator-supplied.
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        with opener.open(req, timeout=timeout) as resp:
            return int(getattr(resp, "status", 200)), resp.read(_MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(_MAX_RESPONSE_BYTES)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TimeAnchorError(f"anchor endpoint unreachable: {exc}") from exc


def _refusal_detail(body: bytes) -> str:
    try:
        parsed: Any = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ""
    if isinstance(parsed, dict):
        reason = cast("dict[str, Any]", parsed).get("reason_code")
        if isinstance(reason, str) and reason:
            return f" ({reason})"
    return ""


def anchor_record(
    record_json: str,
    endpoint: str,
    *,
    timeout: float = DEFAULT_ANCHOR_TIMEOUT_SECS,
    post: PostFn | None = None,
) -> AnchorReceipt:
    """Submit one exported record to ``endpoint`` and check the receipt.

    ``endpoint`` must be ``https://``; plain ``http://`` is accepted only for
    a loopback host, for local testing. ``post`` replaces the HTTP transport
    (tests pass a fake; production uses urllib with redirects refused).

    Raises:
        TimeAnchorError: the endpoint is not acceptable, the service refused
            the record, or its answer does not name this record's digest.
    """
    url = check_endpoint(endpoint)
    expected = record_sha256(record_json)
    body = json.dumps({"record": json.loads(record_json)}, ensure_ascii=False).encode("utf-8")
    status_code, response = (post or _urllib_post)(url, body, timeout)

    if not 200 <= status_code < 300:
        raise TimeAnchorError(f"anchor endpoint refused the record: HTTP {status_code}{_refusal_detail(response)}")

    try:
        parsed: Any = json.loads(response)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TimeAnchorError(f"anchor endpoint answered with invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise TimeAnchorError("anchor endpoint answer is not a JSON object")
    answer = cast("dict[str, Any]", parsed)

    got_sha = answer.get("sha")
    if not isinstance(got_sha, str) or not _SHA256_HEX.match(got_sha):
        raise TimeAnchorError("anchor endpoint answer has no 64-hex 'sha'")
    if got_sha != expected:
        raise TimeAnchorError(
            f"anchor endpoint receipt names sha {got_sha}, but this record's JCS sha256 is {expected}"
        )

    status = answer.get("status")
    if not isinstance(status, str) or status not in ANCHOR_STATUSES:
        raise TimeAnchorError(f"anchor endpoint status {status!r} is not one of {sorted(ANCHOR_STATUSES)}")

    return AnchorReceipt(endpoint=url, sha256=expected, status=status, raw=answer)
