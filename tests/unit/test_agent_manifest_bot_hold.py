"""Bot config holds for agent-manifest==0.12.0."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_renovate_holds_agent_manifest_below_0_13() -> None:
    """Renovate must hold agent-manifest at 0.12.x until 0.15 compatibility is checked."""
    config: dict[str, Any] = json.loads((REPO_ROOT / "renovate.json").read_text(encoding="utf-8"))
    rules = [
        rule
        for rule in config.get("packageRules", [])
        if "agent-manifest" in rule.get("matchPackageNames", ()) and "pypi" in rule.get("matchDatasources", ())
    ]
    assert len(rules) == 1, f"expected one PyPI agent-manifest packageRule in renovate.json, found {len(rules)}"
    assert rules[0].get("allowedVersions") == "<0.13"


def test_dependabot_ignores_agent_manifest_0_13_and_above() -> None:
    """Dependabot uv updates must ignore agent-manifest >=0.13 while 0.12.0 is pinned."""
    config: dict[str, Any] = yaml.safe_load((REPO_ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    uv_entries = [entry for entry in config.get("updates", []) if entry.get("package-ecosystem") == "uv"]
    assert len(uv_entries) == 1, f"expected one uv update entry in dependabot.yml, found {len(uv_entries)}"
    ignores = [item for item in uv_entries[0].get("ignore", []) if item.get("dependency-name") == "agent-manifest"]
    assert len(ignores) == 1, f"expected one agent-manifest ignore in dependabot.yml, found {len(ignores)}"
    assert ignores[0].get("versions") == [">=0.13"]
