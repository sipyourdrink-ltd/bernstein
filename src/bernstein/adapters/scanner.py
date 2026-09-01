"""Scanner adapter contract (issue #3617, slice 2 of #2953).

Every external analysis tool (SAST, SCA, secret, IaC, DAST, recon) currently
lives in its own bespoke wrapper.  This module provides the uniform
``ScannerAdapter`` contract those wrappers should implement, mirroring the
existing ``CLIAdapter`` contract in ``base.py``:

* ``name() -> str``          -- stable registry key
* ``scan(target, scope, workdir) -> list[Finding]`` -- run the analysis

Plus a *class-level* capability declaration that drives the conformance suite:

* ``output_format``  -- sarif / json / xml
* ``determinism``    -- deterministic / feed_pinned / transcript_anchored
* ``pinned_inputs``  -- which digests must be recorded for feed_pinned
* ``category``       -- sast / sca / secret / iac / recon / dast

The determinism tier is not documentation: the conformance suite
(see ``scanner_conformance.py``) *requires* proof of the declared tier, and an
adapter that lies about its tier fails conformance instead of degrading
quietly.

Reused infrastructure (never reimplemented):
* ``enforce_network_policy()`` -- refuse egress to denied destinations
* ``rate_limit_meter`` -- per-adapter rolling 429 counter
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from bernstein.adapters.base import RateLimitMeter, record_rate_limit_hit

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from bernstein.adapters.scanner_finding import Finding


# ---------------------------------------------------------------------------
# Capability enums
# ---------------------------------------------------------------------------


class OutputFormat(StrEnum):
    """The parseable output format the scanner emits."""

    SARIF = "sarif"
    JSON = "json"
    XML = "xml"


class DeterminismTier(StrEnum):
    """How reproducible a scanner's output is, and what conformance must prove.

    ``deterministic``     -- two runs on identical input yield identical
                             finding hashes (no clock / PID / random noise).
    ``feed_pinned``       -- reproducible "as-of" a recorded feed digest:
                             identical hashes given the *same* recorded digest.
    ``transcript_anchored`` -- not byte-deterministic; a transcript is recorded
                             that a later verify step can diff.
    """

    DETERMINISTIC = "deterministic"
    FEED_PINNED = "feed_pinned"
    TRANSCRIPT_ANCHORED = "transcript_anchored"


class ScannerCategory(StrEnum):
    """The class of analysis the scanner performs."""

    SAST = "sast"
    SCA = "sca"
    SECRET = "secret"
    IAC = "iac"
    RECON = "recon"
    DAST = "dast"


@dataclass(frozen=True)
class ScanScope:
    """What a scan is allowed to touch.

    Attributes:
        roots: Paths the scanner may read (usually the repo root or a subset).
        include: Glob patterns to include (empty = everything under roots).
        exclude: Glob patterns to exclude (takes precedence over ``include``).
        max_depth: Optional directory depth cap.
        config: Free-form adapter-specific scan configuration (rule sets,
            severity thresholds, etc.).
    """

    roots: tuple[Path, ...] = ()
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    max_depth: int | None = None
    config: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "roots": [str(p) for p in self.roots],
            "include": list(self.include),
            "exclude": list(self.exclude),
            "max_depth": self.max_depth,
            "config": dict(self.config),
        }


@dataclass
class ScanResult:
    """Result of a :meth:`ScannerAdapter.scan` call.

    Attributes:
        findings: The findings produced by the scan.
        transcript: Optional adapter-recorded transcript (used when determinism
            is ``transcript_anchored``).  Stored content-addressed so a later
            verify step can diff it.
        feed_digest: Optional digest of the recorded feed (used when determinism
            is ``feed_pinned``).  Empty when the adapter did not pin a feed.
    """

    findings: list[Finding] = field(default_factory=list)
    transcript: str = ""
    feed_digest: str = ""

    def finding_hashes(self) -> list[str]:
        """Return the per-finding canonical hashes, sorted."""
        return sorted(f.finding_hash() for f in self.findings)


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


class ScannerAdapter(ABC):
    """Uniform contract for external analysis / scanner tools.

    Subclass and implement :meth:`name` and :meth:`scan`.  Declare the four
    capability fields at class level so the conformance suite can prove the
    determinism tier you claim.

    Reuse the inherited :meth:`enforce_network_policy` and
    :attr:`rate_limit_meter` rather than reimplementing either - they are
    imported from :mod:`bernstein.adapters.base` and shared with the CLI
    adapter layer.
    """

    #: Stable registry key (e.g. ``"grype"``).  Subclasses must override.
    registry_name: str = ""

    # --- capability declaration (drives conformance) -----------------------

    #: Output format the scanner emits.  Consumers parse according to this.
    output_format: OutputFormat = OutputFormat.JSON

    #: The determinism tier this adapter promises.  The conformance suite
    #: enforces a *different* obligation per tier, so a lie here fails
    #: conformance rather than degrading quietly.
    determinism: DeterminismTier = DeterminismTier.TRANSCRIPT_ANCHORED

    #: Which input digests must be recorded for ``feed_pinned`` adapters.
    #: Each entry names a digest the adapter must capture and return in
    #: :attr:`ScanResult.feed_digest` (or record in its transcript).  Ignored
    #: for non-feed-pinned tiers.
    pinned_inputs: tuple[str, ...] = ()

    #: The class of analysis (sast / sca / secret / iac / recon / dast).
    category: ScannerCategory = ScannerCategory.SAST

    #: Endpoints the scanner dials out to (if any).  When non-empty,
    #: :meth:`enforce_network_policy` consults the active airgap policy at
    #: scan time and raises ``NetworkPolicyDenied`` if a destination is blocked.
    external_endpoints: tuple[tuple[str, int], ...] = ()

    def __init__(self) -> None:
        self._rate_limit_meter: RateLimitMeter | None = None

    # --- inherited network + rate-limit infra ------------------------------

    def enforce_network_policy(self) -> None:
        """Refuse to scan when a declared endpoint is denied by the policy.

        No-op when :attr:`external_endpoints` is empty (pure local scanner) or
        when the policy is unrestricted.
        """
        if not self.external_endpoints:
            return
        from bernstein.core.security.network_policy import policy_from_env

        policy = policy_from_env()
        for host, port in self.external_endpoints:
            policy.check(host, port, source=f"scanner:{self.name()}")

    @property
    def rate_limit_meter(self) -> RateLimitMeter:
        """Return the per-adapter rate-limit meter, instantiated on first use."""
        if self._rate_limit_meter is None:
            adapter_name = self.name() or type(self).__name__.lower()
            self._rate_limit_meter = RateLimitMeter(adapter_name=adapter_name)
        return self._rate_limit_meter

    def record_rate_limit_hit(self, *, error_code: str = "") -> None:
        """Convenience hook for adapter rate-limit handlers."""
        record_rate_limit_hit(self.rate_limit_meter, error_code=error_code)

    # --- the two methods every scanner must implement ----------------------

    @abstractmethod
    def name(self) -> str:
        """Return the stable registry key for this scanner (e.g. ``"grype"``)."""
        ...

    @abstractmethod
    def scan(self, target: Path, scope: ScanScope, workdir: Path) -> ScanResult:
        """Run the analysis and return its findings.

        Args:
            target: The file or directory to scan.
            scope: What the scan is allowed to touch (roots, globs, config).
            workdir: A writable directory for transcripts / temp artifacts.

        Returns:
            A :class:`ScanResult` carrying the findings (and, for pinned /
            transcript tiers, the recorded digest / transcript).
        """
        ...

    # --- helpers for subclasses --------------------------------------------

    def _digests_for_pinned_inputs(self, scope: ScanScope) -> dict[str, str]:
        """Compute the declared pinned-input digests for ``feed_pinned`` adapters.

        Default implementation hashes everything reachable under each
        ``ScanScope.root`` plus the JSON-serialised scope config.  Adapters with
        feed sources the default cannot see (a remote ruleset URL, a pinned
        package index) override this to capture those digests.

        Returns:
            ``{pinned_input_name: digest}`` for each declared entry in
            :attr:`pinned_inputs`.  Entries that cannot be resolved are absent
            so the conformance check can tell which pins were missed.
        """
        import hashlib

        digests: dict[str, str] = {}
        for name in self.pinned_inputs:
            h = hashlib.sha256()
            for root in scope.roots:
                if root.is_dir():
                    for p in sorted(root.rglob("*")):
                        if p.is_file():
                            h.update(p.read_bytes())
                elif root.is_file():
                    h.update(root.read_bytes())
            h.update(repr(sorted(scope.config.items())).encode("utf-8"))
            digests[name] = h.hexdigest()
        return digests

    def __iter__(self) -> Iterator[Finding]:
        """Not iterable; present only to satisfy structural typing checks."""
        raise TypeError("ScannerAdapter is not iterable")
