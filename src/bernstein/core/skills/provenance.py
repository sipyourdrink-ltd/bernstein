"""Skill usage provenance: install receipts + a recomputable usage graph.

Issue #2301. A signed skills catalog proves *what a registry claims* about
a skill, but nothing ties an install to what the skill actually did. This
module adds the usage-attestation layer on top of the content-hash pinning
already carried in ``skills.lock``:

* **Install receipt (AC1).** :func:`write_install_receipt` records an
  :class:`InstallReceipt` ``{skill_hash, manifest_hash, install_id,
  timestamp}`` into a dedicated lineage-spine run (``run_id="skills"``).
  The receipt bytes *are* the artifact the spine hashes, so the returned
  anchor is the spine entry hash over the receipt. Strip the spine and the
  receipt is just a file; anchored, it is a chain-verifiable attestation.

* **Usage link (AC2).** Whenever a skill participates in a run,
  :func:`record_usage` appends a line to
  ``.sdd/skills/usage/<skill_hash>.jsonl`` binding the skill hash to that
  run's journal head (the spine head hash). The link is a pointer into the
  Merkle-chained run journal, not a copy of it.

* **Provenance graph (AC3/AC4).** :func:`provenance_graph` walks the usage
  links and returns only runs whose journal head still verifies *and* still
  equals the head recorded at link time. The verified-run count is a pure
  function of the distinct verified ``(run_id, journal_head)`` set - it is
  recomputed on every query, never read from a stored counter.

* **Verify (AC5).** :func:`verify_install` recomputes the install receipt
  and flags a ``manifest_hash`` that no longer matches the installed
  content.

Determinism: receipt rows and usage rows are canonical JSON (sorted keys,
minimal separators, UTF-8), so two byte-identical inputs produce
byte-identical files and anchors.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bernstein.core.lineage.spine import LineageSpine, SpineStatus

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

logger = logging.getLogger(__name__)

#: Run id under which every install receipt is anchored. Install lineage is
#: kept in one dedicated run so it never interleaves with per-task journals.
INSTALL_RUN_ID = "skills"

#: Actor recorded on install-receipt spine entries.
_INSTALL_ACTOR = "bernstein.skill_provenance"

#: Model string recorded on install-receipt spine entries (no model runs
#: at install time; the field is part of the spine schema).
_INSTALL_MODEL = "none"

_RECEIPT_SUBPATH = (".sdd", "skills", "receipts")
_USAGE_SUBPATH = (".sdd", "skills", "usage")


# ---------------------------------------------------------------------------
# skill_hash -> filesystem-safe name
# ---------------------------------------------------------------------------


def _safe_hash_name(skill_hash: str) -> str:
    """Return a filesystem-safe basename for ``skill_hash``.

    ``skill_hash`` is a ``sha256:<hex>`` string; the colon is replaced so
    the name is portable across filesystems and cannot introduce a path
    separator. The value is validated to contain no separators first.
    """
    if not skill_hash:
        raise ValueError("empty skill_hash")
    if "/" in skill_hash or "\\" in skill_hash or "\x00" in skill_hash:
        raise ValueError(f"skill_hash contains an unsafe character: {skill_hash!r}")
    return skill_hash.replace(":", "_")


def receipt_path(workdir: Path, skill_hash: str) -> Path:
    """Return the on-disk install-receipt path for ``skill_hash``."""
    return workdir.joinpath(*_RECEIPT_SUBPATH, f"{_safe_hash_name(skill_hash)}.json")


def usage_index_path(workdir: Path, skill_hash: str) -> Path:
    """Return the usage-index JSONL path for ``skill_hash``."""
    return workdir.joinpath(*_USAGE_SUBPATH, f"{_safe_hash_name(skill_hash)}.jsonl")


# ---------------------------------------------------------------------------
# InstallReceipt
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstallReceipt:
    """The attestable record produced by a single skill install.

    Attributes:
        skill_hash: Content hash of the installed skill (``sha256:<hex>``).
        manifest_hash: SHA-256 of the catalog manifest that authorised the
            install.
        install_id: Per-install unique identifier; ties this receipt to the
            lockfile row and the catalog audit event.
        timestamp: Integer timestamp; caller-chosen but stable so identical
            fixtures anchor byte-identically.
    """

    skill_hash: str
    manifest_hash: str
    install_id: str
    timestamp: int

    def to_canonical_bytes(self) -> bytes:
        """Serialise to canonical JSON bytes (the spine-hashed artifact)."""
        return json.dumps(
            {
                "skill_hash": self.skill_hash,
                "manifest_hash": self.manifest_hash,
                "install_id": self.install_id,
                "timestamp": self.timestamp,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @classmethod
    def from_bytes(cls, raw: bytes) -> InstallReceipt:
        """Parse a receipt from its canonical JSON bytes."""
        row = json.loads(raw)
        return cls(
            skill_hash=str(row["skill_hash"]),
            manifest_hash=str(row["manifest_hash"]),
            install_id=str(row["install_id"]),
            timestamp=int(row["timestamp"]),
        )


# ---------------------------------------------------------------------------
# UsageLink
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UsageLink:
    """One ``skill_hash -> run journal head`` binding in the usage index."""

    skill_hash: str
    run_id: str
    journal_head: str
    timestamp: int

    def to_row(self) -> bytes:
        """Serialise to a canonical single-line JSONL row."""
        return (
            json.dumps(
                {
                    "skill_hash": self.skill_hash,
                    "run_id": self.run_id,
                    "journal_head": self.journal_head,
                    "timestamp": self.timestamp,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")


# ---------------------------------------------------------------------------
# Install receipt (AC1)
# ---------------------------------------------------------------------------


def write_install_receipt(
    *,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    receipt: InstallReceipt,
) -> str:
    """Write ``receipt`` to disk and anchor it in the install spine.

    The receipt's canonical bytes are the artifact the spine hashes, so the
    returned value is the spine entry hash over exactly those bytes: the
    receipt's chain-verifiable identity.

    Args:
        workdir: Project root; the receipt file lands under
            ``.sdd/skills/receipts/``.
        lineage_root: Spine root (``.sdd/lineage``).
        hmac_key: The audit-chain HMAC key that tags spine entries.
        receipt: The receipt to record.

    Returns:
        The spine entry hash anchoring the receipt.
    """
    payload = receipt.to_canonical_bytes()
    path = receipt_path(workdir, receipt.skill_hash)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)

    spine = LineageSpine(lineage_root, run_id=INSTALL_RUN_ID, hmac_key=hmac_key)
    artifact_path = "/".join((*_RECEIPT_SUBPATH, f"{_safe_hash_name(receipt.skill_hash)}.json"))
    return spine.record(
        artifact_path=artifact_path,
        content=payload,
        actor=_INSTALL_ACTOR,
        step_id=receipt.install_id,
        model=_INSTALL_MODEL,
        timestamp=receipt.timestamp,
    )


def read_install_receipt(workdir: Path, skill_hash: str) -> InstallReceipt | None:
    """Return the install receipt for ``skill_hash`` or ``None`` if absent."""
    path = receipt_path(workdir, skill_hash)
    if not path.is_file():
        return None
    try:
        return InstallReceipt.from_bytes(path.read_bytes())
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        logger.warning("skill provenance: malformed install receipt at %s", path)
        return None


# ---------------------------------------------------------------------------
# Usage link (AC2)
# ---------------------------------------------------------------------------


def record_usage(
    *,
    workdir: Path,
    skill_hash: str,
    run_id: str,
    journal_head: str,
    timestamp: int,
) -> UsageLink:
    """Append a ``skill_hash -> journal_head`` usage link for a run.

    Args:
        workdir: Project root; the index lands under ``.sdd/skills/usage/``.
        skill_hash: Content hash of the skill that participated in the run.
        run_id: The run identifier (spine run id).
        journal_head: The run's journal head (spine head hash) captured at
            the moment the skill participated.
        timestamp: Integer timestamp for the link.

    Returns:
        The recorded :class:`UsageLink`.
    """
    link = UsageLink(
        skill_hash=skill_hash,
        run_id=run_id,
        journal_head=journal_head,
        timestamp=timestamp,
    )
    path = usage_index_path(workdir, skill_hash)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as fh:
        fh.write(link.to_row())
    return link


def iter_usage_links(workdir: Path, skill_hash: str) -> Iterator[UsageLink]:
    """Yield every usage link recorded for ``skill_hash`` in append order."""
    path = usage_index_path(workdir, skill_hash)
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            yield UsageLink(
                skill_hash=str(row["skill_hash"]),
                run_id=str(row["run_id"]),
                journal_head=str(row["journal_head"]),
                timestamp=int(row["timestamp"]),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            logger.debug("skill provenance: skipping malformed usage row in %s", path)
            continue


# ---------------------------------------------------------------------------
# Provenance graph (AC3/AC4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunProvenance:
    """One run a skill contributed to, with its verification verdict."""

    run_id: str
    journal_head: str
    verified: bool
    reason: str = ""


@dataclass(frozen=True)
class ProvenanceGraph:
    """A recomputable provenance graph for one skill.

    ``verified_run_count`` is a derived property over the distinct verified
    ``(run_id, journal_head)`` set - it is never a stored counter (AC4).
    """

    skill_hash: str
    runs: tuple[RunProvenance, ...]

    @property
    def verified_runs(self) -> tuple[RunProvenance, ...]:
        """Runs whose journal head verifies and matches the link."""
        return tuple(r for r in self.runs if r.verified)

    @property
    def unverified_runs(self) -> tuple[RunProvenance, ...]:
        """Runs whose journal head failed verification."""
        return tuple(r for r in self.runs if not r.verified)

    @property
    def verified_run_count(self) -> int:
        """Distinct verified ``(run_id, journal_head)`` count (recomputed)."""
        return len({(r.run_id, r.journal_head) for r in self.verified_runs})


def provenance_graph(
    *,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    skill_hash: str,
) -> ProvenanceGraph:
    """Return the recomputable provenance graph for ``skill_hash``.

    For every recorded usage link the run's spine is re-verified. A run is
    counted only when its chain verifies *and* its current head still equals
    the head captured at link time - a run whose journal was tampered with,
    or advanced past the linked head, is surfaced as unverified.

    Args:
        workdir: Project root holding ``.sdd/skills/usage/``.
        lineage_root: Spine root (``.sdd/lineage``).
        hmac_key: The audit-chain HMAC key that tags spine entries.
        skill_hash: The skill whose provenance is queried.

    Returns:
        A :class:`ProvenanceGraph`. Distinct links are collapsed by
        ``(run_id, journal_head)`` so a duplicated link cannot inflate the
        count.
    """
    seen: set[tuple[str, str]] = set()
    runs: list[RunProvenance] = []
    for link in iter_usage_links(workdir, skill_hash):
        key = (link.run_id, link.journal_head)
        if key in seen:
            continue
        seen.add(key)
        verified, reason = _verify_link_head(lineage_root, hmac_key, link)
        runs.append(
            RunProvenance(
                run_id=link.run_id,
                journal_head=link.journal_head,
                verified=verified,
                reason=reason,
            )
        )
    return ProvenanceGraph(skill_hash=skill_hash, runs=tuple(runs))


def _verify_link_head(lineage_root: Path, hmac_key: bytes, link: UsageLink) -> tuple[bool, str]:
    """Return ``(verified, reason)`` for one usage link's journal head."""
    spine = LineageSpine(lineage_root, run_id=link.run_id, hmac_key=hmac_key)
    result = spine.verify()
    if result.status is SpineStatus.NO_ENTRIES:
        return False, "run journal is empty"
    if result.status is SpineStatus.TAMPERED:
        return False, "run journal failed chain verification"
    current_head = spine.head_hash()
    if current_head != link.journal_head:
        return False, "recorded journal head does not match current spine head"
    return True, ""


# ---------------------------------------------------------------------------
# Verify install (AC5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstallVerifyResult:
    """Outcome of :func:`verify_install`."""

    ok: bool
    reason: str
    receipt: InstallReceipt | None = None


def verify_install(
    *,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    skill_hash: str,
    installed_manifest_hash: str,
) -> InstallVerifyResult:
    """Recompute the install receipt and flag a manifest drift.

    Args:
        workdir: Project root.
        lineage_root: Spine root (``.sdd/lineage``).
        hmac_key: The audit-chain HMAC key.
        skill_hash: The installed skill's content hash.
        installed_manifest_hash: SHA-256 of the manifest recomputed from the
            currently installed content.

    Returns:
        An :class:`InstallVerifyResult`. ``ok`` is True only when the
        receipt exists, its install spine verifies, and the receipt's
        ``manifest_hash`` matches ``installed_manifest_hash`` (AC5).
    """
    receipt = read_install_receipt(workdir, skill_hash)
    if receipt is None:
        return InstallVerifyResult(ok=False, reason="no install receipt found")

    spine = LineageSpine(lineage_root, run_id=INSTALL_RUN_ID, hmac_key=hmac_key)
    spine_result = spine.verify()
    if not spine_result.ok:
        return InstallVerifyResult(
            ok=False,
            reason=f"install spine failed verification ({spine_result.status.value})",
            receipt=receipt,
        )

    if receipt.manifest_hash != installed_manifest_hash:
        return InstallVerifyResult(
            ok=False,
            reason=(
                f"manifest hash mismatch (receipt {receipt.manifest_hash[:12]}..., "
                f"installed {installed_manifest_hash[:12]}...)"
            ),
            receipt=receipt,
        )

    return InstallVerifyResult(ok=True, reason="", receipt=receipt)


__all__ = [
    "INSTALL_RUN_ID",
    "InstallReceipt",
    "InstallVerifyResult",
    "ProvenanceGraph",
    "RunProvenance",
    "UsageLink",
    "iter_usage_links",
    "provenance_graph",
    "read_install_receipt",
    "receipt_path",
    "record_usage",
    "usage_index_path",
    "verify_install",
    "write_install_receipt",
]
