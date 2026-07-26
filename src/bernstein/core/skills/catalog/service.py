"""High-level skill catalog service.

Mirrors :class:`bernstein.core.protocols.mcp_catalog.service.CatalogService`
but for skills. Wires together:

- :class:`SkillCatalogFetcher` (cache + ETag revalidation),
- :class:`SkillCatalogAuditor` (HMAC-chained audit emission),
- :func:`install_catalog_entry` (plugin installer + standard layout),
- :func:`upsert_catalog_install` (lockfile + lineage receipts).

The service is intentionally testable: it accepts an optional
:class:`SkillCatalog` for the in-memory path used by unit tests so they
do not have to spin up an HTTP transport.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from bernstein.core.plugins_core.plugin_installer import install_plugin
from bernstein.core.skills.catalog.audit import (
    SkillCatalogAuditor,
    compute_manifest_sha256,
)
from bernstein.core.skills.catalog.fetcher import (
    DEFAULT_CHECK_INTERVAL_SECONDS,
    SkillCatalogFetcher,
)
from bernstein.core.skills.catalog.installer import (
    CatalogInstallError,
    install_catalog_entry,
    remove_catalog_install,
)
from bernstein.core.skills.catalog.lockfile import (
    CATALOG_LOCK_FILENAME,
    CatalogLockEntry,
    TransparencyHead,
    detect_drift,
    fresh_install_id,
    read_state,
    record_pin,
    record_transparency_head,
    remove_catalog_entry,
    upsert_catalog_install,
)
from bernstein.core.skills.catalog.revocation import (
    RevocationEntry,
    parse_revocations,
)
from bernstein.core.skills.catalog.signature import (
    ManifestSignatureError,
    VerificationOutcome,
    verify_entry,
)
from bernstein.core.skills.catalog.transparency import (
    InclusionReceipt,
    TransparencyVerificationError,
    build_inclusion_receipt,
    leaf_hash_for_state,
    parse_catalog_envelope,
)
from bernstein.core.skills.lifecycle import (
    InstallScope,
    compute_skill_digest,
    scope_root,
)

if TYPE_CHECKING:
    from bernstein.core.skills.catalog.fetcher import FetchResult
    from bernstein.core.skills.catalog.installer import (
        CatalogInstallResult,
        InstallerCallable,
    )
    from bernstein.core.skills.catalog.lockfile import LineageReceipt
    from bernstein.core.skills.catalog.manifest import (
        SkillCatalog,
        SkillCatalogEntry,
    )

logger = logging.getLogger(__name__)


_GENESIS_HEAD = "0" * 64

#: Default poll interval (5 minutes) for the revocation kill switch. Bounds how
#: long a compromised skill can keep being injected after a signed revocation
#: is published (issue #2527).
DEFAULT_REVOCATION_POLL_SECONDS = 5 * 60


class SkillCatalogError(RuntimeError):
    """Raised on operator-visible catalog failures (not signature-specific)."""


@dataclass(frozen=True)
class InstallOutcome:
    """Outcome of a single install or upgrade operation."""

    entry_id: str
    name: str
    version: str
    install_dir: Path
    manifest_url: str
    manifest_sha256: str
    content_digest: str
    install_id: str
    chain_head: str
    verified: bool
    verification_reason: str = ""
    inclusion_receipt: InclusionReceipt | None = None


@dataclass(frozen=True)
class UpgradeOutcome:
    """Outcome of a single upgrade operation."""

    entry_id: str
    from_version: str
    to_version: str
    applied: bool
    skipped_reason: str = ""
    install: InstallOutcome | None = None


@dataclass(frozen=True)
class EntryVerifyResult:
    """Per-entry outcome of ``bernstein skills catalog verify``."""

    entry_id: str
    version: str
    ok: bool
    detail: str
    pinned_references: int = 0


@dataclass(frozen=True)
class VerifyReport:
    """Offline verification report for ``bernstein skills catalog verify``."""

    ok: bool
    entries_checked: int
    results: tuple[EntryVerifyResult, ...]
    revoked: tuple[str, ...]

    def failures(self) -> list[EntryVerifyResult]:
        """Return the failing per-entry results."""
        return [r for r in self.results if not r.ok]


@dataclass(frozen=True)
class CatalogStatus:
    """Status summary printed by ``bernstein skills catalog status``."""

    cache_path: Path
    last_fetch_at: str
    revalidate_seconds: int
    installed_count: int
    drift_count: int
    lockfile_path: Path
    lockfile_digest: str


@dataclass(frozen=True)
class SkillCatalogServiceConfig:
    """Config knobs for :class:`SkillCatalogService`.

    Attributes:
        audit_key_path: Optional override for the audit HMAC key file. Used
            to anchor install receipts in the lineage spine; tests pass an
            isolated path so the receipt spine does not depend on XDG state.
    """

    workdir: Path
    scope: InstallScope = InstallScope.PROJECT
    home: Path | None = None
    check_interval_seconds: int = DEFAULT_CHECK_INTERVAL_SECONDS
    audit_key_path: Path | None = None
    require_transparency: bool = False
    revocation_poll_interval_seconds: int = DEFAULT_REVOCATION_POLL_SECONDS


class SkillCatalogService:
    """Coordinates catalog browsing and installation.

    Args:
        fetcher: Catalog fetcher (network + cache). Either this or
            ``preloaded_catalog`` is required.
        auditor: Audit emitter. Defaults to a disabled in-memory wrapper.
        config: Service configuration (workdir, scope, etc.).
        plugin_installer: Optional override for the plugin installer
            dispatcher; tests substitute a fixture-based installer to
            avoid network access.
        preloaded_catalog: When provided, the service uses this catalog
            in place of fetcher.fetch() (unit-test path).
    """

    def __init__(
        self,
        *,
        fetcher: SkillCatalogFetcher | None = None,
        auditor: SkillCatalogAuditor | None = None,
        config: SkillCatalogServiceConfig,
        plugin_installer: InstallerCallable | None = None,
        preloaded_catalog: SkillCatalog | None = None,
    ) -> None:
        if fetcher is preloaded_catalog is None:
            raise ValueError("either fetcher or preloaded_catalog must be supplied")
        self._fetcher = fetcher
        self._auditor = auditor or SkillCatalogAuditor()
        self._config = config
        self._installer: InstallerCallable = plugin_installer if plugin_installer is not None else install_plugin
        self._preloaded = preloaded_catalog
        self._last_fetch: FetchResult | None = None

    # ------------------------------------------------------------------
    # Catalog access
    # ------------------------------------------------------------------

    @property
    def workdir(self) -> Path:
        """Current project root."""
        return self._config.workdir

    @property
    def lockfile_path(self) -> Path:
        """Path to the project-local ``skills.lock``."""
        return self._config.workdir / CATALOG_LOCK_FILENAME

    @property
    def auditor(self) -> SkillCatalogAuditor:
        """Underlying audit emitter (read-only)."""
        return self._auditor

    def browse(self, *, force_refresh: bool = False) -> SkillCatalog:
        """Return the active catalog (cached or network-fetched)."""
        if self._preloaded is not None and not force_refresh:
            return self._preloaded
        if self._fetcher is None:
            assert self._preloaded is not None
            return self._preloaded
        result = self._fetcher.fetch(force=force_refresh)
        self._last_fetch = result
        self._auditor.fetch(
            source_url=result.source_url,
            from_cache=result.from_cache,
            revalidated=result.revalidated,
        )
        return result.catalog

    def search(self, query: str, *, force_refresh: bool = False) -> list[SkillCatalogEntry]:
        """Substring search over id/name/description/tags."""
        return self.browse(force_refresh=force_refresh).search(query)

    def info(self, entry_id: str, *, force_refresh: bool = False) -> SkillCatalogEntry | None:
        """Return a single catalog entry."""
        return self.browse(force_refresh=force_refresh).find(entry_id)

    def list_installed(self) -> list[CatalogLockEntry]:
        """Return the catalog-installed skills recorded in the lockfile."""
        return list(read_state(self.lockfile_path).catalog)

    # ------------------------------------------------------------------
    # Install / upgrade
    # ------------------------------------------------------------------

    def install(
        self,
        entry_id: str,
        *,
        allow_unverified: bool = False,
        force_refresh: bool = False,
    ) -> InstallOutcome:
        """Install a single catalog entry.

        Args:
            entry_id: The catalog id.
            allow_unverified: When False, an unsigned or unverifiable
                entry refuses to install. When True, the install
                proceeds but the audit event records
                ``manifest_signer_pubkey=null``.
            force_refresh: Bypass the fetcher's cache.

        Returns:
            :class:`InstallOutcome` summarising the install.

        Raises:
            SkillCatalogError: If the entry is not found, the upstream
                content digest disagrees with the resolved install, or
                the chain anchor refuses a replay.
            ManifestSignatureError: When ``allow_unverified=False`` and
                the signature does not verify.
        """
        catalog = self.browse(force_refresh=force_refresh)
        entry = catalog.find(entry_id)
        if entry is None:
            raise SkillCatalogError(f"catalog entry {entry_id!r} not found")

        outcome = verify_entry(
            entry,
            catalog.signer_pubkey,
            allow_unverified=allow_unverified,
        )
        if not outcome.verified and not allow_unverified:
            raise ManifestSignatureError(
                f"signature verification failed for {entry_id!r}: {outcome.reason}",
            )

        # Kill switch: a signed revocation covering this exact version is
        # refused here so the install never even stages a known-bad skill.
        self._enforce_revocation_at_install(catalog, entry)

        # Transparency: require an inclusion proof for the fetched catalog
        # state under a signed head, and a consistency proof against the last
        # head recorded in ``skills.lock``. Refuse (with a chain-anchored
        # refusal receipt) on any failure. Returns ``None`` for a legacy
        # catalog that ships no transparency envelope.
        inclusion_receipt = self._verify_transparency(catalog, entry)

        # Audit replay check: compare upstream manifest sha against the
        # chain's known-good set. A mismatch on a previously-installed
        # entry means upstream drifted; refuse.
        manifest_url = entry.source.url_for_audit()
        manifest_sha = compute_manifest_sha256(manifest_url, entry.to_dict())
        prior = self._auditor.last_install_for_entry(entry_id)
        if prior is not None:
            prior_sha = prior.details.get("manifest_sha256")
            if isinstance(prior_sha, str) and prior_sha != manifest_sha:
                known = self._auditor.known_good_manifest_shas()
                if manifest_sha not in known:
                    raise SkillCatalogError(
                        f"upstream manifest sha for {entry_id!r} drifted from chain head "
                        f"(known {prior_sha[:12]}..., now {manifest_sha[:12]}...). "
                        "Refusing replay; run `bernstein skills catalog sync --force` to "
                        "acknowledge the drift and emit a new chain entry.",
                    )

        # Stage install on disk.
        try:
            install_result: CatalogInstallResult = install_catalog_entry(
                entry,
                scope=self._config.scope,
                workdir=self._config.workdir,
                home=self._config.home,
                plugin_installer=self._installer,
            )
        except CatalogInstallError as exc:
            raise SkillCatalogError(str(exc)) from exc

        # Verify the installed content_digest matches the catalog claim.
        # The catalog publishes the SHA-256 of the canonicalised skill;
        # an installer that resolves to different bytes (eg a tampered
        # tarball) is rejected here.
        if entry.content_digest != install_result.content_digest:
            # On mismatch, roll back the install so the operator's
            # skill directory is not left in a half-baked state.
            installed_dir = (
                scope_root(
                    self._config.scope,
                    workdir=self._config.workdir,
                    home=self._config.home,
                )
                / entry.name
            )
            if installed_dir.is_dir():
                remove_catalog_install(
                    entry.name,
                    scope=self._config.scope,
                    workdir=self._config.workdir,
                    home=self._config.home,
                )
            raise SkillCatalogError(
                f"installed content digest for {entry_id!r} does not match catalog "
                f"(catalog {entry.content_digest[:12]}..., installed "
                f"{install_result.content_digest[:12]}...)",
            )

        install_id = fresh_install_id()
        # The previous install event *for this entry*, not this record's chain
        # predecessor: unrelated records land in between. Recorded under a name
        # that says so (#3062).
        prior_install_chain_head = prior.hmac if prior is not None else _GENESIS_HEAD

        audit_event = self._auditor.install(
            entry_id=entry_id,
            manifest_url=manifest_url,
            manifest_sha256=manifest_sha,
            manifest_signer_pubkey=catalog.signer_pubkey if outcome.verified else None,
            install_id=install_id,
            prior_install_chain_head=prior_install_chain_head,
        )
        chain_head = audit_event.hmac if audit_event is not None else _GENESIS_HEAD

        lock_entry = CatalogLockEntry(
            id=entry.id,
            name=entry.name,
            version=entry.version,
            manifest_url=manifest_url,
            manifest_sha256=manifest_sha,
            content_digest=install_result.content_digest,
            install_id=install_id,
            chain_head=chain_head,
            installed_at=datetime.now(tz=UTC).isoformat(),
        )
        upsert_catalog_install(
            self.lockfile_path,
            lock_entry,
            workdir=self._config.workdir,
            from_chain_head=prior_install_chain_head,
        )

        # Record the verified transparency head so the *next* install can prove
        # a consistency proof against it (rewrite / split-view detection).
        if inclusion_receipt is not None:
            record_transparency_head(
                self.lockfile_path,
                TransparencyHead(
                    entry_id=entry.id,
                    tree_size=inclusion_receipt.head.tree_size,
                    root_hash=inclusion_receipt.head.root_hash,
                    leaf_index=inclusion_receipt.leaf_index,
                    leaf_hash=inclusion_receipt.leaf_hash,
                    signature=inclusion_receipt.head.signature,
                    recorded_at=datetime.now(tz=UTC).isoformat(),
                ),
            )

        # Anchor an install receipt in the lineage spine (issue #2301). The
        # receipt's canonical bytes are the spine-hashed artifact, so its
        # anchor is a chain-verifiable identity for the install; a matching
        # audit-chain event ties the anchor into the HMAC audit log.
        self._emit_install_receipt(
            skill_hash=install_result.content_digest,
            manifest_hash=manifest_sha,
            install_id=install_id,
        )

        return InstallOutcome(
            entry_id=entry.id,
            name=entry.name,
            version=entry.version,
            install_dir=install_result.install_dir,
            manifest_url=manifest_url,
            manifest_sha256=manifest_sha,
            content_digest=install_result.content_digest,
            install_id=install_id,
            chain_head=chain_head,
            verified=outcome.verified,
            verification_reason=outcome.reason,
            inclusion_receipt=inclusion_receipt,
        )

    @property
    def lineage_root(self) -> Path:
        """Root of the per-run lineage spine store (``.sdd/lineage``)."""
        return self._config.workdir / ".sdd" / "lineage"

    def _emit_install_receipt(
        self,
        *,
        skill_hash: str,
        manifest_hash: str,
        install_id: str,
    ) -> None:
        """Write the install receipt to the spine and the audit chain.

        Never raises into the install path: a spine or audit-chain hiccup
        must not leave the operator's skill directory half-installed. The
        failure is logged and the content-hash pinning in the lockfile still
        stands (issue #2301).
        """
        from bernstein.core.security.audit import load_or_create_audit_key
        from bernstein.core.security.audit_chain import (
            AuditChainStore,
            record_skill_install_receipt,
        )
        from bernstein.core.skills.provenance import (
            InstallReceipt,
            write_install_receipt,
        )

        try:
            hmac_key = load_or_create_audit_key(self._config.audit_key_path)
            # ``skill_hash`` is the skill's content digest (BLAKE2b hex from
            # the lifecycle layer); it is an opaque content-address here.
            receipt = InstallReceipt(
                skill_hash=skill_hash,
                manifest_hash=manifest_hash,
                install_id=install_id,
                timestamp=int(datetime.now(tz=UTC).timestamp()),
            )
            anchor = write_install_receipt(
                workdir=self._config.workdir,
                lineage_root=self.lineage_root,
                hmac_key=hmac_key,
                receipt=receipt,
            )
            audit_dir = self._config.workdir / ".sdd" / "audit"
            chain = AuditChainStore(audit_dir, key=hmac_key)
            record_skill_install_receipt(
                chain=chain,
                skill_hash=skill_hash,
                manifest_hash=manifest_hash,
                install_id=install_id,
                spine_anchor=anchor,
            )
        except Exception as exc:
            logger.warning("skill install receipt emission failed for %s: %s", skill_hash, exc)

    # ------------------------------------------------------------------
    # Transparency + revocation enforcement (issue #2527)
    # ------------------------------------------------------------------

    def _verified_revocations(self, catalog: SkillCatalog) -> list[RevocationEntry]:
        """Return the catalog's signed revocations that verify against its key."""
        if not catalog.revocations:
            return []
        return parse_revocations(catalog.revocations, catalog.signer_pubkey)

    def _enforce_revocation_at_install(self, catalog: SkillCatalog, entry: SkillCatalogEntry) -> None:
        """Refuse an install whose ``(id, version)`` is covered by a revocation."""
        for revocation in self._verified_revocations(catalog):
            if revocation.covers(entry.id, entry.version):
                detail = (
                    f"skill {entry.id!r} version {entry.version} is revoked ({revocation.reason}); refusing install"
                )
                self._record_refusal(
                    skill_id=entry.id,
                    version=entry.version,
                    stage="install",
                    reason_code="revoked",
                    detail=detail,
                )
                raise SkillCatalogError(detail)

    def _verify_transparency(
        self,
        catalog: SkillCatalog,
        entry: SkillCatalogEntry,
    ) -> InclusionReceipt | None:
        """Verify the catalog transparency envelope for an install.

        Returns the verified :class:`InclusionReceipt` when the catalog ships a
        transparency envelope, or ``None`` for a legacy catalog when
        ``require_transparency`` is off. Refuses (recording a chain-anchored
        refusal receipt) on any proof failure.
        """
        envelope = catalog.transparency
        if envelope is None:
            if self._config.require_transparency:
                detail = (
                    f"catalog for {entry.id!r} ships no transparency log head; "
                    "refusing install under require_transparency"
                )
                self._record_refusal(
                    skill_id=entry.id,
                    version=entry.version,
                    stage="install",
                    reason_code="missing_transparency",
                    detail=detail,
                )
                raise SkillCatalogError(detail)
            return None

        prior = read_state(self.lockfile_path).find_transparency(entry.id)
        prev_size = prior.tree_size if prior is not None else 0
        prev_root = prior.root_hash if prior is not None else ""
        try:
            view = parse_catalog_envelope(envelope)
            state_leaf = leaf_hash_for_state(catalog.to_dict())
            receipt = build_inclusion_receipt(
                view,
                entry_digest=entry.content_digest,
                state_leaf=state_leaf,
                prev_tree_size=prev_size,
                prev_root_hash=prev_root,
            )
            receipt.verify(catalog.signer_pubkey)
        except TransparencyVerificationError as exc:
            reason_code = "consistency_proof_failed" if "consistency" in str(exc) else "inclusion_proof_failed"
            detail = f"transparency verification failed for {entry.id!r}: {exc}"
            self._record_refusal(
                skill_id=entry.id,
                version=entry.version,
                stage="install",
                reason_code=reason_code,
                detail=detail,
            )
            raise SkillCatalogError(detail) from exc
        return receipt

    def _record_refusal(
        self,
        *,
        skill_id: str,
        version: str,
        stage: str,
        reason_code: str,
        detail: str,
    ) -> None:
        """Append a chain-anchored ``skill.verification_refusal`` receipt.

        Never raises into the refusal path: recording the receipt must not mask
        the underlying refusal, which is surfaced by the caller regardless.
        """
        from bernstein.core.security.audit import load_or_create_audit_key
        from bernstein.core.security.audit_chain import (
            AuditChainStore,
            record_skill_verification_refusal,
        )

        try:
            hmac_key = load_or_create_audit_key(self._config.audit_key_path)
            audit_dir = self._config.workdir / ".sdd" / "audit"
            chain = AuditChainStore(audit_dir, key=hmac_key)
            record_skill_verification_refusal(
                chain=chain,
                skill_id=skill_id,
                stage=stage,
                reason_code=reason_code,
                detail=detail,
                version=version,
            )
        except Exception as exc:  # pragma: no cover - refusal receipt is best-effort
            logger.warning("skill refusal receipt emission failed for %s: %s", skill_id, exc)

    def upgrade(
        self,
        entry_id: str,
        *,
        allow_unverified: bool = False,
        force_refresh: bool = False,
    ) -> UpgradeOutcome:
        """Upgrade a single installed catalog entry."""
        prior = read_state(self.lockfile_path).find_catalog(entry_id)
        if prior is None:
            raise SkillCatalogError(f"{entry_id!r} is not installed")

        upstream = self.browse(force_refresh=force_refresh).find(entry_id)
        if upstream is None:
            return UpgradeOutcome(
                entry_id=entry_id,
                from_version=prior.version,
                to_version=prior.version,
                applied=False,
                skipped_reason="entry no longer in catalog",
            )

        manifest_url = upstream.source.url_for_audit()
        upstream_sha = compute_manifest_sha256(manifest_url, upstream.to_dict())

        if prior.manifest_sha256 == upstream_sha:
            return UpgradeOutcome(
                entry_id=entry_id,
                from_version=prior.version,
                to_version=upstream.version,
                applied=False,
                skipped_reason="already on latest",
            )

        outcome = self.install(
            entry_id,
            allow_unverified=allow_unverified,
            force_refresh=False,
        )
        # The install path emitted EVENT_INSTALL. For upgrades the audit
        # surface adds an explicit EVENT_UPGRADE for readability.
        self._auditor.upgrade(
            entry_id=entry_id,
            from_version=prior.version,
            to_version=upstream.version,
            manifest_url=manifest_url,
            manifest_sha256=upstream_sha,
            install_id=outcome.install_id,
            prior_install_chain_head=prior.chain_head,
        )
        return UpgradeOutcome(
            entry_id=entry_id,
            from_version=prior.version,
            to_version=upstream.version,
            applied=True,
            install=outcome,
        )

    def upgrade_all(
        self,
        *,
        allow_unverified: bool = False,
        force_refresh: bool = False,
    ) -> list[UpgradeOutcome]:
        """Upgrade every installed catalog entry that has a newer version."""
        outcomes: list[UpgradeOutcome] = []
        for row in self.list_installed():
            try:
                outcomes.append(
                    self.upgrade(
                        row.id,
                        allow_unverified=allow_unverified,
                        force_refresh=force_refresh,
                    )
                )
            except SkillCatalogError as exc:
                outcomes.append(
                    UpgradeOutcome(
                        entry_id=row.id,
                        from_version=row.version,
                        to_version=row.version,
                        applied=False,
                        skipped_reason=str(exc),
                    )
                )
        return outcomes

    def uninstall(self, entry_id: str) -> bool:
        """Remove a catalog-installed skill and its lockfile row."""
        state = read_state(self.lockfile_path)
        if state.find_catalog(entry_id) is None:
            return False
        ok = remove_catalog_install(
            entry_id,
            scope=self._config.scope,
            workdir=self._config.workdir,
            home=self._config.home,
        )
        remove_catalog_entry(self.lockfile_path, entry_id)
        self._auditor.uninstall(entry_id=entry_id)
        return ok

    # ------------------------------------------------------------------
    # Drift / sync
    # ------------------------------------------------------------------

    def sync(self) -> dict[str, tuple[str, str]]:
        """Detect lockfile vs on-disk drift and emit a sync audit event."""
        state = read_state(self.lockfile_path)
        installed_digests: dict[str, str] = {}
        for row in state.catalog:
            install_dir = (
                scope_root(
                    self._config.scope,
                    workdir=self._config.workdir,
                    home=self._config.home,
                )
                / row.id
            )
            if not install_dir.is_dir():
                installed_digests[row.id] = ""
                continue
            try:
                installed_digests[row.id] = compute_skill_digest(install_dir).digest
            except Exception:
                installed_digests[row.id] = ""

        drift = detect_drift(self.lockfile_path, installed_digests)
        receipt_id = json.dumps(sorted(drift.keys()), sort_keys=True)
        digest = state.digest()
        self._auditor.sync(lockfile_digest=digest, lineage_receipt=receipt_id)
        return drift

    def adopt_or_pin(
        self,
        entry_id: str,
        *,
        action: str,
        allow_unverified: bool = False,
    ) -> InstallOutcome | LineageReceipt:
        """Decide between adopting an upstream update or pinning.

        This is the deterministic decision a sibling worktree (wt-b)
        runs after wt-a's lockfile has advanced. The receipt records
        which way wt-b went; ``action="adopt"`` re-runs the install with
        the upstream manifest, while ``action="pin"`` emits a pin
        receipt without touching the install.
        """
        if action == "adopt":
            return self.install(entry_id, allow_unverified=allow_unverified)
        if action == "pin":
            state = read_state(self.lockfile_path)
            prior = state.find_catalog(entry_id)
            if prior is None:
                raise SkillCatalogError(f"{entry_id!r} is not installed; cannot pin")
            new_state = record_pin(
                self.lockfile_path,
                entry_id=entry_id,
                chain_head=prior.chain_head,
                manifest_sha256=prior.manifest_sha256,
                workdir=self._config.workdir,
            )
            return new_state.receipts[-1]
        raise SkillCatalogError(f"unknown action {action!r}; expected 'adopt' or 'pin'")

    # ------------------------------------------------------------------
    # Offline verification (issue #2527)
    # ------------------------------------------------------------------

    def verify(self, *, force_refresh: bool = False) -> VerifyReport:
        """Verify the catalog offline: transitive pins, inclusion, revocations.

        For every entry this proves each transitive source is pinned by digest;
        for every entry that ships a transparency envelope it verifies the
        inclusion proof (and, for an installed entry, a consistency proof
        against the head recorded in ``skills.lock``) against the signer key;
        and it fails any installed entry a signed revocation covers.

        A fetch that fails strict validation (an unpinned transitive source is
        rejected at parse time) raises :class:`SkillCatalogValidationError`; the
        CLI turns that into a failing report.
        """
        catalog = self.browse(force_refresh=force_refresh)
        lock = read_state(self.lockfile_path)
        revocations = self._verified_revocations(catalog)
        revoked_ids = {
            row.id for row in lock.catalog for revocation in revocations if revocation.covers(row.id, row.version)
        }

        envelope = catalog.transparency
        view = None
        state_leaf = None
        envelope_error = ""
        if envelope is not None:
            try:
                view = parse_catalog_envelope(envelope)
                state_leaf = leaf_hash_for_state(catalog.to_dict())
            except TransparencyVerificationError as exc:
                envelope_error = str(exc)

        results: list[EntryVerifyResult] = []
        for entry in catalog.entries:
            ok = True
            details: list[str] = []

            for index, ref in enumerate(entry.references):
                if not re.fullmatch(r"[0-9a-f]{64}", ref.digest):
                    ok = False
                    details.append(f"reference[{index}] is not pinned by a valid digest")
            if entry.references:
                details.append(f"{len(entry.references)} transitive source(s) pinned")

            recorded = lock.find_transparency(entry.id)
            if envelope is not None:
                if view is None:
                    ok = False
                    details.append(f"transparency envelope invalid: {envelope_error}")
                else:
                    prev_size = recorded.tree_size if recorded is not None else 0
                    prev_root = recorded.root_hash if recorded is not None else ""
                    try:
                        receipt = build_inclusion_receipt(
                            view,
                            entry_digest=entry.content_digest,
                            state_leaf=state_leaf or "",
                            prev_tree_size=prev_size,
                            prev_root_hash=prev_root,
                        )
                        receipt.verify(catalog.signer_pubkey)
                        details.append(f"inclusion proof verified under signed head size={view.head.tree_size}")
                        if prev_size > 0:
                            details.append("consistency proof verified against recorded head")
                    except TransparencyVerificationError as exc:
                        ok = False
                        details.append(f"transparency verification failed: {exc}")
            elif recorded is not None:
                ok = False
                details.append("recorded transparency head but catalog ships no envelope to verify it")

            if entry.id in revoked_ids:
                ok = False
                details.append("installed version is under a signed revocation")

            results.append(
                EntryVerifyResult(
                    entry_id=entry.id,
                    version=entry.version,
                    ok=ok,
                    detail="; ".join(details) or "ok",
                    pinned_references=len(entry.references),
                )
            )

        overall = all(r.ok for r in results)
        return VerifyReport(
            ok=overall,
            entries_checked=len(results),
            results=tuple(results),
            revoked=tuple(sorted(revoked_ids)),
        )

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> CatalogStatus:
        """Return a summary suitable for ``bernstein skills catalog status``."""
        drift = self.sync()
        state = read_state(self.lockfile_path)
        last_fetch_at = ""
        if self._last_fetch is not None and self._fetcher is not None:
            last_fetch_at = "available"
        cache_path = self._fetcher.cache_path if self._fetcher is not None else Path("(no fetcher)")
        revalidate_seconds = self._fetcher.revalidate_seconds if self._fetcher is not None else 0
        return CatalogStatus(
            cache_path=cache_path,
            last_fetch_at=last_fetch_at,
            revalidate_seconds=revalidate_seconds,
            installed_count=len(state.catalog),
            drift_count=len(drift),
            lockfile_path=self.lockfile_path,
            lockfile_digest=state.digest(),
        )


__all__ = [
    "DEFAULT_REVOCATION_POLL_SECONDS",
    "CatalogStatus",
    "EntryVerifyResult",
    "InstallOutcome",
    "ManifestSignatureError",
    "SkillCatalogError",
    "SkillCatalogService",
    "SkillCatalogServiceConfig",
    "UpgradeOutcome",
    "VerificationOutcome",
    "VerifyReport",
]
