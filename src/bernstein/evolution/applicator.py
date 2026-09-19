"""Change applicator - execute upgrades via file modification."""

from __future__ import annotations

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
        """Restore the files *proposal* changed, from the record left at apply time.

        Driven by the proposal and by what was written to disk, never by
        process-local state. The previous version restored from
        ``self._backup_files``, a dictionary populated during the same process
        that applied the change: a rollback after a restart found it empty,
        swallowed the emptiness, and returned ``True``. It also ignored its own
        argument, so it could not roll back a NAMED proposal even in the
        process that applied one.

        The three answers this can give are kept distinct, because the
        dangerous one used to be indistinguishable from success:

        * nothing was applied for this proposal, so there is nothing to undo -
          ``True``, and that is the honest answer rather than a convenient one;
        * a manifest exists and every file in it was restored - ``True``;
        * a manifest is missing for an apply that happened, or a restore
          failed - :class:`RollbackError`. A rollback that reports success for
          work it did not do is worse than one that fails, because the caller
          stops looking.

        Args:
            proposal: The proposal to undo.

        Returns:
            ``True`` when the tree is in the state that preceded *proposal*.

        Raises:
            RollbackError: The files this proposal changed could not be put
                back, or the record needed to put them back is gone.
        """
        manifest = self._backup_manifest_path(proposal.id)
        if not manifest.exists():
            # No record. Either nothing was applied - the common case today,
            # since every category resolves to `_skip_no_sink` - or the record
            # was lost. `history.jsonl` is what tells those apart, and getting
            # it wrong in the reassuring direction is the defect above.
            if self._was_applied(proposal.id):
                raise RollbackError(
                    f"proposal {proposal.id} was applied but no backup manifest exists at "
                    f"{manifest}; the files it changed cannot be restored from here"
                )
            self._write_rollback_receipt(proposal, restored=[], note="nothing was applied")
            return True

        recorded = self._read_backup_manifest(manifest)
        wanted = self._files_to_restore(proposal, recorded)
        restored: list[str] = []
        try:
            for filename in wanted:
                backup_path = self.upgrades_dir / recorded[filename]
                if not backup_path.exists():
                    raise RollbackError(
                        f"proposal {proposal.id} recorded a backup for {filename!r} at "
                        f"{backup_path}, and it is not there"
                    )
                shutil.copy2(backup_path, self.config_dir / filename)
                restored.append(filename)
        except OSError as exc:
            # Partial restore named rather than hidden: the operator has to know
            # WHICH files are back and which are not, or the tree's state is
            # unknown to everyone.
            raise RollbackError(f"proposal {proposal.id} rollback failed after restoring {restored}: {exc}") from exc

        self._record_history(proposal, "rolled_back")
        self._write_rollback_receipt(proposal, restored=restored, note="restored from the apply-time manifest")
        return True

    # ------------------------------------------------------------------
    # Rollback bookkeeping
    # ------------------------------------------------------------------

    def _backup_dir(self, proposal_id: str) -> Path:
        """Where this proposal's backups live. Keyed by proposal, not by clock.

        The old path embedded ``int(time.time())``, which made a backup
        un-addressable by anything but the in-memory map that recorded it.
        """
        return self.upgrades_dir / "backups" / proposal_id

    def _backup_manifest_path(self, proposal_id: str) -> Path:
        return self._backup_dir(proposal_id) / "manifest.json"

    def _read_backup_manifest(self, manifest: Path) -> dict[str, str]:
        """``{config-relative filename: backup path relative to upgrades_dir}``."""
        try:
            raw = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RollbackError(f"backup manifest at {manifest} is unreadable: {exc}") from exc
        if not isinstance(raw, dict):
            raise RollbackError(f"backup manifest at {manifest} is not an object")
        return {str(k): str(v) for k, v in raw.items()}

    def _files_to_restore(self, proposal: UpgradeProposal, recorded: dict[str, str]) -> list[str]:
        """The files to put back: what the apply recorded backing up.

        NOT ``proposal.rollback_plan``. This executor takes
        ``evolution.proposals.UpgradeProposal``, whose ``rollback_plan`` is a
        :class:`RollbackPlan` of operator-facing ``steps`` - prose, with no
        machine-readable file list. The ``ChangeContract.rollback
        .files_to_restore`` that WOULD name files belongs to the other
        ``UpgradeProposal``, in ``evolution.types``, which never reaches here.
        Reading a file list off this proposal is therefore not possible today,
        and inventing one from the prose would be worse than using the record
        the apply actually left.

        The manifest is keyed by proposal id and written at apply time, so it
        is per-proposal and survives a restart - which is what the process-local
        map failed at. When the two proposal types are reconciled, the declared
        inverse becomes the authority and this becomes the check on it.
        """
        _ = proposal
        return sorted(recorded)

    def _was_applied(self, proposal_id: str) -> bool:
        """Did any history row for this proposal record an APPLIED change?

        Read rather than remembered, so this answers the same way in a process
        that did not perform the apply.
        """
        history_file = self.upgrades_dir / "history.jsonl"
        if not history_file.exists():
            return False
        applied = False
        for line in history_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                # A torn final line is not evidence either way, and refusing to
                # read the whole file because of one would turn a truncated
                # journal into an un-rollbackable tree.
                continue
            if row.get("proposal_id") != proposal_id:
                continue
            status = row.get("status")
            if status == "applied":
                applied = True
            elif status == "rolled_back":
                applied = False
        return applied

    def _write_rollback_receipt(self, proposal: UpgradeProposal, *, restored: list[str], note: str) -> Path:
        """Persist a hash-anchored record of what this rollback did.

        The same shape the verdict receipt uses (``change_contract_replay``):
        a body, and a sha256 over its canonical JSON, so a reader holding only
        the file can tell whether it has been edited since it was written.
        Rollback used to record nothing at all, so "was this rolled back, and
        what did that restore" had no answer after the process exited.
        """
        body: dict[str, Any] = {
            "proposal_id": proposal.id,
            "title": proposal.title,
            "category": proposal.category.value,
            "restored_files": sorted(restored),
            "note": note,
            "rolled_back_at": time.time(),
        }
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        receipt = body | {"receipt_hash": hashlib.sha256(canonical).hexdigest()}
        out_path = self.upgrades_dir / "rollbacks" / f"{proposal.id}.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic_text(out_path, json.dumps(receipt, indent=2, ensure_ascii=False))
        return out_path

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

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

    def _backup_file(self, filename: str, proposal_id: str) -> None:
        """Copy a config file aside before modifying it, and RECORD that it did.

        The record is the point. The previous version wrote the copy to a
        clock-named path and remembered the mapping in an instance attribute,
        so the bytes survived a restart and nothing could find them. Here the
        path is derived from the proposal id and the mapping is written to a
        manifest beside it, which is what lets `rollback_upgrade` work in a
        process that did not perform the apply.
        """
        source_path = self.config_dir / filename
        if not source_path.exists():
            return
        backup_dir = self._backup_dir(proposal_id)
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / filename
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, backup_path)

        manifest = self._backup_manifest_path(proposal_id)
        recorded = self._read_backup_manifest(manifest) if manifest.exists() else {}
        recorded[filename] = str(backup_path.relative_to(self.upgrades_dir))
        write_atomic_text(manifest, json.dumps(recorded, indent=2, sort_keys=True))

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
