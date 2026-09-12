"""Change applicator - execute upgrades via file modification."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import shutil
import time
from typing import TYPE_CHECKING, Any, Protocol

import yaml

from bernstein.core.persistence.atomic_write import write_atomic_text
from bernstein.evolution.admission import AdmissionPolicy
from bernstein.evolution.proposals import UpgradeCategory, UpgradeProposal
from bernstein.evolution.types import RollbackError

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pathlib import Path


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

    def rollback_upgrade(self, proposal: UpgradeProposal) -> bool:
        """Restore files declared in the proposal rollback plan.

        Uses the proposal's rollback_plan.steps to determine rollback operations
        on the proposal's target_files, restoring from backups in upgrades_dir.
        Raises RollbackError on any failure.
        """
        manifest = self._read_manifest()
        errors: list[str] = []

        # Interpret rollback_plan.steps as file revert operations
        for filename in proposal.target_files:
            relative_path = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            target_path = self.config_dir / relative_path
            backup_path = self.upgrades_dir / f"backup_{relative_path}"

            # Check manifest for expected backup hash
            manifest_key = f"backup_{relative_path}"
            if manifest_key in manifest:
                expected_hash = manifest[manifest_key]["hash"]
                try:
                    actual_hash = self._hash_file(backup_path)
                    if actual_hash != expected_hash:
                        errors.append(
                            f"Backup integrity check failed for {relative_path}: "
                            f"expected {expected_hash[:8]}, got {actual_hash[:8]}"
                        )
                        continue
                except OSError as e:
                    errors.append(f"Backup integrity check failed: {backup_path} ({e})")
                    continue
            else:
                # No manifest entry yet — capture the current backup hash as the
                # baseline so any subsequent corruption is detected on the next
                # rollback call. This is the only place the manifest gains a
                # key outside `_backup_file`, and only on first observation.
                if backup_path.exists():
                    with contextlib.suppress(OSError):
                        manifest[manifest_key] = {
                            "hash": self._hash_file(backup_path),
                            "created_at": time.time(),
                        }

            # Validate backup exists
            if not backup_path.exists():
                errors.append(f"Backup not found: {backup_path}")
                continue

            # Validate backup is readable
            try:
                backup_path.read_text()
            except OSError as e:
                errors.append(f"Backup unreadable: {backup_path} ({e})")
                continue

            # Perform the rollback
            try:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup_path, target_path)
            except Exception as exc:
                errors.append(f"Failed to restore {target_path} from {backup_path}: {exc}")

        # Persist any baseline hashes captured above so future rollbacks can
        # detect backup corruption.
        self._write_manifest(manifest)

        if errors:
            error_msg = "; ".join(errors)
            logger.error("Rollback failed for %s: %s", proposal.id, error_msg)
            self._record_history(proposal, "rolled_back")
            raise RollbackError(proposal.id) from Exception(error_msg)

        self._record_history(proposal, "rolled_back")
        return True

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _hash_file(self, file_path: Path) -> str:
        """Compute SHA-256 hash of a file."""
        h = hashlib.sha256()
        with file_path.open("rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()

    def _read_manifest(self) -> dict[str, Any]:
        """Read backup manifest from upgrades_dir."""
        manifest_path = self.upgrades_dir / "backup_manifest.json"
        if not manifest_path.exists():
            return {}
        try:
            with manifest_path.open() as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def _write_manifest(self, manifest: dict[str, Any]) -> None:
        """Write backup manifest to upgrades_dir."""
        manifest_path = self.upgrades_dir / "backup_manifest.json"
        with manifest_path.open("w") as f:
            json.dump(manifest, f)

    def _read_yaml(self, file_path: Path) -> dict[str, Any]:
        """Read a YAML file; return empty dict if missing or empty."""
        if not file_path.exists():
            return {}
        with file_path.open(encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def _atomic_write(self, file_path: Path, data: dict[str, Any]) -> None:
        """Write *data* to *file_path* through the crash-safe write path.

        Three things the previous local version got wrong. It renamed with
        ``Path.rename``, which on Windows raises ``FileExistsError`` when
        the destination exists rather than replacing it, so rewriting a
        proposal failed outright there. It never called ``fsync``. And it
        opened the temporary with no encoding, so the YAML was written in
        the host locale rather than UTF-8.
        """
        write_atomic_text(file_path, yaml.dump(data, default_flow_style=False, sort_keys=False))

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
        relative_path = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        source_path = self.config_dir / relative_path
        backup_path = self.upgrades_dir / f"backup_{relative_path}"
        if source_path.exists():
            shutil.copy2(source_path, backup_path)
            # Update manifest with backup hash
            manifest = self._read_manifest()
            manifest[f"backup_{relative_path}"] = {
                "hash": self._hash_file(backup_path),
                "created_at": time.time(),
            }
            self._write_manifest(manifest)

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
