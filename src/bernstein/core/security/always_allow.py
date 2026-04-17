"""Always-allow rules that match tool+input patterns.

Provides a rules layer that short-circuits approval prompts when a tool
invocation matches a known-safe signature.  For example, ``grep`` on
``src/*`` paths is always allowed, while ``grep`` on ``/etc`` still
triggers an ask or deny.

Rules take **highest precedence** — an ALLOW from this engine overrides
any ASK or DENY from other guardrails (except IMMUNE and SAFETY which
remain bypass-immune).

Security model (audit-046):
    ALLOW decisions from this engine override almost every other guardrail,
    so the rules file must be *read-only from the agent's perspective*. The
    loader enforces this by:

    1. Preferring an orchestrator-only location (``.sdd/config/always_allow.yaml``
       or whatever ``BERNSTEIN_ALWAYS_ALLOW_PATH`` points at). ``.sdd/*`` is
       already an IMMUNE path, so agents cannot modify it.
    2. Accepting the legacy agent-writable path
       (``.bernstein/always_allow.yaml``) **only** when a companion manifest
       sitting in the orchestrator-only ``.sdd/config/`` directory pins the
       exact sha256 of the rules file. Any drift between file and manifest is
       treated as tampering: the loader refuses the file, emits a SAFETY
       audit event, and raises :class:`AlwaysAllowTamperError`.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from bernstein.core.security.policy_engine import DecisionType, PermissionDecision

logger = logging.getLogger(__name__)


#: Environment variable that points to an orchestrator-controlled rules file.
#: When set, it takes priority over every default search path. The loader will
#: still verify the path is not inside the agent's writable workspace.
ENV_ALWAYS_ALLOW_PATH = "BERNSTEIN_ALWAYS_ALLOW_PATH"

#: Relative orchestrator-only location for the rules file. ``.sdd/*`` is an
#: IMMUNE path — agents cannot modify anything there without being hard-blocked.
TRUSTED_RULES_REL = Path(".sdd") / "config" / "always_allow.yaml"

#: Relative orchestrator-only location for the manifest that pins the sha256
#: of any agent-writable rules file (legacy ``.bernstein/always_allow.yaml``).
TRUSTED_MANIFEST_REL = Path(".sdd") / "config" / "always_allow.manifest.json"

#: Path prefixes the agent can write to. A rules file found here requires a
#: valid manifest in the trusted location or it is rejected as tampered.
_AGENT_WRITABLE_PREFIXES: tuple[str, ...] = (".bernstein", ".claude")


class AlwaysAllowTamperError(RuntimeError):
    """Raised when the always-allow rules file fails tamper verification.

    This is a SAFETY-critical condition: the file either lives in an
    agent-writable location without an orchestrator-signed manifest, or its
    sha256 diverged from the manifest value. Callers MUST treat this as a
    refusal-to-load (no rules loaded) rather than fall back silently.
    """


#: A PermissionDecision indicating a match by an always-allow rule.
ALWAYS_ALLOW_DECISION = PermissionDecision(
    type=DecisionType.ALLOW,
    reason="Always-allowed by project rule",
    bypass_immune=False,
)


@dataclass(frozen=True)
class AlwaysAllowRule:
    """A single always-allow rule entry."""

    id: str
    tool: str
    input_pattern: str
    input_field: str = "path"
    content_patterns: list[str] = field(default_factory=lambda: [])
    description: str = ""


@dataclass(frozen=True)
class AlwaysAllowMatch:
    """Result of evaluating always-allow rules."""

    matched: bool
    rule_id: str | None = None
    reason: str = ""


@dataclass
class AlwaysAllowEngine:
    """Evaluates tool invocations against always-allow rules."""

    rules: list[AlwaysAllowRule] = field(default_factory=lambda: [])

    def match(
        self,
        tool_name: str,
        input_value: str,
        input_field: str = "path",
        full_content: str | None = None,
    ) -> AlwaysAllowMatch:
        """Check whether tool+input matches a rule."""
        for rule in self.rules:
            if rule.tool.lower() != tool_name.lower():
                continue
            if rule.input_field.lower() != input_field.lower():
                continue
            if not _pattern_matches(rule.input_pattern, input_value):
                continue
            if rule.content_patterns:
                content = full_content or input_value
                if not all(cp in content for cp in rule.content_patterns):
                    continue
            return AlwaysAllowMatch(
                matched=True,
                rule_id=rule.id,
                reason=f"Always-allow rule '{rule.id}' matched: {rule.description or rule.input_pattern}",
            )
        return AlwaysAllowMatch(matched=False, reason=f"No always-allow rule matched {tool_name}")


def _pattern_matches(pattern: str, value: str) -> bool:
    """Match *value* against *pattern* (glob by default, regex if anchored)."""
    import re

    is_regex = pattern.startswith("^") or ".*" in pattern or pattern.endswith("$")
    if is_regex:
        try:
            return re.search(pattern, value) is not None
        except re.error:
            logger.debug("Invalid regex %r — falling back to glob", pattern)
    return fnmatch.fnmatch(value, pattern)


def _load_entries(path: Path) -> list[dict[str, object]]:
    """Parse YAML into a list of typed dicts.

    Args:
        path: Path to YAML file.

    Returns:
        List of typed dicts (empty on error or wrong shape).
    """
    import yaml

    try:
        raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        logger.warning("Failed to load YAML from %s: %s", path, type(exc).__name__)  # lgtm[py/clear-text-logging-sensitive-data]  # noqa: E501
        return []

    if isinstance(raw, dict) and "always_allow" in raw:
        mapping = cast("dict[str, object]", raw)
        aa_section = mapping.get("always_allow", [])
        items: dict[str, object] | list[object] | None = cast("dict[str, object] | list[object] | None", aa_section)
    elif isinstance(raw, (dict, list)):
        items = cast("dict[str, object] | list[object]", raw)
    else:
        items = None

    if isinstance(items, dict):
        return [items]
    if isinstance(items, list):
        return [cast("dict[str, object]", item) for item in items if isinstance(item, dict)]
    return []


def _sha256_of(path: Path) -> str:
    """Return the sha256 hex digest of *path*'s bytes."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_agent_writable(rules_path: Path, workdir: Path) -> bool:
    """Return True when *rules_path* sits inside an agent-writable location.

    The agent's worktree is the primary writable surface; any file under
    ``.bernstein/`` or ``.claude/`` below *workdir* is assumed reachable by
    a compromised agent.  Paths that resolve outside *workdir* (e.g. the
    orchestrator's ``.sdd/config/`` when rules live there, or an absolute
    path passed via env var) are treated as trusted.
    """
    try:
        resolved_rules = rules_path.resolve()
        resolved_workdir = workdir.resolve()
    except OSError:
        # If we cannot resolve, err on the side of caution: treat as writable.
        return True

    try:
        rel = resolved_rules.relative_to(resolved_workdir)
    except ValueError:
        # File lives outside the agent workdir — trusted.
        return False

    parts = rel.parts
    return bool(parts) and parts[0] in _AGENT_WRITABLE_PREFIXES


def _record_tamper_event(workdir: Path, rules_path: Path, reason: str) -> None:
    """Append a SAFETY-level tamper event to ``.sdd/metrics/guardrails.jsonl``.

    Uses the same metrics file as :func:`record_guardrail_event` so operators
    see always-allow tampering alongside every other guardrail block.
    """
    import re as _re
    try:
        metrics_dir = workdir / ".sdd" / "metrics"
        metrics_dir.mkdir(parents=True, exist_ok=True)
        # Strip sha256 hex digests from the stored detail to avoid clear-text secret storage.
        _sanitized = _re.sub(r"\b[0-9a-f]{64}\b", "<digest-redacted>", reason)
        event = {
            "timestamp": datetime.now(UTC).isoformat(),
            "task_id": "orchestrator",
            "check": "always_allow_tamper",
            "result": "blocked",
            "files": [str(rules_path)],
            "detail": _sanitized,
        }
        with open(metrics_dir / "guardrails.jsonl", "a", encoding="utf-8") as f:  # lgtm[py/clear-text-storage-sensitive-data]  # noqa: E501
            f.write(json.dumps(event) + "\n")
    except OSError as exc:
        logger.error("Failed to record always-allow tamper event: %s", exc)


def write_always_allow_manifest(workdir: Path, rules_path: Path) -> Path:
    """Pin the sha256 of *rules_path* in the orchestrator-only manifest.

    Call this from the orchestrator (never from an agent process) immediately
    after authoring or updating the rules file. The resulting manifest lives
    under ``.sdd/config/`` which is an IMMUNE path — agents cannot forge a
    matching manifest without already breaking a higher guardrail.

    Args:
        workdir: Project root.
        rules_path: Path whose digest should be recorded.

    Returns:
        The absolute path of the manifest that was written.
    """
    manifest_path = workdir / TRUSTED_MANIFEST_REL
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "path": str(rules_path.resolve().relative_to(workdir.resolve()))
        if rules_path.resolve().is_relative_to(workdir.resolve())
        else str(rules_path.resolve()),
        "sha256": _sha256_of(rules_path),
        "size": rules_path.stat().st_size,
        "created_at": datetime.now(UTC).isoformat(),
    }
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return manifest_path


def _verify_manifest(workdir: Path, rules_path: Path) -> None:
    """Verify the sha256 of *rules_path* matches the trusted manifest.

    Raises:
        AlwaysAllowTamperError: if the manifest is missing, malformed, or does
            not match the current file. A SAFETY event is logged before the
            exception is raised so the tamper attempt is auditable even when
            the caller swallows the error.
    """
    manifest_path = workdir / TRUSTED_MANIFEST_REL
    if not manifest_path.exists():
        reason = (
            f"Rules file {rules_path} lives in an agent-writable location but "
            f"no orchestrator manifest exists at {manifest_path}. Refusing to "
            "load agent-supplied always-allow rules."
        )
        _record_tamper_event(workdir, rules_path, reason)
        logger.error(reason)  # lgtm[py/clear-text-logging-sensitive-data] — file paths only, no credential material
        raise AlwaysAllowTamperError(reason)

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        reason = f"Always-allow manifest at {manifest_path} is unreadable: {type(exc).__name__}"
        _record_tamper_event(workdir, rules_path, reason)
        logger.error(reason)  # lgtm[py/clear-text-logging-sensitive-data] — file path + exception type only
        raise AlwaysAllowTamperError(
            f"Always-allow manifest at {manifest_path} is unreadable: {exc}"
        ) from exc

    expected_digest = str(manifest.get("sha256", "")).lower()
    actual_digest = _sha256_of(rules_path).lower()
    if not expected_digest or expected_digest != actual_digest:
        reason = (
            f"Always-allow rules file {rules_path} sha256={actual_digest} "
            f"does not match manifest sha256={expected_digest or '<missing>'}. "
            "Refusing to load — possible agent self-escalation."
        )
        _record_tamper_event(workdir, rules_path, reason)
        logger.error(  # lgtm[py/clear-text-logging-sensitive-data] — digests are integrity fingerprints, not secrets
            "Always-allow rules file %s digest mismatch (digests redacted). "
            "Refusing to load — possible agent self-escalation.",
            rules_path,
        )
        raise AlwaysAllowTamperError(reason)


def _resolve_rules_path(workdir: Path) -> tuple[Path | None, bool]:
    """Return ``(rules_path, is_trusted)`` for the highest-priority source.

    Priority order:
        1. ``$BERNSTEIN_ALWAYS_ALLOW_PATH`` (always treated as explicit, but
           still verified against agent-writable heuristics).
        2. ``.sdd/config/always_allow.yaml`` — orchestrator-only trusted path.
        3. ``.bernstein/always_allow.yaml`` — legacy, requires manifest.
        4. ``.bernstein/rules.yaml`` — legacy combined file, requires manifest.

    Returns:
        Tuple of (path, is_trusted). ``path`` is ``None`` when no rules file
        exists on disk. ``is_trusted`` is ``True`` when the file sits outside
        the agent's writable surface.
    """
    env_value = os.environ.get(ENV_ALWAYS_ALLOW_PATH, "").strip()
    if env_value:
        candidate = Path(env_value).expanduser()
        if candidate.exists():
            return candidate, not _is_agent_writable(candidate, workdir)

    trusted = workdir / TRUSTED_RULES_REL
    if trusted.exists():
        return trusted, not _is_agent_writable(trusted, workdir)

    legacy_dedicated = workdir / ".bernstein" / "always_allow.yaml"
    if legacy_dedicated.exists():
        return legacy_dedicated, not _is_agent_writable(legacy_dedicated, workdir)

    legacy_combined = workdir / ".bernstein" / "rules.yaml"
    if legacy_combined.exists():
        return legacy_combined, not _is_agent_writable(legacy_combined, workdir)

    return None, False


def load_always_allow_rules(workdir: Path, *, strict: bool = True) -> AlwaysAllowEngine:
    """Load always-allow rules with tamper-aware source selection.

    The loader walks a priority list (env var, ``.sdd/config/``, legacy
    ``.bernstein/``) and — for any source that lives in the agent's writable
    surface — verifies the sha256 against an orchestrator-signed manifest in
    ``.sdd/config/always_allow.manifest.json`` before parsing.

    Args:
        workdir: Project root.
        strict: When ``True`` (default), tamper detection raises
            :class:`AlwaysAllowTamperError`. When ``False``, the error is
            swallowed and an empty engine is returned — useful for callers
            that prefer a safe-default (no ALLOW overrides) over a crash.

    Returns:
        A populated :class:`AlwaysAllowEngine`, or an empty one when no rules
        are configured (or when ``strict=False`` and tampering is detected).

    Raises:
        AlwaysAllowTamperError: when ``strict=True`` and the rules file lives
            in an agent-writable location without a matching manifest.
    """
    rules_path, is_trusted = _resolve_rules_path(workdir)
    if rules_path is None:
        return AlwaysAllowEngine(rules=[])

    if not is_trusted:
        try:
            _verify_manifest(workdir, rules_path)
        except AlwaysAllowTamperError:
            if strict:
                raise
            return AlwaysAllowEngine(rules=[])

    raw_items = _load_entries(rules_path)

    parsed: list[AlwaysAllowRule] = []
    for i, entry in enumerate(raw_items, start=1):
        tool = str(entry.get("tool", "")).strip()
        pattern = str(entry.get("input_pattern", "")).strip()
        if not tool or not pattern:
            continue
        # Extract content_patterns if present
        _cp_val = entry.get("content_patterns")
        if isinstance(_cp_val, list):
            content_patterns = [
                str(cp).strip() for cp in cast("list[object]", _cp_val) if isinstance(cp, (str, int, float))
            ]
        else:
            content_patterns = []
        parsed.append(
            AlwaysAllowRule(
                id=str(entry.get("id", f"aa-{tool.lower()}-{i}")),
                tool=tool,
                input_pattern=pattern,
                input_field=str(entry.get("input_field", "path")),
                content_patterns=content_patterns,
                description=str(entry.get("description", "")),
            )
        )
    return AlwaysAllowEngine(rules=parsed)


def check_always_allow(
    tool_name: str,
    input_value: str,
    engine: AlwaysAllowEngine,
    input_field: str = "path",
    full_content: str | None = None,
) -> AlwaysAllowMatch:
    """Check whether a tool invocation is always allowed."""
    return engine.match(tool_name, input_value, input_field, full_content=full_content)
