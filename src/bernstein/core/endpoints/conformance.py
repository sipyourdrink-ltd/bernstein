"""Deterministic conformance subset for OpenAI-compatible endpoint profiles.

Issue #2356. Per-role ``base_url`` / ``api_key_env`` (v2.13) made it possible
to point workers at local runtimes, but whether an endpoint can actually
carry a role -- call tools, reproduce a unified diff byte-exactly, answer
within a budget, accept a minimum context -- was discovered at run time. This
module turns that discovery into a fixed probe subset whose verdict is a
pure function of the endpoint's responses:

* the probe requests are constants (fixed prompts, ``temperature 0``);
* every probe result carries a machine reason code and the SHA-256 of the
  raw response body it was judged on;
* :func:`evaluate_roles` maps a transcript onto per-role certify/reject
  verdicts using the role presets below, deterministically -- two runs that
  observe the same responses produce byte-identical transcripts and
  verdicts.

The transcript is consumed by
:mod:`bernstein.core.endpoints.certification`, which seals it into a signed
receipt anchored to the lineage spine and the HMAC audit chain.
"""

from __future__ import annotations

import hashlib
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.security.url_allowlist import UrlSchemeError, ensure_http_url

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    #: Transport contract: ``(method, url, headers, body, timeout) ->
    #: (status, body_bytes)``. Timeouts raise ``TimeoutError``;
    #: connection-level failures raise ``OSError``. Tests inject fakes;
    #: the default uses ``urllib``.
    Transport = Callable[[str, str, Mapping[str, str], bytes | None, float], tuple[int, bytes]]

logger = logging.getLogger(__name__)

__all__ = [
    "ALL_PROBES",
    "CONFORMANCE_SUITE_VERSION",
    "LOCAL_TIER_ROLES",
    "PATCH_REFERENCE_DIFF",
    "PROBE_CHAT_COMPLETION",
    "PROBE_CONTEXT_FLOOR",
    "PROBE_PATCH_FIDELITY",
    "PROBE_REACHABILITY",
    "PROBE_TIMEOUT_BEHAVIOR",
    "PROBE_TOOL_CALLING",
    "ConformanceTranscript",
    "ProbeResult",
    "RoleVerdict",
    "discover_default_model",
    "evaluate_roles",
    "is_gated_role",
    "normalize_base_url",
    "required_probes_for_role",
    "run_conformance",
]

#: Version of the probe subset. Bump when a probe request or verdict rule
#: changes so existing receipts can be told apart from the new contract.
CONFORMANCE_SUITE_VERSION = 1

PROBE_REACHABILITY = "reachability"
PROBE_CHAT_COMPLETION = "chat_completion"
PROBE_TOOL_CALLING = "tool_calling"
PROBE_PATCH_FIDELITY = "patch_fidelity"
PROBE_TIMEOUT_BEHAVIOR = "timeout_behavior"
PROBE_CONTEXT_FLOOR = "context_floor"

#: Fixed probe order; the transcript always lists results in this order.
ALL_PROBES: tuple[str, ...] = (
    PROBE_REACHABILITY,
    PROBE_CHAT_COMPLETION,
    PROBE_TOOL_CALLING,
    PROBE_PATCH_FIDELITY,
    PROBE_TIMEOUT_BEHAVIOR,
    PROBE_CONTEXT_FLOOR,
)

#: Low-stakes roles a local endpoint may carry without a gated-role
#: certification. Every role NOT in this set is treated as merge-critical
#: (gated) and fails closed: it requires a receipt certifying the role.
LOCAL_TIER_ROLES: frozenset[str] = frozenset({"linter", "test_writer", "triage", "doc_sweeper"})

_BASE_PROBES: frozenset[str] = frozenset(
    {PROBE_REACHABILITY, PROBE_CHAT_COMPLETION, PROBE_TIMEOUT_BEHAVIOR, PROBE_CONTEXT_FLOOR}
)
_TOOLING_PROBES: frozenset[str] = _BASE_PROBES | {PROBE_TOOL_CALLING, PROBE_PATCH_FIDELITY}

#: Reference unified diff for the patch-fidelity probe. The endpoint must
#: return it byte-exactly (modulo surrounding whitespace / a code fence);
#: an endpoint that rewrites hunk lines emits patches the merge gate
#: rejects, which is exactly what this probe certifies against.
PATCH_REFERENCE_DIFF = (
    "--- a/tools/conformance_probe.py\n"
    "+++ b/tools/conformance_probe.py\n"
    "@@ -1,2 +1,3 @@\n"
    " def probe() -> str:\n"
    '-    return "before"\n'
    "+    # conformance reference hunk\n"
    '+    return "after"\n'
)

#: Context floor: the endpoint must accept a prompt of this many characters
#: (approximately a 4k-token context) without rejecting the request.
_CONTEXT_FLOOR_CHARS = 16384

_CHAT_PROBE_PROMPT = "Reply with the single word: ready"
_TIMEOUT_PROBE_PROMPT = "Summarize what a unit test is in exactly three sentences."
_PATCH_PROBE_PROMPT = (
    "Return the following unified diff exactly as given, character for "
    "character, with no commentary:\n" + PATCH_REFERENCE_DIFF
)
_CONTEXT_PROBE_FILLER = "The quick brown fox jumps over the lazy dog. "

_TOOL_PROBE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "record_finding",
        "description": "Record one lint finding for a file.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "line": {"type": "integer"},
                "message": {"type": "string"},
            },
            "required": ["path", "line", "message"],
        },
    },
}
_TOOL_PROBE_PROMPT = "Use the record_finding tool to report an unused import at line 12 of src/app.py."


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeResult:
    """One probe outcome: a verdict plus the hash of the judged response.

    ``reason`` is a machine code (empty when passed); ``response_hash`` is
    the SHA-256 of the raw response body bytes (of ``b""`` when the
    transport failed before a response existed), so a receipt holder can
    tie the verdict to the exact bytes it was computed from.
    """

    probe: str
    passed: bool
    reason: str
    response_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "probe": self.probe,
            "passed": self.passed,
            "reason": self.reason,
            "response_hash": self.response_hash,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ProbeResult:
        return cls(
            probe=str(raw["probe"]),
            passed=bool(raw["passed"]),
            reason=str(raw["reason"]),
            response_hash=str(raw["response_hash"]),
        )


@dataclass(frozen=True)
class ConformanceTranscript:
    """The full probe transcript for one ``(base_url, model)`` pair."""

    base_url: str
    model: str
    suite_version: int
    results: tuple[ProbeResult, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "model": self.model,
            "suite_version": self.suite_version,
            "results": [r.to_dict() for r in self.results],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ConformanceTranscript:
        return cls(
            base_url=str(raw["base_url"]),
            model=str(raw["model"]),
            suite_version=int(raw["suite_version"]),
            results=tuple(ProbeResult.from_dict(r) for r in raw["results"]),
        )

    def transcript_hash(self) -> str:
        """SHA-256 over the canonical transcript bytes (``sha256:`` prefixed)."""
        payload = json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def result_for(self, probe: str) -> ProbeResult | None:
        """Return the result for *probe*, or ``None`` when not probed."""
        return next((r for r in self.results if r.probe == probe), None)


@dataclass(frozen=True)
class RoleVerdict:
    """Deterministic certify/reject verdict for one role."""

    role: str
    certified: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "certified": self.certified, "reasons": list(self.reasons)}


# ---------------------------------------------------------------------------
# Role presets
# ---------------------------------------------------------------------------


def is_gated_role(role: str) -> bool:
    """Return True when *role* is merge-critical (fails closed).

    Only the explicit low-stakes preset (:data:`LOCAL_TIER_ROLES`) may run
    on an uncertified endpoint; every other role -- including ``default``
    and roles this install has never seen -- is gated.
    """
    return role not in LOCAL_TIER_ROLES


def required_probes_for_role(role: str) -> frozenset[str]:
    """Return the probe subset *role* requires for certification."""
    if is_gated_role(role):
        return frozenset(ALL_PROBES)
    if role == "test_writer":
        return _TOOLING_PROBES
    return _BASE_PROBES


def evaluate_roles(transcript: ConformanceTranscript, roles: Sequence[str]) -> tuple[RoleVerdict, ...]:
    """Map *transcript* onto per-role verdicts, deterministically.

    A role is certified iff every probe it requires is present in the
    transcript and passed. Every rejection carries ``probe:reason`` codes;
    a required probe missing from the transcript fails closed with
    ``probe:not_probed``. Verdicts are returned sorted by role name so the
    output is stable regardless of caller ordering.
    """
    verdicts: list[RoleVerdict] = []
    for role in sorted(set(roles)):
        reasons: list[str] = []
        for probe in ALL_PROBES:
            if probe not in required_probes_for_role(role):
                continue
            result = transcript.result_for(probe)
            if result is None:
                reasons.append(f"{probe}:not_probed")
            elif not result.passed:
                reasons.append(f"{probe}:{result.reason or 'failed'}")
        verdicts.append(RoleVerdict(role=role, certified=not reasons, reasons=tuple(reasons)))
    return tuple(verdicts)


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


def normalize_base_url(url: str) -> str:
    """Return *url* without any trailing slash (fingerprint-stable form)."""
    return url.rstrip("/")


def _default_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
    timeout: float,
) -> tuple[int, bytes]:
    """Issue one HTTP request with ``urllib``; return ``(status, body)``.

    Raises ``TimeoutError`` on budget exhaustion and ``OSError`` on
    connection-level failure so :func:`run_conformance` can map them onto
    deterministic reason codes.
    """
    checked = ensure_http_url(url, allow_http=True, source="endpoint conformance probe")
    request = urllib.request.Request(checked, data=body, method=method)
    for name, value in headers.items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()


def _hash_body(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


class _EndpointClient:
    """Small deterministic client over an injected transport."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None,
        timeout: float,
        transport: Transport | None,
    ) -> None:
        self._base_url = normalize_base_url(base_url)
        self._api_key = api_key
        self._timeout = timeout
        self._transport = transport or _default_transport

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def request(self, method: str, path: str, payload: Mapping[str, Any] | None) -> tuple[int, bytes]:
        """Return ``(status, body)``; raises like the transport contract."""
        body = None if payload is None else json.dumps(payload, sort_keys=True).encode("utf-8")
        return self._transport(method, f"{self._base_url}{path}", self._headers(), body, self._timeout)


def discover_default_model(
    *,
    base_url: str,
    api_key: str | None = None,
    timeout: float = 30.0,
    transport: Transport | None = None,
) -> str | None:
    """Return the first model id the endpoint lists, or ``None``.

    Best-effort: any transport failure, non-200 status, or unexpected
    payload shape yields ``None`` so the caller can demand an explicit
    ``--endpoint-model``.
    """
    client = _EndpointClient(base_url, api_key=api_key, timeout=timeout, transport=transport)
    try:
        status, body = client.request("GET", "/models", None)
    except (TimeoutError, OSError, UrlSchemeError, ValueError) as exc:
        logger.debug("endpoint model discovery failed: %s", type(exc).__name__)
        return None
    if status != 200:
        return None
    try:
        data = json.loads(body)
        first = data["data"][0]["id"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return None
    return str(first) if first else None


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


def _chat_payload(model: str, prompt: str, **extra: Any) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        **extra,
    }


def _message_of(body: bytes) -> tuple[dict[str, Any] | None, str]:
    """Parse ``choices[0].message`` from *body*; return ``(message, reason)``."""
    try:
        data = json.loads(body)
        message = data["choices"][0]["message"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return None, "malformed_response"
    if not isinstance(message, dict):
        return None, "malformed_response"
    return message, ""


def _probe(
    client: _EndpointClient,
    probe: str,
    method: str,
    path: str,
    payload: Mapping[str, Any] | None,
    judge: Callable[[int, bytes], str],
) -> ProbeResult:
    """Run one probe: issue the request, map the response onto a verdict."""
    try:
        status, body = client.request(method, path, payload)
    except TimeoutError:
        return ProbeResult(probe=probe, passed=False, reason="timed_out", response_hash=_hash_body(b""))
    except (OSError, UrlSchemeError, ValueError) as exc:
        logger.debug("endpoint probe %s transport failure: %s", probe, type(exc).__name__)
        return ProbeResult(probe=probe, passed=False, reason="unreachable", response_hash=_hash_body(b""))
    reason = judge(status, body)
    return ProbeResult(probe=probe, passed=not reason, reason=reason, response_hash=_hash_body(body))


def _judge_models(status: int, body: bytes) -> str:
    if status != 200:
        return "unreachable"
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return "bad_models_payload"
    return "" if isinstance(data.get("data"), list) else "bad_models_payload"


def _judge_completion(status: int, body: bytes) -> str:
    if status != 200:
        return "chat_failed"
    message, reason = _message_of(body)
    if message is None:
        return reason
    content = message.get("content")
    return "" if isinstance(content, str) and content.strip() else "empty_completion"


def _judge_tool_call(status: int, body: bytes) -> str:
    if status != 200:
        return "chat_failed"
    message, reason = _message_of(body)
    if message is None:
        return reason
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list) or not tool_calls:
        return "no_tool_call"
    call = tool_calls[0]
    function = call.get("function") if isinstance(call, dict) else None
    if not isinstance(function, dict):
        return "no_tool_call"
    if function.get("name") != _TOOL_PROBE_TOOL["function"]["name"]:
        return "tool_call_wrong_function"
    try:
        arguments = json.loads(function.get("arguments") or "")
    except (json.JSONDecodeError, TypeError):
        return "tool_call_malformed_arguments"
    return "" if isinstance(arguments, dict) else "tool_call_malformed_arguments"


def _extract_diff(content: str) -> str:
    """Strip an optional markdown fence and surrounding whitespace."""
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _judge_patch(status: int, body: bytes) -> str:
    if status != 200:
        return "chat_failed"
    message, reason = _message_of(body)
    if message is None:
        return reason
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return "patch_not_returned"
    returned = _extract_diff(content)
    if returned == PATCH_REFERENCE_DIFF.strip():
        return ""
    return "patch_corrupted" if "--- a/" in returned or "@@" in returned else "patch_not_returned"


def _judge_context(status: int, body: bytes) -> str:
    if status != 200:
        return "context_rejected"
    message, reason = _message_of(body)
    if message is None:
        return reason
    content = message.get("content")
    return "" if isinstance(content, str) and content.strip() else "empty_completion"


def run_conformance(
    *,
    base_url: str,
    model: str,
    api_key: str | None = None,
    timeout: float = 60.0,
    transport: Transport | None = None,
) -> ConformanceTranscript:
    """Run the full probe subset against ``(base_url, model)``.

    Args:
        base_url: OpenAI-compatible base URL (e.g. ``http://127.0.0.1:11434/v1``).
        model: Model id passed in every completion request.
        api_key: Optional bearer token value (resolved by the caller from
            the profile's ``api_key_env``; never logged).
        timeout: Per-probe response budget in seconds; exceeding it fails
            the probe with ``timed_out``.
        transport: Injectable transport for tests / golden replays.

    Returns:
        The transcript, with one result per probe in :data:`ALL_PROBES` order.
    """
    client = _EndpointClient(base_url, api_key=api_key, timeout=timeout, transport=transport)
    filler_count = (_CONTEXT_FLOOR_CHARS // len(_CONTEXT_PROBE_FILLER)) + 1
    context_prompt = (_CONTEXT_PROBE_FILLER * filler_count)[:_CONTEXT_FLOOR_CHARS] + "\nReply with the single word: ok"

    results = (
        _probe(client, PROBE_REACHABILITY, "GET", "/models", None, _judge_models),
        _probe(
            client,
            PROBE_CHAT_COMPLETION,
            "POST",
            "/chat/completions",
            _chat_payload(model, _CHAT_PROBE_PROMPT),
            _judge_completion,
        ),
        _probe(
            client,
            PROBE_TOOL_CALLING,
            "POST",
            "/chat/completions",
            _chat_payload(model, _TOOL_PROBE_PROMPT, tools=[_TOOL_PROBE_TOOL], tool_choice="auto"),
            _judge_tool_call,
        ),
        _probe(
            client,
            PROBE_PATCH_FIDELITY,
            "POST",
            "/chat/completions",
            _chat_payload(model, _PATCH_PROBE_PROMPT),
            _judge_patch,
        ),
        _probe(
            client,
            PROBE_TIMEOUT_BEHAVIOR,
            "POST",
            "/chat/completions",
            _chat_payload(model, _TIMEOUT_PROBE_PROMPT, max_tokens=256),
            _judge_completion,
        ),
        _probe(
            client,
            PROBE_CONTEXT_FLOOR,
            "POST",
            "/chat/completions",
            _chat_payload(model, context_prompt),
            _judge_context,
        ),
    )
    return ConformanceTranscript(
        base_url=normalize_base_url(base_url),
        model=model,
        suite_version=CONFORMANCE_SUITE_VERSION,
        results=results,
    )
