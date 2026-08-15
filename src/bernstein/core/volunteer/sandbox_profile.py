"""The containment boundary a volunteer task runs behind.

Volunteer tasks are hostile input by default.  The issue text comes from a
repository the donor does not control, the patch is written by a model reading
that text, and the whole thing executes on a stranger's laptop.  This module
is the boundary that makes the rest of the program safe to run.

Why the profile is derived and not chosen
-----------------------------------------

"The sandbox was hardened" is not a checkable statement.  Every containment
claim in this program has to survive a maintainer asking *prove it*, months
later, from a receipt and a git commit.

So the profile is a pure function of the project's declared manifest plus the
donor's own limits, and it is content-addressed.  :attr:`VolunteerSandboxProfile.digest`
is the value a result receipt carries as its sandbox profile identifier, and it
binds :attr:`VolunteerSandboxProfile.manifest_sha256` -- the digest of the
policy it was derived from.  That gives a verifier a chain with no operator
testimony in it:

    manifest bytes -> manifest digest -> profile digest -> receipt bundle

Recompute the manifest digest from the repository at the commit the submission
names, rebuild the profile from it, and compare.  A profile that does not
reproduce means the run was not contained the way the receipt says it was, and
that is a refusal rather than a conversation.

The consequence worth stating plainly: the donor cannot loosen containment and
still produce a verifying receipt.  There is no flag for it, because a flag
would have to appear in the digest, and a digest that says "egress was open"
is not a digest anybody accepts.

What the boundary actually is
-----------------------------

*Backend.*  microVM where the host has one; the container backend as fallback.
A plain container -- no user-space kernel -- is refused unless the project
manifest and the donor both accept it, and the acceptance is recorded in the
digest so it cannot be quiet.

*Egress.*  Deny-all, plus the manifest's ``egress_allowlist``, plus the package
registries the gates need to resolve dependencies.  An empty effective list
means the network is off, not "off by convention".

*Credentials.*  Nothing from the host environment reaches the task sandbox
except an explicit allowlist of non-secret variables.  Adapter credentials
belong to the adapter process, which runs outside this boundary; they are
never part of the sandbox environment.  :func:`sandbox_env` builds that
environment from the profile alone and never reads ``os.environ``, which is
what makes the canary test a test of the seam rather than of a filter.

*Resources.*  CPU, memory and wall clock, floored to the tighter of what the
project asks and what the donor allows.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

    from bernstein.core.volunteer.manifest import VolunteerManifest

#: Name the profile registers under, and the value a receipt records.
VOLUNTEER_PROFILE_NAME = "volunteer"

#: Backends acceptable for volunteer work, strongest first.
#:
#: ``container-userns`` is the container backend running on a user-space
#: kernel; ``container`` is a plain one sharing the host kernel, and it is the
#: only entry that needs a second opinion before it may be used.
BACKEND_PREFERENCE: tuple[str, ...] = ("microvm", "container-userns", "container")

#: Backends that contain a kernel exploit as well as a process escape.
STRONG_BACKENDS = frozenset({"microvm", "container-userns"})

#: Package registries a gate command needs to resolve dependencies.
#:
#: These are not a convenience.  Without them almost every real project's gates
#: fail to install anything and the program is unusable, so the choice is
#: between naming the set explicitly here -- where it is reviewable and hashed
#: into every profile -- and having each project paste its own list into a
#: manifest nobody audits.
PACKAGE_REGISTRY_HOSTS: tuple[str, ...] = (
    "crates.io",
    "files.pythonhosted.org",
    "index.crates.io",
    "proxy.golang.org",
    "pypi.org",
    "registry.npmjs.org",
    "repo.maven.apache.org",
    "rubygems.org",
    "static.crates.io",
    "sum.golang.org",
)

#: Environment variables a task sandbox may see.
#:
#: Deliberately tiny and deliberately not derived from
#: :mod:`bernstein.adapters.env_isolation`: that allowlist is built for adapter
#: processes, which legitimately carry provider credentials.  This one is for
#: the process running a stranger's code, and it has no reason to carry
#: anything a credential could hide behind.  ``PATH`` and the locale are the
#: whole of it, plus a marker so a gate script can tell where it is.
SANDBOX_ENV_ALLOWLIST: tuple[str, ...] = (
    "BERNSTEIN_VOLUNTEER",
    "CI",
    "HOME",
    "LANG",
    "LC_ALL",
    "PATH",
    "TMPDIR",
)

_DEFAULT_MEMORY_MB = 2048
_DEFAULT_CPU_QUOTA_US = 200_000


class SandboxProfileRefusal(RuntimeError):
    """No profile can be built that satisfies both sides.

    A refusal is a normal outcome, not an error condition: it means the donor's
    machine cannot offer what the project requires, or the project asked for
    containment weaker than either party accepts.  Carries ``reason`` as a
    stable machine-readable code so a runner can record it without parsing
    prose.
    """

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class VolunteerSandboxProfile:
    """The containment decision for one volunteer task, as a value.

    Every field is part of the digest.  A field that could vary without moving
    the digest would be a containment knob a donor could turn while still
    producing a receipt that verifies.

    Attributes:
        manifest_sha256: Digest of the manifest this profile was derived from.
        backend: Sandbox backend, one of :data:`BACKEND_PREFERENCE`.
        egress_allowlist: The complete set of hosts reachable from inside,
            sorted and deduplicated.  Empty means the network is off.
        env_allowlist: Environment variable names the sandbox may see.
        memory_mb: Memory ceiling.
        cpu_quota_us: CFS quota against a 100 ms period.
        wall_clock_seconds: Hard ceiling on the task; exceeding it is a kill.
        plain_container_accepted: Whether both sides opted in to a sandbox
            without a user-space kernel.  Recorded even when false, so the
            digest distinguishes "was not needed" from "was not asked".
    """

    manifest_sha256: str
    backend: str
    egress_allowlist: tuple[str, ...]
    env_allowlist: tuple[str, ...]
    memory_mb: int
    cpu_quota_us: int
    wall_clock_seconds: int
    plain_container_accepted: bool

    @property
    def network_enabled(self) -> bool:
        """Whether any host is reachable at all."""
        return bool(self.egress_allowlist)

    @property
    def name(self) -> str:
        return VOLUNTEER_PROFILE_NAME

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "cpu_quota_us": self.cpu_quota_us,
            "egress_allowlist": list(self.egress_allowlist),
            "env_allowlist": list(self.env_allowlist),
            "manifest_sha256": self.manifest_sha256,
            "memory_mb": self.memory_mb,
            "plain_container_accepted": self.plain_container_accepted,
            "profile": VOLUNTEER_PROFILE_NAME,
            "wall_clock_seconds": self.wall_clock_seconds,
        }

    @property
    def digest(self) -> str:
        """Content address of the containment decision, 64 hex characters."""
        payload = json.dumps(
            self.to_canonical_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def build_volunteer_profile(
    manifest: VolunteerManifest,
    *,
    available_backends: Collection[str],
    donor_accepts_plain_container: bool = False,
    donor_wall_clock_minutes: int | None = None,
    donor_memory_mb: int | None = None,
) -> VolunteerSandboxProfile:
    """Derive the containment boundary from the project's policy and the donor's.

    Pure: same manifest and same donor limits give the same profile and so the
    same digest, on any machine, at any time.  That is what lets a maintainer
    rebuild the profile from the repository months later and compare.

    Args:
        manifest: The project's declared policy.
        available_backends: Backends this host can actually provide.
        donor_accepts_plain_container: Whether the donor consented to running
            without a user-space kernel.  Half of the dual opt-in.
        donor_wall_clock_minutes: The donor's own ceiling.  The effective limit
            is the tighter of this and the manifest's.
        donor_memory_mb: The donor's memory ceiling, same rule.

    Raises:
        SandboxProfileRefusal: The host offers nothing the project accepts, or
            a plain container is the only option and both sides did not agree
            to it.
    """
    backend = _select_backend(
        manifest_minimum=manifest.sandbox,
        available_backends=available_backends,
        donor_accepts_plain_container=donor_accepts_plain_container,
    )

    wall_clock_minutes = manifest.max_wall_clock_minutes
    if donor_wall_clock_minutes is not None:
        wall_clock_minutes = min(wall_clock_minutes, donor_wall_clock_minutes)
    if wall_clock_minutes < 1:
        raise SandboxProfileRefusal(
            "wall_clock_below_floor",
            f"effective wall clock is {wall_clock_minutes} minutes; a task needs at least 1",
        )

    memory_mb = _DEFAULT_MEMORY_MB if donor_memory_mb is None else min(_DEFAULT_MEMORY_MB, donor_memory_mb)
    if memory_mb < 256:
        raise SandboxProfileRefusal(
            "memory_below_floor",
            f"effective memory ceiling is {memory_mb} MB; a task needs at least 256",
        )

    return VolunteerSandboxProfile(
        manifest_sha256=manifest.digest,
        backend=backend,
        egress_allowlist=effective_egress(manifest),
        env_allowlist=SANDBOX_ENV_ALLOWLIST,
        memory_mb=memory_mb,
        cpu_quota_us=_DEFAULT_CPU_QUOTA_US,
        wall_clock_seconds=wall_clock_minutes * 60,
        plain_container_accepted=donor_accepts_plain_container,
    )


def effective_egress(manifest: VolunteerManifest) -> tuple[str, ...]:
    """Every host the sandbox may reach, sorted and deduplicated.

    A project that lists no hosts still gets the package registries, because
    otherwise its gates cannot install anything and the profile is a costume.
    A project that wants a genuinely airgapped run vendors its dependencies and
    the registries go unused -- unreachable-but-listed is not a weakening,
    since nothing in the sandbox is obliged to connect.
    """
    return tuple(sorted({*manifest.egress_allowlist, *PACKAGE_REGISTRY_HOSTS}))


def sandbox_env(profile: VolunteerSandboxProfile, *, path: str = "/usr/local/bin:/usr/bin:/bin") -> dict[str, str]:
    """Build the task sandbox's environment from the profile alone.

    Never reads ``os.environ``.  A filter over the host environment is only as
    good as its denylist, and the thing on the other side of this boundary is
    reading a stranger's issue text: an allowlist that starts from nothing has
    no denylist to be wrong about.

    Adapter credentials are absent by construction, not by exclusion -- they
    live in the adapter process, which runs outside this sandbox.
    """
    values = {
        "BERNSTEIN_VOLUNTEER": "1",
        "CI": "1",
        "HOME": "/workspace",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": path,
        "TMPDIR": "/tmp",
    }
    return {key: values[key] for key in profile.env_allowlist if key in values}


def backend_options(
    profile: VolunteerSandboxProfile,
    *,
    resolved_hosts: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Translate the profile into options the sandbox backends understand.

    Kept next to the profile rather than inside a backend so the mapping from
    "what was decided" to "what was configured" is one function a reviewer can
    read, instead of a behaviour spread across four backends.

    How the egress list is actually enforced, and how far that goes
    ---------------------------------------------------------------

    Naming hosts in a list enforces nothing by itself, and a field that looks
    like a control but is not one is worse than no field: it reads as a
    boundary in a review and is absent at runtime.  Two mechanisms carry it:

    *No reachable host at all* becomes ``network_disabled``.  That one is
    complete -- the container gets no interface.

    *Some reachable hosts* become a pinned resolver: the allowlisted names are
    resolved on the host side and passed as static host entries, and the
    sandbox is given no DNS server.  Names outside the list do not resolve, so
    ordinary client code cannot reach them.

    The limit, stated rather than implied: a connection to a raw IP address
    does not consult DNS, so DNS pinning does not stop it.  Closing that needs
    packet-level filtering -- the microVM backend's own network policy, or a
    filtering proxy the sandbox is forced through -- and that is a separate
    change against the backends rather than against this decision layer.  Until
    it lands, a project whose threat model includes exfiltration to a
    hard-coded address should declare an empty ``egress_allowlist`` and vendor
    its dependencies, which turns the network off completely.

    Args:
        profile: The containment decision.
        resolved_hosts: Allowlisted hostname to IP address, resolved by the
            caller.  Resolution is the caller's job because it is I/O and this
            module is pure -- a profile has to rebuild identically on a machine
            with different DNS answers, or the digest would depend on the
            weather.
    """
    options: dict[str, Any] = {
        "memory_mb": profile.memory_mb,
        "cpu_quota": profile.cpu_quota_us,
        "network_disabled": not profile.network_enabled,
        "egress_allowlist": list(profile.egress_allowlist),
        "timeout_seconds": profile.wall_clock_seconds,
        "mount_home": False,
        "inherit_host_env": False,
    }
    if not profile.network_enabled:
        return options

    allowed = set(profile.egress_allowlist)
    entries = {host: address for host, address in (resolved_hosts or {}).items() if host in allowed}
    options["extra_hosts"] = dict(sorted(entries.items()))
    # An unroutable resolver rather than none: a container with no DNS
    # configuration inherits the host's, which would resolve everything.
    options["dns"] = ["0.0.0.0"]
    return options


def _select_backend(
    *,
    manifest_minimum: str,
    available_backends: Collection[str],
    donor_accepts_plain_container: bool,
) -> str:
    available = set(available_backends)

    for candidate in BACKEND_PREFERENCE:
        if candidate not in available:
            continue
        if candidate == "microvm":
            return candidate
        if manifest_minimum == "microvm":
            # The project demanded a microVM and the host has not got one.
            # Everything weaker is out regardless of what the donor accepts.
            break
        if candidate in STRONG_BACKENDS:
            return candidate
        if donor_accepts_plain_container:
            return candidate
        raise SandboxProfileRefusal(
            "plain_container_needs_dual_opt_in",
            "the only available sandbox is a plain container, which shares the host kernel; "
            "the project accepts it but this donor has not, so nothing runs",
        )

    if manifest_minimum == "microvm":
        raise SandboxProfileRefusal(
            "microvm_required_but_unavailable",
            f"the project requires a microVM sandbox; this host offers {sorted(available) or 'nothing'}",
        )
    raise SandboxProfileRefusal(
        "no_acceptable_backend",
        f"no sandbox backend on this host is acceptable for volunteer work; offered {sorted(available) or 'nothing'}",
    )


def profile_matches(profile: VolunteerSandboxProfile, *, expected_digest: str) -> bool:
    """Whether a rebuilt profile is the one a receipt claims.

    The verification side of the chain: a maintainer rebuilds the profile from
    the manifest at the submitted commit and compares digests.
    """
    return profile.digest == expected_digest


def describe_refusal(error: SandboxProfileRefusal, *, manifest_sha256: str) -> Mapping[str, str]:
    """A refusal as a record, so a runner can persist it without prose parsing.

    Refusals are the common case on a heterogeneous donor fleet.  They deserve
    the same structure as a success, or they end up as log lines nobody counts.
    """
    return {
        "outcome": "refused",
        "reason": error.reason,
        "detail": str(error),
        "manifest_sha256": manifest_sha256,
        "profile": VOLUNTEER_PROFILE_NAME,
    }
