"""Change applicator - execute upgrades via file modification."""

from __future__ import annotations

import json
import logging
import shutil
import time
from typing import TYPE_CHECKING, Any, Protocol

import yaml

from bernstein.evolution.admission import AdmissionPolicy
from bernstein.evolution.proposals import UpgradeCategory, UpgradeProposal

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


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

    def __init__(
        self,
        state_dir: Path,
        admission: AdmissionPolicy | None = None,
    ) -> None:
        self.state_dir = state_dir
        self.upgrades_dir = state_dir / "upgrades"
        self.config_dir = state_dir / "config"
        self.upgrades_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)

        # The gate lives here rather than at each call site: both the
        # EvolutionLoop path and the older EvolutionEngine.execute_pending_upgrades
        # path reach the same executor, so one wiring covers both without
        # copying a gate into each.
        self._admission = admission if admission is not None else AdmissionPolicy()

        self._backup_files: dict[str, Path] = {}

    def execute_upgrade(self, proposal: UpgradeProposal) -> bool:
        """Execute an upgrade by applying configuration changes.

        Admission is checked first: a proposal whose producer has no measured
        history, or a poor one, does not reach the filesystem. The outcome is
        recorded only after the apply returns, against the same key admission
        used.
        """
        decision = self._admission.evaluate(proposal)
        if not decision.admitted:
            logger.warning(
                "Upgrade %s refused by admission policy: %s",
                proposal.id,
                decision.reason,
            )
            return False

        applied = False
        try:
            if proposal.category == UpgradeCategory.POLICY_UPDATE:
                applied = self._apply_policy_update(proposal)
            elif proposal.category == UpgradeCategory.ROUTING_RULES:
                applied = self._apply_routing_rules(proposal)
            elif proposal.category == UpgradeCategory.MODEL_ROUTING:
                applied = self._apply_model_routing(proposal)
            elif proposal.category == UpgradeCategory.PROVIDER_CONFIG:
                applied = self._apply_provider_config(proposal)
            else:
                # Role templates need special handling
                applied = self._apply_role_template(proposal)
        except Exception as exc:
            logger.exception("Failed to execute upgrade %s: %s", proposal.category, exc)
            applied = False

        # Recorded after the apply resolves, including the failure path: a gate
        # that only learns from successes cannot lower its opinion of a
        # producer that keeps breaking things.
        self._admission.record_outcome(decision, applied)
        return applied

    def rollback_upgrade(self, _proposal: UpgradeProposal) -> bool:
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
            f.write(
                json.dumps(
                    {
                        "proposal_id": proposal.id,
                        "title": proposal.title,
                        "category": proposal.category.value,
                        "change": proposal.proposed_change,
                        "applied_at": time.time(),
                        "status": status,
                    }
                )
                + "\n"
            )

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

    def _skip_no_sink(self, proposal: UpgradeProposal) -> bool:
        """Record a no-sink category as skipped and report it as not applied.

        Every category below resolves to a target nothing reads back: the three
        config categories appended the proposal to a ``pending_upgrades:`` key
        no subsystem consults, and role templates appended to a JSONL file
        outside ``.sdd/`` with no reader either. Those writes still returned
        ``True``, so both callers scored the proposal as a landed change and the
        offline loop closed its tracker issue saying so. Until a category has a
        real sink, the honest answer is that nothing was applied - but the
        decision stays auditable in ``history.jsonl`` under a status that says
        what actually happened.
        """
        self._record_history(proposal, "skipped_no_sink")
        return False

    def _apply_policy_update(self, proposal: UpgradeProposal) -> bool:
        """Apply a policy update to .sdd/config/policies.yaml.

        This category no longer has a valid sink, so the upgrade is not applied.
        """
        return self._skip_no_sink(proposal)

    def _apply_routing_rules(self, proposal: UpgradeProposal) -> bool:
        """Apply routing rule changes to .sdd/config/routing.yaml.

        This category no longer has a valid sink, so the upgrade is not applied.
        """
        return self._skip_no_sink(proposal)

    def _apply_model_routing(self, proposal: UpgradeProposal) -> bool:
        """Apply model routing changes (stored in routing.yaml).

        This category no longer has a valid sink, so the upgrade is not applied.
        """
        return self._skip_no_sink(proposal)

    def _apply_provider_config(self, proposal: UpgradeProposal) -> bool:
        """Apply provider configuration changes to .sdd/config/providers.yaml.

        This category no longer has a valid sink, so the upgrade is not applied.
        """
        return self._skip_no_sink(proposal)

    def _apply_role_template(self, proposal: UpgradeProposal) -> bool:
        """Record a role template upgrade proposal in the templates directory.

        This category no longer has a valid sink, so the upgrade is not applied.
        """
        return self._skip_no_sink(proposal)
