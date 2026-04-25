"""User MCP config preservation tests."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.protocols.mcp_catalog.manifest import CatalogEntry
from bernstein.core.protocols.mcp_catalog.user_config import (
    BERNSTEIN_MANAGED_KEY,
    SERVERS_KEY,
    install_entry,
    list_installed,
    touch_upgrade_check,
    uninstall_entry,
    upgrade_entry,
)


def _entry(version: str = "1.0.0", *, auto_upgrade: bool = False) -> CatalogEntry:
    return CatalogEntry(
        id="fs-readonly",
        name="FS",
        description="x",
        homepage="https://x",
        repository="https://x.git",
        install_command=("true",),
        version_pin=version,
        transports=("stdio",),
        verified_by_bernstein=True,
        auto_upgrade=auto_upgrade,
        command="node",
        args=("./server.js",),
        env={"FS_ROOT": "/tmp"},
    )


def test_install_writes_managed_block_only(tmp_path: Path) -> None:
    cfg = tmp_path / "mcp.json"
    cfg.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "manual-thing": {"command": "user", "args": ["x"]},
                },
                "userPrefs": {"theme": "dark"},
            },
            indent=2,
        )
    )

    entry = _entry()
    install_entry(cfg, entry)

    payload = json.loads(cfg.read_text())
    assert "userPrefs" in payload
    assert payload["userPrefs"] == {"theme": "dark"}
    assert payload["mcpServers"]["manual-thing"] == {"command": "user", "args": ["x"]}
    managed = payload[BERNSTEIN_MANAGED_KEY][SERVERS_KEY]
    assert "fs-readonly" in managed
    assert managed["fs-readonly"]["version_pin"] == "1.0.0"
    assert managed["fs-readonly"]["command"] == "node"


def test_uninstall_removes_only_block_entry(tmp_path: Path) -> None:
    cfg = tmp_path / "mcp.json"
    cfg.write_text(
        json.dumps(
            {
                "mcpServers": {"manual-thing": {"command": "user"}},
                "notes": "edited by hand",
            }
        )
    )
    install_entry(cfg, _entry())
    assert uninstall_entry(cfg, "fs-readonly") is True

    payload = json.loads(cfg.read_text())
    assert payload.get(BERNSTEIN_MANAGED_KEY) is None
    assert payload["mcpServers"] == {"manual-thing": {"command": "user"}}
    assert payload["notes"] == "edited by hand"


def test_upgrade_updates_pin_preserves_installed_at(tmp_path: Path) -> None:
    cfg = tmp_path / "mcp.json"
    install_entry(cfg, _entry("1.0.0"))
    pre = list_installed(cfg)[0]

    upgraded = upgrade_entry(cfg, _entry("1.1.0"))
    assert upgraded is not None
    assert upgraded.version_pin == "1.1.0"
    assert upgraded.installed_at == pre.installed_at
    assert upgraded.last_upgrade_check != ""


def test_touch_upgrade_check_only_changes_timestamp(tmp_path: Path) -> None:
    cfg = tmp_path / "mcp.json"
    install_entry(cfg, _entry("1.0.0"))
    touch_upgrade_check(cfg, "fs-readonly")
    after = list_installed(cfg)[0]
    assert after.version_pin == "1.0.0"
    assert after.last_upgrade_check != ""


def test_list_installed_handles_missing_file(tmp_path: Path) -> None:
    cfg = tmp_path / "missing.json"
    assert list_installed(cfg) == []
