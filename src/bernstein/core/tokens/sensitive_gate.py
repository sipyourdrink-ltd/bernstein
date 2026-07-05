"""Deny gate for credential-shaped content in compaction input.

The compaction pipeline forwards slices of worker context to a model API
to produce summaries. Worker contexts routinely contain file reads, so a
context that includes ``.env`` contents, private keys, or cloud
credentials would otherwise be shipped to an external API. This module
scans compaction input before the summary stage and either redacts the
offending span or refuses the whole compaction.

Design rules
------------
* **Pure and deterministic.** :func:`scan_for_sensitive_content` performs
  no IO and yields identical verdicts for identical input on every
  platform. Audit emission is a separate, explicit step
  (:func:`emit_gate_audit`).
* **Redact when safely delimitable, refuse otherwise.** Content patterns
  with a well-defined span (a complete PEM block, an AWS access key id, a
  ``ghp_`` token) are replaced with a typed placeholder
  ``[REDACTED:<rule-id>:<sha256[:8] of original>]``. Path-shaped tokens
  (``.env``, ``id_rsa``, ``*.pem``, ``.ssh/`` ...) signal that credential
  *file contents* may surround the token in ways the gate cannot
  delimit, so any such hit refuses the compaction outright. An
  unterminated PEM header likewise refuses.
* **Hash only, never content.** Findings carry the SHA-256 of the span;
  the span text itself is never stored on a finding or an audit event.

Entropy heuristic
-----------------
The generic assignment rule fires only when ALL of the following hold:

* the assigned name normalizes (separators stripped, lower-cased, so
  ``api-key`` -> ``apikey``) to contain a sensitive keyword
  (``key``, ``secret``, ``token``, ``passwd``, ``password``,
  ``credential``);
* the value is at least :data:`ENTROPY_MIN_VALUE_LENGTH` (24) characters;
* the Shannon entropy of the value is STRICTLY above
  :data:`ENTROPY_MIN_BITS_PER_CHAR` (4.0 bits/char).

The strict 4.0-bit threshold is deliberately conservative (prefer false
negatives over noise): a pure-hex value draws from a 16-symbol alphabet
whose entropy is capped at ``log2(16) == 4.0``, so SHA/MD5 digests and
UUIDs are structurally incapable of crossing it, while real secrets
(mixed-case base64-ish material) comfortably exceed it at 24+ chars.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from bernstein.core.security.sanitize import sanitize_log

if TYPE_CHECKING:
    from bernstein.core.security.audit_chain import AuditChainStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Entropy heuristic constants (documented above)
# ---------------------------------------------------------------------------

#: Minimum Shannon entropy (bits per character, strict) for the generic
#: assignment rule. Pure-hex values cannot exceed 4.0, so digests and
#: UUIDs never fire this rule.
ENTROPY_MIN_BITS_PER_CHAR: float = 4.0

#: Minimum value length for the generic assignment rule.
ENTROPY_MIN_VALUE_LENGTH: int = 24

#: Sensitive keywords matched against the normalized assignment name.
_SENSITIVE_NAME_KEYWORDS: tuple[str, ...] = (
    "key",
    "secret",
    "token",
    "passwd",
    "password",
    "credential",
)

# ---------------------------------------------------------------------------
# Rule table
# ---------------------------------------------------------------------------

_PEM_HEADER = r"-----BEGIN [A-Z][A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"
_PEM_FOOTER = r"-----END [A-Z][A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"


@dataclass(frozen=True)
class _Rule:
    """A single deny rule: id, compiled pattern, and delimitability."""

    rule_id: str
    pattern: re.Pattern[str]
    delimitable: bool
    #: Regex group whose span is the offending content (0 = whole match).
    group: int = 0


#: Content rules with safely delimitable spans -> redact on hit.
_CONTENT_RULES: tuple[_Rule, ...] = (
    _Rule(
        rule_id="content.pem-private-key",
        pattern=re.compile(_PEM_HEADER + r".*?" + _PEM_FOOTER, re.DOTALL),
        delimitable=True,
    ),
    _Rule(
        rule_id="content.aws-access-key",
        pattern=re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        delimitable=True,
    ),
    _Rule(
        rule_id="content.github-token",
        pattern=re.compile(r"\bghp_[A-Za-z0-9]{36,}\b"),
        delimitable=True,
    ),
    _Rule(
        rule_id="content.anthropic-key",
        pattern=re.compile(r"\bsk-ant-[A-Za-z0-9_-]{24,}\b"),
        delimitable=True,
    ),
)

#: A PEM header without a matching footer: the key material extends past
#: anything the gate can delimit -> refuse.
_PEM_UNTERMINATED_RULE = _Rule(
    rule_id="content.pem-unterminated",
    pattern=re.compile(_PEM_HEADER),
    delimitable=False,
)

#: Path-shaped tokens: the token itself is harmless, but its presence
#: signals surrounding credential file contents that the gate cannot
#: safely delimit -> refuse the whole compaction.
_PATH_RULES: tuple[_Rule, ...] = (
    _Rule(
        rule_id="path.env",
        pattern=re.compile(r"""(?:^|[\s"'=(/])(\.env(?:\.\w[\w.-]*)?)(?=["'):,;\s]|\Z)"""),
        delimitable=False,
        group=1,
    ),
    _Rule(
        rule_id="path.id-rsa",
        pattern=re.compile(r"\bid_rsa\b(?!\.pub\b)"),
        delimitable=False,
    ),
    _Rule(
        rule_id="path.pem",
        pattern=re.compile(r"\b[\w.~-]+\.pem\b"),
        delimitable=False,
    ),
    _Rule(
        rule_id="path.credentials",
        pattern=re.compile(r"[\w.~-]*/credentials\b|(?<![\w.-])credentials\.(?:json|csv|ini|xml|ya?ml|txt)\b"),
        delimitable=False,
    ),
    _Rule(rule_id="path.ssh-dir", pattern=re.compile(r"\.ssh/"), delimitable=False),
    _Rule(rule_id="path.aws-dir", pattern=re.compile(r"\.aws/"), delimitable=False),
    _Rule(rule_id="path.gnupg-dir", pattern=re.compile(r"\.gnupg/"), delimitable=False),
    _Rule(rule_id="path.kube-dir", pattern=re.compile(r"\.kube/"), delimitable=False),
)

#: Generic assignment: ``<name> = <value>`` / ``<name>: <value>``.
#: Group 1 is the name (checked against the keyword set after
#: normalization), group 2 the value (checked against the entropy
#: heuristic). Only the value span is redacted.
_ASSIGNMENT_PATTERN = re.compile(
    r"""([A-Za-z][A-Za-z0-9_.\-]{0,40})\s*[:=]\s*["']?([A-Za-z0-9+/=_\-]{24,})""",
)

#: Value shapes excluded from the entropy rule regardless of entropy:
#: UUIDs and pure-hex digests are identifiers, not secrets. (Belt and
#: braces -- the 4.0-bit threshold already excludes them structurally.)
_UUID_PATTERN = re.compile(r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
_HEX_PATTERN = re.compile(r"\A[0-9a-fA-F]+\Z")


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateConfig:
    """Configuration for the sensitive-content gate.

    Attributes:
        enabled: Master switch; when ``False`` the gate allows everything.
        extra_deny: Operator-supplied regex strings. Hits are redacted
            under a synthetic ``extra.<sha256[:8] of pattern>`` rule id.
            Invalid patterns are logged and skipped.
        allow: Allowlist entries. Each entry is either a rule id
            (suppresses every hit of that rule) or ``<rule-id>:<hash8>``
            (suppresses only the span whose SHA-256 starts with those 8
            hex chars). Suppressions are audit-logged.
    """

    enabled: bool = True
    extra_deny: tuple[str, ...] = ()
    allow: tuple[str, ...] = ()

    @classmethod
    def from_defaults(cls) -> GateConfig:
        """Build a config from the ``compaction`` defaults section."""
        from bernstein.core import defaults

        return cls(
            enabled=defaults.COMPACTION.sensitive_gate_enabled,
            extra_deny=tuple(defaults.COMPACTION.sensitive_gate_extra_deny),
            allow=tuple(defaults.COMPACTION.sensitive_gate_allow),
        )


@dataclass(frozen=True)
class GateFinding:
    """A single deny-rule hit. Carries the span hash, never the content.

    Attributes:
        rule_id: Identifier of the rule that fired.
        span_hash: Hex SHA-256 of the offending span bytes (UTF-8).
        start: Span start offset in the scanned text.
        end: Span end offset in the scanned text.
        delimitable: Whether the span can be safely replaced in place.
    """

    rule_id: str
    span_hash: str
    start: int
    end: int
    delimitable: bool


@dataclass(frozen=True)
class GateDecision:
    """Outcome of a gate scan.

    Attributes:
        action: ``allow`` (clean or fully suppressed), ``redacted``
            (all hits replaced with typed placeholders), or ``refused``
            (at least one non-delimitable hit; ``text`` is untouched and
            the caller MUST NOT forward it to a model API).
        text: The gated text. Original input for ``allow`` and
            ``refused``; placeholder-substituted for ``redacted``.
        findings: Non-suppressed hits, ordered by span start.
        suppressed: Hits suppressed by allowlist entries.
    """

    action: Literal["allow", "redacted", "refused"]
    text: str
    findings: tuple[GateFinding, ...] = ()
    suppressed: tuple[GateFinding, ...] = field(default=())


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def shannon_entropy(value: str) -> float:
    """Return the Shannon entropy of *value* in bits per character."""
    if not value:
        return 0.0
    counts = Counter(value)
    total = len(value)
    return -sum((n / total) * math.log2(n / total) for n in counts.values())


def _span_hash(text: str, start: int, end: int) -> str:
    return hashlib.sha256(text[start:end].encode("utf-8")).hexdigest()


def _normalize_name(name: str) -> str:
    """Normalize an assignment name for keyword matching (``api-key`` -> ``apikey``)."""
    return re.sub(r"[-_.]", "", name).lower()


def _extra_rules(config: GateConfig) -> tuple[_Rule, ...]:
    """Compile operator-supplied deny patterns; skip invalid ones."""
    rules: list[_Rule] = []
    for raw in config.extra_deny:
        try:
            pattern = re.compile(raw)
        except re.error as exc:
            logger.warning(
                "sensitive gate: skipping invalid extra_deny pattern (%s)",
                sanitize_log(str(exc)),
            )
            continue
        rule_id = f"extra.{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:8]}"
        rules.append(_Rule(rule_id=rule_id, pattern=pattern, delimitable=True))
    return tuple(rules)


def _collect_raw_findings(text: str, config: GateConfig) -> list[GateFinding]:
    """Run every rule over *text* and return all hits (may overlap)."""
    findings: list[GateFinding] = []

    complete_pem_spans: list[tuple[int, int]] = []
    for rule in _CONTENT_RULES + _PATH_RULES + _extra_rules(config):
        for match in rule.pattern.finditer(text):
            start, end = match.span(rule.group)
            findings.append(
                GateFinding(
                    rule_id=rule.rule_id,
                    span_hash=_span_hash(text, start, end),
                    start=start,
                    end=end,
                    delimitable=rule.delimitable,
                )
            )
            if rule.rule_id == "content.pem-private-key":
                complete_pem_spans.append((start, end))

    # PEM headers not inside a complete block are unterminated -> refuse.
    for match in _PEM_UNTERMINATED_RULE.pattern.finditer(text):
        start, end = match.span()
        inside_complete = any(s <= start and end <= e for s, e in complete_pem_spans)
        if not inside_complete:
            findings.append(
                GateFinding(
                    rule_id=_PEM_UNTERMINATED_RULE.rule_id,
                    span_hash=_span_hash(text, start, end),
                    start=start,
                    end=end,
                    delimitable=False,
                )
            )

    # Generic high-entropy assignment heuristic.
    for match in _ASSIGNMENT_PATTERN.finditer(text):
        name, value = match.group(1), match.group(2)
        if not any(kw in _normalize_name(name) for kw in _SENSITIVE_NAME_KEYWORDS):
            continue
        if len(value) < ENTROPY_MIN_VALUE_LENGTH:
            continue
        if _UUID_PATTERN.match(value) or _HEX_PATTERN.match(value):
            continue
        if shannon_entropy(value) <= ENTROPY_MIN_BITS_PER_CHAR:
            continue
        start, end = match.span(2)
        findings.append(
            GateFinding(
                rule_id="content.entropy-assignment",
                span_hash=_span_hash(text, start, end),
                start=start,
                end=end,
                delimitable=True,
            )
        )

    return findings


def _dedupe_overlaps(findings: list[GateFinding]) -> list[GateFinding]:
    """Resolve overlapping spans deterministically.

    Sort by (start, longest-first, rule id) and greedily keep spans that
    do not overlap an already-accepted span. Non-delimitable hits are
    always kept: they force refusal regardless of span geometry.
    """
    ordered = sorted(findings, key=lambda f: (f.start, -(f.end - f.start), f.rule_id))
    accepted: list[GateFinding] = []
    for finding in ordered:
        if not finding.delimitable:
            accepted.append(finding)
            continue
        overlaps = any(f.delimitable and finding.start < f.end and f.start < finding.end for f in accepted)
        if not overlaps:
            accepted.append(finding)
    return sorted(accepted, key=lambda f: (f.start, f.rule_id))


def _is_suppressed(finding: GateFinding, allow: tuple[str, ...]) -> bool:
    """Return whether an allowlist entry suppresses *finding*."""
    specific = f"{finding.rule_id}:{finding.span_hash[:8]}"
    return finding.rule_id in allow or specific in allow


def _redact(text: str, findings: list[GateFinding]) -> str:
    """Replace each finding span with its typed placeholder, right to left."""
    result = text
    for finding in sorted(findings, key=lambda f: f.start, reverse=True):
        placeholder = f"[REDACTED:{finding.rule_id}:{finding.span_hash[:8]}]"
        result = result[: finding.start] + placeholder + result[finding.end :]
    return result


# ---------------------------------------------------------------------------
# Public scan entrypoint
# ---------------------------------------------------------------------------


def scan_for_sensitive_content(text: str, *, config: GateConfig | None = None) -> GateDecision:
    """Scan *text* for credential-shaped content. Pure and deterministic.

    Args:
        text: The compaction input to scan.
        config: Gate configuration; defaults to the ``compaction``
            defaults section when omitted.

    Returns:
        A :class:`GateDecision`. ``refused`` decisions return the
        original text untouched -- the caller must not forward it.
    """
    if config is None:
        config = GateConfig.from_defaults()
    if not config.enabled or not text:
        return GateDecision(action="allow", text=text)

    raw = _collect_raw_findings(text, config)
    deduped = _dedupe_overlaps(raw)

    active: list[GateFinding] = []
    suppressed: list[GateFinding] = []
    for finding in deduped:
        if _is_suppressed(finding, config.allow):
            suppressed.append(finding)
        else:
            active.append(finding)

    if not active:
        return GateDecision(action="allow", text=text, suppressed=tuple(suppressed))

    if any(not finding.delimitable for finding in active):
        return GateDecision(
            action="refused",
            text=text,
            findings=tuple(active),
            suppressed=tuple(suppressed),
        )

    return GateDecision(
        action="redacted",
        text=_redact(text, active),
        findings=tuple(active),
        suppressed=tuple(suppressed),
    )


# ---------------------------------------------------------------------------
# Audit emission (explicitly separate from the pure scan)
# ---------------------------------------------------------------------------


def resolve_default_chain(base_dir: Path | None = None) -> AuditChainStore | None:
    """Return an audit chain rooted at ``<base_dir>/.sdd/audit`` if present.

    Never creates the directory: callers outside an initialized install
    (unit tests, ad-hoc scripts) get ``None`` and emission is skipped.
    """
    root = base_dir if base_dir is not None else Path.cwd()
    audit_dir = root / ".sdd" / "audit"
    if not audit_dir.is_dir():
        return None
    try:
        from bernstein.core.security.audit_chain import AuditChainStore

        return AuditChainStore(audit_dir)
    except Exception as exc:
        logger.warning("sensitive gate: audit chain unavailable (%s)", sanitize_log(str(exc)))
        return None


def emit_gate_audit(
    decision: GateDecision,
    *,
    chain: AuditChainStore | None = None,
    task_id: str | None = None,
    session_id: str = "",
) -> None:
    """Record every redaction, refusal, and suppression in the audit chain.

    Each event carries ``{task_id, rule_id, action, span_hash}`` -- the
    hash only, never the span content. Emission failures are logged and
    never abort the caller: the protective redact/refuse outcome stands
    regardless of audit availability.

    Args:
        decision: The scan outcome to record.
        chain: Audit chain store; resolved from ``./.sdd/audit`` when
            omitted, and emission is skipped when no chain is available.
        task_id: Task whose compaction input was gated; falls back to
            *session_id* for callers without a task id.
        session_id: Agent session id (fallback actor).
    """
    if not decision.findings and not decision.suppressed:
        return
    if chain is None:
        chain = resolve_default_chain()
    if chain is None:
        logger.debug("sensitive gate: no audit chain available; skipping event emission")
        return

    from bernstein.core.security.audit_chain import record_sensitive_gate

    actor = task_id or session_id or "unknown"
    records = [(finding, decision.action) for finding in decision.findings]
    records.extend((finding, "suppressed") for finding in decision.suppressed)
    for finding, action in records:
        try:
            record_sensitive_gate(
                chain=chain,
                task_id=actor,
                rule_id=finding.rule_id,
                action=action,
                span_hash=finding.span_hash,
            )
        except Exception as exc:
            logger.warning(
                "sensitive gate: audit emission failed for %s (%s)",
                sanitize_log(actor),
                sanitize_log(str(exc)),
            )


__all__ = [
    "ENTROPY_MIN_BITS_PER_CHAR",
    "ENTROPY_MIN_VALUE_LENGTH",
    "GateConfig",
    "GateDecision",
    "GateFinding",
    "emit_gate_audit",
    "resolve_default_chain",
    "scan_for_sensitive_content",
    "shannon_entropy",
]
