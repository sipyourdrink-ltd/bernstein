"""Declarative adapter capability profiles and the profile factory.

The CLI-agent ecosystem turns over faster than a hand-written adapter
module per agent can absorb. Every upstream rename, flag reshuffle or
project handover currently lands as a source edit in a bespoke module,
so the cost of tracking the ecosystem scales with the size of the
catalogue rather than with the size of the change.

This module makes an adapter a *declaration* instead. A
:class:`AdapterCapabilityProfile` states what an agent supports (MCP,
native subagents, sandbox tier, local models, vision, computer use,
parallel workers, AGENTS.md, resume semantics) and how it is invoked;
:func:`build_adapter_class_from_profile` turns that declaration into a
working :class:`~bernstein.adapters.base.CLIAdapter` subclass. Adding an
agent becomes a data edit plus a contract YAML, and the generic CLI
fallback still covers agents nobody has profiled yet.

Two things keep the declaration honest rather than merely convenient:

* **Content addressing.** :attr:`AdapterCapabilityProfile.profile_hash`
  is a SHA-256 over the profile's canonical form. The hash an adapter
  presents at dispatch is recorded alongside the run, so a declaration
  that changes between two runs shows up as a hash divergence with a
  named cause instead of as unexplained behaviour drift.
* **Contract cross-checking.**
  :func:`profile_contract_discrepancies` compares a profile's declared
  invocation surface against the pinned contract YAML the conformance
  suite already enforces, so a profile cannot claim a flag or
  subcommand the contract does not back.

Profiles come in two shapes, distinguished by
:class:`ProfileImplementation`:

``FACTORY``
    No hand-written module exists; the adapter is built from the
    profile. This is the path a newly-tracked agent takes.
``MODULE``
    A hand-written adapter module owns the spawn path; the profile is
    the machine-readable declaration of what that module supports.
    Migration to the factory is therefore incremental and opt-in - an
    existing adapter gains a profile without changing how it spawns.

Import note: this module deliberately depends only on
:mod:`bernstein.adapters.base`, :mod:`bernstein.adapters._contract` and
:mod:`bernstein.adapters.env_isolation`. It imports no sibling adapter,
so the ``adapters-independent`` import-linter contract holds.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from bernstein.adapters._contract import (
    AdapterStrategy,
    ContractSpec,
    DangerousModeStrategy,
    EventChannel,
    ResumeStrategy,
)
from bernstein.adapters.base import (
    DEFAULT_TIMEOUT_SECONDS,
    CLIAdapter,
    SpawnResult,
    build_worker_cmd,
)
from bernstein.adapters.env_isolation import build_filtered_env

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from pathlib import Path

    from bernstein.core.models import ModelConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Typed errors
# ---------------------------------------------------------------------------


class ProfileError(ValueError):
    """Base class for every capability-profile failure.

    Subclasses ``ValueError`` so existing callers that guard adapter
    construction with ``except ValueError`` keep working unchanged.
    """


class ProfileValidationError(ProfileError):
    """A profile is underspecified, self-contradictory, or unbuildable.

    Raised at construction time (so an invalid profile cannot exist) and
    by the factory when asked to build an adapter from a profile whose
    implementation is owned by a hand-written module.
    """


class UnknownProfileError(ProfileError, KeyError):
    """No capability profile is registered under the requested name.

    Also a :class:`KeyError` so ``PROFILES``-style lookup habits behave
    the way callers expect.
    """

    def __str__(self) -> str:
        # KeyError.__str__ reprs its argument, which double-quotes the
        # message. Fall back to the plain text so error output reads well.
        return self.args[0] if self.args else ""


class CapabilityMismatchError(ProfileError):
    """No candidate profile satisfies the task's declared requirements.

    Carries the content-addressed :attr:`receipt` describing exactly
    which requirements went unmet and which candidates were considered,
    so a routing refusal is auditable rather than a silent fallback.
    """

    def __init__(self, message: str, receipt: CapabilityRefusalReceipt) -> None:
        super().__init__(message)
        self.receipt = receipt


# ---------------------------------------------------------------------------
# Capability vocabulary
# ---------------------------------------------------------------------------


class SandboxTier(StrEnum):
    """Isolation an agent can apply to the commands it runs.

    Ordered weakest to strongest by :func:`sandbox_rank`; a task that
    requires a tier is satisfied by any adapter declaring that tier or
    stronger.
    """

    #: No isolation; the agent runs commands directly on the host.
    NONE = "none"
    #: Process-level confinement (seccomp, landlock, restricted user).
    PROCESS = "process"
    #: Container-level confinement (Docker, Podman, or equivalent).
    CONTAINER = "container"
    #: Full virtual machine or microVM confinement.
    VM = "vm"


_SANDBOX_ORDER: dict[SandboxTier, int] = {
    SandboxTier.NONE: 0,
    SandboxTier.PROCESS: 1,
    SandboxTier.CONTAINER: 2,
    SandboxTier.VM: 3,
}


def sandbox_rank(tier: SandboxTier) -> int:
    """Return the comparable strength rank of a sandbox tier."""
    return _SANDBOX_ORDER[tier]


class ProfileImplementation(StrEnum):
    """Who owns the spawn path for a profiled adapter."""

    #: The adapter is constructed from the profile by the factory.
    FACTORY = "factory"
    #: A hand-written adapter module owns spawn; the profile declares
    #: its capability surface without changing how it runs.
    MODULE = "module"


# ---------------------------------------------------------------------------
# Invocation spec
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InvocationSpec:
    """How an agent's CLI is always invoked.

    This describes the *always-passed* surface - the tokens every spawn
    emits - which is the same surface the contract YAML pins. Optional
    flags an adapter passes conditionally are deliberately out of scope:
    they are not part of the pinned contract and would make the
    profile-to-contract cross-check produce false drift.

    Args:
        binary: Executable name resolved on ``PATH``.
        subcommands: Subcommand tokens emitted after the binary, in order.
        model_flag: Flag carrying the model id, or ``None`` when the CLI
            takes no model selection (server-side selection, or the
            module supplies it conditionally).
        prompt_flag: Flag carrying the prompt, or ``None`` when the
            prompt is positional.
        prompt_positional: Whether the prompt is appended as a trailing
            positional argument. Ignored when ``prompt_flag`` is set.
        extra_args: Fixed tokens emitted on every invocation, in order
            (for example a non-interactive or auto-approve flag).
        env_passthrough: Environment variables the agent needs forwarded
            through the isolated environment (API keys, config paths).
    """

    binary: str
    subcommands: tuple[str, ...] = ()
    model_flag: str | None = None
    prompt_flag: str | None = None
    prompt_positional: bool = True
    extra_args: tuple[str, ...] = ()
    env_passthrough: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Refuse an invocation spec that could not produce a real command.

        Raises:
            ProfileValidationError: The binary is blank, a declared flag
                is empty, or the prompt has no delivery mechanism.
        """
        if not self.binary.strip():
            raise ProfileValidationError("InvocationSpec requires a non-empty binary name")
        if self.model_flag is not None and not self.model_flag.strip():
            raise ProfileValidationError("InvocationSpec model_flag must be a real flag or None, not an empty string")
        if self.prompt_flag is not None and not self.prompt_flag.strip():
            raise ProfileValidationError("InvocationSpec prompt_flag must be a real flag or None, not an empty string")
        if self.prompt_flag is None and not self.prompt_positional:
            raise ProfileValidationError(
                f"InvocationSpec for {self.binary!r} declares no way to deliver the prompt: "
                "set prompt_flag, or leave prompt_positional enabled"
            )

    def declared_flags(self) -> tuple[str, ...]:
        """Return every flag token this spec always emits, in sorted order.

        Only dash-prefixed tokens count: values that follow a flag (for
        example the ``json`` in ``--format json``) are not flags and are
        not pinned by the contract's flag list.
        """
        flags: set[str] = set()
        if self.model_flag:
            flags.add(self.model_flag)
        if self.prompt_flag:
            flags.add(self.prompt_flag)
        flags.update(token for token in self.extra_args if token.startswith("-"))
        return tuple(sorted(flags))

    def build_argv(self, *, prompt: str, model: str) -> list[str]:
        """Build the full argv for one spawn.

        Token order is fixed - binary, subcommands, model pair, extra
        args, prompt - so two builds from the same profile produce a
        byte-identical command line and replay stays sound.

        Args:
            prompt: The task prompt.
            model: The model id, used only when ``model_flag`` is set.

        Returns:
            The argv list, ready to be wrapped by ``build_worker_cmd``.
        """
        argv = [self.binary, *self.subcommands]
        if self.model_flag:
            argv.extend([self.model_flag, model])
        argv.extend(self.extra_args)
        if self.prompt_flag:
            argv.extend([self.prompt_flag, prompt])
        else:
            argv.append(prompt)
        return argv

    def to_canonical_dict(self) -> dict[str, Any]:
        """Return a sorted, JSON-safe view for content addressing."""
        return {
            "binary": self.binary,
            "env_passthrough": list(self.env_passthrough),
            "extra_args": list(self.extra_args),
            "model_flag": self.model_flag,
            "prompt_flag": self.prompt_flag,
            "prompt_positional": self.prompt_positional,
            "subcommands": list(self.subcommands),
        }


# ---------------------------------------------------------------------------
# Capability profile
# ---------------------------------------------------------------------------


#: Boolean capability axes, in the order they are reported in refusals.
#: Kept as a module constant so the requirement checker, the canonical
#: serialisation and the operator-facing table cannot drift apart.
BOOLEAN_CAPABILITIES: tuple[str, ...] = (
    "agents_md",
    "computer_use",
    "local_models",
    "mcp_client",
    "mcp_server",
    "native_subagents",
    "vision",
)


@dataclass(frozen=True)
class AdapterCapabilityProfile:
    """A machine-readable declaration of what one agent supports.

    Every field is a claim the project is willing to have checked: the
    invocation surface is cross-checked against the pinned contract by
    :func:`profile_contract_discrepancies`, and the strategy axes are
    cross-checked against ``STRATEGY_MATRIX`` by the profile test suite.

    Args:
        name: Registry key, for example ``"pydantic_ai"``.
        display_name: Human-readable name returned by ``CLIAdapter.name``.
        invocation: The always-passed CLI surface.
        implementation: Whether the factory builds this adapter or a
            hand-written module owns its spawn path.
        mcp_client: The agent can consume MCP servers.
        mcp_server: The agent can expose itself as an MCP server.
        native_subagents: The agent can fan work out to its own subagents.
        sandbox: Strongest isolation tier the agent applies.
        local_models: The agent can drive a locally-hosted model.
        vision: The agent accepts image input. Gated against the
            multimodal capability table so a profile cannot grant itself
            attachment access.
        computer_use: The agent can drive a screen, mouse or keyboard.
        max_parallel_workers: Workers the agent can run concurrently
            within one session. ``1`` means strictly sequential.
        agents_md: The agent reads ``AGENTS.md`` project instructions.
        resume: How the agent reattaches to a prior session.
        dangerous_mode: How the agent is told to skip approval prompts.
        event_channel: The surface Bernstein reads for lifecycle signals.
        provides: Provider-name aliases this agent answers to.
        notes: Free-text provenance for the declaration.
    """

    name: str
    display_name: str
    invocation: InvocationSpec
    implementation: ProfileImplementation = ProfileImplementation.MODULE
    mcp_client: bool = False
    mcp_server: bool = False
    native_subagents: bool = False
    sandbox: SandboxTier = SandboxTier.NONE
    local_models: bool = False
    vision: bool = False
    computer_use: bool = False
    max_parallel_workers: int = 1
    agents_md: bool = False
    resume: ResumeStrategy = ResumeStrategy.UNSUPPORTED
    dangerous_mode: DangerousModeStrategy = DangerousModeStrategy.UNSUPPORTED
    event_channel: EventChannel = EventChannel.TEXT_SIGNALS
    provides: tuple[str, ...] = ()
    notes: str = ""

    def __post_init__(self) -> None:
        """Refuse an underspecified profile at construction time.

        Raises:
            ProfileValidationError: A required field is blank or a
                numeric capability is out of range.
        """
        if not self.name.strip():
            raise ProfileValidationError("capability profile requires a non-empty name")
        if not self.display_name.strip():
            raise ProfileValidationError(f"capability profile {self.name!r} requires a non-empty display_name")
        if self.max_parallel_workers < 1:
            raise ProfileValidationError(
                f"capability profile {self.name!r} declares max_parallel_workers="
                f"{self.max_parallel_workers}; it must be at least 1"
            )

    # -- content addressing -------------------------------------------------

    def to_canonical_dict(self) -> dict[str, Any]:
        """Return the canonical, JSON-safe form used for content addressing.

        Keys are sorted and every value is a primitive so the encoding is
        stable across processes and Python versions.
        """
        canonical: dict[str, Any] = {
            "agents_md": self.agents_md,
            "computer_use": self.computer_use,
            "dangerous_mode": str(self.dangerous_mode),
            "display_name": self.display_name,
            "event_channel": str(self.event_channel),
            "implementation": str(self.implementation),
            "invocation": self.invocation.to_canonical_dict(),
            "local_models": self.local_models,
            "max_parallel_workers": self.max_parallel_workers,
            "mcp_client": self.mcp_client,
            "mcp_server": self.mcp_server,
            "name": self.name,
            "native_subagents": self.native_subagents,
            "provides": list(self.provides),
            "resume": str(self.resume),
            "sandbox": str(self.sandbox),
            "vision": self.vision,
        }
        return dict(sorted(canonical.items()))

    def canonical_bytes(self) -> bytes:
        """Return the canonical JSON encoding of this profile."""
        return json.dumps(
            self.to_canonical_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")

    @property
    def profile_hash(self) -> str:
        """SHA-256 over the canonical form.

        Recorded at dispatch so a declaration that changes between two
        runs surfaces as a hash divergence with a named cause rather
        than as unexplained behaviour drift. ``notes`` is excluded from
        the canonical form: prose about the declaration is not part of
        the declaration.
        """
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    # -- derived views ------------------------------------------------------

    def to_strategy(self) -> AdapterStrategy:
        """Return the declared strategy in the shared adapter vocabulary."""
        return AdapterStrategy(
            resume=self.resume,
            dangerous_mode=self.dangerous_mode,
            event_channel=self.event_channel,
        )

    def capability_value(self, axis: str) -> Any:
        """Return the declared value for one capability axis by name."""
        return getattr(self, axis)


# ---------------------------------------------------------------------------
# Capability-aware selection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskCapabilityRequirements:
    """What a task needs from whichever adapter runs it.

    Boolean axes default to ``False`` (not required). ``sandbox``
    defaults to :attr:`SandboxTier.NONE` and ``max_parallel_workers`` to
    ``1``, so an empty requirement set is satisfied by every profile.
    """

    mcp_client: bool = False
    mcp_server: bool = False
    native_subagents: bool = False
    sandbox: SandboxTier = SandboxTier.NONE
    local_models: bool = False
    vision: bool = False
    computer_use: bool = False
    max_parallel_workers: int = 1
    agents_md: bool = False

    def to_canonical_dict(self) -> dict[str, Any]:
        """Return the sorted, JSON-safe form used for refusal receipts."""
        canonical: dict[str, Any] = {
            "agents_md": self.agents_md,
            "computer_use": self.computer_use,
            "local_models": self.local_models,
            "max_parallel_workers": self.max_parallel_workers,
            "mcp_client": self.mcp_client,
            "mcp_server": self.mcp_server,
            "native_subagents": self.native_subagents,
            "sandbox": str(self.sandbox),
            "vision": self.vision,
        }
        return dict(sorted(canonical.items()))


def unmet_requirements(
    profile: AdapterCapabilityProfile,
    requirements: TaskCapabilityRequirements,
) -> tuple[str, ...]:
    """Return the requirement axes ``profile`` fails to satisfy.

    A capability stronger than required still satisfies the requirement:
    a container-sandboxed adapter serves a task that only asks for
    process isolation, and an adapter with sixteen parallel workers
    serves a task that asks for four.

    Args:
        profile: The candidate adapter's declaration.
        requirements: What the task needs.

    Returns:
        Sorted axis names that went unmet; empty when the profile
        satisfies the task.
    """
    unmet: list[str] = [
        axis for axis in BOOLEAN_CAPABILITIES if getattr(requirements, axis) and not profile.capability_value(axis)
    ]
    if sandbox_rank(profile.sandbox) < sandbox_rank(requirements.sandbox):
        unmet.append("sandbox")
    if profile.max_parallel_workers < requirements.max_parallel_workers:
        unmet.append("max_parallel_workers")
    return tuple(sorted(unmet))


@dataclass(frozen=True)
class CapabilityRefusalReceipt:
    """A content-addressed record of a routing refusal.

    Emitted instead of a silent fallback when no candidate adapter can
    serve a task, so the refusal is reconstructable after the fact: the
    receipt names the requirements, every candidate considered with the
    profile hash it presented, and the axes that went unmet.

    Args:
        requirements: Canonical form of the task's requirements.
        candidates: ``(adapter name, profile hash)`` pairs considered,
            in the order they were offered.
        unmet: Sorted union of every unmet axis across candidates.
    """

    requirements: dict[str, Any]
    candidates: tuple[tuple[str, str], ...]
    unmet: tuple[str, ...]

    def to_canonical_dict(self) -> dict[str, Any]:
        """Return the sorted, JSON-safe form used for content addressing."""
        return {
            "candidates": [list(pair) for pair in self.candidates],
            "kind": "adapter-capability-refusal",
            "requirements": dict(sorted(self.requirements.items())),
            "unmet": list(self.unmet),
        }

    @property
    def receipt_hash(self) -> str:
        """SHA-256 over the canonical form.

        Deterministic for a given ``(requirements, candidates, unmet)``
        triple, so two operators reconstructing the same refusal derive
        the same identifier.
        """
        payload = json.dumps(
            self.to_canonical_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def _assert_capability_axes_are_requestable() -> None:
    """Fail at import when an axis exists that no task could ever require.

    :data:`BOOLEAN_CAPABILITIES` drives both the profile side and the
    requirement side of :func:`unmet_requirements`. If an axis were added
    to a profile without a matching field on
    :class:`TaskCapabilityRequirements`, the requirement would become
    silently unenforceable - a task could never ask for it, so the
    routing gate would pass adapters that do not support it. Checking the
    two sides agree at import turns that drift into an immediate, named
    failure instead.

    Raises:
        ProfileValidationError: An axis is missing from one of the sides.
    """
    profile_axes = set(AdapterCapabilityProfile.__dataclass_fields__)
    requirement_axes = set(TaskCapabilityRequirements.__dataclass_fields__)
    for axis in BOOLEAN_CAPABILITIES:
        missing_from = [
            side
            for side, axes in (
                ("AdapterCapabilityProfile", profile_axes),
                ("TaskCapabilityRequirements", requirement_axes),
            )
            if axis not in axes
        ]
        if missing_from:
            raise ProfileValidationError(
                f"capability axis {axis!r} is declared in BOOLEAN_CAPABILITIES but missing from "
                f"{' and '.join(missing_from)}; every axis must exist on both sides or the "
                "requirement is unenforceable"
            )


_assert_capability_axes_are_requestable()


def select_profile_for(
    requirements: TaskCapabilityRequirements,
    *,
    profiles: Iterable[AdapterCapabilityProfile] | None = None,
) -> AdapterCapabilityProfile:
    """Return the first profile satisfying ``requirements``.

    Candidates are considered in the order supplied; when ``profiles``
    is omitted the shipped catalogue is enumerated in sorted name order,
    so selection is deterministic across processes.

    Args:
        requirements: What the task needs.
        profiles: Candidate profiles. Defaults to the shipped catalogue.

    Returns:
        The first satisfying profile.

    Raises:
        CapabilityMismatchError: No candidate satisfies the task. The
            error carries a content-addressed
            :class:`CapabilityRefusalReceipt` naming the unmet axes;
            routing never silently falls back to a weaker adapter.
    """
    candidates = tuple(profiles) if profiles is not None else tuple(profile for _, profile in sorted(PROFILES.items()))

    all_unmet: set[str] = set()
    for profile in candidates:
        unmet = unmet_requirements(profile, requirements)
        if not unmet:
            return profile
        all_unmet.update(unmet)

    receipt = CapabilityRefusalReceipt(
        requirements=requirements.to_canonical_dict(),
        candidates=tuple((profile.name, profile.profile_hash) for profile in candidates),
        unmet=tuple(sorted(all_unmet)),
    )
    considered = ", ".join(profile.name for profile in candidates) or "<none>"
    raise CapabilityMismatchError(
        f"no adapter satisfies the declared task requirements: unmet {', '.join(receipt.unmet) or '<none>'} "
        f"(considered: {considered}; refusal receipt {receipt.receipt_hash})",
        receipt,
    )


# ---------------------------------------------------------------------------
# Profile-to-contract cross-check
# ---------------------------------------------------------------------------


def profile_contract_discrepancies(
    profile: AdapterCapabilityProfile,
    spec: ContractSpec,
) -> tuple[str, ...]:
    """Return the ways ``profile`` claims more surface than ``spec`` backs.

    The pinned contract is authoritative. A profile may declare less
    than the contract requires (a module can pass a required flag the
    profile does not model), but it may never declare a binary,
    subcommand or flag the contract does not carry - that would let an
    adapter advertise a capability nothing verified.

    Args:
        profile: The declaration under test.
        spec: The contract loaded for the same adapter.

    Returns:
        Sorted human-readable discrepancies; empty when the profile is
        fully backed by the contract.
    """
    discrepancies: list[str] = []
    if profile.invocation.binary != spec.binary:
        discrepancies.append(f"profile declares binary {profile.invocation.binary!r} but contract pins {spec.binary!r}")
    required_flags = set(spec.required_flags)
    for flag in profile.invocation.declared_flags():
        if flag not in required_flags:
            discrepancies.append(f"profile declares flag {flag!r} that contract {spec.adapter!r} does not require")
    required_subcommands = set(spec.required_subcommands)
    for sub in profile.invocation.subcommands:
        if sub not in required_subcommands:
            discrepancies.append(f"profile declares subcommand {sub!r} that contract {spec.adapter!r} does not require")
    return tuple(sorted(discrepancies))


def assert_profile_backed_by_contract(profile: AdapterCapabilityProfile) -> None:
    """Raise when a profile's declaration outruns its pinned contract.

    The promotion gate for a profiled adapter: a declaration that claims
    surface the contract does not carry is refused rather than shipped.

    Args:
        profile: The declaration to verify.

    Raises:
        ProfileValidationError: The contract is missing, or the profile
            declares surface the contract does not back.
    """
    try:
        spec = ContractSpec.load(profile.name)
    except FileNotFoundError as exc:
        raise ProfileValidationError(
            f"capability profile {profile.name!r} has no contract on disk; "
            "a profile ships only with the contract that pins its CLI surface"
        ) from exc
    discrepancies = profile_contract_discrepancies(profile, spec)
    if discrepancies:
        raise ProfileValidationError(
            f"capability profile {profile.name!r} declares surface its contract does not back: "
            + "; ".join(discrepancies)
        )


# ---------------------------------------------------------------------------
# The factory
# ---------------------------------------------------------------------------


class ProfileAdapter(CLIAdapter):
    """A CLI adapter whose behaviour is supplied by a capability profile.

    Subclasses are produced by :func:`build_adapter_class_from_profile`,
    which binds the profile as the :attr:`profile` class attribute. The
    spawn path mirrors the hand-written adapters exactly - the same
    worker wrapper, the same isolated environment, the same log layout
    and timeout watchdog - so a profile-built adapter is
    indistinguishable from a hand-written one to the conformance suite,
    the scheduler and replay.
    """

    #: Bound by the factory on each generated subclass.
    profile: AdapterCapabilityProfile

    @property
    def profile_hash(self) -> str:
        """The content address of the declaration this adapter was built from.

        Read at dispatch so the audit trail records which declaration
        was in force, making profile drift detectable on replay.
        """
        return self.profile.profile_hash

    def name(self) -> str:
        """Return the human-readable adapter name."""
        return self.profile.display_name

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        task_scope: str = "medium",
        budget_multiplier: float = 1.0,
        system_addendum: str = "",
        multimodal_context: Any | None = None,
    ) -> SpawnResult:
        """Launch the profiled CLI agent.

        Args:
            prompt: Task description for the agent.
            workdir: Directory to run the agent in.
            model_config: Model selection and effort level.
            session_id: Unique session identifier.
            mcp_config: Optional MCP tool configuration. Ignored unless
                the profile declares ``mcp_client``.
            timeout_seconds: Maximum runtime before the watchdog kills
                the process. ``0`` disables the watchdog.
            task_scope: Task scope hint; unused by profiled adapters.
            budget_multiplier: Scope budget multiplier; unused.
            system_addendum: Extra system prompt text; unused.
            multimodal_context: Optional attachments. Refused unless the
                adapter is registered as multimodal-capable.

        Returns:
            SpawnResult with the spawned PID and log path.

        Raises:
            RuntimeError: The declared binary is missing from ``PATH`` or
                is not executable.
        """
        self.refuse_multimodal_if_needed(multimodal_context)
        self.enforce_network_policy()

        profile = self.profile
        log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        if mcp_config and not profile.mcp_client:
            logger.debug(
                "%s ignoring runtime MCP config for session %s: profile declares no MCP client support",
                profile.name,
                session_id,
            )

        cmd = profile.invocation.build_argv(prompt=prompt, model=model_config.model)
        pid_dir = workdir / ".sdd" / "runtime" / "pids"
        wrapped_cmd = build_worker_cmd(
            cmd,
            role=session_id.rsplit("-", 1)[0],
            session_id=session_id,
            pid_dir=pid_dir,
            workdir=workdir,
            log_path=log_path,
            model=model_config.model,
        )

        env = build_filtered_env(list(profile.invocation.env_passthrough))
        preexec_fn = self._get_preexec_fn()
        binary = profile.invocation.binary
        with log_path.open("w") as log_file:
            try:
                proc = subprocess.Popen(
                    wrapped_cmd,
                    cwd=workdir,
                    env=env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    preexec_fn=preexec_fn,
                )
            except FileNotFoundError as exc:
                raise RuntimeError(f"{binary!r} not found in PATH") from exc
            except PermissionError as exc:
                raise RuntimeError(f"Permission denied executing {binary!r}: {exc}") from exc

        self._probe_fast_exit(proc, log_path, provider_name=profile.name)

        result = SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)
        if timeout_seconds > 0:
            result.timeout_timer = self._start_timeout_watchdog(proc.pid, timeout_seconds, session_id)
        return result


def _class_name_for(profile: AdapterCapabilityProfile) -> str:
    """Return a PEP 8 class name derived from a profile name."""
    camel = "".join(part.capitalize() for part in profile.name.replace("-", "_").split("_") if part)
    return f"{camel or 'Profile'}ProfileAdapter"


def build_adapter_class_from_profile(
    profile: AdapterCapabilityProfile,
) -> type[ProfileAdapter]:
    """Build a ``CLIAdapter`` subclass from a capability profile.

    A class (rather than a shared instance) is returned so the registry
    keeps its existing semantics: every ``get_adapter`` call yields a
    fresh adapter with its own resource limits and rate-limit meter, and
    no state leaks between spawns.

    Args:
        profile: The declaration to build from. Must be a
            :attr:`ProfileImplementation.FACTORY` profile.

    Returns:
        A generated :class:`ProfileAdapter` subclass.

    Raises:
        ProfileValidationError: The profile's spawn path is owned by a
            hand-written module, so building it would shadow that module.
    """
    if profile.implementation is not ProfileImplementation.FACTORY:
        raise ProfileValidationError(
            f"capability profile {profile.name!r} declares implementation="
            f"{ProfileImplementation.MODULE.name}; its spawn path is owned by a hand-written "
            "adapter module and must not be rebuilt by the factory"
        )
    class_name = _class_name_for(profile)
    return type(
        class_name,
        (ProfileAdapter,),
        {
            # ``CLIAdapter`` is an ABC, so the three-argument ``type`` call
            # runs through ``ABCMeta.__new__`` and would otherwise stamp
            # ``__module__`` as ``"abc"`` (the frame that creates the class).
            # Pinning it here keeps generated adapters addressable from this
            # module - the conformance harness derives its ``subprocess.Popen``
            # patch target from ``type(adapter).__module__``, so a wrong value
            # would silently un-mock the spawn under test.
            "__module__": __name__,
            "__qualname__": class_name,
            "__doc__": f"Adapter for {profile.display_name}, built from its capability profile.",
            "profile": profile,
            "registry_name": profile.name,
            "provides": profile.provides,
            "strategy_override": profile.to_strategy(),
        },
    )


def build_adapter_from_profile(profile: AdapterCapabilityProfile) -> ProfileAdapter:
    """Build and instantiate an adapter from a capability profile.

    Args:
        profile: The declaration to build from.

    Returns:
        A live adapter honouring the full ``CLIAdapter`` contract.

    Raises:
        ProfileValidationError: The profile is not factory-built.
    """
    return build_adapter_class_from_profile(profile)()


# ---------------------------------------------------------------------------
# Shipped catalogue
# ---------------------------------------------------------------------------
#
# Adding an agent here (plus a contract YAML and a STRATEGY_MATRIX row)
# is the whole of "support a new agent" for a CLI that fits the
# always-passed-surface shape. Declarations stay conservative: a profile
# claims a capability only when the pinned contract or upstream
# documentation backs it, because the promotion gate refuses a profile
# whose declaration outruns what was verified.

_PROFILE_LIST: tuple[AdapterCapabilityProfile, ...] = (
    # Pydantic AI's clai. No hand-written module: the first agent
    # tracked purely as a declaration.
    #   clai [-m MODEL] [--no-stream] [prompt]
    # --no-stream is always passed: streaming is a REPL affordance and
    # the orchestrator reads a log file, not a TTY.
    AdapterCapabilityProfile(
        name="pydantic_ai",
        display_name="Pydantic AI",
        implementation=ProfileImplementation.FACTORY,
        invocation=InvocationSpec(
            binary="clai",
            model_flag="-m",
            prompt_positional=True,
            extra_args=("--no-stream",),
            env_passthrough=(
                "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY",
                "GOOGLE_API_KEY",
                "GEMINI_API_KEY",
                "GROQ_API_KEY",
                "MISTRAL_API_KEY",
            ),
        ),
        # clai takes "<provider>:<model>", which covers locally-hosted
        # OpenAI-compatible endpoints.
        local_models=True,
        provides=("pydantic_ai", "pydantic-ai", "clai"),
        notes="Surface pinned from `clai --help` (one-shot positional prompt mode).",
    ),
    # Declaration-only profiles. The hand-written modules keep owning
    # their spawn paths; these make their capability surface
    # machine-readable and canary-checkable.
    AdapterCapabilityProfile(
        name="droid",
        display_name="Droid",
        implementation=ProfileImplementation.MODULE,
        invocation=InvocationSpec(binary="droid", prompt_positional=True),
        notes="Factory AI CLI. Prompt is positional; no always-passed flags.",
    ),
    AdapterCapabilityProfile(
        name="kimi",
        display_name="Kimi",
        implementation=ProfileImplementation.MODULE,
        invocation=InvocationSpec(
            binary="kimi",
            prompt_flag="-c",
            extra_args=("--yolo",),
        ),
        dangerous_mode=DangerousModeStrategy.CLI_FLAG,
        notes="Moonshot Kimi CLI. --yolo auto-approves tool calls.",
    ),
    AdapterCapabilityProfile(
        name="opencode",
        display_name="OpenCode",
        implementation=ProfileImplementation.MODULE,
        invocation=InvocationSpec(
            binary="opencode",
            subcommands=("run",),
            model_flag="-m",
            prompt_positional=True,
            extra_args=("--format", "json"),
        ),
        agents_md=True,
        local_models=True,
        notes="Structured JSON output via --format json.",
    ),
    AdapterCapabilityProfile(
        name="goose",
        display_name="Goose",
        implementation=ProfileImplementation.MODULE,
        invocation=InvocationSpec(
            binary="goose",
            subcommands=("run",),
            prompt_flag="--instruction",
        ),
        mcp_client=True,
        event_channel=EventChannel.ACP,
        notes="Block Goose. Speaks ACP natively; model flag is optional and module-supplied.",
    ),
)


#: Shipped capability profiles keyed by registry name.
PROFILES: dict[str, AdapterCapabilityProfile] = {profile.name: profile for profile in _PROFILE_LIST}


def get_profile(name: str) -> AdapterCapabilityProfile:
    """Return the capability profile registered under ``name``.

    Args:
        name: Registry key, for example ``"pydantic_ai"``.

    Returns:
        The registered profile.

    Raises:
        UnknownProfileError: No profile is registered under that name.
            Agents without a profile are served by the generic CLI
            fallback, which is unaffected by this layer.
    """
    try:
        return PROFILES[name]
    except KeyError:
        available = ", ".join(sorted(PROFILES)) or "<none>"
        raise UnknownProfileError(f"no capability profile registered for {name!r}. Available: {available}") from None


def iter_profiles() -> Iterator[tuple[str, AdapterCapabilityProfile]]:
    """Yield every shipped profile as ``(name, profile)``, sorted by name."""
    yield from sorted(PROFILES.items())


#: Adapter classes built from the shipped ``FACTORY`` profiles, keyed by
#: registry name. Built exactly once at import so a generated class has a
#: stable identity for the lifetime of the process, the way a
#: hand-written adapter class does.
_PROFILE_ADAPTER_CLASSES: dict[str, type[ProfileAdapter]] = {
    name: build_adapter_class_from_profile(profile)
    for name, profile in sorted(PROFILES.items())
    if profile.implementation is ProfileImplementation.FACTORY
}


def _bind_generated_classes_to_module() -> None:
    """Expose each generated adapter class as a module attribute.

    Golden-transcript conformance loads an adapter by dotted class path
    (``bernstein.adapters.capability_profile.PydanticAiProfileAdapter``),
    so a class that existed only inside a dict would be unreachable to
    the replay harness - and a profile-built adapter that cannot be
    replayed could not clear the same conformance bar as a hand-written
    one.
    """
    module_globals = globals()
    for generated in _PROFILE_ADAPTER_CLASSES.values():
        module_globals[generated.__name__] = generated


_bind_generated_classes_to_module()


def profile_built_adapter_classes() -> dict[str, type[ProfileAdapter]]:
    """Return the adapter classes the factory builds from shipped profiles.

    Only :attr:`ProfileImplementation.FACTORY` profiles are built;
    declaration-only profiles leave their hand-written module in charge.
    Consumed by :mod:`bernstein.adapters.registry` so profile-built
    agents are ordinary registry citizens - they resolve through
    ``get_adapter``, are enumerated by ``iter_adapter_specs``, and face
    the same conformance suite as a hand-written adapter.

    Returns:
        A copy of the registry-name to generated-class mapping, sorted by
        name. A copy so a caller mutating the result cannot disturb the
        classes the registry already handed out.
    """
    return dict(_PROFILE_ADAPTER_CLASSES)


def profile_binary_for(name: str) -> str | None:
    """Return the CLI binary a profiled adapter invokes, or ``None``.

    Registry keys and binary names diverge often enough that the report
    and CLI surfaces keep a hand-maintained override table. A profile
    already declares its binary, so profiled adapters answer the
    question from their declaration instead of needing a second entry in
    a table that can drift from the adapter it describes.

    Args:
        name: Adapter registry name.

    Returns:
        The declared binary name, or ``None`` when the adapter has no
        profile (callers then fall back to their override table).
    """
    profile = PROFILES.get(name)
    return profile.invocation.binary if profile is not None else None


def profile_hash_for(name: str) -> str | None:
    """Return the profile hash for ``name``, or ``None`` when unprofiled.

    The dispatch path records this alongside a run so replay can detect
    a changed declaration as a hash divergence. Unprofiled agents return
    ``None`` and stay on the generic fallback.

    Args:
        name: Adapter registry name.

    Returns:
        The 64-character hex profile hash, or ``None``.
    """
    profile = PROFILES.get(name)
    return profile.profile_hash if profile is not None else None


__all__ = [
    "BOOLEAN_CAPABILITIES",
    "PROFILES",
    "AdapterCapabilityProfile",
    "CapabilityMismatchError",
    "CapabilityRefusalReceipt",
    "InvocationSpec",
    "ProfileAdapter",
    "ProfileError",
    "ProfileImplementation",
    "ProfileValidationError",
    "SandboxTier",
    "TaskCapabilityRequirements",
    "UnknownProfileError",
    "assert_profile_backed_by_contract",
    "build_adapter_class_from_profile",
    "build_adapter_from_profile",
    "get_profile",
    "iter_profiles",
    "profile_binary_for",
    "profile_built_adapter_classes",
    "profile_contract_discrepancies",
    "profile_hash_for",
    "sandbox_rank",
    "select_profile_for",
    "unmet_requirements",
]
