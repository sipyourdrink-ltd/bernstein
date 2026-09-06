"""OWASP Top 10 for Agentic Apps (ASI01-10) heuristic detector pack.

Provides ten lightweight detectors for the canonical agentic-app risk
classes from the OWASP Top 10 for Agentic Apps (Dec 2025). Each
detector is a self-contained heuristic that consumes a uniform
``context`` dict and returns an :class:`ASIFinding`. The pack is wired
into the existing :class:`GuardrailPipeline` via
:class:`OwaspAsiGuardrail` and is **on by default** (the post-rollout
phase). To opt out, set ``BERNSTEIN_DISABLE_OWASP_ASI=1`` or pass
``enable_owasp_asi=False`` to :meth:`GuardrailPipeline.default`.

Honesty caveats - every detector here is a *heuristic*. Each docstring
calls out the risk it tries to catch, the known false-positive
patterns, and (where applicable) the deeper module that should
eventually own the check. Deferred deeper integrations are flagged in
the per-detector ``status`` field so consumers can tell apart a
working heuristic from a stub awaiting a real signal source.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from bernstein.core.security.guardrail_pipeline import GuardrailResult

logger = logging.getLogger(__name__)


class ASIClass(StrEnum):
    """OWASP Top 10 for Agentic Apps risk class identifiers (Dec 2025)."""

    ASI01_GOAL_HIJACK = "ASI01"
    ASI02_TOOL_MISUSE = "ASI02"
    ASI03_IDENTITY_PRIVILEGE = "ASI03"
    ASI04_SUPPLY_CHAIN = "ASI04"
    ASI05_CODE_EXECUTION = "ASI05"
    ASI06_MEMORY_POISONING = "ASI06"
    ASI07_INSECURE_A2A = "ASI07"
    ASI08_UNBOUNDED_CONSUMPTION = "ASI08"
    ASI09_OBSERVABILITY_GAP = "ASI09"
    ASI10_MISALIGNMENT_DRIFT = "ASI10"


class ASISeverity(StrEnum):
    """Finding severity. Mirrors ``sandbox_escape_detector.ViolationSeverity``."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class DetectorStatus(StrEnum):
    """Honesty marker for the detector's depth of coverage."""

    HEURISTIC = "heuristic"  # working pattern-based check
    DELEGATING = "delegating"  # delegates to another module when present
    DEFERRED = "deferred"  # placeholder; returns INFO until real integration lands


@dataclass(frozen=True)
class ASIFinding:
    """Structured finding for a single ASI detector.

    Attributes:
        asi_class: The ASI risk class this finding belongs to.
        severity: Severity of the finding.
        passed: True when no violation was detected.
        detector_name: Identifier of the detector that produced the finding.
        evidence: Short human-readable evidence snippet.
        remediation: Short remediation hint.
        status: Whether the detector is heuristic, delegating, or deferred.
    """

    asi_class: ASIClass
    severity: ASISeverity
    passed: bool
    detector_name: str
    evidence: str = ""
    remediation: str = ""
    status: DetectorStatus = DetectorStatus.HEURISTIC

    def __bool__(self) -> bool:
        return self.passed


# Type for a detector callable.
Detector = Callable[[dict[str, Any]], ASIFinding]


# ---------------------------------------------------------------------------
# Heuristic helpers
# ---------------------------------------------------------------------------


def _truthy(value: str | None) -> bool:
    """Return True when an env-var-style string is set to an enabling value."""
    if not value:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on", "enable", "enabled"}


def _ok(asi: ASIClass, name: str, status: DetectorStatus = DetectorStatus.HEURISTIC) -> ASIFinding:
    return ASIFinding(
        asi_class=asi,
        severity=ASISeverity.INFO,
        passed=True,
        detector_name=name,
        status=status,
    )


def _flag(
    asi: ASIClass,
    name: str,
    severity: ASISeverity,
    evidence: str,
    remediation: str,
    status: DetectorStatus = DetectorStatus.HEURISTIC,
) -> ASIFinding:
    return ASIFinding(
        asi_class=asi,
        severity=severity,
        passed=False,
        detector_name=name,
        evidence=evidence,
        remediation=remediation,
        status=status,
    )


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------


def _as_text(value: Any) -> str:
    """Render one payload value as text, decoding a binary transport.

    A payload carried as ``bytes`` is the same payload. Skipping it is how
    a detector reports clean on a string it never read.

    Args:
        value: One value out of a detector's input payload.

    Returns:
        The value as text, with undecodable bytes replaced rather than
        raising, so a malformed transport cannot suppress the scan.
    """
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", errors="replace")
    return str(value)


def _rendered_values(payload: Any) -> str:
    """Flatten a tool-argument payload of any wire shape into one string.

    Tool calls carry their arguments as a mapping, as a positional list,
    and occasionally as a bare scalar. A detector that reads only the
    mapping shape returns a pass on the other two that the caller never
    earned, so every shape is rendered here instead.

    Args:
        payload: The ``tool_args`` value as it arrived, of any type.

    Returns:
        The payload's values joined by spaces, or ``""`` when it holds
        nothing to scan.
    """
    if payload is None:
        return ""
    if isinstance(payload, Mapping):
        values: list[Any] = list(payload.values())
    elif isinstance(payload, (str, bytes, bytearray, memoryview)):
        values = [payload]
    elif isinstance(payload, Iterable):
        values = list(payload)
    else:
        values = [payload]
    return " ".join(_as_text(value) for value in values if value is not None)


_GOAL_HIJACK_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"ignore\s+(?:all\s+)?(?:previous|prior)\s+(?:instructions|goal|task)",
        r"new\s+(?:goal|task|objective)\s*[:\-]",
        r"forget\s+(?:everything|the\s+previous\s+goal)",
        r"override\s+the\s+(?:original|user)\s+(?:goal|task)",
        r"your\s+real\s+(?:goal|task)\s+is",
    )
)


def detect_asi01_goal_hijack(context: dict[str, Any]) -> ASIFinding:
    """ASI01 Goal Hijack - lexical detector for goal-rewrite injections.

    Risk: an attacker-controlled input rewrites the active task goal
    (e.g., via prompt-injection in retrieved content). This heuristic
    scans the prompt and any retrieved content for canonical
    goal-rewrite phrases.

    False positives: pedagogical or documentation text discussing
    prompt injection (e.g., this docstring) will trip the patterns;
    callers reviewing security writeups should disable this detector
    or down-grade severity.
    """
    name = "asi01_goal_hijack"
    haystack_parts: list[str] = []
    for key in ("prompt", "retrieved_content", "system_prompt"):
        value = context.get(key)
        if value is None:
            continue
        # bytes and bytearray are Iterable, so an unguarded Iterable branch
        # would scan them one integer at a time. They are decoded whole
        # instead: a payload sent down a binary channel is still a payload.
        if isinstance(value, (str, bytes, bytearray, memoryview)):
            haystack_parts.append(_as_text(value))
        elif isinstance(value, Iterable):
            haystack_parts.extend(_as_text(item) for item in value)
        else:
            haystack_parts.append(_as_text(value))
    haystack = "\n".join(haystack_parts)
    if not haystack:
        return _ok(ASIClass.ASI01_GOAL_HIJACK, name)
    for pattern in _GOAL_HIJACK_PATTERNS:
        match = pattern.search(haystack)
        if match:
            return _flag(
                ASIClass.ASI01_GOAL_HIJACK,
                name,
                ASISeverity.WARNING,
                evidence=f"matched pattern {pattern.pattern!r}: {match.group(0)!r}",
                remediation=(
                    "Review the retrieved/user content for prompt injection; "
                    "isolate untrusted input and quote it before passing to the model."
                ),
            )
    return _ok(ASIClass.ASI01_GOAL_HIJACK, name)


def detect_asi02_tool_misuse(context: dict[str, Any]) -> ASIFinding:
    """ASI02 Tool Misuse - args shape vs declared tool description.

    Risk: a tool is called with arguments that fall outside its declared
    purpose (e.g., a "search" tool invoked with a shell command).
    Heuristic: when ``tool_descriptions`` is provided and a tool's args
    contain shell metacharacters or path traversal while the
    description does not advertise file or shell access, flag it.

    False positives: tools whose description omits keywords like
    "shell", "command", "filesystem" but legitimately accept paths
    will be flagged; supply a richer description or whitelist the tool.
    """
    name = "asi02_tool_misuse"
    tool_name = context.get("tool_name")
    tool_args = context.get("tool_args") or {}
    descriptions: dict[str, str] = context.get("tool_descriptions") or {}
    if not tool_name:
        return _ok(ASIClass.ASI02_TOOL_MISUSE, name)
    description = (descriptions.get(tool_name) or "").lower()
    advertises_shell = any(token in description for token in ("shell", "command", "exec", "filesystem", "path", "file"))
    suspicious = re.compile(r"[;&|`$><]|(?:\.\./){2,}|/etc/(?:passwd|shadow)")
    rendered = _rendered_values(tool_args)
    if rendered and not advertises_shell and suspicious.search(rendered):
        return _flag(
            ASIClass.ASI02_TOOL_MISUSE,
            name,
            ASISeverity.WARNING,
            evidence=f"tool {tool_name!r} args contain shell-shaped tokens: {rendered[:120]!r}",
            remediation=(
                "Validate tool inputs against the declared tool schema; reject shell/path tokens for non-shell tools."
            ),
        )
    return _ok(ASIClass.ASI02_TOOL_MISUSE, name)


def detect_asi03_identity_privilege(context: dict[str, Any]) -> ASIFinding:
    """ASI03 Identity & Privilege Abuse - capability matrix violation.

    Risk: an agent attempts an action outside its capability grant
    (e.g., a read-only researcher invoking a write tool). Delegates
    to ``capability_matrix`` when the caller passes
    ``capability_violation=True``; otherwise treats the call as
    in-bounds. The deep integration with
    ``core.security.permission_graph`` is deferred to a follow-up.

    False positives: callers that forget to populate
    ``capability_violation`` for legitimately blocked actions will
    silently pass; rely on the upstream permission graph for the
    authoritative decision.
    """
    name = "asi03_identity_privilege"
    if context.get("capability_violation") is True:
        return _flag(
            ASIClass.ASI03_IDENTITY_PRIVILEGE,
            name,
            ASISeverity.CRITICAL,
            evidence=str(context.get("capability_violation_reason", "capability matrix denied")),
            remediation="Tighten the capability grant; never widen at runtime.",
            status=DetectorStatus.DELEGATING,
        )
    return _ok(ASIClass.ASI03_IDENTITY_PRIVILEGE, name, DetectorStatus.DELEGATING)


def _unsigned_components(components: Any) -> list[str] | None:
    """Return the names of loaded components that carry no signature.

    ``None`` means the manifest could not be read as a component list at
    all, which is not the same answer as an empty list: an unreadable
    manifest is one whose components were never checked, so the caller
    reports it rather than passing.

    A mapping is read as ``name -> record``, the shape a manifest takes
    once it has travelled as a JSON object instead of an array. An entry
    that is not a record carries no ``signed`` field to read, so it counts
    as unsigned rather than as absent.

    Args:
        components: The ``loaded_components`` value as it arrived.

    Returns:
        The unsigned components' names, or ``None`` when the value is not
        a component manifest.
    """
    entries: list[tuple[str, Any]]
    if isinstance(components, Mapping):
        entries = [(str(key), record) for key, record in components.items()]
    elif isinstance(components, (list, tuple, set, frozenset)):
        entries = [("<anonymous>", record) for record in components]
    else:
        return None
    unsigned: list[str] = []
    for label, record in entries:
        if isinstance(record, Mapping):
            if not record.get("signed"):
                unsigned.append(str(record.get("name", label)))
        else:
            unsigned.append(label)
    return unsigned


def detect_asi04_supply_chain(context: dict[str, Any]) -> ASIFinding:
    """ASI04 Agentic Supply Chain - unsigned MCP/plugin/skill load.

    Risk: an unsigned or unverified MCP server, plugin, or skill is
    loaded into the agent's tool surface. Heuristic: the caller
    populates ``loaded_components`` with ``{"name": ..., "signed":
    bool}`` and we flag any with ``signed`` falsy. Deeper signature
    verification is the responsibility of FEAT
    ``mcp-server-signing-and-scanning``.

    False positives: trusted local-dev components without signatures
    will be flagged in dev mode; gate the detector via
    ``allow_unsigned_in_dev=True`` to demote to INFO.
    """
    name = "asi04_supply_chain"
    components = context.get("loaded_components") or []
    unsigned = _unsigned_components(components)
    severity = ASISeverity.INFO if context.get("allow_unsigned_in_dev") else ASISeverity.WARNING
    if unsigned is None:
        return _flag(
            ASIClass.ASI04_SUPPLY_CHAIN,
            name,
            severity,
            evidence=(
                f"loaded_components is a {type(components).__name__}, not a component "
                "manifest, so no component in it was checked for a signature"
            ),
            remediation=(
                "Pass loaded_components as a list of {'name': ..., 'signed': bool} "
                "records, or as a mapping of name to that record."
            ),
            status=DetectorStatus.DELEGATING,
        )
    if not unsigned:
        return _ok(ASIClass.ASI04_SUPPLY_CHAIN, name, DetectorStatus.DELEGATING)
    return _flag(
        ASIClass.ASI04_SUPPLY_CHAIN,
        name,
        severity,
        evidence=f"unsigned components loaded: {unsigned}",
        remediation="Require Sigstore/PGP signatures on all MCP servers, plugins, and skills.",
        status=DetectorStatus.DELEGATING,
    )


_CODE_EXEC_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"\beval\s*\(",
        r"\bexec\s*\(",
        r"\b__import__\s*\(",
        r"subprocess\.(?:Popen|run|call|check_output)",
        r";\s*(?:rm|curl|wget|nc|bash|sh)\s",
        r"\$\(.+\)",
    )
)


def detect_asi05_code_execution(context: dict[str, Any]) -> ASIFinding:
    """ASI05 Unexpected Code Execution - eval-shaped or shell-shaped args.

    Risk: an agent crafts a tool argument that executes code outside
    the sandbox (e.g., shell injection through a "filename" field).
    Heuristic complements ``sandbox_escape_detector`` by scanning the
    in-flight tool args before dispatch.

    False positives: legitimate Python source being passed to a
    "lint" or "format" tool will trip the patterns; whitelist such
    tools via ``code_safe_tools``.
    """
    name = "asi05_code_execution"
    tool_name = context.get("tool_name")
    tool_args = context.get("tool_args") or {}
    safe = set(context.get("code_safe_tools") or [])
    if tool_name in safe:
        return _ok(ASIClass.ASI05_CODE_EXECUTION, name)
    rendered = _rendered_values(tool_args)
    for pattern in _CODE_EXEC_PATTERNS:
        match = pattern.search(rendered)
        if match:
            return _flag(
                ASIClass.ASI05_CODE_EXECUTION,
                name,
                ASISeverity.CRITICAL,
                evidence=f"matched pattern {pattern.pattern!r}: {match.group(0)!r}",
                remediation=(
                    "Reject the call or run inside a hardened sandbox; see core.security.sandbox_escape_detector."
                ),
            )
    return _ok(ASIClass.ASI05_CODE_EXECUTION, name)


def detect_asi06_memory_poisoning(context: dict[str, Any]) -> ASIFinding:
    """ASI06 Memory & Context Poisoning - out-of-band memory writes.

    Risk: an attacker-controlled retrieval pollutes long-term memory
    with payloads that hijack future runs. Heuristic flags memory
    writes that originate from an untrusted source
    (``memory_write.source == "untrusted"``) or whose content matches
    a goal-hijack pattern.

    False positives: legitimate writes from external knowledge bases
    flagged "untrusted" by default; tag trusted sources explicitly.
    """
    name = "asi06_memory_poisoning"
    write = context.get("memory_write")
    if not isinstance(write, dict):
        return _ok(ASIClass.ASI06_MEMORY_POISONING, name)
    if write.get("source") == "untrusted":
        return _flag(
            ASIClass.ASI06_MEMORY_POISONING,
            name,
            ASISeverity.WARNING,
            evidence=f"untrusted memory write: {str(write.get('content', ''))[:120]!r}",
            remediation="Quarantine writes from untrusted sources or require provenance.",
        )
    content = str(write.get("content", ""))
    for pattern in _GOAL_HIJACK_PATTERNS:
        if pattern.search(content):
            return _flag(
                ASIClass.ASI06_MEMORY_POISONING,
                name,
                ASISeverity.WARNING,
                evidence=f"memory write contains goal-hijack pattern {pattern.pattern!r}",
                remediation="Reject the write; sanitize before persisting.",
            )
    return _ok(ASIClass.ASI06_MEMORY_POISONING, name)


def detect_asi07_insecure_a2a(context: dict[str, Any]) -> ASIFinding:
    """ASI07 Insecure Inter-Agent Communication - missing JWS on A2A msg.

    Risk: an agent-to-agent message arrives without a valid JWS, so
    its origin and integrity cannot be verified. Heuristic flags
    ``a2a_message`` envelopes whose ``jws`` field is empty/missing.
    Deep verification is delegated to FEAT
    ``a2a-v1-signed-agent-card``.

    False positives: in-process loopback messages that legitimately
    skip signing should be tagged ``loopback=True`` to bypass.
    """
    name = "asi07_insecure_a2a"
    message = context.get("a2a_message")
    if not isinstance(message, dict):
        return _ok(ASIClass.ASI07_INSECURE_A2A, name, DetectorStatus.DELEGATING)
    if message.get("loopback"):
        return _ok(ASIClass.ASI07_INSECURE_A2A, name, DetectorStatus.DELEGATING)
    if not message.get("jws"):
        return _flag(
            ASIClass.ASI07_INSECURE_A2A,
            name,
            ASISeverity.WARNING,
            evidence=f"a2a message from {message.get('from', '<unknown>')!r} missing jws",
            remediation="Sign every A2A message with a verifiable JWS (see a2a-v1-signed-agent-card).",
            status=DetectorStatus.DELEGATING,
        )
    return _ok(ASIClass.ASI07_INSECURE_A2A, name, DetectorStatus.DELEGATING)


def detect_asi08_unbounded_consumption(context: dict[str, Any]) -> ASIFinding:
    """ASI08 Unbounded Consumption - task without budget envelope.

    Risk: an agent runs a task without a budget cap, allowing runaway
    spend or compute. Heuristic flags any context where
    ``budget_usd`` is unset or non-positive while the task is active
    (``task_active=True``).

    False positives: short interactive sessions intentionally without
    a budget; tag them with ``task_active=False`` to skip.
    """
    name = "asi08_unbounded_consumption"
    if not context.get("task_active"):
        return _ok(ASIClass.ASI08_UNBOUNDED_CONSUMPTION, name)
    budget = context.get("budget_usd")
    if not isinstance(budget, (int, float)) or budget <= 0:
        return _flag(
            ASIClass.ASI08_UNBOUNDED_CONSUMPTION,
            name,
            ASISeverity.INFO,
            evidence=f"task_active=True but budget_usd={budget!r}",
            remediation="Set a per-task budget on the AgentIdentityCard.",
        )
    return _ok(ASIClass.ASI08_UNBOUNDED_CONSUMPTION, name)


def detect_asi09_observability_gap(context: dict[str, Any]) -> ASIFinding:
    """ASI09 Observability Gap - tool call missing from audit chain.

    Risk: a tool call executes but its result is not journaled to the
    audit chain, blinding incident response. Heuristic flags context
    with ``tool_call_id`` set but ``audit_recorded=False``.

    False positives: dry-run / preview calls that intentionally skip
    journaling; tag them with ``dry_run=True``.
    """
    name = "asi09_observability_gap"
    if context.get("dry_run"):
        return _ok(ASIClass.ASI09_OBSERVABILITY_GAP, name)
    if context.get("tool_call_id") and not context.get("audit_recorded", True):
        return _flag(
            ASIClass.ASI09_OBSERVABILITY_GAP,
            name,
            ASISeverity.WARNING,
            evidence=f"tool_call_id={context['tool_call_id']!r} not journaled",
            remediation="Wire the call site through core.security.audit before returning.",
        )
    return _ok(ASIClass.ASI09_OBSERVABILITY_GAP, name)


def detect_asi10_misalignment_drift(context: dict[str, Any]) -> ASIFinding:
    """ASI10 Misalignment Drift - stated intent vs imminent action.

    Risk: an agent's chain-of-thought says one thing while the action
    it's about to take does another (e.g., "I'll only read X" then
    issues a write). Heuristic compares ``stated_intent`` and
    ``planned_action`` strings: if intent mentions "read" but action
    is "write"/"delete"/"send" (or vice versa), flag the mismatch.

    False positives: noisy chain-of-thought with both verbs is
    common; this detector should be treated as a soft signal until a
    semantic-similarity backend lands (deferred).
    """
    name = "asi10_misalignment_drift"
    intent = str(context.get("stated_intent") or "").lower()
    action = str(context.get("planned_action") or "").lower()
    if not intent or not action:
        return _ok(ASIClass.ASI10_MISALIGNMENT_DRIFT, name, DetectorStatus.DEFERRED)
    read_only_intent = "read" in intent and not any(w in intent for w in ("write", "delete", "send", "modify"))
    write_action = any(w in action for w in ("write", "delete", "send", "modify", "post", "push"))
    if read_only_intent and write_action:
        return _flag(
            ASIClass.ASI10_MISALIGNMENT_DRIFT,
            name,
            ASISeverity.WARNING,
            evidence=f"intent={intent!r} planned_action={action!r}",
            remediation="Pause and re-confirm with the operator before mutating state.",
            status=DetectorStatus.DEFERRED,
        )
    return _ok(ASIClass.ASI10_MISALIGNMENT_DRIFT, name, DetectorStatus.DEFERRED)


# Fixed-order registry of detectors - order matches ASI01..ASI10.
DEFAULT_DETECTORS: tuple[Detector, ...] = (
    detect_asi01_goal_hijack,
    detect_asi02_tool_misuse,
    detect_asi03_identity_privilege,
    detect_asi04_supply_chain,
    detect_asi05_code_execution,
    detect_asi06_memory_poisoning,
    detect_asi07_insecure_a2a,
    detect_asi08_unbounded_consumption,
    detect_asi09_observability_gap,
    detect_asi10_misalignment_drift,
)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def run_owasp_asi_checks(
    context: dict[str, Any],
    detectors: Iterable[Detector] | None = None,
) -> list[ASIFinding]:
    """Run every detector against ``context`` and return findings.

    Each detector runs in isolation: an exception inside one detector
    is caught and converted to a CRITICAL finding so the orchestrator
    keeps running. The default detector list is :data:`DEFAULT_DETECTORS`
    (ASI01..ASI10 in fixed order).

    Args:
        context: Uniform call envelope. Detectors look up named keys
            (``prompt``, ``tool_name``, ``tool_args``, ...); see each
            detector's docstring.
        detectors: Optional override for the detector list. Defaults
            to :data:`DEFAULT_DETECTORS`.

    Returns:
        List of :class:`ASIFinding`, one per detector, in detector order.
    """
    chain = tuple(detectors) if detectors is not None else DEFAULT_DETECTORS
    findings: list[ASIFinding] = []
    for detector in chain:
        try:
            findings.append(detector(context))
        except Exception as exc:
            logger.exception(
                "OWASP ASI detector %s crashed; emitting CRITICAL stub finding.",
                getattr(detector, "__name__", repr(detector)),
            )
            findings.append(
                ASIFinding(
                    asi_class=ASIClass.ASI09_OBSERVABILITY_GAP,
                    severity=ASISeverity.CRITICAL,
                    passed=False,
                    detector_name=getattr(detector, "__name__", "unknown"),
                    evidence=f"detector raised {type(exc).__name__}: {exc}",
                    remediation="Fix or disable the detector; check service logs.",
                    status=DetectorStatus.HEURISTIC,
                )
            )
    return findings


_SEVERITY_RANK: dict[ASISeverity, int] = {
    ASISeverity.INFO: 0,
    ASISeverity.WARNING: 1,
    ASISeverity.CRITICAL: 2,
}


@dataclass
class OwaspAsiGuardrail:
    """Adapter that lets :class:`GuardrailPipeline` run the ASI pack.

    The adapter forwards ``check_input``/``check_output`` to
    :func:`run_owasp_asi_checks` and aggregates the findings into a
    single :class:`GuardrailResult`. ``critical`` findings flip
    ``passed`` to False; ``info`` findings never block but are
    surfaced as ``violations`` so callers can log them.
    """

    name: str = "owasp_asi"
    block_on: ASISeverity = ASISeverity.WARNING
    detectors: tuple[Detector, ...] = field(default_factory=lambda: DEFAULT_DETECTORS)

    def _aggregate(self, context: dict[str, Any]) -> GuardrailResult:
        findings = run_owasp_asi_checks(context, self.detectors)
        block_threshold = _SEVERITY_RANK[self.block_on]
        violations: list[str] = []
        blocked = False
        for finding in findings:
            if finding.passed:
                continue
            severity_rank = _SEVERITY_RANK[finding.severity]
            line = f"[{finding.asi_class}/{finding.severity}] {finding.detector_name}: {finding.evidence}"
            violations.append(line)
            if severity_rank >= block_threshold:
                blocked = True
        return GuardrailResult(
            passed=not blocked,
            guardrail_name=self.name,
            violations=violations,
        )

    def check_input(self, prompt: str, context: dict[str, Any]) -> GuardrailResult:
        merged = context | {"prompt": prompt}
        return self._aggregate(merged)

    def check_output(self, output: str, context: dict[str, Any]) -> GuardrailResult:
        merged = context | {"agent_output": output}
        return self._aggregate(merged)


def is_owasp_asi_enabled(env: dict[str, str] | None = None) -> bool:
    """Return True when the OWASP ASI pack should run.

    The pack is **on by default** (post-spec/test → default-on phase).
    Opt out by setting ``BERNSTEIN_DISABLE_OWASP_ASI=1`` (or any other
    truthy value such as ``true``, ``yes``, ``on``).

    The legacy ``BERNSTEIN_ENABLE_OWASP_ASI=1`` opt-in remains
    accepted as a no-op for forward compatibility - explicitly setting
    it to a falsy value (``0``, ``false``) suppresses the pack so
    operators who scripted the previous opt-in semantics keep their
    expected behaviour.

    Resolution (highest priority first):

    1. ``BERNSTEIN_DISABLE_OWASP_ASI`` truthy → pack disabled.
    2. ``BERNSTEIN_ENABLE_OWASP_ASI`` set to a falsy value
       (``0``/``false``/``no``/``off``) → pack disabled.
    3. Otherwise → pack enabled (the new default).

    Args:
        env: Environment mapping override for tests; defaults to
            :data:`os.environ`.

    Returns:
        ``True`` when the pack should be loaded into the pipeline.
    """
    source = env if env is not None else os.environ
    if _truthy(source.get("BERNSTEIN_DISABLE_OWASP_ASI")):
        return False
    # Honour an explicitly falsy legacy opt-in (e.g. operators who scripted
    # BERNSTEIN_ENABLE_OWASP_ASI=0 expect the pack to stay off).
    legacy_opt_in = source.get("BERNSTEIN_ENABLE_OWASP_ASI")
    return not (legacy_opt_in is not None and not _truthy(legacy_opt_in))
