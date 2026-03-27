"""Change applicator — execute upgrades via file modification."""
from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

import yaml

from bernstein.evolution.proposals import UpgradeCategory, UpgradeProposal


class UpgradeExecutor(Protocol):
    """Protocol for executing upgrades."""

    def execute_upgrade(self, proposal: UpgradeProposal) -> bool:
        """Execute an upgrade proposal. Returns True if successful."""
        ...

    def rollback_upgrade(self, proposal: UpgradeProposal) -> bool:
        """Rollback an upgrade. Returns True if successful."""
        ...


class FileUpgradeExecutor:
    """
    Executes upgrades by modifying files.

    Supports atomic file writes with rollback capability.
    """

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.upgrades_dir = state_dir / "upgrades"
        self.config_dir = state_dir / "config"
        self.upgrades_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)

        self._backup_files: dict[str, Path] = {}

    def execute_upgrade(self, proposal: UpgradeProposal) -> bool:
        """Execute an upgrade by applying configuration changes."""
        try:
            if proposal.category == UpgradeCategory.POLICY_UPDATE:
                return self._apply_policy_update(proposal)
            elif proposal.category == UpgradeCategory.ROUTING_RULES:
                return self._apply_routing_rules(proposal)
            elif proposal.category == UpgradeCategory.MODEL_ROUTING:
                return self._apply_model_routing(proposal)
            elif proposal.category == UpgradeCategory.PROVIDER_CONFIG:
                return self._apply_provider_config(proposal)
            else:
                # Role templates need special handling
                return self._apply_role_template(proposal)
        except Exception as exc:
            logger.exception("Failed to execute upgrade %s: %s", proposal.category, exc)
            return False

    def rollback_upgrade(self, proposal: UpgradeProposal) -> bool:
        """Rollback an upgrade by restoring backup files."""
        try:
            for backup_key, backup_path in self._backup_files.items():
                if backup_path.exists():
                    target_path = self.config_dir / backup_key
                    shutil.copy2(backup_path, target_path)
                    backup_path.unlink()
            self._backup_files.clear()
            return True
        except Exception as exc:
            logger.exception("Failed to rollback upgrade: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _read_yaml(self, file_path: Path) -> dict[str, Any]:
        """Read a YAML file; return empty dict if missing or empty."""
        if not file_path.exists():
            return {}
        with file_path.open() as f:
            return yaml.safe_load(f) or {}

    def _atomic_write(self, file_path: Path, data: dict[str, Any]) -> None:
        """Write *data* to *file_path* atomically (tmp + rename)."""
        file_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = file_path.with_suffix(file_path.suffix + ".tmp")
        with tmp_path.open("w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        tmp_path.rename(file_path)

    def _record_history(self, proposal: UpgradeProposal, status: str) -> None:
        """Append an upgrade record to history.jsonl."""
        history_file = self.upgrades_dir / "history.jsonl"
        with history_file.open("a") as f:
            f.write(json.dumps({
                "proposal_id": proposal.id,
                "title": proposal.title,
                "category": proposal.category.value,
                "change": proposal.proposed_change,
                "applied_at": time.time(),
                "status": status,
            }) + "\n")

    def _backup_file(self, filename: str) -> None:
        """Create a backup copy of a config file before modifying it."""
        source_path = self.config_dir / filename
        backup_path = self.upgrades_dir / f"backup_{filename}_{int(time.time())}"
        if source_path.exists():
            shutil.copy2(source_path, backup_path)
            self._backup_files[filename] = backup_path

    # ------------------------------------------------------------------
    # Category-specific apply methods
    # ------------------------------------------------------------------

    def _apply_policy_update(self, proposal: UpgradeProposal) -> bool:
        """Apply a policy update to .sdd/config/policies.yaml."""
        config_file = self.config_dir / "policies.yaml"
        self._backup_file("policies.yaml")

        data = self._read_yaml(config_file)

        # Append a proposed-upgrade entry so the orchestrator can act on it
        pending: list[dict[str, Any]] = data.get("pending_upgrades", [])
        pending.append({
            "id": proposal.id,
            "title": proposal.title,
            "change": proposal.proposed_change,
            "confidence": proposal.confidence,
            "applied_at": time.time(),
        })
        data["pending_upgrades"] = pending

        self._atomic_write(config_file, data)
        self._record_history(proposal, "applied")
        return True

    def _apply_routing_rules(self, proposal: UpgradeProposal) -> bool:
        """Apply routing rule changes to .sdd/config/routing.yaml."""
        config_file = self.config_dir / "routing.yaml"
        self._backup_file("routing.yaml")

        data = self._read_yaml(config_file)

        pending: list[dict[str, Any]] = data.get("pending_upgrades", [])
        pending.append({
            "id": proposal.id,
            "title": proposal.title,
            "change": proposal.proposed_change,
            "confidence": proposal.confidence,
            "applied_at": time.time(),
        })
        data["pending_upgrades"] = pending

        self._atomic_write(config_file, data)
        self._record_history(proposal, "applied")
        return True

    def _apply_model_routing(self, proposal: UpgradeProposal) -> bool:
        """Apply model routing changes (stored in routing.yaml)."""
        return self._apply_routing_rules(proposal)

    def _apply_provider_config(self, proposal: UpgradeProposal) -> bool:
        """Apply provider configuration changes to .sdd/config/providers.yaml."""
        config_file = self.config_dir / "providers.yaml"
        self._backup_file("providers.yaml")

        data = self._read_yaml(config_file)

        pending: list[dict[str, Any]] = data.get("pending_upgrades", [])
        pending.append({
            "id": proposal.id,
            "title": proposal.title,
            "change": proposal.proposed_change,
            "confidence": proposal.confidence,
            "applied_at": time.time(),
        })
        data["pending_upgrades"] = pending

        self._atomic_write(config_file, data)
        self._record_history(proposal, "applied")
        return True

    def _apply_role_template(self, proposal: UpgradeProposal) -> bool:
        """Record a role template upgrade proposal in the templates directory."""
        templates_dir = self.state_dir.parent / "templates" / "roles"
        templates_dir.mkdir(parents=True, exist_ok=True)

        proposals_file = templates_dir / "PROPOSED_UPGRADES.jsonl"
        with proposals_file.open("a") as f:
            f.write(json.dumps({
                "id": proposal.id,
                "title": proposal.title,
                "change": proposal.proposed_change,
                "confidence": proposal.confidence,
                "applied_at": time.time(),
            }) + "\n")

        self._record_history(proposal, "applied")
        return True
