"""Operator declaration of the isolation the host already provides (#5341).

Some CLI agents ship a sandbox of their own. ``codex exec --sandbox
workspace-write`` is one: on Linux it is implemented with bubblewrap, which
needs an unprivileged user namespace to start. An operator running Bernstein
inside a container or VM they control has typically removed exactly that --
``--cap-drop ALL``, ``no-new-privileges``, unprivileged user namespaces off --
so the vendor sandbox cannot initialise and every model-issued shell command
is refused while the run still exits 0 (see ``detect_sandbox_failure`` in the
codex adapter).

The blunt way to fix that is the escalated dangerous-mode strategy, which
means "no permission surface exists to skip" and says nothing about why that
is safe. This module is the narrow alternative: the operator states which tier
of isolation the host applies and what the evidence for it is, and an adapter
that owns a vendor sandbox drops that sandbox only when the declared tier
actually replaces it.

The declaration is ordinary layered config -- two flat keys resolved through
:func:`~bernstein.core.config.home.resolve_config`, so ``bernstein config
get`` / ``set`` / ``list`` already operate on it and the precedence chain is
the documented one (environment > project ``.sdd/config.yaml`` > user
``~/.bernstein/config.yaml`` > built-in default). What this module adds is the
closed vocabulary: the allowed values are derived from
:class:`~bernstein.adapters.capability_profile.SandboxTier` rather than
restated, so a tier added to the enum cannot be missing here, and a value
outside it is a hard :exc:`ValueError` rather than a silently ignored typo
that would leave the operator believing a sandbox was dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from bernstein.adapters.capability_profile import SandboxTier
from bernstein.core.config.home import BernsteinHome, ConfigSource, resolve_config

#: Config key naming the isolation tier the host already applies.
HOST_ISOLATION_TIER_KEY = "host_isolation_tier"

#: Config key carrying the operator's free-text evidence for that isolation.
HOST_ISOLATION_EVIDENCE_KEY = "host_isolation_evidence"


def allowed_tier_values() -> tuple[str, ...]:
    """Return the accepted tier names, weakest first.

    Derived from :class:`SandboxTier` rather than restated, so the vocabulary
    cannot drift from the enum the rest of the sandbox layer ranks against.
    """
    return tuple(tier.value for tier in SandboxTier)


@dataclass(frozen=True)
class HostIsolationDeclaration:
    """What the operator declared about the host, and where they declared it.

    Attributes:
        tier: Isolation the host applies to this process.
        evidence: The operator's description of that isolation. Free text --
            it is recorded verbatim into the audit chain so a later reader can
            judge the claim, not parsed.
        source: The config layer the *tier* was resolved from, as
            :func:`resolve_config` reports it.
    """

    tier: SandboxTier
    evidence: str
    source: ConfigSource


def _parse_tier(raw: object) -> SandboxTier:
    """Coerce a resolved config value into a :class:`SandboxTier`.

    ``None`` maps to :attr:`SandboxTier.NONE`: the session layer coerces the
    literal strings ``none`` and ``null`` to ``None`` before this sees them
    (see ``_coerce_config_value``), and both spellings mean the same thing here
    -- no declared isolation, so the vendor sandbox stays. Everything else is
    matched against the closed vocabulary and raises when it does not fit.

    Raises:
        ValueError: *raw* is not one of :func:`allowed_tier_values`.
    """
    if raw is None:
        return SandboxTier.NONE
    text = str(raw).strip().lower()
    if not text:
        return SandboxTier.NONE
    try:
        return SandboxTier(text)
    except ValueError:
        allowed = ", ".join(allowed_tier_values())
        raise ValueError(
            f"{HOST_ISOLATION_TIER_KEY}: {raw!r} is not a declared isolation tier. Allowed values: {allowed}."
        ) from None


def resolve_host_isolation(
    project_dir: Path | None = None,
    home: BernsteinHome | None = None,
) -> HostIsolationDeclaration:
    """Resolve the host-isolation declaration for *project_dir*.

    Args:
        project_dir: Project root whose ``.sdd/config.yaml`` participates in
            the chain. Defaults to the current working directory.
        home: Global config home. Defaults to ``~/.bernstein``.

    Returns:
        The declared :class:`HostIsolationDeclaration`. An undeclared host
        resolves to :attr:`SandboxTier.NONE` with empty evidence, which is the
        posture that keeps every vendor sandbox in place.

    Raises:
        ValueError: The declared tier is outside :func:`allowed_tier_values`.
            Guessing is not an option: the value decides whether a sandbox is
            dropped, so a typo has to be visible rather than absorbed.
    """
    resolved_home = home if home is not None else BernsteinHome.default()
    resolved_project = project_dir if project_dir is not None else Path.cwd()

    tier_resolution = resolve_config(
        HOST_ISOLATION_TIER_KEY,
        home=resolved_home,
        project_dir=resolved_project,
    )
    evidence_resolution = resolve_config(
        HOST_ISOLATION_EVIDENCE_KEY,
        home=resolved_home,
        project_dir=resolved_project,
    )
    raw_evidence = evidence_resolution["value"]
    return HostIsolationDeclaration(
        tier=_parse_tier(tier_resolution["value"]),
        evidence="" if raw_evidence is None else str(raw_evidence),
        source=tier_resolution["source"],
    )
