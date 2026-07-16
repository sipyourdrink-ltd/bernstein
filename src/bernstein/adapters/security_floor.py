"""Adapter security-floor spawn preflight with signed refusal receipts (#2515).

Promotes the advisory version floor (see :mod:`advisories`) from a
doctor-warning into an enforced spawn-time contract. The spawn boundary is
the most privileged hand-off in the system: the binary we exec receives the
task context, the workspace, and the worker's credential scope. Before a
third-party CLI adapter is trusted with that hand-off, the installed binary's
version is compared against its minimum-safe floor and the decision -- permit,
refuse, or an explicit warn-only override -- is sealed into a
content-addressed receipt whose canonical bytes hash to its identity and is
mirrored into the HMAC audit chain.

Killer shape: the refusal *is* the receipt. Strip the audit chain and the
floor-map content hash and the feature degrades to an ordinary version check
with a log line; with them a refusal is a tamper-evident, offline-verifiable
proof artefact anchored to the chain, a permit is the same proof that a
floor-satisfying binary ran, and a contiguous chain slice proves offline that
no below-floor adapter was spawned during a window -- a gap in the slice is
detectable rather than silently exculpatory.

Lever: the receipt is meaningful only against the HMAC-chained audit log and
the deterministic (byte-identical replay) floor-map hash. The advisory map
(:data:`bernstein.adapters.advisories.ADAPTER_MIN_SAFE_VERSIONS`) stays the
single source of truth, so a floor bump remains a data-only diff.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.adapters.advisories import ADAPTER_MIN_SAFE_VERSIONS, AdapterAdvisory

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from bernstein.core.security.audit_chain import AuditChainStore

logger = logging.getLogger(__name__)

__all__ = [
    "POLICY_BLOCK",
    "POLICY_WARN",
    "RECEIPT_KIND",
    "SECURITY_FLOOR_SCHEMA_VERSION",
    "VERDICT_PERMIT",
    "VERDICT_REFUSE",
    "VERDICT_UNKNOWN_VERSION",
    "VERDICT_WARN_OVERRIDE",
    "AdapterSecurityFloorRefusal",
    "FloorVerdict",
    "audit_preflight_no_below_floor",
    "build_preflight_receipt",
    "evaluate_spawn_floor",
    "floor_map_content_hash",
    "hash_floor_map",
    "policy_from_env",
    "preflight_spawn_floor",
    "probe_installed_version",
    "receipt_floor_map_matches",
    "receipt_sha256",
    "security_floor_for",
    "verify_preflight_receipt",
    "write_preflight_receipt",
]

#: Version stamped into every receipt preimage. Bump only on a wire-format
#: change so old receipts stay verifiable against the version they sealed.
SECURITY_FLOOR_SCHEMA_VERSION = 1

#: ``kind`` discriminator carried in the content-addressed receipt body.
RECEIPT_KIND = "adapter.spawn_preflight_receipt"

#: The installed binary meets or exceeds its floor (or the adapter is
#: untracked): the spawn proceeds.
VERDICT_PERMIT = "permit"

#: The installed binary is below its floor under the block policy: the spawn
#: is refused and this receipt is the proof artefact.
VERDICT_REFUSE = "refuse"

#: The installed binary is below its floor but an explicit warn-only policy
#: let the spawn proceed. Recorded so an audited window still shows the
#: below-floor spawn was consciously permitted.
VERDICT_WARN_OVERRIDE = "warn_override"

#: The adapter is tracked but its version could not be determined, so the
#: floor cannot be enforced. Recorded (does not block) so the gap is visible.
VERDICT_UNKNOWN_VERSION = "unknown_version"

#: Enforcement policies. ``block`` (default) refuses a below-floor spawn;
#: ``warn`` is the explicit operator opt-out that permits it and records the
#: override.
POLICY_BLOCK = "block"
POLICY_WARN = "warn"

#: Operator opt-out env var. Any value in :data:`_WARN_TOKENS` selects the
#: warn-only policy; everything else (including unset) keeps block-by-default.
_POLICY_ENV = "BERNSTEIN_ADAPTER_FLOOR_POLICY"
_WARN_TOKENS = frozenset({"warn", "warn-only", "warn_only", "advisory"})

_VERSION_TOKEN_RE = re.compile(r"\d+(?:\.\d+){1,3}")
_VERSION_TIMEOUT_SECONDS = 10
_RECEIPT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class AdapterSecurityFloorRefusal(RuntimeError):
    """Raised when a spawn is refused because the adapter is below its floor.

    Carries the sealed refusal receipt (and its content hash) so the caller
    surfaces the proof artefact, not just an error string. The exception is a
    plain :class:`RuntimeError` subclass -- deliberately *not* a
    :class:`~bernstein.adapters.base.SpawnError` -- so the spawner's
    per-provider failover loop never swallows it into an alternate-provider
    retry: a floor refusal is a hard stop, not a transient spawn failure.
    """

    def __init__(self, message: str, *, receipt: dict[str, Any], receipt_sha256: str) -> None:
        super().__init__(message)
        self.receipt = receipt
        self.receipt_sha256 = receipt_sha256


# ---------------------------------------------------------------------------
# Canonical JSON helpers
# ---------------------------------------------------------------------------


def _canonical_bytes(data: dict[str, Any]) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# Floor-map content hash (tamper anchor)
# ---------------------------------------------------------------------------


def hash_floor_map(mapping: dict[str, AdapterAdvisory]) -> str:
    """Content hash of a floor map, prefixed ``sha256:``.

    Every field an operator could mutate (floor, advisory id, note) feeds the
    hash, so any edit to the map -- not just a floor bump -- is detectable when
    a receipt's recorded hash is checked against the live map.
    """
    payload = {
        name: {
            "min_safe_version": adv.min_safe_version,
            "advisory_id": adv.advisory_id,
            "note": adv.note,
        }
        for name, adv in sorted(mapping.items())
    }
    return "sha256:" + _sha256_hex(_canonical_bytes(payload))


def floor_map_content_hash() -> str:
    """Content hash of the packaged advisory floor map."""
    return hash_floor_map(ADAPTER_MIN_SAFE_VERSIONS)


def security_floor_for(adapter: str) -> str | None:
    """Return the adapter's minimum-safe floor, or ``None`` when untracked."""
    advisory = ADAPTER_MIN_SAFE_VERSIONS.get(adapter)
    return advisory.min_safe_version if advisory is not None else None


# ---------------------------------------------------------------------------
# Version probe
# ---------------------------------------------------------------------------


def probe_installed_version(binary_path: str) -> str | None:
    """Return the first dotted-numeric token from ``<binary> --version``.

    Best-effort: any launch failure, timeout, or unparseable output yields
    ``None`` (reported as unknown rather than fabricating a below-floor
    verdict from a garbled ``--version`` string).
    """
    try:
        proc = subprocess.run(
            [binary_path, "--version"],
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # Log the exception type only: --version output on agent CLIs can
        # echo credential-adjacent environment fragments.
        logger.debug("floor: version probe failed for %s (%s)", binary_path, type(exc).__name__)
        return None
    blob = f"{proc.stdout}\n{proc.stderr}"
    match = _VERSION_TOKEN_RE.search(blob)
    return match.group(0) if match else None


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FloorVerdict:
    """The floor decision for one adapter spawn candidate.

    Attributes:
        adapter: Adapter registry key.
        binary: Binary name the floor was probed from.
        installed_version: Discovered upstream version, or ``None``.
        floor: The minimum-safe floor, or ``None`` when the adapter is
            untracked (no advisory).
        advisory_id: Bernstein-local advisory id, or ``None`` when untracked.
        verdict: One of :data:`VERDICT_PERMIT`, :data:`VERDICT_REFUSE`,
            :data:`VERDICT_WARN_OVERRIDE`, :data:`VERDICT_UNKNOWN_VERSION`.
        blocked: Whether the spawn must be refused.
        policy: The enforcement policy that produced this verdict.
        floor_map_hash: Content hash of the floor map at decision time.
    """

    adapter: str
    binary: str
    installed_version: str | None
    floor: str | None
    advisory_id: str | None
    verdict: str
    blocked: bool
    policy: str
    floor_map_hash: str

    @property
    def tracked(self) -> bool:
        """True when the adapter has a curated floor to enforce."""
        return self.floor is not None


def policy_from_env(env: dict[str, str] | None = None) -> str:
    """Resolve the enforcement policy from the environment.

    Block-by-default: only an explicit opt-out token selects warn-only.
    """
    source = env if env is not None else os.environ
    raw = (source.get(_POLICY_ENV) or "").strip().lower()
    return POLICY_WARN if raw in _WARN_TOKENS else POLICY_BLOCK


def evaluate_spawn_floor(
    adapter: str,
    binary: str,
    installed_version: str | None,
    *,
    policy: str = POLICY_BLOCK,
    floor_map_hash: str | None = None,
) -> FloorVerdict:
    """Decide permit / refuse / warn-override / unknown for one candidate.

    Pure and deterministic: a fixed ``(adapter, binary, installed_version,
    policy, floor map)`` always yields the same verdict.
    """
    fmh = floor_map_hash if floor_map_hash is not None else floor_map_content_hash()
    advisory = ADAPTER_MIN_SAFE_VERSIONS.get(adapter)
    if advisory is None:
        return FloorVerdict(
            adapter=adapter,
            binary=binary,
            installed_version=installed_version,
            floor=None,
            advisory_id=None,
            verdict=VERDICT_PERMIT,
            blocked=False,
            policy=policy,
            floor_map_hash=fmh,
        )

    floor = advisory.min_safe_version
    advisory_id = advisory.advisory_id

    if installed_version is None:
        return FloorVerdict(
            adapter=adapter,
            binary=binary,
            installed_version=None,
            floor=floor,
            advisory_id=advisory_id,
            verdict=VERDICT_UNKNOWN_VERSION,
            blocked=False,
            policy=policy,
            floor_map_hash=fmh,
        )

    from packaging.version import InvalidVersion, Version

    try:
        installed = Version(installed_version)
        floor_v = Version(floor)
    except InvalidVersion:
        # An unparseable version is unknown, not below-floor: a garbled
        # --version string must never fabricate a refusal.
        return FloorVerdict(
            adapter=adapter,
            binary=binary,
            installed_version=installed_version,
            floor=floor,
            advisory_id=advisory_id,
            verdict=VERDICT_UNKNOWN_VERSION,
            blocked=False,
            policy=policy,
            floor_map_hash=fmh,
        )

    if installed < floor_v:
        if policy == POLICY_WARN:
            return FloorVerdict(
                adapter=adapter,
                binary=binary,
                installed_version=installed_version,
                floor=floor,
                advisory_id=advisory_id,
                verdict=VERDICT_WARN_OVERRIDE,
                blocked=False,
                policy=policy,
                floor_map_hash=fmh,
            )
        return FloorVerdict(
            adapter=adapter,
            binary=binary,
            installed_version=installed_version,
            floor=floor,
            advisory_id=advisory_id,
            verdict=VERDICT_REFUSE,
            blocked=True,
            policy=policy,
            floor_map_hash=fmh,
        )

    return FloorVerdict(
        adapter=adapter,
        binary=binary,
        installed_version=installed_version,
        floor=floor,
        advisory_id=advisory_id,
        verdict=VERDICT_PERMIT,
        blocked=False,
        policy=policy,
        floor_map_hash=fmh,
    )


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------


def build_preflight_receipt(verdict: FloorVerdict, *, generated_at: str) -> dict[str, Any]:
    """Bind a verdict into a canonical receipt mapping.

    Determinism: the receipt is a pure function of the verdict and the
    caller-supplied timestamp, so two decisions over an identical installed
    set and floor map at the same timestamp produce byte-identical receipts
    (and therefore the same :func:`receipt_sha256` identity).
    """
    return {
        "schema_version": SECURITY_FLOOR_SCHEMA_VERSION,
        "kind": RECEIPT_KIND,
        "adapter": verdict.adapter,
        "binary": verdict.binary,
        "installed_version": verdict.installed_version,
        "floor": verdict.floor,
        "advisory_id": verdict.advisory_id,
        "verdict": verdict.verdict,
        "blocked": verdict.blocked,
        "policy": verdict.policy,
        "floor_map_hash": verdict.floor_map_hash,
        "generated_at": generated_at,
    }


def receipt_sha256(receipt: dict[str, Any]) -> str:
    """Content hash (identity) of a receipt's canonical bytes."""
    return _sha256_hex(_canonical_bytes(receipt))


def verify_preflight_receipt(doc: dict[str, Any]) -> bool:
    """Check a written receipt document against its embedded content hash.

    A mutated receipt body no longer hashes to the recorded identity, so
    tampering is detected offline with no key material required (the HMAC
    audit-chain mirror additionally binds the hash into the chain).
    """
    receipt = doc.get("receipt")
    recorded = doc.get("receipt_sha256")
    if not isinstance(receipt, dict) or not isinstance(recorded, str):
        return False
    return receipt_sha256(receipt) == recorded


def receipt_floor_map_matches(doc: dict[str, Any], *, current_hash: str | None = None) -> bool:
    """True when the receipt's recorded floor-map hash matches the live map.

    A floor map mutated after the receipt was sealed yields a hash mismatch,
    so tampering with the floor map itself is flagged at verification -- not
    only tampering with the receipt body.
    """
    receipt = doc.get("receipt")
    if not isinstance(receipt, dict):
        return False
    recorded = receipt.get("floor_map_hash")
    expected = current_hash if current_hash is not None else floor_map_content_hash()
    return recorded == expected


def write_preflight_receipt(base_dir: Path, receipt: dict[str, Any]) -> Path:
    """Write a receipt under its content-addressed name.

    The filename embeds the adapter name and the receipt hash prefix. The
    adapter component is validated against a strict allow-pattern and the
    resolved path is containment-checked against ``base_dir`` so a hostile
    adapter string can never escape the receipts directory.
    """
    adapter = str(receipt.get("adapter") or "")
    if not _RECEIPT_NAME_RE.match(adapter):
        raise ValueError(f"invalid adapter name for receipt filename: {adapter!r}")
    sha = receipt_sha256(receipt)
    base_dir.mkdir(parents=True, exist_ok=True)
    resolved_base = base_dir.resolve()
    candidate = (resolved_base / f"{adapter}-{sha[:16]}.json").resolve()
    if not candidate.is_relative_to(resolved_base):
        raise ValueError("receipt path escapes the receipts directory")
    doc = {"receipt": receipt, "receipt_sha256": sha}
    tmp = candidate.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(candidate)
    return candidate


# ---------------------------------------------------------------------------
# Enforcement orchestration
# ---------------------------------------------------------------------------


def _mirror_receipt(chain: AuditChainStore, verdict: FloorVerdict, sha: str) -> None:
    from bernstein.core.security.audit_chain import record_adapter_spawn_preflight_receipt

    record_adapter_spawn_preflight_receipt(
        chain=chain,
        adapter=verdict.adapter,
        binary=verdict.binary,
        installed_version=verdict.installed_version,
        floor=verdict.floor,
        advisory_id=verdict.advisory_id,
        verdict=verdict.verdict,
        blocked=verdict.blocked,
        policy=verdict.policy,
        floor_map_hash=verdict.floor_map_hash,
        receipt_sha256=sha,
    )


def preflight_spawn_floor(
    *,
    adapter: str,
    chain: AuditChainStore,
    generated_at: str,
    which: Callable[[str], str | None] | None = None,
    version_probe: Callable[[str], str | None] | None = None,
    policy: str = POLICY_BLOCK,
    receipts_dir: Path | None = None,
) -> FloorVerdict:
    """Evaluate the floor, seal + chain-anchor the receipt, and enforce.

    For an untracked adapter (no advisory) there is no floor to enforce and
    nothing to prove, so no receipt is emitted and the spawn proceeds.

    For a tracked adapter the installed version is probed, the verdict is
    computed, a content-addressed receipt is sealed and mirrored into the HMAC
    chain (so a permit is as provable as a refusal), and:

    * a below-floor version under the block policy raises
      :class:`AdapterSecurityFloorRefusal` carrying the sealed receipt -- the
      refusal *is* the proof artefact;
    * a below-floor version under the warn policy records a warn-override and
      returns normally;
    * a floor-satisfying version (or an unknown version) records a permit /
      unknown-version receipt and returns normally.

    The chain mirror is attempted before any refusal is raised so the refusal
    is anchored. If the mirror itself fails the spawn still fails closed.

    Args:
        adapter: Adapter registry key (also the binary name for tracked ones).
        chain: The audit chain store the receipt is mirrored into.
        generated_at: Timestamp stamped into the receipt preimage.
        which: ``shutil.which``-shaped resolver; tests inject stubs.
        version_probe: ``--version`` probe; tests inject stubs.
        policy: :data:`POLICY_BLOCK` (default) or :data:`POLICY_WARN`.
        receipts_dir: When given, the receipt is also written as a
            content-addressed file for offline verification.

    Returns:
        The :class:`FloorVerdict` on permit / warn-override / unknown /
        untracked.

    Raises:
        AdapterSecurityFloorRefusal: On a below-floor version under the block
            policy.
    """
    advisory = ADAPTER_MIN_SAFE_VERSIONS.get(adapter)
    if advisory is None:
        return FloorVerdict(
            adapter=adapter,
            binary=adapter,
            installed_version=None,
            floor=None,
            advisory_id=None,
            verdict=VERDICT_PERMIT,
            blocked=False,
            policy=policy,
            floor_map_hash=floor_map_content_hash(),
        )

    resolver = which if which is not None else shutil.which
    probe = version_probe if version_probe is not None else probe_installed_version
    binary = advisory.adapter
    binary_path = resolver(binary)
    installed_version = probe(binary_path) if binary_path else None

    verdict = evaluate_spawn_floor(adapter, binary, installed_version, policy=policy)
    receipt = build_preflight_receipt(verdict, generated_at=generated_at)
    sha = receipt_sha256(receipt)

    if receipts_dir is not None:
        try:
            write_preflight_receipt(receipts_dir, receipt)
        except (OSError, ValueError) as exc:
            logger.warning("Could not write spawn-preflight receipt for %s: %s", adapter, type(exc).__name__)

    try:
        _mirror_receipt(chain, verdict, sha)
    except Exception as exc:  # the mirror must never mask the enforcement decision
        logger.warning(
            "Could not mirror adapter.spawn_preflight_receipt for %s: %s",
            adapter,
            type(exc).__name__,
        )

    if verdict.blocked:
        raise AdapterSecurityFloorRefusal(
            f"Adapter {adapter!r} version {installed_version} is below its security floor "
            f"{verdict.floor} [{verdict.advisory_id}]; spawn refused "
            f"(receipt sha256:{sha}). Upgrade {binary} to >= {verdict.floor}, "
            f"or set {_POLICY_ENV}=warn to override.",
            receipt=receipt,
            receipt_sha256=sha,
        )
    return verdict


# ---------------------------------------------------------------------------
# Offline audit of a chain slice
# ---------------------------------------------------------------------------


def audit_preflight_no_below_floor(events: list[Any]) -> tuple[bool, list[str]]:
    """Prove, over a slice of preflight receipt events, that no below-floor
    adapter spawn was permitted.

    Each event may be an :class:`~bernstein.core.security.audit.AuditEvent`
    (its ``details`` mapping is read) or the details mapping directly. A
    below-floor spawn is *permitted* exactly when a decision was below floor
    and not blocked -- i.e. a recorded warn-override. Under the block policy
    the slice contains no warn-overrides, so ``ok`` is ``True``.

    Chain continuity (a gap in the slice) is a separate, complementary check:
    :meth:`AuditChainStore.verify` detects a truncated or tampered chain, so a
    missing window is detectable rather than silently exculpatory.

    Returns:
        ``(ok, violations)`` where ``violations`` names each permitted
        below-floor spawn found.
    """
    violations: list[str] = []
    for event in events:
        details = getattr(event, "details", event) or {}
        if not isinstance(details, dict):
            continue
        if details.get("verdict") == VERDICT_WARN_OVERRIDE:
            violations.append(
                f"{details.get('adapter')} {details.get('installed_version')} "
                f"below floor {details.get('floor')} permitted via warn-override "
                f"(advisory {details.get('advisory_id')})"
            )
    return (not violations, violations)
