"""Catalog service: glue between fetcher, sandbox, user config, audit.

The service exposes the operations the CLI surfaces:

* :meth:`browse` — list catalog entries.
* :meth:`search` — substring/keyword search across catalog entries.
* :meth:`info` — full info for one entry.
* :meth:`install` — sandboxed dry-run + write to user config.
* :meth:`list_installed` — read installed entries from user config.
* :meth:`upgrade` / :meth:`upgrade_all` — re-fetch + version comparison.
* :meth:`status` — last fetch / next-due timestamps for the cadence.
* :meth:`background_check_due` — gating helper for ``mcp serve`` startup.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from bernstein.core.protocols.mcp_catalog.audit import CatalogAuditor
from bernstein.core.protocols.mcp_catalog.fetcher import (
    DEFAULT_CHECK_INTERVAL_SECONDS,
    CatalogFetcher,
)
from bernstein.core.protocols.mcp_catalog.sandbox_preview import (
    SandboxRunner,
    run_install_preview,
)
from bernstein.core.protocols.mcp_catalog.user_config import (
    install_entry,
    list_installed,
    touch_upgrade_check,
    uninstall_entry,
    upgrade_entry,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from bernstein.core.protocols.mcp_catalog.fetcher import Catalog, FetchResult
    from bernstein.core.protocols.mcp_catalog.manifest import CatalogEntry
    from bernstein.core.protocols.mcp_catalog.sandbox_preview import InstallPreview
    from bernstein.core.protocols.mcp_catalog.user_config import InstalledEntry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InstallOutcome:
    """The result of :meth:`CatalogService.install`.

    Attributes:
        entry: Catalog entry installed (or that would be installed).
        preview: Sandboxed dry-run preview.
        installed: The persisted :class:`InstalledEntry`, or ``None``
            when the install was aborted before the host config was
            written (preview failure or user declined).
        warning_unverified: True when the entry has
            ``verified_by_bernstein=false``; the CLI surfaces a
            prominent warning before the install preview runs.
        confirmed: Whether the user confirmed the write.
    """

    entry: CatalogEntry
    preview: InstallPreview
    installed: InstalledEntry | None
    warning_unverified: bool
    confirmed: bool


@dataclass(frozen=True)
class UpgradeOutcome:
    """The result of :meth:`CatalogService.upgrade` for a single entry.

    Attributes:
        entry_id: Catalog id.
        from_version: Previously installed version pin.
        to_version: Version pin in the latest catalog. Equal to
            ``from_version`` when no upgrade was needed.
        applied: Whether the upgrade was applied to the user config.
        skipped_reason: Human-readable reason when ``applied=False``.
        preview: Sandboxed preview, when one was executed.
    """

    entry_id: str
    from_version: str
    to_version: str
    applied: bool
    skipped_reason: str | None
    preview: InstallPreview | None


@dataclass(frozen=True)
class CatalogStatus:
    """Snapshot of cache + cadence state for ``mcp catalog status``.

    Attributes:
        cache_path: Path of the on-disk cache.
        last_fetch_at: ISO-8601 timestamp of the last fetch attempt, or
            empty string when no cache exists.
        next_due_at: ISO-8601 timestamp when the next background check
            is allowed to run.
        check_interval_seconds: The configured cadence in seconds.
        installed_count: Number of installed entries in the user config.
        last_check_log: Optional human-readable last-check status.
    """

    cache_path: str
    last_fetch_at: str
    next_due_at: str
    check_interval_seconds: int
    installed_count: int
    last_check_log: str = ""


@dataclass
class CatalogServiceConfig:
    """Service-wide configuration knobs.

    Attributes:
        check_interval_seconds: Cadence for background upgrade checks
            (``mcp.catalog.check_interval``). Default: 24h.
        sandbox_runner: Optional :class:`SandboxRunner` overriding the
            default tempdir + subprocess runner.
        user_config_path: Override for the user MCP config path.
    """

    check_interval_seconds: int = DEFAULT_CHECK_INTERVAL_SECONDS
    sandbox_runner: SandboxRunner | None = None
    user_config_path: Path | None = None


@dataclass
class CatalogService:
    """High-level facade orchestrating catalog operations.

    Args:
        fetcher: :class:`CatalogFetcher` instance (cache + ETag aware).
        user_config_path: Path to the user MCP config; entries are
            written under the ``bernstein-managed`` block here.
        auditor: HMAC audit emitter.
        config: Service config (cadence, sandbox runner, ...).
        confirm_callback: Callable that returns ``True`` when the user
            confirms a write. The CLI substitutes a Click prompt; tests
            substitute ``lambda _: True``.
    """

    fetcher: CatalogFetcher
    user_config_path: Path
    auditor: CatalogAuditor = field(default_factory=CatalogAuditor)
    config: CatalogServiceConfig = field(default_factory=CatalogServiceConfig)
    confirm_callback: Callable[[InstallPreview], bool] = field(
        default=lambda _preview: True
    )

    # ------------------------------------------------------------------
    # Catalog browsing
    # ------------------------------------------------------------------
    def browse(self, *, force_refresh: bool = False) -> Catalog:
        """Fetch the catalog (using cache when fresh) and return it."""
        result = self._fetch(force=force_refresh)
        return result.catalog

    def search(self, query: str, *, force_refresh: bool = False) -> list[CatalogEntry]:
        """Substring search over id / name / description."""
        catalog = self.browse(force_refresh=force_refresh)
        needle = query.strip().lower()
        if not needle:
            return list(catalog.entries)
        out: list[CatalogEntry] = []
        for entry in catalog.entries:
            if (
                needle in entry.id.lower()
                or needle in entry.name.lower()
                or needle in entry.description.lower()
            ):
                out.append(entry)
        return out

    def info(self, entry_id: str, *, force_refresh: bool = False) -> CatalogEntry | None:
        """Look up a single entry by id."""
        return self.browse(force_refresh=force_refresh).find(entry_id)

    # ------------------------------------------------------------------
    # Install
    # ------------------------------------------------------------------
    def install(
        self,
        entry_id: str,
        *,
        skip_confirmation: bool = False,
        force_refresh: bool = False,
    ) -> InstallOutcome:
        """Run a sandboxed dry-run and install on success+confirmation.

        The host MCP config is **never** written when:

        * the entry is unknown,
        * the sandbox preview returns a non-zero exit code or times out,
        * the confirmation gate returns False.

        On any of those paths the cache and the user config are left
        untouched (acceptance criterion).
        """
        catalog = self.browse(force_refresh=force_refresh)
        entry = catalog.find(entry_id)
        if entry is None:
            raise KeyError(f"Catalog entry {entry_id!r} not found")

        preview = run_install_preview(
            entry, runner=self.config.sandbox_runner
        )
        warning_unverified = not entry.verified_by_bernstein

        if not preview.succeeded:
            self.auditor.install(
                entry_id=entry.id,
                version_pin=entry.version_pin,
                verified=entry.verified_by_bernstein,
                exit_code=preview.exit_code,
            )
            return InstallOutcome(
                entry=entry,
                preview=preview,
                installed=None,
                warning_unverified=warning_unverified,
                confirmed=False,
            )

        confirmed = True if skip_confirmation else self.confirm_callback(preview)
        if not confirmed:
            self.auditor.install(
                entry_id=entry.id,
                version_pin=entry.version_pin,
                verified=entry.verified_by_bernstein,
                exit_code=preview.exit_code,
            )
            return InstallOutcome(
                entry=entry,
                preview=preview,
                installed=None,
                warning_unverified=warning_unverified,
                confirmed=False,
            )

        installed = install_entry(self.user_config_path, entry)
        self.auditor.install(
            entry_id=entry.id,
            version_pin=entry.version_pin,
            verified=entry.verified_by_bernstein,
            exit_code=preview.exit_code,
        )
        return InstallOutcome(
            entry=entry,
            preview=preview,
            installed=installed,
            warning_unverified=warning_unverified,
            confirmed=True,
        )

    # ------------------------------------------------------------------
    # List installed
    # ------------------------------------------------------------------
    def list_installed(self) -> list[InstalledEntry]:
        """Read installed entries from the user MCP config."""
        return list_installed(self.user_config_path)

    def installed_with_catalog_state(
        self, *, force_refresh: bool = False
    ) -> list[tuple[InstalledEntry, CatalogEntry | None]]:
        """Pair each installed entry with its current catalog entry, if any."""
        catalog = self.browse(force_refresh=force_refresh)
        out: list[tuple[InstalledEntry, CatalogEntry | None]] = []
        for installed in self.list_installed():
            out.append((installed, catalog.find(installed.id)))
        return out

    # ------------------------------------------------------------------
    # Uninstall
    # ------------------------------------------------------------------
    def uninstall(self, entry_id: str) -> bool:
        """Remove an entry from the bernstein-managed block."""
        removed = uninstall_entry(self.user_config_path, entry_id)
        if removed:
            self.auditor.uninstall(entry_id=entry_id)
        return removed

    # ------------------------------------------------------------------
    # Upgrade
    # ------------------------------------------------------------------
    def upgrade(
        self,
        entry_id: str,
        *,
        skip_confirmation: bool = False,
        force_refresh: bool = False,
    ) -> UpgradeOutcome:
        """Upgrade a single installed entry to the catalog's pin."""
        installed_lookup = {e.id: e for e in self.list_installed()}
        installed = installed_lookup.get(entry_id)
        if installed is None:
            raise KeyError(f"Entry {entry_id!r} is not installed")

        catalog = self.browse(force_refresh=force_refresh)
        catalog_entry = catalog.find(entry_id)
        if catalog_entry is None:
            return UpgradeOutcome(
                entry_id=entry_id,
                from_version=installed.version_pin,
                to_version=installed.version_pin,
                applied=False,
                skipped_reason="entry no longer present in catalog",
                preview=None,
            )

        if catalog_entry.version_pin == installed.version_pin:
            touch_upgrade_check(self.user_config_path, entry_id)
            return UpgradeOutcome(
                entry_id=entry_id,
                from_version=installed.version_pin,
                to_version=catalog_entry.version_pin,
                applied=False,
                skipped_reason="already on latest version",
                preview=None,
            )

        if not (installed.auto_upgrade or skip_confirmation):
            preview = run_install_preview(
                catalog_entry, runner=self.config.sandbox_runner
            )
            if not preview.succeeded:
                self.auditor.upgrade(
                    entry_id=entry_id,
                    from_version=installed.version_pin,
                    to_version=catalog_entry.version_pin,
                    verified=catalog_entry.verified_by_bernstein,
                    exit_code=preview.exit_code,
                )
                return UpgradeOutcome(
                    entry_id=entry_id,
                    from_version=installed.version_pin,
                    to_version=catalog_entry.version_pin,
                    applied=False,
                    skipped_reason="sandboxed preview failed",
                    preview=preview,
                )
            confirmed = self.confirm_callback(preview)
            if not confirmed:
                self.auditor.upgrade(
                    entry_id=entry_id,
                    from_version=installed.version_pin,
                    to_version=catalog_entry.version_pin,
                    verified=catalog_entry.verified_by_bernstein,
                    exit_code=preview.exit_code,
                )
                return UpgradeOutcome(
                    entry_id=entry_id,
                    from_version=installed.version_pin,
                    to_version=catalog_entry.version_pin,
                    applied=False,
                    skipped_reason="user declined",
                    preview=preview,
                )
        else:
            preview = run_install_preview(
                catalog_entry, runner=self.config.sandbox_runner
            )
            if not preview.succeeded:
                self.auditor.upgrade(
                    entry_id=entry_id,
                    from_version=installed.version_pin,
                    to_version=catalog_entry.version_pin,
                    verified=catalog_entry.verified_by_bernstein,
                    exit_code=preview.exit_code,
                )
                return UpgradeOutcome(
                    entry_id=entry_id,
                    from_version=installed.version_pin,
                    to_version=catalog_entry.version_pin,
                    applied=False,
                    skipped_reason="sandboxed preview failed",
                    preview=preview,
                )

        upgrade_entry(self.user_config_path, catalog_entry)
        self.auditor.upgrade(
            entry_id=entry_id,
            from_version=installed.version_pin,
            to_version=catalog_entry.version_pin,
            verified=catalog_entry.verified_by_bernstein,
            exit_code=preview.exit_code,
        )
        return UpgradeOutcome(
            entry_id=entry_id,
            from_version=installed.version_pin,
            to_version=catalog_entry.version_pin,
            applied=True,
            skipped_reason=None,
            preview=preview,
        )

    def upgrade_all(
        self, *, skip_confirmation: bool = False, force_refresh: bool = False
    ) -> list[UpgradeOutcome]:
        """Upgrade every installed entry that has a newer catalog pin."""
        outcomes: list[UpgradeOutcome] = []
        for installed in self.list_installed():
            try:
                outcomes.append(
                    self.upgrade(
                        installed.id,
                        skip_confirmation=skip_confirmation or installed.auto_upgrade,
                        force_refresh=force_refresh,
                    )
                )
            except KeyError:  # pragma: no cover - race with concurrent uninstall
                continue
        return outcomes

    # ------------------------------------------------------------------
    # Cadence + status
    # ------------------------------------------------------------------
    def background_check_due(self, *, now: datetime | None = None) -> bool:
        """Whether the upgrade-cadence interval has elapsed.

        Uses the most recent ``last_upgrade_check`` across all installed
        entries; returns True when none are installed (the first check
        on a fresh machine is always allowed).
        """
        installed = self.list_installed()
        if not installed:
            return True
        latest: datetime | None = None
        for entry in installed:
            if not entry.last_upgrade_check:
                return True
            try:
                ts = datetime.fromisoformat(
                    entry.last_upgrade_check.replace("Z", "+00:00")
                )
            except ValueError:
                return True
            if latest is None or ts > latest:
                latest = ts
        if latest is None:  # pragma: no cover - covered by None branch above
            return True
        current = now or datetime.now(tz=UTC)
        return current - latest >= timedelta(seconds=self.config.check_interval_seconds)

    def status(self, *, now: datetime | None = None) -> CatalogStatus:
        """Return cache + cadence state for the ``mcp catalog status`` view."""
        cache_path = str(self.fetcher.cache_path)
        cached = self.fetcher.cached()
        last_fetch = ""
        if self.fetcher.cache_path.exists():
            try:
                last_fetch = datetime.fromtimestamp(
                    self.fetcher.cache_path.stat().st_mtime, tz=UTC
                ).isoformat()
            except OSError:
                last_fetch = ""
        installed = self.list_installed()

        latest_check: datetime | None = None
        for entry in installed:
            if not entry.last_upgrade_check:
                continue
            try:
                ts = datetime.fromisoformat(
                    entry.last_upgrade_check.replace("Z", "+00:00")
                )
            except ValueError:
                continue
            if latest_check is None or ts > latest_check:
                latest_check = ts

        if latest_check is not None:
            next_due = latest_check + timedelta(
                seconds=self.config.check_interval_seconds
            )
            next_due_str = next_due.isoformat()
        else:
            next_due_str = (now or datetime.now(tz=UTC)).isoformat()

        last_check_log = "no cache" if cached is None else "cache valid"

        return CatalogStatus(
            cache_path=cache_path,
            last_fetch_at=last_fetch,
            next_due_at=next_due_str,
            check_interval_seconds=self.config.check_interval_seconds,
            installed_count=len(installed),
            last_check_log=last_check_log,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _fetch(self, *, force: bool) -> FetchResult:
        result = self.fetcher.fetch(force=force)
        self.auditor.fetch(
            source_url=result.source_url,
            from_cache=result.from_cache,
            revalidated=result.revalidated,
        )
        return result


__all__ = [
    "CatalogService",
    "CatalogServiceConfig",
    "CatalogStatus",
    "InstallOutcome",
    "UpgradeOutcome",
]
