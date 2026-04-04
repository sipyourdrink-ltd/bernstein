"""Plugin marketplace reconciliation — auto-uninstall delisted plugins.

Compares installed plugins against a marketplace listing on startup and
removes plugins that are no longer offered.  Mirrors the intent of Claude
Code's ``utils/plugins/pluginBlocklist.ts``, adapted for directory-based
plugins.

Call :func:`reconcile_plugins` at startup to automatically remove plugins
that have been delisted from the marketplace.

Usage::

    from bernstein.core.plugin_reconciler import reconcile_plugins

    result = reconcile_plugins(
        plugins_dir=workdir / ".bernstein" / "plugins",
        marketplace_path=workdir / ".bernstein" / "marketplace.yaml",
    )
    if result.removed:
        print(f"Auto-removed {len(result.removed)} delisted plugin(s): {result.removed}")

Marketplace YAML schema::

    # Active plugins available in the marketplace.
    plugins:
      - name: audit-logger
        version: "2.0.0"
      - name: metrics-reporter
        version: "1.5.0"

    # Plain name strings are also accepted:
    plugins:
      - audit-logger
      - metrics-reporter
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pathlib import Path

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketplaceEntry:
    """A plugin entry in the marketplace listing.

    Attributes:
        name: Plugin name (must match the installed directory name).
        version: Latest available version string (informational only).
    """

    name: str
    version: str = ""


@dataclass
class ReconcileResult:
    """Result of a plugin reconciliation pass.

    Attributes:
        removed: Plugin names that were uninstalled (or would be in dry-run).
        kept: Plugin names that are still listed in the marketplace.
        errors: Non-fatal error messages from failed removal attempts.
    """

    removed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Marketplace loading
# ---------------------------------------------------------------------------


def load_marketplace(marketplace_path: Path) -> list[MarketplaceEntry]:
    """Load the marketplace listing from a YAML or JSON file.

    Returns an empty list (no-op policy) when the file does not exist.

    Args:
        marketplace_path: Path to ``marketplace.yaml``.

    Returns:
        List of :class:`MarketplaceEntry` objects.  Empty when the file is
        missing or unparseable.
    """
    if not marketplace_path.exists():
        log.debug("plugin_reconciler: marketplace file not found at %s", marketplace_path)
        return []

    try:
        import yaml

        raw: Any = yaml.safe_load(marketplace_path.read_text(encoding="utf-8"))
    except ImportError:
        import json

        try:
            raw = json.loads(marketplace_path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("plugin_reconciler: failed to parse marketplace file %s: %s", marketplace_path, exc)
            return []
    except Exception as exc:
        log.warning("plugin_reconciler: failed to parse marketplace file %s: %s", marketplace_path, exc)
        return []

    if not isinstance(raw, dict):
        log.warning("plugin_reconciler: marketplace file %s must be a YAML mapping", marketplace_path)
        return []

    raw_dict: dict[str, Any] = cast("dict[str, Any]", raw)
    plugins_raw: Any = raw_dict.get("plugins", [])
    if not isinstance(plugins_raw, list):
        return []

    entries: list[MarketplaceEntry] = []
    for item in cast("list[Any]", plugins_raw):
        if isinstance(item, str):
            name = item.strip()
            if name:
                entries.append(MarketplaceEntry(name=name))
        elif isinstance(item, dict):
            name = str(item.get("name", "")).strip()
            if name:
                entries.append(MarketplaceEntry(name=name, version=str(item.get("version", ""))))
    return entries


# ---------------------------------------------------------------------------
# Installed plugin discovery
# ---------------------------------------------------------------------------


def get_installed_plugins(plugins_dir: Path) -> list[str]:
    """Return names of installed plugins (subdirectories in *plugins_dir*).

    Args:
        plugins_dir: Directory containing installed plugin directories.

    Returns:
        Sorted list of directory names (each corresponds to a plugin name).
    """
    if not plugins_dir.is_dir():
        return []
    return sorted(p.name for p in plugins_dir.iterdir() if p.is_dir())


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def reconcile_plugins(
    plugins_dir: Path,
    marketplace_path: Path,
    *,
    dry_run: bool = False,
) -> ReconcileResult:
    """Compare installed plugins against marketplace and remove delisted ones.

    When the marketplace file is absent, reconciliation is skipped entirely
    (no plugins are removed).  This is intentional — an absent marketplace
    file means the user has not opted into marketplace reconciliation.

    Args:
        plugins_dir: Directory containing installed plugin directories.
        marketplace_path: Path to ``marketplace.yaml`` with the active plugin
            listing.
        dry_run: When True, detect but do not actually remove any plugins.
            Plugins that would be removed still appear in :attr:`ReconcileResult.removed`.

    Returns:
        :class:`ReconcileResult` describing what was removed, kept, or errored.
    """
    result = ReconcileResult()

    marketplace = load_marketplace(marketplace_path)
    if not marketplace:
        log.debug("plugin_reconciler: no marketplace listing found — skipping reconciliation")
        return result

    listed_names: frozenset[str] = frozenset(e.name for e in marketplace)
    installed = get_installed_plugins(plugins_dir)

    for plugin_name in installed:
        if plugin_name in listed_names:
            result.kept.append(plugin_name)
        else:
            if dry_run:
                log.info(
                    "plugin_reconciler: [dry-run] would remove delisted plugin %r",
                    plugin_name,
                )
                result.removed.append(plugin_name)
            else:
                plugin_path = plugins_dir / plugin_name
                try:
                    shutil.rmtree(plugin_path)
                    log.info(
                        "plugin_reconciler: removed delisted plugin %r from %s",
                        plugin_name,
                        plugin_path,
                    )
                    result.removed.append(plugin_name)
                except OSError as exc:
                    err = f"Failed to remove plugin {plugin_name!r}: {exc}"
                    log.warning("plugin_reconciler: %s", err)
                    result.errors.append(err)

    if result.removed:
        log.info(
            "plugin_reconciler: removed %d delisted plugin(s): %s",
            len(result.removed),
            result.removed,
        )

    return result
