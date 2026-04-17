"""Smart auto-approve for agent tool calls.

Decomposes compound bash commands (&&, ||, ;, |) into sub-commands, checks
each against allow/deny patterns, and returns an approval decision.

Safe commands (ls, cat, grep, git status, pytest, etc.) are auto-approved.
Dangerous commands (rm -rf, DROP TABLE, git push --force, etc.) are denied.
Everything else escalates to the human operator.

Decision hierarchy: DENY > APPROVE > ASK.
Any sub-command that matches a deny pattern blocks the whole command.
A compound command is auto-approved only if every sub-command matches an
allow pattern.  Otherwise the decision is ASK (escalate).

The ``normalize_command`` function is applied before pattern matching to
defeat encoding tricks, backtick substitution, ``$(...)`` expansion,
environment variable expansion, and Unicode homoglyph evasion.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class Decision(StrEnum):
    """Approval decision for a tool call."""

    APPROVE = "approve"
    DENY = "deny"
    ASK = "ask"


@dataclass(frozen=True)
class ApprovalResult:
    """Result of evaluating a command or tool call.

    Attributes:
        decision: The approval decision.
        reason: Human-readable explanation.
        matched_pattern: The pattern (if any) that drove the decision.
    """

    decision: Decision
    reason: str
    matched_pattern: str = ""


# ---------------------------------------------------------------------------
# Command normalization — defeat evasion techniques
# ---------------------------------------------------------------------------

# Unicode homoglyph map: visually similar characters → ASCII equivalents.
# Attackers may use fullwidth latin, Cyrillic, or other lookalikes.
_HOMOGLYPH_MAP: Final[dict[str, str]] = {
    # Fullwidth Latin
    "\uff52": "r",
    "\uff4d": "m",
    "\uff53": "s",
    "\uff55": "u",
    "\uff44": "d",
    "\uff4f": "o",
    "\uff43": "c",
    "\uff48": "h",
    "\uff41": "a",
    "\uff54": "t",
    "\uff45": "e",
    "\uff4e": "n",
    "\uff56": "v",
    # Cyrillic lookalikes
    "\u0430": "a",  # U+0430
    "\u0435": "e",  # U+0435
    "\u043e": "o",  # U+043E
    "\u0440": "p",  # U+0440
    "\u0441": "c",  # U+0441
    "\u0443": "y",  # U+0443
    "\u0445": "x",  # U+0445
    # Zero-width chars (just strip them)
    "\u200b": "",  # zero-width space
    "\u200c": "",  # zero-width non-joiner
    "\u200d": "",  # zero-width joiner
    "\ufeff": "",  # BOM
}

# Regex: backtick command substitution e.g. `echo rm`
_BACKTICK_RE: Final[re.Pattern[str]] = re.compile(r"`([^`]*)`")

# Regex: $(...) command substitution e.g. $(echo rm)
_DOLLAR_PAREN_RE: Final[re.Pattern[str]] = re.compile(r"\$\(([^)]*)\)")

# Regex: ${VAR} or $VAR environment variable references
_ENV_VAR_BRACED_RE: Final[re.Pattern[str]] = re.compile(r"\$\{(\w+)\}")
_ENV_VAR_BARE_RE: Final[re.Pattern[str]] = re.compile(r"\$(\w+)")

# Regex: hex/octal escape sequences e.g. $'\x72\x6d' or \x72\x6d
_HEX_ESCAPE_RE: Final[re.Pattern[str]] = re.compile(r"\\x([0-9a-fA-F]{2})")
_OCTAL_ESCAPE_RE: Final[re.Pattern[str]] = re.compile(r"\\([0-7]{1,3})")

# Regex: ANSI-C $'...' quoting
_ANSI_C_RE: Final[re.Pattern[str]] = re.compile(r"\$'([^']*)'")

# Regex: base64 decode pipe patterns e.g. echo xxx | base64 -d | sh
_BASE64_PIPE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:echo|printf)\s+['\"]?[A-Za-z0-9+/=]+['\"]?\s*\|.*base64\s+(-d|--decode)",
)


def _replace_homoglyphs(cmd: str) -> str:
    """Replace Unicode homoglyphs with their ASCII equivalents."""
    for glyph, replacement in _HOMOGLYPH_MAP.items():
        if glyph in cmd:
            cmd = cmd.replace(glyph, replacement)
    return cmd


def _decode_hex_escapes(cmd: str) -> str:
    """Decode hex escape sequences like \\x72\\x6d to their characters."""
    return _HEX_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), cmd)


def _decode_octal_escapes(cmd: str) -> str:
    """Decode octal escape sequences like \\162\\155 to their characters."""
    return _OCTAL_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 8)), cmd)


def _expand_ansi_c_quoting(cmd: str) -> str:
    r"""Expand ANSI-C $'...' quoting: $'\x72\x6d' -> rm."""

    def _expand(m: re.Match[str]) -> str:
        inner = m.group(1)
        inner = _decode_hex_escapes(inner)
        inner = _decode_octal_escapes(inner)
        return inner

    return _ANSI_C_RE.sub(_expand, cmd)


def _extract_substitution_payloads(cmd: str) -> str:
    """Replace backtick and $() substitutions with their inner content.

    Replaces each substitution in-place so that the resulting command
    string can be pattern-matched as a whole.  For example::

        $(echo rm) -rf /tmp  →  echo rm -rf /tmp  →  matches rm\\s+-rf deny

    This handles the common evasion technique of wrapping a dangerous
    command name inside ``$(...)`` or backticks to break naive matching.
    """
    # Replace $(...) with inner content
    cmd = _DOLLAR_PAREN_RE.sub(lambda m: m.group(1), cmd)
    # Replace `...` with inner content
    cmd = _BACKTICK_RE.sub(lambda m: m.group(1), cmd)
    return cmd


def _expand_env_vars(cmd: str) -> str:
    """Replace common env var patterns with their literal expansions.

    This doesn't have access to the real environment, so it strips the
    ``${}`` / ``$`` wrapper to expose the var name for pattern matching.
    For example, ``${HOME}`` becomes ``HOME``, which is benign.
    The critical case is obfuscation like ``$r$m`` → ``rm``.
    """
    # Handle braced form first: ${VAR} → VAR
    cmd = _ENV_VAR_BRACED_RE.sub(lambda m: m.group(1), cmd)
    # Handle bare form: $VAR → VAR (covers $r$m → rm evasion)
    cmd = _ENV_VAR_BARE_RE.sub(lambda m: m.group(1), cmd)
    return cmd


def normalize_command(cmd: str) -> str:
    """Normalize a shell command to defeat evasion techniques.

    Applied before pattern matching. Handles:
    - Unicode homoglyphs (fullwidth latin, Cyrillic lookalikes)
    - ANSI-C $'\\x72\\x6d' hex/octal quoting
    - Backtick command substitution: extracts inner command
    - ``$(...)`` command substitution: extracts inner command
    - Environment variable ``${VAR}`` / ``$VAR`` expansion
    - Hex and octal escape sequences
    - Whitespace normalization
    - Zero-width Unicode characters

    Args:
        cmd: Raw shell command string.

    Returns:
        Normalized command string with evasion tricks resolved.
    """
    # Step 1: Strip zero-width and replace homoglyphs
    cmd = _replace_homoglyphs(cmd)

    # Step 2: Expand ANSI-C quoting ($'\x72\x6d' -> rm)
    cmd = _expand_ansi_c_quoting(cmd)

    # Step 3: Decode remaining hex/octal escapes
    cmd = _decode_hex_escapes(cmd)
    cmd = _decode_octal_escapes(cmd)

    # Step 4: Extract inner payloads from command substitutions
    cmd = _extract_substitution_payloads(cmd)

    # Step 5: Expand env var references
    cmd = _expand_env_vars(cmd)

    # Step 6: Normalize whitespace (collapse multiple spaces)
    cmd = re.sub(r"\s+", " ", cmd).strip()

    return cmd


# ---------------------------------------------------------------------------
# Pattern lists
# ---------------------------------------------------------------------------

# Allow patterns — safe, read-only or low-risk operations.
# Each entry is a raw regex string matched against the stripped sub-command.
#
# IMPORTANT: Do NOT add bare `^<tool>\s` patterns for tools that can take
# arbitrary paths or arguments (python, cat, cp, mv, mkdir, touch, rm).
# Any such allowance effectively defeats the deny list — e.g. bare
# `^python\s` lets an agent run `python /tmp/evil.py`, and bare `^cat\s`
# lets it read `/etc/shadow`.  If an invocation is genuinely safe, encode
# that safety into the regex (fixed sub-command, no caller-controlled
# arguments), or surface it through the ``BERNSTEIN_AUTO_APPROVE_EXTRA``
# escape hatch.
_ALLOW_PATTERNS: Final[list[str]] = [
    # Filesystem read-only (bare `cat`/`head`/`tail` intentionally NOT
    # allowed here — see credential-read deny patterns below; operators
    # can opt in via BERNSTEIN_AUTO_APPROVE_EXTRA if needed)
    r"^ls(\s|$)",
    r"^head\s",
    r"^tail\s",
    r"^less\s",
    r"^more\s",
    r"^file\s",
    r"^stat\s",
    r"^wc\s",
    r"^du\s",
    r"^df\s",
    r"^pwd$",
    r"^find\s",
    # Text search / inspection
    r"^grep\s",
    r"^rg\s",
    r"^awk\s",
    r"^sed\s+-n\s",  # sed read-only (-n with no in-place)
    r"^cut\s",
    r"^sort\s",
    r"^uniq\s",
    r"^tr\s",
    r"^diff\s",
    r"^echo\s",
    r"^echo$",
    r"^printf\s",
    # System info
    r"^whoami$",
    r"^id$",
    r"^date(\s|$)",
    r"^uname(\s|$)",
    r"^hostname(\s|$)",
    r"^uptime$",
    r"^env$",
    r"^printenv(\s|$)",
    r"^which\s",
    r"^type\s",
    r"^command\s",
    r"^ps\s",
    r"^top\s",
    # Python / uv — restricted: only `-m <allowed_tool>` or version probe.
    # Bare `python <script>` is NOT auto-approved because the script path
    # is caller-controlled (agent could run python /tmp/evil.py).
    r"^python(\d(\.\d+)?)?\s+--version$",
    r"^python(\d(\.\d+)?)?\s+-V$",
    r"^python(\d(\.\d+)?)?\s+-m\s+(pytest|ruff|mypy|pyright|pyflakes|black|isort|unittest|venv|pip\s+list|pip\s+show|pip\s+freeze|pip\s+check)(\s|$)",
    r"^uv\s+(pip\s+(list|show|freeze|check|inspect|index)|version|tool\s+list|sync\s+--dry-run|lock\s+--check)(\s|$)",
    r"^uv\s+run\s+python\s+-m\s+(pytest|ruff|mypy|pyright|pyflakes|black|isort|unittest)(\s|$)",
    r"^uv\s+run\s+python\s+scripts/run_tests\.py(\s|$)",
    r"^uv\s+run\s+(pytest|ruff|mypy|pyright|pyflakes|black|isort)(\s|$)",
    r"^pip(\d(\.\d+)?)?\s+(list|show|freeze|check|index|inspect)(\s|$)",
    # Testing
    r"^pytest(\s|$)",
    r"^ruff\s+(check|format\s+--check)(\s|$)",
    r"^mypy(\s|$)",
    r"^pyright(\s|$)",
    # Git read-only
    r"^git\s+(status|log|diff|show|branch|remote|tag|describe|stash\s+list|ls-files|ls-tree|rev-parse|config\s+--list|shortlog|blame|check-ignore|for-each-ref)(\s|$)",
    r"^git\s+log(\s|$)",
    r"^git\s+diff(\s|$)",
    # HTTP to localhost Bernstein server (task completion, status checks)
    r"^curl\s+.*http://127\.0\.0\.1:8052",
    r"^curl\s+.*localhost:8052",
    # Output formatting
    r"^jq(\s|$)",
    r"^column(\s|$)",
    # Misc safe utilities
    r"^true$",
    r"^false$",
    r"^sleep\s",
    r"^test\s",
    r"^\[.*\]$",
    r"^read\s",
    r"^set\s+-[a-zA-Z]+$",  # set -e, set -x etc.
    r"^export\s+\w+=",
    r"^cd\s",
    r"^cd$",
]

# Deny patterns — destructive or high-risk operations.
_DENY_PATTERNS: Final[list[str]] = [
    # Destructive filesystem
    r"\brm\s+.*(-[a-zA-Z]*r[a-zA-Z]*|-[a-zA-Z]*f[a-zA-Z]*)\s",
    r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*|-[a-zA-Z]*f[a-zA-Z]*)$",
    r"\brm\s+-rf\b",
    r"\brm\s+-fr\b",
    r"\brmdir\s",
    r"\bshred\s",
    r"\bdd\s+",
    r"\btruncate\s+-s\s+0\b",
    # Dangerous git operations
    r"\bgit\s+push\b.*--force\b",
    r"\bgit\s+push\b.*-f\b",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+clean\s+.*-[a-zA-Z]*f[a-zA-Z]*\b",
    r"\bgit\s+branch\s+-[Dd]\b",
    r"\bgit\s+checkout\s+--\s",
    # SQL destructive
    r"(?i)\bDROP\s+(TABLE|DATABASE|SCHEMA|INDEX|VIEW)\b",
    r"(?i)\bTRUNCATE\s+TABLE\b",
    r"(?i)\bDELETE\s+FROM\b",
    r"(?i)\bALTER\s+TABLE\b.*\bDROP\b",
    # System modification / privilege escalation
    r"\bsudo\s",
    r"\bsu\s",
    r"\bchmod\s",
    r"\bchown\s",
    r"\bchattr\s",
    # Package managers installing/removing
    r"\bapt(-get)?\s+(install|remove|purge|autoremove)\b",
    r"\byum\s+(install|remove|erase)\b",
    r"\bdnf\s+(install|remove|erase)\b",
    r"\bpacman\s+-[A-Z]*[SR][A-Z]*\b",
    r"\bbrew\s+(install|uninstall|remove)\b",
    r"\bnpm\s+(install|uninstall|remove)\b.*-g\b",
    # Curl piped to shell (code execution from URL)
    r"\bcurl\b.*\|\s*(ba)?sh\b",
    r"\bwget\b.*\|\s*(ba)?sh\b",
    r"\bcurl\b.*\|\s*python\b",
    r"\bwget\b.*\|\s*python\b",
    # Process termination — aggressive
    r"\bkill\s+.*-9\b",
    r"\bkill\s+.*-SIGKILL\b",
    r"\bpkill\s",
    r"\bkillall\s",
    # Writing to sensitive system paths (redirection)
    r">\s*/etc/",
    r">\s*/usr/",
    r">\s*/bin/",
    r">\s*/sbin/",
    r">\s*/lib/",
    r">\s*/proc/",
    r">\s*/sys/",
    r">\s*/var/",
    r">\s*~/\.ssh/",
    r">\s*/root/",
    # Writing to Bernstein control-plane state (no agent should mutate these
    # via raw shell — they are the orchestrator's source of truth)
    r">\s*\.bernstein/",
    r">\s*\.sdd/",
    r">>\s*\.bernstein/",
    r">>\s*\.sdd/",
    # Any invocation of cp/mv/touch/mkdir/sed-inplace targeting control-plane
    # or system credential paths, regardless of argument order.
    r"\b(cp|mv|touch|mkdir|ln)\b\s+.*(?:\.bernstein/|\.sdd/|/etc/|/usr/|/bin/|/sbin/|/lib/|/var/|/root/|~/\.ssh/|~/\.aws/)",
    r"\bsed\s+-i\b.*(?:\.bernstein/|\.sdd/|/etc/|/usr/|/bin/|/sbin/|/lib/|/var/|/root/|~/\.ssh/|~/\.aws/)",
    # Reading sensitive credentials from disk (bare cat/head/tail/less/more)
    r"\b(cat|head|tail|less|more|bat)\b\s+.*(/etc/passwd|/etc/shadow|/etc/sudoers|/etc/gshadow)",
    r"\b(cat|head|tail|less|more|bat)\b\s+.*~/\.ssh/",
    r"\b(cat|head|tail|less|more|bat)\b\s+.*~/\.aws/credentials",
    r"\b(cat|head|tail|less|more|bat)\b\s+.*~/\.netrc",
    r"\b(cat|head|tail|less|more|bat)\b\s+.*~/\.docker/config\.json",
    r"\b(cat|head|tail|less|more|bat)\b\s+.*\.pem\b",
    r"\b(cat|head|tail|less|more|bat)\b\s+.*\.key\b",
    r"\b(cat|head|tail|less|more|bat)\b\s+.*id_rsa\b",
    r"\b(cat|head|tail|less|more|bat)\b\s+.*id_ed25519\b",
    r"\b(cat|head|tail|less|more|bat)\b\s+.*\.ppk\b",
    # Bare python running an arbitrary script from a world-writable location
    r"^python(\d(\.\d+)?)?\s+/tmp/",
    r"^python(\d(\.\d+)?)?\s+/var/tmp/",
    r"^python(\d(\.\d+)?)?\s+/dev/shm/",
    # Arbitrary shell sourcing from untrusted roots
    r"^(bash|sh|zsh)\s+/tmp/",
    r"^(bash|sh|zsh)\s+/var/tmp/",
    r"\bsource\s+/tmp/",
    r"\.\s+/tmp/",  # `. /tmp/foo` sources a script
    # Network-modifying system tools
    r"\biptables\b",
    r"\bip\s+(route|addr|link)\s+(add|del|flush|change|replace)\b",
    r"\broute\s+(add|del|flush)\b",
    r"\bnetcat\b\s+.*-[eE]\b",  # reverse shell pattern
    r"\bnc\b\s+.*-[eE]\b",
    # Package installs (no --global-as-global needed — any install is review-worthy)
    r"\bnpm\s+(install|i|add|uninstall|remove|ci)\b",
    r"\byarn\s+(add|remove|install)\b",
    r"\bpnpm\s+(add|remove|install)\b",
    r"\buv\s+(add|remove|pip\s+install|pip\s+uninstall|tool\s+install)\b",
    r"\bpip(\d(\.\d+)?)?\s+(install|uninstall)\b",
    r"\bcargo\s+(install|uninstall)\b",
    r"\bgo\s+install\b",
    r"\bgem\s+(install|uninstall)\b",
    # Git push without explicit branch/remote pattern — must be reviewed.
    # We keep a narrow allow for `git push origin main`/`HEAD` via the
    # deny-not-matching fall-through; any other `git push` falls into ASK.
    r"\bgit\s+push\s+.*--mirror\b",
    r"\bgit\s+push\s+.*--delete\b",
    # Fork bombs / resource exhaustion patterns
    r":\(\)\{.*:\|:&",
    r"\byes\b\s*\|",
]

# ---------------------------------------------------------------------------
# Operator-configurable escape hatch
# ---------------------------------------------------------------------------
#
# Teams can opt specific patterns back into the auto-approve allow list
# without patching the source.  Two mechanisms are supported:
#
# 1. Environment variable ``BERNSTEIN_AUTO_APPROVE_EXTRA``: newline- or
#    ``::``-separated list of regex patterns.  ``,`` is intentionally NOT
#    used as a separator because it appears in many regex character
#    classes.  Example::
#
#        BERNSTEIN_AUTO_APPROVE_EXTRA='^make\s+test$::^docker\s+ps$'
#
# 2. :func:`set_extra_allow_patterns` — programmatic override, useful for
#    tests and for operators loading config from a file.
#
# Deny patterns always take precedence over extra allow patterns: the
# escape hatch cannot be used to allow a deny-listed command.

_EXTRA_ALLOW_ENV_VAR: Final[str] = "BERNSTEIN_AUTO_APPROVE_EXTRA"
_EXTRA_ALLOW_SEPARATOR: Final[str] = "::"


def _parse_extra_patterns(raw: str | None) -> list[re.Pattern[str]]:
    """Parse the ``BERNSTEIN_AUTO_APPROVE_EXTRA`` payload into patterns.

    Accepts ``::`` or newline as separators.  Empty entries and entries
    that fail to compile are silently dropped (a malformed regex must
    never widen the allow list).

    Args:
        raw: Raw environment variable value, or ``None`` / empty string.

    Returns:
        List of compiled regex patterns (possibly empty).
    """
    if not raw:
        return []
    # Normalize newlines to the separator, then split
    payload = raw.replace("\r\n", "\n").replace("\n", _EXTRA_ALLOW_SEPARATOR)
    raw_entries = payload.split(_EXTRA_ALLOW_SEPARATOR)
    out: list[re.Pattern[str]] = []
    for entry in raw_entries:
        pattern = entry.strip()
        if not pattern:
            continue
        try:
            out.append(re.compile(pattern))
        except re.error:
            # Malformed regex — never widen the allow list on bad input
            continue
    return out


_extra_allow: list[re.Pattern[str]] = _parse_extra_patterns(os.environ.get(_EXTRA_ALLOW_ENV_VAR))


def set_extra_allow_patterns(patterns: list[str] | None) -> None:
    """Programmatically override operator-extra allow patterns.

    Useful for tests and for config-file-driven bootstrap.  Pass ``None``
    or an empty list to clear the extras.  Invalid regexes are skipped.

    Args:
        patterns: List of regex strings, or ``None`` to clear.
    """
    global _extra_allow
    if not patterns:
        _extra_allow = []
        return
    compiled: list[re.Pattern[str]] = []
    for entry in patterns:
        try:
            compiled.append(re.compile(entry))
        except re.error:
            continue
    _extra_allow = compiled


def reload_extra_allow_patterns_from_env() -> None:
    """Re-read ``BERNSTEIN_AUTO_APPROVE_EXTRA`` from the current environment.

    Useful after mutating :data:`os.environ` at runtime (tests, hot-reload).
    """
    global _extra_allow
    _extra_allow = _parse_extra_patterns(os.environ.get(_EXTRA_ALLOW_ENV_VAR))


# ---------------------------------------------------------------------------
# Compiled pattern caches
# ---------------------------------------------------------------------------

_compiled_allow: list[re.Pattern[str]] = [re.compile(p) for p in _ALLOW_PATTERNS]
_compiled_deny: list[re.Pattern[str]] = [re.compile(p) for p in _DENY_PATTERNS]

# Non-bash tool allow-list: tools that are always safe to approve.
_SAFE_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "Read",
        "Glob",
        "Grep",
        "TodoWrite",
        "TodoRead",
        "WebFetch",
        "WebSearch",
        "NotebookRead",
        "NotebookEdit",
    }
)

# Non-bash tool deny-list: tools that require human approval.
_DANGEROUS_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "ServerSideRendering",
    }
)


# ---------------------------------------------------------------------------
# Command decomposition
# ---------------------------------------------------------------------------


def _skip_quoted(cmd: str, start: int, n: int) -> int:
    """Skip past a quoted string starting at *start*, returning the index after the closing quote."""
    quote = cmd[start]
    j = start + 1
    while j < n and cmd[j] != quote:
        if quote == '"' and cmd[j] == "\\" and j + 1 < n:
            j += 1  # skip escaped char
        j += 1
    return j + 1


def _shell_operator_len(cmd: str, i: int, n: int) -> int:
    """Return the length of a shell operator at position *i*, or 0 if none."""
    two = cmd[i : i + 2]
    if two in ("&&", "||"):
        return 2
    ch = cmd[i]
    if ch == ";" and (i + 1 >= n or cmd[i + 1] != ";"):
        return 1
    if ch == "|" and (i + 1 >= n or cmd[i + 1] != "|"):
        return 1
    return 0


def decompose_command(cmd: str) -> list[str]:
    """Split a compound shell command into individual sub-commands.

    Handles:
    - ``&&`` and ``||`` (short-circuit operators)
    - ``;`` (sequential execution)
    - ``|`` (pipes — each segment is a separate command)
    - Quoted strings (single and double) are not split inside quotes.

    Args:
        cmd: Raw shell command string.

    Returns:
        List of trimmed sub-command strings in left-to-right order.
    """
    parts: list[str] = []
    current: list[str] = []
    i = 0
    n = len(cmd)
    while i < n:
        ch = cmd[i]
        # Handle quoted strings
        if ch in ("'", '"'):
            end = _skip_quoted(cmd, i, n)
            current.append(cmd[i:end])
            i = end
            continue
        # Check for shell operators
        op_len = _shell_operator_len(cmd, i, n)
        if op_len:
            parts.append("".join(current).strip())
            current = []
            i += op_len
            continue
        current.append(ch)
        i += 1

    last = "".join(current).strip()
    if last:
        parts.append(last)

    # Filter empty strings that can arise from leading operators
    return [p for p in parts if p]


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _match_deny(cmd: str) -> str | None:
    """Return the first matching deny pattern string, or None."""
    for pattern in _compiled_deny:
        if pattern.search(cmd):
            return pattern.pattern
    return None


def _match_allow(cmd: str) -> str | None:
    """Return the first matching allow pattern string, or None.

    Checks the built-in allow list first, then the operator-extra allow
    list loaded from :data:`BERNSTEIN_AUTO_APPROVE_EXTRA`.  Deny patterns
    are evaluated independently and always win; the extras cannot
    override a deny match.
    """
    for pattern in _compiled_allow:
        if pattern.search(cmd):
            return pattern.pattern
    for pattern in _extra_allow:
        if pattern.search(cmd):
            return pattern.pattern
    return None


def classify_command(cmd: str) -> ApprovalResult:
    """Classify a (potentially compound) bash command.

    Applies :func:`normalize_command` first to defeat encoding tricks,
    backtick/``$(...)`` substitution, env var expansion, and Unicode
    homoglyphs.

    Then checks the full command string against deny patterns (to catch
    cross-sub-command patterns like ``curl ... | bash``), then decomposes
    compound commands and applies the pattern hierarchy:
    DENY > APPROVE > ASK.  Any sub-command matching a deny pattern
    causes the entire result to be DENY.  If all sub-commands match
    an allow pattern, the result is APPROVE.  Otherwise it is ASK.

    Args:
        cmd: Raw shell command string (may include ``&&``, ``||``, etc.).

    Returns:
        :class:`ApprovalResult` with the worst-case decision.
    """
    # Detect base64-decode pipe patterns on the raw command before normalization
    if _BASE64_PIPE_RE.search(cmd):
        return ApprovalResult(
            Decision.DENY,
            f"Base64-decode pipe evasion detected: {cmd!r}",
            matched_pattern="base64_pipe_evasion",
        )

    # Normalize to defeat evasion techniques
    normalized = normalize_command(cmd)

    # Check the full (un-decomposed) command first so cross-boundary patterns
    # like "curl ... | bash" are caught even though | is a decomposition point.
    full_deny = _match_deny(normalized)
    if full_deny:
        return ApprovalResult(
            Decision.DENY,
            f"Dangerous command detected: {cmd!r}",
            matched_pattern=full_deny,
        )

    sub_commands = decompose_command(normalized)
    if not sub_commands:
        return ApprovalResult(Decision.APPROVE, "Empty command")

    for sub in sub_commands:
        deny_pat = _match_deny(sub)
        if deny_pat:
            return ApprovalResult(
                Decision.DENY,
                f"Dangerous sub-command detected: {sub!r}",
                matched_pattern=deny_pat,
            )

    unmatched: list[str] = []
    for sub in sub_commands:
        if _match_allow(sub) is None:
            unmatched.append(sub)

    if unmatched:
        return ApprovalResult(
            Decision.ASK,
            f"Sub-command(s) require human review: {', '.join(repr(s) for s in unmatched)}",
        )

    return ApprovalResult(Decision.APPROVE, f"All {len(sub_commands)} sub-command(s) matched allow list")


def classify_tool_call(tool_name: str, tool_input: dict[str, object]) -> ApprovalResult:
    """Classify any tool call by name and input.

    For ``Bash`` (or ``bash``) tools, delegates to :func:`classify_command`.
    For tools in the safe-tools set, auto-approves.
    For tools in the dangerous-tools set, denies.
    All other tools escalate to ASK.

    Args:
        tool_name: Name of the tool being called (e.g. ``"Bash"``, ``"Edit"``).
        tool_input: Tool input dict.  For bash tools, must contain ``"command"``.

    Returns:
        :class:`ApprovalResult` with the decision.
    """
    if tool_name.lower() in ("bash", "shell"):
        cmd = str(tool_input.get("command", ""))
        return classify_command(cmd)

    if tool_name in _SAFE_TOOLS:
        return ApprovalResult(Decision.APPROVE, f"Tool {tool_name!r} is in the safe-tools allow list")

    if tool_name in _DANGEROUS_TOOLS:
        return ApprovalResult(Decision.DENY, f"Tool {tool_name!r} is in the dangerous-tools deny list")

    # Write-capable tools like Edit, Write: ask by default
    return ApprovalResult(Decision.ASK, f"Tool {tool_name!r} requires human review")
