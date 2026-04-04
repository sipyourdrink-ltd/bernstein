"""Enterprise plugin allowlist/blocklist policy.

Reads ``.bernstein/plugins-policy.yaml`` in the project root and enforces
admin-controlled plugin access policies for enterprise deployments.

Policy schema (all fields optional):

.. code-block:: yaml

    version: "1"

    # If set, ONLY these plugins are allowed to load (allowlist mode).
    allowlist:
      - approved-plugin
      - audit-logger

    # These plugins are always rejected at load time.
    blocklist:
      - dangerous-plugin
      - untrusted-plugin

    # Managed plugins: admin-provided; always allowed and cannot be removed.
    managed:
      - audit-logger
      - compliance-reporter

Enforcement rules (in priority order):

1. A plugin on the blocklist is **always rejected**, even if also on the
   allowlist or managed list.
2. A managed plugin is **always allowed** (provided it is not blocked).
3. If the allowlist is non-empty, only listed plugins are permitted.
4. If no allowlist and no blocklist, all plugins are permitted (default).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

log = logging.getLogger(__name__)

_POLICY_FILE = "plugins-policy.yaml"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PluginPolicy:
    """Enterprise plugin access policy.

    Attributes:
        allowlist: If non-empty, only these plugin names are permitted.
        blocklist: Plugin names that are always rejected at load time.
        managed: Admin-provided plugin names that are always allowed.
    """

    allowlist: frozenset[str] = field(default_factory=frozenset)
    blocklist: frozenset[str] = field(default_factory=frozenset)
    managed: frozenset[str] = field(default_factory=frozenset)

    @property
    def is_empty(self) -> bool:
        """True when no restrictions are defined (permit-all)."""
        return not self.allowlist and not self.blocklist and not self.managed


class PluginPolicyViolation(Exception):
    """Raised when a plugin is rejected by the enterprise policy.

    Attributes:
        plugin_name: Name of the rejected plugin.
        reason: Human-readable reason for rejection.
    """

    def __init__(self, plugin_name: str, reason: str) -> None:
        super().__init__(f"Plugin {plugin_name!r} rejected by policy: {reason}")
        self.plugin_name = plugin_name
        self.reason = reason


# ---------------------------------------------------------------------------
# Policy loading
# ---------------------------------------------------------------------------


def load_plugin_policy(workdir: Path) -> PluginPolicy:
    """Load the plugin policy from ``.bernstein/plugins-policy.yaml``.

    Returns an empty (permit-all) policy if the file does not exist.

    Args:
        workdir: Project root directory containing ``.bernstein/``.

    Returns:
        The loaded :class:`PluginPolicy`.  Never raises for missing file.

    Raises:
        ValueError: If the policy file contains invalid YAML or unexpected
            structure (logged as a warning; returns empty policy on error).
    """
    policy_path = workdir / ".bernstein" / _POLICY_FILE
    if not policy_path.exists():
        return PluginPolicy()

    try:
        import yaml

        raw: Any = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    except ImportError:
        import json

        raw = json.loads(policy_path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Could not parse plugin policy %s: %s — using permit-all policy", policy_path, exc)
        return PluginPolicy()

    if not isinstance(raw, dict):
        log.warning("Plugin policy %s must be a YAML mapping — using permit-all policy", policy_path)
        return PluginPolicy()

    def _coerce_list(value: Any, field_name: str) -> frozenset[str]:
        if value is None:
            return frozenset()
        if not isinstance(value, list):
            log.warning("Plugin policy field %r must be a list — ignoring", field_name)
            return frozenset()
        return frozenset(str(item) for item in value if item)

    allowlist = _coerce_list(raw.get("allowlist"), "allowlist")
    blocklist = _coerce_list(raw.get("blocklist"), "blocklist")
    managed = _coerce_list(raw.get("managed"), "managed")

    policy = PluginPolicy(allowlist=allowlist, blocklist=blocklist, managed=managed)
    log.debug(
        "Loaded plugin policy from %s: %d allowed, %d blocked, %d managed",
        policy_path,
        len(allowlist),
        len(blocklist),
        len(managed),
    )
    return policy


# ---------------------------------------------------------------------------
# Policy enforcement
# ---------------------------------------------------------------------------


def check_plugin_allowed(plugin_name: str, policy: PluginPolicy) -> None:
    """Assert that *plugin_name* is permitted by *policy*.

    Args:
        plugin_name: The name of the plugin being registered.
        policy: The active :class:`PluginPolicy`.

    Raises:
        PluginPolicyViolation: When the plugin is blocked or not in the
            allowlist.
    """
    if policy.is_empty:
        return

    # Rule 1: blocklist always wins.
    if plugin_name in policy.blocklist:
        raise PluginPolicyViolation(plugin_name, "plugin is in the enterprise blocklist")

    # Rule 2: managed plugins bypass allowlist restrictions.
    if plugin_name in policy.managed:
        return

    # Rule 3: allowlist mode — reject anything not explicitly listed.
    if policy.allowlist and plugin_name not in policy.allowlist:
        raise PluginPolicyViolation(
            plugin_name,
            f"plugin is not in the enterprise allowlist ({len(policy.allowlist)} approved plugins)",
        )
