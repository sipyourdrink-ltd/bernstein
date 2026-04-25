"""User MCP config IO with a ``bernstein-managed`` block.

Bernstein writes catalog-installed entries inside a ``bernstein-managed``
block of the user's MCP config. Manual edits outside that block are
preserved verbatim (acceptance criterion). Uninstall removes only the
block; if a user moved an entry out of the block they keep ownership.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bernstein.core.protocols.mcp_catalog.manifest import CatalogEntry

#: Top-level key into which Bernstein-installed entries are nested.
BERNSTEIN_MANAGED_KEY = "bernstein-managed"

#: Inner key listing actual MCP servers (mirrors the conventional
#: ``mcpServers`` field used by Claude Desktop / Codex / etc.).
SERVERS_KEY = "mcpServers"


@dataclass(frozen=True)
class InstalledEntry:
    """A catalog-installed entry as recorded in the user's MCP config.

    Attributes:
        id: Catalog id.
        name: Display name copied from the catalog.
        version_pin: Version recorded at install time.
        installed_at: ISO-8601 UTC string when the entry was installed.
        last_upgrade_check: ISO-8601 UTC string of the last successful
            upgrade check; empty when never checked.
        auto_upgrade: Whether ``upgrade`` may auto-apply new versions.
        verified_by_bernstein: Whether the source entry was verified.
        command: Executable launched at runtime.
        args: Argv passed to ``command``.
        env: Environment variables.
        signature: Optional signature copied from the catalog.
    """

    id: str
    name: str
    version_pin: str
    installed_at: str
    last_upgrade_check: str = ""
    auto_upgrade: bool = False
    verified_by_bernstein: bool = False
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    signature: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise to JSON-friendly dict."""
        out: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "version_pin": self.version_pin,
            "installed_at": self.installed_at,
            "last_upgrade_check": self.last_upgrade_check,
            "auto_upgrade": self.auto_upgrade,
            "verified_by_bernstein": self.verified_by_bernstein,
        }
        if self.command is not None:
            out["command"] = self.command
        if self.args:
            out["args"] = list(self.args)
        if self.env:
            out["env"] = dict(self.env)
        if self.signature is not None:
            out["signature"] = self.signature
        return out

    @classmethod
    def from_dict(cls, raw: Any) -> InstalledEntry | None:
        """Parse an installed-entry record. Returns ``None`` on bad input."""
        if not isinstance(raw, dict):
            return None
        try:
            entry_id = str(raw["id"])
            name = str(raw.get("name", entry_id))
            version_pin = str(raw["version_pin"])
            installed_at = str(raw.get("installed_at", ""))
        except (KeyError, TypeError):
            return None
        args_value = raw.get("args", [])
        args = tuple(str(item) for item in args_value) if isinstance(args_value, list) else ()
        env_value = raw.get("env", {})
        env: dict[str, str] = {str(k): str(v) for k, v in env_value.items()} if isinstance(env_value, dict) else {}
        return cls(
            id=entry_id,
            name=name,
            version_pin=version_pin,
            installed_at=installed_at,
            last_upgrade_check=str(raw.get("last_upgrade_check", "")),
            auto_upgrade=bool(raw.get("auto_upgrade", False)),
            verified_by_bernstein=bool(raw.get("verified_by_bernstein", False)),
            command=str(raw["command"]) if raw.get("command") is not None else None,
            args=args,
            env=env,
            signature=str(raw["signature"]) if raw.get("signature") is not None else None,
        )


def default_user_config_path() -> Path:
    """Default user MCP config path under ``~/.config/bernstein``.

    Honours ``XDG_CONFIG_HOME`` when set.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "bernstein" / "mcp.json"


def _load_raw(path: Path) -> dict[str, Any]:
    """Read the user MCP config file, returning ``{}`` when absent."""
    if not path.exists():
        return {}
    try:
        text = path.read_text()
    except OSError:
        return {}
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _ensure_managed_block(config: dict[str, Any]) -> dict[str, Any]:
    """Return the bernstein-managed block, creating it if missing."""
    block = config.get(BERNSTEIN_MANAGED_KEY)
    if not isinstance(block, dict):
        block = {}
        config[BERNSTEIN_MANAGED_KEY] = block
    servers = block.get(SERVERS_KEY)
    if not isinstance(servers, dict):
        block[SERVERS_KEY] = {}
    return block


def list_installed(path: Path) -> list[InstalledEntry]:
    """List catalog-installed entries from the user config."""
    config = _load_raw(path)
    block = config.get(BERNSTEIN_MANAGED_KEY)
    if not isinstance(block, dict):
        return []
    servers = block.get(SERVERS_KEY)
    if not isinstance(servers, dict):
        return []
    out: list[InstalledEntry] = []
    for server_id, server_data in servers.items():
        record = dict(server_data) if isinstance(server_data, dict) else {}
        record.setdefault("id", server_id)
        installed = InstalledEntry.from_dict(record)
        if installed is not None:
            out.append(installed)
    return sorted(out, key=lambda entry: entry.id)


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON atomically: tempfile + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp.replace(path)


def install_entry(
    path: Path,
    entry: CatalogEntry,
    *,
    auto_upgrade: bool | None = None,
    now: datetime | None = None,
) -> InstalledEntry:
    """Write a catalog entry into the bernstein-managed block.

    Args:
        path: User config path.
        entry: Catalog entry to install.
        auto_upgrade: Override ``entry.auto_upgrade``. ``None`` keeps the
            manifest value.
        now: Override the wall clock (testing only).

    Returns:
        The :class:`InstalledEntry` recorded in the config.
    """
    config = _load_raw(path)
    block = _ensure_managed_block(config)
    servers: dict[str, Any] = block[SERVERS_KEY]

    timestamp = (now or datetime.now(tz=UTC)).isoformat()
    installed = InstalledEntry(
        id=entry.id,
        name=entry.name,
        version_pin=entry.version_pin,
        installed_at=timestamp,
        last_upgrade_check=timestamp,
        auto_upgrade=entry.auto_upgrade if auto_upgrade is None else auto_upgrade,
        verified_by_bernstein=entry.verified_by_bernstein,
        command=entry.command,
        args=entry.args,
        env=dict(entry.env),
        signature=entry.signature,
    )
    servers[entry.id] = installed.to_dict()
    _atomic_write(path, config)
    return installed


def upgrade_entry(
    path: Path,
    entry: CatalogEntry,
    *,
    now: datetime | None = None,
) -> InstalledEntry | None:
    """Update an existing managed entry's version_pin / metadata.

    Returns ``None`` when the entry is not installed.
    """
    config = _load_raw(path)
    block = config.get(BERNSTEIN_MANAGED_KEY)
    if not isinstance(block, dict):
        return None
    servers = block.get(SERVERS_KEY)
    if not isinstance(servers, dict) or entry.id not in servers:
        return None

    existing_raw = servers[entry.id]
    existing = InstalledEntry.from_dict(existing_raw) if isinstance(existing_raw, dict) else None
    timestamp = (now or datetime.now(tz=UTC)).isoformat()
    upgraded = InstalledEntry(
        id=entry.id,
        name=entry.name,
        version_pin=entry.version_pin,
        installed_at=existing.installed_at if existing else timestamp,
        last_upgrade_check=timestamp,
        auto_upgrade=existing.auto_upgrade if existing else entry.auto_upgrade,
        verified_by_bernstein=entry.verified_by_bernstein,
        command=entry.command,
        args=entry.args,
        env=dict(entry.env),
        signature=entry.signature,
    )
    servers[entry.id] = upgraded.to_dict()
    _atomic_write(path, config)
    return upgraded


def touch_upgrade_check(
    path: Path,
    entry_id: str,
    *,
    now: datetime | None = None,
) -> bool:
    """Record a successful upgrade check timestamp without changing version.

    Returns True when the entry exists and was updated.
    """
    config = _load_raw(path)
    block = config.get(BERNSTEIN_MANAGED_KEY)
    if not isinstance(block, dict):
        return False
    servers = block.get(SERVERS_KEY)
    if not isinstance(servers, dict) or entry_id not in servers:
        return False
    record = servers[entry_id]
    if not isinstance(record, dict):
        return False
    record["last_upgrade_check"] = (now or datetime.now(tz=UTC)).isoformat()
    _atomic_write(path, config)
    return True


def uninstall_entry(path: Path, entry_id: str) -> bool:
    """Remove an entry from the bernstein-managed block.

    Returns True when an entry was removed; False when it wasn't there.
    The bernstein-managed block is removed when emptied so manual edits
    elsewhere are preserved verbatim.
    """
    config = _load_raw(path)
    block = config.get(BERNSTEIN_MANAGED_KEY)
    if not isinstance(block, dict):
        return False
    servers = block.get(SERVERS_KEY)
    if not isinstance(servers, dict) or entry_id not in servers:
        return False
    del servers[entry_id]
    if not servers:
        block.pop(SERVERS_KEY, None)
    if not block:
        config.pop(BERNSTEIN_MANAGED_KEY, None)
    _atomic_write(path, config)
    return True


__all__ = [
    "BERNSTEIN_MANAGED_KEY",
    "SERVERS_KEY",
    "InstalledEntry",
    "default_user_config_path",
    "install_entry",
    "list_installed",
    "touch_upgrade_check",
    "uninstall_entry",
    "upgrade_entry",
]
