"""Network egress policy for the air-gap profile.

Wires the global ``--allow-network`` flag from the CLI through the
orchestrator, adapters, and MCP transports. The policy is the single
source of truth for "is this destination allowed to receive a packet
from a Bernstein run?" and is consulted at every site that opens an
outbound connection.

Specs:
- ``--allow-network 127.0.0.1`` (host-only)
- ``--allow-network 10.0.0.0/8`` (CIDR)
- ``--allow-network host:443`` (host:port)
- ``--allow-network none`` (refuse everything; air-gap default)
- ``--allow-network any`` (back-compat default outside ``--profile airgap``)

Subprocesses inherit the policy via ``BERNSTEIN_NETWORK_POLICY``;
``--profile airgap`` is propagated via ``BERNSTEIN_PROFILE_MODE``.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import urlparse

ENV_NETWORK_POLICY: Final[str] = "BERNSTEIN_NETWORK_POLICY"
ENV_PROFILE_MODE: Final[str] = "BERNSTEIN_PROFILE_MODE"
#: Marker set alongside the airgap network posture when ``--profile sovereign``
#: is selected (issue #2518). Sovereign composes the airgap network profile, so
#: it sets ``ENV_PROFILE_MODE=airgap`` for the deny-all + socket-guard posture
#: and additionally sets this marker so the sovereign-specific gates (drift
#: refusal, ``doctor sovereign``) can tell they are active.
#:
#: The two markers are a *pair*, never a standalone flag: sovereign is only
#: active when this marker carries a recognised value **and**
#: ``ENV_PROFILE_MODE`` is ``airgap``. See :func:`is_sovereign_profile`.
ENV_SOVEREIGN_MODE: Final[str] = "BERNSTEIN_SOVEREIGN_MODE"

PROFILE_AIRGAP: Final[str] = "airgap"
PROFILE_SOVEREIGN: Final[str] = "sovereign"

#: The only values ``ENV_SOVEREIGN_MODE`` may carry. Any other non-empty value
#: is a half-set / mistyped marker and is refused rather than read as "off".
SOVEREIGN_MARKER_VALUES: Final[frozenset[str]] = frozenset({"1", "true", PROFILE_SOVEREIGN})


class NetworkPolicyDenied(RuntimeError):
    """Raised when an outbound connection is refused by the active policy.

    Carries the destination so adapters can name it in the failure
    message that surfaces to the operator.
    """

    def __init__(self, destination: str, *, source: str = "") -> None:
        self.destination = destination
        self.source = source
        msg = f"network egress denied by policy: {destination}"
        if source:
            msg = f"{msg} (from {source})"
        super().__init__(msg)


class SovereignMarkerError(RuntimeError):
    """Raised when the sovereign marker pair is half-set or unrecognised.

    Sovereign mode is a pair of markers, not a standalone flag: the sovereign
    marker asserts the residency posture and ``BERNSTEIN_PROFILE_MODE=airgap``
    asserts the network posture the residency posture is built on. A process
    carrying only one of them is in a state nobody intended - either the
    residency gates are armed over an unrestricted network, or the network is
    shut while the drift gate believes it is not sovereign and waves spawns
    through. Neither half is safe to interpret, so we refuse instead of picking
    one, exactly like the airgap-plus-``any`` combination.
    """


class NetworkPolicyConfigError(ValueError):
    """Raised when ``--profile airgap`` is combined with ``--allow-network any``.

    Sovereign-customer compliance teams rely on the airgap profile being a
    hard fail-closed boundary. Allowing ``any`` to silently override the
    deny-all default would let a typo or copy-paste mistake escape the
    boundary without the operator noticing - so we reject the combination
    at parse time and force them to choose one or the other.
    """


@dataclass(frozen=True)
class _HostPort:
    host: str
    port: int | None = None


@dataclass(frozen=True)
class NetworkPolicy:
    """An immutable allow-list of egress destinations.

    A policy with ``allow_any=True`` permits everything (legacy
    default). A policy with ``allow_any=False`` and an empty
    ``rules`` list is the explicit ``none`` mode and denies all.

    Attributes:
        allow_any: When True the policy is a no-op (back-compat).
        rules: Host / CIDR / host:port tokens to permit.
    """

    allow_any: bool = True
    rules: tuple[str, ...] = ()
    _hosts: tuple[_HostPort, ...] = field(default=(), repr=False, compare=False)
    _networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = field(default=(), repr=False, compare=False)

    @classmethod
    def from_specs(cls, specs: tuple[str, ...] | list[str] | None) -> NetworkPolicy:
        """Build a policy from raw CLI tokens.

        ``specs`` is the multi-valued ``--allow-network`` argument.
        ``None`` or an empty tuple yields ``allow_any=True`` for
        back-compat. ``("none",)`` yields the deny-all policy.
        ``("any",)`` is an explicit opt-out.
        """
        if not specs:
            return cls(allow_any=True)
        cleaned = tuple(s.strip() for s in specs if s and s.strip())
        if not cleaned:
            return cls(allow_any=True)
        lowered = {s.lower() for s in cleaned}
        if "any" in lowered:
            return cls(allow_any=True, rules=("any",))
        if lowered == {"none"}:
            return cls(allow_any=False, rules=())
        rules = tuple(s for s in cleaned if s.lower() != "none")
        hosts: list[_HostPort] = []
        networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for token in rules:
            net = _maybe_network(token)
            if net is not None:
                networks.append(net)
                continue
            hosts.append(_parse_host_port(token))
        return cls(
            allow_any=False,
            rules=rules,
            _hosts=tuple(hosts),
            _networks=tuple(networks),
        )

    @classmethod
    def deny_all(cls) -> NetworkPolicy:
        """Return the explicit ``none`` policy."""
        return cls(allow_any=False, rules=())

    @classmethod
    def allow_all(cls) -> NetworkPolicy:
        """Return the legacy unrestricted policy."""
        return cls(allow_any=True, rules=("any",))

    def is_allowed(self, host: str, port: int | None = None) -> bool:
        """Return True iff (host, port) satisfies the policy."""
        if self.allow_any:
            return True
        if not host:
            return False
        host_l = host.lower()
        loopback_aliases = {"localhost", "127.0.0.1", "::1"}
        candidates = {host_l}
        if host_l in loopback_aliases:
            candidates |= loopback_aliases
        try:
            ip = ipaddress.ip_address(host_l if host_l != "localhost" else "127.0.0.1")
        except ValueError:
            ip = None
        for hp in self._hosts:
            if hp.host.lower() in candidates and (hp.port is None or hp.port == port):
                return True
        if ip is not None:
            for net in self._networks:
                if ip in net:
                    return True
        return False

    def check(self, host: str, port: int | None = None, *, source: str = "") -> None:
        """Raise :class:`NetworkPolicyDenied` if the destination is blocked."""
        if not self.is_allowed(host, port):
            dest = f"{host}:{port}" if port is not None else host
            raise NetworkPolicyDenied(dest, source=source)

    def check_url(self, url: str, *, source: str = "") -> None:
        """Parse ``url`` and apply :meth:`check` to its host/port."""
        parsed = urlparse(url)
        host = parsed.hostname or ""
        port = parsed.port
        if port is None and parsed.scheme:
            if parsed.scheme == "https":
                port = 443
            elif parsed.scheme == "http":
                port = 80
        self.check(host, port, source=source or url)

    def to_env_value(self) -> str:
        """Serialise the policy for child processes (comma-joined)."""
        if self.allow_any:
            return "any"
        if not self.rules:
            return "none"
        return ",".join(self.rules)


def _maybe_network(token: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    if "/" not in token:
        return None
    try:
        return ipaddress.ip_network(token, strict=False)
    except ValueError:
        return None


def _parse_host_port(token: str) -> _HostPort:
    # Bracketed IPv6 form: [::1] or [2001:db8::1]:443.
    if token.startswith("["):
        end = token.find("]")
        if end == -1:
            return _HostPort(host=token)
        host = token[1:end]
        rest = token[end + 1 :]
        if rest.startswith(":"):
            try:
                return _HostPort(host=host, port=int(rest[1:]))
            except ValueError:
                return _HostPort(host=token)
        return _HostPort(host=host)
    if ":" in token:
        host, _, port_s = token.rpartition(":")
        try:
            return _HostPort(host=host, port=int(port_s))
        except ValueError:
            return _HostPort(host=token)
    return _HostPort(host=token)


def policy_from_env() -> NetworkPolicy:
    """Reconstruct the active policy from process environment.

    Adapters and MCP transports use this so they don't need a handle
    to the CLI args.

    Under a network-locked profile marker an absent or empty
    ``BERNSTEIN_NETWORK_POLICY`` is treated as deny-all rather than the legacy
    allow-all: a child process whose policy variable was dropped must not
    silently regain unrestricted egress while the profile marker still claims a
    shut network. Outside those profiles the back-compat allow-all default is
    unchanged.
    """
    raw = os.environ.get(ENV_NETWORK_POLICY)
    if not raw or not raw.strip():
        return NetworkPolicy.deny_all() if is_airgap_profile() else NetworkPolicy.allow_all()
    if raw.strip().lower() == "any":
        return NetworkPolicy.allow_all()
    if raw.strip().lower() == "none":
        return NetworkPolicy.deny_all()
    return NetworkPolicy.from_specs(tuple(raw.split(",")))


def is_airgap_profile() -> bool:
    """Return True iff the active run was started with ``--profile airgap``."""
    return os.environ.get(ENV_PROFILE_MODE, "").strip().lower() == PROFILE_AIRGAP


def is_sovereign_profile() -> bool:
    """Return True iff the active run was started with ``--profile sovereign``.

    Sovereign is a *pair* of markers: ``ENV_SOVEREIGN_MODE`` carrying one of
    :data:`SOVEREIGN_MARKER_VALUES` **and** ``ENV_PROFILE_MODE=airgap`` for the
    network posture the residency posture composes. Both present is sovereign;
    neither present is not sovereign (a bare airgap run keeps
    :func:`is_airgap_profile` True and this False).

    Raises:
        SovereignMarkerError: When the pair is half-set - the sovereign marker
            carries an unrecognised value, or it is set while the network
            profile marker is not ``airgap``. Reading a half-set state as
            "off" would let a dropped or mistyped marker disable the drift
            gate, so the ambiguity is refused rather than resolved.
    """
    raw = os.environ.get(ENV_SOVEREIGN_MODE, "").strip().lower()
    if not raw:
        return False
    if raw not in SOVEREIGN_MARKER_VALUES:
        raise SovereignMarkerError(
            f"{ENV_SOVEREIGN_MODE}={raw!r} is not a recognised sovereign marker "
            f"(expected one of {sorted(SOVEREIGN_MARKER_VALUES)}); refusing to interpret it"
        )
    if not is_airgap_profile():
        raise SovereignMarkerError(
            f"{ENV_SOVEREIGN_MODE} is set but {ENV_PROFILE_MODE} is "
            f"{os.environ.get(ENV_PROFILE_MODE, '')!r} instead of {PROFILE_AIRGAP!r}; "
            "the sovereign residency posture requires the airgap network posture it composes"
        )
    return True


def install_policy(policy: NetworkPolicy, *, profile: str | None = None, sovereign: bool = False) -> None:
    """Persist ``policy`` and the profile markers into the process environment.

    Subprocess adapters and MCP transports read these on startup. When
    *sovereign* is True the marker pair is written together so no observer can
    see a half-set state; the caller must pass ``profile=PROFILE_AIRGAP``
    alongside it because sovereign composes the airgap network posture.

    Markers that the new policy does not assert are *cleared*, not left behind.
    Installing a policy is a full statement of the process posture: a second
    install in the same process must not inherit the first one's sovereign or
    airgap marker, which would leave exactly the half-set state
    :func:`is_sovereign_profile` refuses.

    Raises:
        SovereignMarkerError: When *sovereign* is requested without the airgap
            network profile.
    """
    if sovereign and (profile or "").strip().lower() != PROFILE_AIRGAP:
        raise SovereignMarkerError(
            f"sovereign mode requires profile={PROFILE_AIRGAP!r}; refusing to install a half-set marker pair"
        )
    os.environ[ENV_NETWORK_POLICY] = policy.to_env_value()
    if profile:
        os.environ[ENV_PROFILE_MODE] = profile
    else:
        os.environ.pop(ENV_PROFILE_MODE, None)
    if sovereign:
        os.environ[ENV_SOVEREIGN_MODE] = PROFILE_SOVEREIGN
    else:
        os.environ.pop(ENV_SOVEREIGN_MODE, None)
