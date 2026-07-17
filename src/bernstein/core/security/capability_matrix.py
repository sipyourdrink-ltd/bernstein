"""Lethal-trifecta capability matrix for tool/MCP/adapter calls.

Implements Simon Willison's "lethal trifecta" structural rule: every tool
is tagged with which of the three capabilities it carries
(``PRIVATE_DATA``, ``UNTRUSTED_INPUT``, ``EXTERNAL_COMM``); whenever the
union of capabilities along a single execution path covers all three, the
chain is denied.

This is a structural orchestration-time check - not a guardrail prompt -
so it cannot be bypassed by injection attempts in untrusted content.

Usage::

    from bernstein.core.security.capability_matrix import (
        Capability,
        CapabilityRegistry,
        EnforcementMode,
    )

    registry = CapabilityRegistry.load_default()
    decision = registry.evaluate_chain(
        ["fs.read_secret", "github.fetch_issue", "github.post_comment"]
    )
    if not decision.allowed:
        raise PermissionError(decision.reason)
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, cast

import yaml

from bernstein import _BUNDLED_TEMPLATES_DIR  # type: ignore[reportPrivateUsage]

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

logger = logging.getLogger(__name__)


class Capability(StrEnum):
    """Lethal-trifecta capability axes.

    A tool may carry zero or more of these tags.  When all three are
    carried by the *union* of tools on an execution path, the path is
    refused.
    """

    PRIVATE_DATA = "private_data"
    UNTRUSTED_INPUT = "untrusted_input"
    EXTERNAL_COMM = "external_comm"


CapabilitySource = Literal["declared", "inferred", "default"]


class EnforcementMode(StrEnum):
    """Lethal-trifecta enforcement levels."""

    ENFORCE = "enforce"
    WARN = "warn"
    OFF = "off"


_ALL_CAPABILITIES: frozenset[Capability] = frozenset(Capability)


@dataclass(frozen=True)
class ToolCapabilities:
    """Capability tags attached to a single tool name.

    Attributes:
        tool_name: Stable identifier (MCP tool, adapter command, hook).
        capabilities: Capability axes the tool carries.
        source: Where the tags came from - declared YAML, inferred from
            heuristics, or defaulted because the tool was unknown.
    """

    tool_name: str
    capabilities: frozenset[Capability]
    source: CapabilitySource = "declared"


@dataclass(frozen=True)
class ChainDecision:
    """Result of evaluating a tool chain against the capability matrix.

    Attributes:
        allowed: True when the chain is permitted under the active mode.
        reason: Human-readable explanation (constant for stable audit).
        triggered: The set of capabilities present along the chain.
        offending_tools: Tools that contributed each triggering capability.
        unknown_tools: Tools missing declarations (treated as high-risk).
        mode: The enforcement mode used for this evaluation.
    """

    allowed: bool
    reason: str
    triggered: frozenset[Capability] = frozenset()
    offending_tools: tuple[str, ...] = ()
    unknown_tools: tuple[str, ...] = ()
    mode: EnforcementMode = EnforcementMode.ENFORCE


@dataclass
class CapabilityRegistry:
    """In-memory map of tool name → :class:`ToolCapabilities`.

    Unknown tools are treated as carrying *all three* capabilities - the
    safest default, since the orchestrator cannot prove otherwise.
    """

    tools: dict[str, ToolCapabilities] = field(default_factory=dict[str, ToolCapabilities])
    mode: EnforcementMode = EnforcementMode.ENFORCE

    DEFAULT_REASON: str = "lethal trifecta"
    UNKNOWN_REASON: str = "lethal trifecta (unknown tool defaults to all capabilities)"

    def register(self, entry: ToolCapabilities) -> None:
        """Register a capability declaration, replacing any prior entry."""
        self.tools[entry.tool_name] = entry

    def lookup(self, tool_name: str) -> ToolCapabilities:
        """Return the entry for *tool_name*; default-deny when unknown.

        Missing tools yield a synthetic ``ToolCapabilities`` carrying all
        three capabilities with ``source="default"`` so callers can
        distinguish declared from defaulted entries.
        """
        existing = self.tools.get(tool_name)
        if existing is not None:
            return existing
        return ToolCapabilities(
            tool_name=tool_name,
            capabilities=_ALL_CAPABILITIES,
            source="default",
        )

    def evaluate_chain(
        self,
        tools: Sequence[str],
        *,
        operand_trust: Sequence[object] | None = None,
    ) -> ChainDecision:
        """Evaluate a tool chain for the lethal trifecta.

        Args:
            tools: Tools that will run on a single execution path.
            operand_trust: Optional trust classes of the *data operands* the
                chain acts on, propagated through the lineage graph (issue
                #2513). When any operand is untrusted (a
                :class:`~bernstein.core.lineage.provenance.TrustClass` at or
                below the untrusted threshold) or ``None`` (provenance could
                not be resolved -> fail closed), the chain carries
                ``UNTRUSTED_INPUT`` by *data*, even when no static tool tag
                does. Omitting this argument preserves the pre-#2513 behaviour
                exactly.

        Returns:
            A :class:`ChainDecision`.  In ``OFF`` mode the chain is always
            allowed (the audit trail still records the triggered set).
            In ``WARN`` mode the chain is allowed but the reason carries
            the warning so the caller can log it.
        """
        triggered: set[Capability] = set()
        offending: dict[Capability, list[str]] = {cap: [] for cap in Capability}
        unknown: list[str] = []

        for tool in tools:
            entry = self.lookup(tool)
            if entry.source == "default":
                unknown.append(tool)
            for cap in entry.capabilities:
                triggered.add(cap)
                offending[cap].append(tool)

        # Data-carried taint: an untrusted operand contributes UNTRUSTED_INPUT
        # to the union regardless of which tool last touched the bytes. This
        # closes the data-flow laundering path the static tags leave open.
        if operand_trust is not None and _any_untrusted(operand_trust):
            triggered.add(Capability.UNTRUSTED_INPUT)
            offending[Capability.UNTRUSTED_INPUT].append("(tainted operand)")

        triggered_frozen = frozenset(triggered)
        full_trifecta = triggered_frozen >= _ALL_CAPABILITIES

        if not full_trifecta:
            return ChainDecision(
                allowed=True,
                reason="capability chain ok",
                triggered=triggered_frozen,
                unknown_tools=tuple(unknown),
                mode=self.mode,
            )

        offending_tools = tuple(sorted(set(itertools.chain.from_iterable(offending.values()))))
        reason = self.UNKNOWN_REASON if unknown else self.DEFAULT_REASON

        if self.mode is EnforcementMode.OFF:
            return ChainDecision(
                allowed=True,
                reason=f"{reason} (enforcement off)",
                triggered=triggered_frozen,
                offending_tools=offending_tools,
                unknown_tools=tuple(unknown),
                mode=self.mode,
            )
        if self.mode is EnforcementMode.WARN:
            return ChainDecision(
                allowed=True,
                reason=f"{reason} (warn-only)",
                triggered=triggered_frozen,
                offending_tools=offending_tools,
                unknown_tools=tuple(unknown),
                mode=self.mode,
            )
        return ChainDecision(
            allowed=False,
            reason=reason,
            triggered=triggered_frozen,
            offending_tools=offending_tools,
            unknown_tools=tuple(unknown),
            mode=self.mode,
        )

    @classmethod
    def from_directory(
        cls,
        directory: Path,
        *,
        mode: EnforcementMode = EnforcementMode.ENFORCE,
    ) -> CapabilityRegistry:
        """Load all ``*.yaml`` declarations under *directory* recursively."""
        registry = cls(mode=mode)
        if not directory.is_dir():
            return registry
        files = sorted(directory.rglob("*.yaml")) + sorted(directory.rglob("*.yml"))
        for path in files:
            for entry in _load_yaml_file(path):
                registry.register(entry)
        return registry

    @classmethod
    def load_default(
        cls,
        *,
        workdir: Path | None = None,
        mode: EnforcementMode = EnforcementMode.ENFORCE,
    ) -> CapabilityRegistry:
        """Load capability declarations from the workdir or bundled templates.

        Resolution order:
            1. ``<workdir>/templates/capabilities/`` if present.
            2. Bundled ``_default_templates/capabilities/``.
        """
        if workdir is not None:
            local = workdir / "templates" / "capabilities"
            if local.is_dir():
                return cls.from_directory(local, mode=mode)
        bundled = _BUNDLED_TEMPLATES_DIR / "capabilities"
        return cls.from_directory(bundled, mode=mode)


def _any_untrusted(operand_trust: Sequence[object]) -> bool:
    """Return True when any operand is untrusted or has unresolved provenance.

    ``None`` operands mean provenance could not be resolved and fail closed to
    untrusted. A :class:`~bernstein.core.lineage.provenance.TrustClass` operand
    is untrusted when it is at or below the untrusted threshold. Any other
    object type is treated conservatively as untrusted so a caller cannot widen
    the gate by passing an unexpected value.
    """
    from bernstein.core.lineage.provenance import TrustClass, is_untrusted

    for operand in operand_trust:
        if operand is None:
            return True
        if isinstance(operand, TrustClass):
            if is_untrusted(operand):
                return True
            continue
        # Unknown operand shape -> fail closed.
        return True
    return False


def _coerce_capabilities(values: Iterable[object]) -> frozenset[Capability]:
    """Coerce raw YAML strings into a :class:`Capability` frozenset.

    Unknown tokens are dropped with a warning rather than crashing the
    registry loader; the surrounding default-deny semantics still keep
    the path safe.
    """
    out: set[Capability] = set()
    for raw in values:
        token = str(raw).strip().lower()
        try:
            out.add(Capability(token))
        except ValueError:
            logger.warning("Unknown capability token %r - ignoring", token)
    return frozenset(out)


def _load_yaml_file(path: Path) -> list[ToolCapabilities]:
    """Parse a capabilities YAML file into :class:`ToolCapabilities` rows."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        logger.warning("Failed to read capabilities file %s: %s", path, exc)
        return []
    except yaml.YAMLError as exc:
        logger.warning("Failed to parse capabilities file %s: %s", path, exc)
        return []
    if not isinstance(raw, dict):
        return []

    items = cast("dict[str, object]", raw).get("tools", [])
    if not isinstance(items, list):
        return []

    out: list[ToolCapabilities] = []
    for item in cast("list[object]", items):
        if not isinstance(item, dict):
            continue
        entry = cast("dict[str, object]", item)
        name_raw = entry.get("name")
        if not isinstance(name_raw, str) or not name_raw.strip():
            continue
        caps_raw = entry.get("capabilities", [])
        if not isinstance(caps_raw, list):
            continue
        source_raw = entry.get("source", "declared")
        source: CapabilitySource = "declared"
        if source_raw in ("declared", "inferred", "default"):
            source = cast(CapabilitySource, source_raw)
        out.append(
            ToolCapabilities(
                tool_name=name_raw.strip(),
                capabilities=_coerce_capabilities(cast("list[object]", caps_raw)),
                source=source,
            )
        )
    return out


def find_violating_chains(
    registry: CapabilityRegistry,
    chains: Sequence[Sequence[str]],
) -> list[ChainDecision]:
    """Return decisions for chains that trigger the lethal trifecta.

    Helper used by the ``bernstein audit capabilities`` CLI to scan agent
    configs.  Only chains where the full trifecta is reached are returned.
    """
    out: list[ChainDecision] = []
    for chain in chains:
        decision = registry.evaluate_chain(chain)
        if decision.triggered >= _ALL_CAPABILITIES:
            out.append(decision)
    return out


class LethalTrifectaError(PermissionError):
    """Raised when a spawn would chain the full lethal trifecta."""

    def __init__(self, decision: ChainDecision) -> None:
        super().__init__(decision.reason)
        self.decision = decision


def record_spawn_capabilities(
    workdir: Path,
    agent_id: str,
    role: str,
    tools: Sequence[str],
    *,
    registry: CapabilityRegistry | None = None,
) -> ChainDecision:
    """Record the capability set for a spawned agent and enforce the rule.

    Persists a small JSON manifest under
    ``.sdd/runtime/spawn_capabilities/<agent_id>.json`` so the
    ``bernstein audit capabilities`` CLI can replay spawns and so the
    HMAC audit chain can attest to the structural decision.

    Args:
        workdir: Project root.  The capability registry is loaded from
            ``<workdir>/templates/capabilities/`` if present.
        agent_id: Stable identifier for the spawn record.
        role: Agent role name (recorded for inspection).
        tools: Tool chain the agent will be permitted to invoke.
        registry: Optional pre-loaded registry (tests / repeated spawns).

    Returns:
        The :class:`ChainDecision` produced by the registry.

    Raises:
        LethalTrifectaError: When enforcement is on and the chain trips
            all three capabilities.
    """
    import json
    from datetime import UTC, datetime

    decision = (registry if registry is not None else CapabilityRegistry.load_default(workdir=workdir)).evaluate_chain(
        tools
    )

    runtime_dir = workdir / ".sdd" / "runtime" / "spawn_capabilities"
    try:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "agent_id": agent_id,
            "role": role,
            "timestamp": datetime.now(UTC).isoformat(),
            "tools": list(tools),
            "triggered": sorted(c.value for c in decision.triggered),
            "allowed": decision.allowed,
            "reason": decision.reason,
            "mode": decision.mode.value,
            "unknown_tools": list(decision.unknown_tools),
            "offending_tools": list(decision.offending_tools),
        }
        (runtime_dir / f"{agent_id}.json").write_text(
            json.dumps(manifest, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("Could not persist capability manifest for %s: %s", agent_id, exc)

    if not decision.allowed:
        raise LethalTrifectaError(decision)
    return decision
