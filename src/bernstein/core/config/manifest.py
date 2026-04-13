"""Agent Run Manifest — hashable, verifiable configuration record for each run.

Every ``bernstein run`` generates a manifest that captures the complete
configuration of that orchestration run.  The manifest is:

- **Immutable**: written once at run start, never mutated.
- **Hashable**: a SHA-256 digest over canonical JSON uniquely identifies the
  run configuration, satisfying SOC2/ISO 27001 evidence requirements.
- **Diffable**: ``bernstein manifest diff <a> <b>`` highlights configuration
  changes between two runs.

Storage: ``.sdd/runtime/manifests/<run-id>.json``
"""

from __future__ import annotations

import getpass
import hashlib
import json
import logging
import subprocess
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bernstein.core.models import OrchestratorConfig

logger = logging.getLogger(__name__)


def _git_head_sha() -> str:
    """Return current HEAD commit SHA, or empty string on failure."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            timeout=5,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return ""


# ---------------------------------------------------------------------------
# RunManifest dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApprovalGateConfig:
    """Which phases require human sign-off."""

    mode: str = "auto"  # "auto" | "review" | "pr"
    plan_mode: bool = False


@dataclass(frozen=True)
class ModelRoutingPolicy:
    """Model routing constraints for the run."""

    default_model: str | None = None
    allowed_providers: tuple[str, ...] = ()
    denied_providers: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentAdapterConfig:
    """Which CLI agent adapter is used and its version."""

    cli: str = "auto"
    model: str | None = None
    max_agents: int = 6
    max_tasks_per_agent: int = 1


@dataclass(frozen=True)
class Provenance:
    """Who triggered the run, when, from which commit."""

    triggered_by: str = ""
    triggered_at_iso: str = ""
    commit_sha: str = ""


@dataclass(frozen=True)
class RunManifest:
    """Complete configuration record for an orchestration run.

    Written to ``.sdd/runtime/manifests/<run-id>.json`` at the start of
    every run.  The ``manifest_hash`` field is a SHA-256 digest over the
    canonical JSON serialization of all other fields, uniquely identifying
    this configuration.

    Attributes:
        run_id: Unique run identifier (timestamp-based).
        workflow_definition_hash: SHA-256 of the governed workflow definition
            (empty string when running in adaptive mode).
        workflow_name: Name of the governed workflow, if any.
        model_routing: Model routing policy for the run.
        budget_ceiling_usd: Maximum spend allowed (0 = unlimited).
        approval_gates: Approval gate configuration.
        agent_adapter: CLI agent adapter configuration.
        provenance: Who/when/what-commit triggered the run.
        orchestrator_config: Snapshot of OrchestratorConfig values.
        manifest_hash: SHA-256 digest computed over all other fields.
    """

    run_id: str
    workflow_definition_hash: str = ""
    workflow_name: str = ""
    model_routing: ModelRoutingPolicy = field(default_factory=ModelRoutingPolicy)
    budget_ceiling_usd: float = 0.0
    approval_gates: ApprovalGateConfig = field(default_factory=ApprovalGateConfig)
    agent_adapter: AgentAdapterConfig = field(default_factory=AgentAdapterConfig)
    provenance: Provenance = field(default_factory=Provenance)
    orchestrator_config: dict[str, Any] = field(default_factory=dict)
    manifest_hash: str = ""

    # ------------------------------------------------------------------
    # Canonical JSON & hashing
    # ------------------------------------------------------------------

    def _canonical_payload(self) -> dict[str, Any]:
        """Return the dict used for canonical JSON serialization.

        ``manifest_hash`` is excluded — it is *derived* from this payload.
        """
        d = asdict(self)
        d.pop("manifest_hash", None)
        return d

    def canonical_json(self) -> str:
        """Deterministic JSON string (sorted keys, compact separators)."""
        return json.dumps(
            self._canonical_payload(),
            sort_keys=True,
            separators=(",", ":"),
        )

    def compute_hash(self) -> str:
        """Compute SHA-256 over the canonical JSON payload."""
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()

    # ------------------------------------------------------------------
    # Serialization helpers
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Full dict including the manifest_hash."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunManifest:
        """Reconstruct a RunManifest from a saved dict."""
        return cls(
            run_id=str(data.get("run_id", "")),
            workflow_definition_hash=str(data.get("workflow_definition_hash", "")),
            workflow_name=str(data.get("workflow_name", "")),
            model_routing=ModelRoutingPolicy(
                default_model=data.get("model_routing", {}).get("default_model"),
                allowed_providers=tuple(data.get("model_routing", {}).get("allowed_providers", ())),
                denied_providers=tuple(data.get("model_routing", {}).get("denied_providers", ())),
            ),
            budget_ceiling_usd=float(data.get("budget_ceiling_usd", 0.0)),
            approval_gates=ApprovalGateConfig(
                mode=str(data.get("approval_gates", {}).get("mode", "auto")),
                plan_mode=bool(data.get("approval_gates", {}).get("plan_mode", False)),
            ),
            agent_adapter=AgentAdapterConfig(
                cli=str(data.get("agent_adapter", {}).get("cli", "auto")),
                model=data.get("agent_adapter", {}).get("model"),
                max_agents=int(data.get("agent_adapter", {}).get("max_agents", 6)),
                max_tasks_per_agent=int(data.get("agent_adapter", {}).get("max_tasks_per_agent", 1)),
            ),
            provenance=Provenance(
                triggered_by=str(data.get("provenance", {}).get("triggered_by", "")),
                triggered_at_iso=str(data.get("provenance", {}).get("triggered_at_iso", "")),
                commit_sha=str(data.get("provenance", {}).get("commit_sha", "")),
            ),
            orchestrator_config=dict(data.get("orchestrator_config", {})),
            manifest_hash=str(data.get("manifest_hash", "")),
        )


# ---------------------------------------------------------------------------
# Factory: build a manifest from current orchestrator state
# ---------------------------------------------------------------------------


def build_manifest(
    *,
    run_id: str,
    config: OrchestratorConfig,
    cli: str = "auto",
    model: str | None = None,
    workflow_name: str = "",
    workflow_definition_hash: str = "",
) -> RunManifest:
    """Build a RunManifest from orchestrator configuration.

    Call this at the very start of a run, before any tasks execute.
    The returned manifest has ``manifest_hash`` populated.
    """
    from datetime import UTC, datetime

    # Build model routing from config
    model_routing = ModelRoutingPolicy(
        default_model=model,
    )

    approval_gates = ApprovalGateConfig(
        mode=config.approval,
        plan_mode=config.plan_mode,
    )

    agent_adapter = AgentAdapterConfig(
        cli=cli,
        model=model,
        max_agents=config.max_agents,
        max_tasks_per_agent=config.max_tasks_per_agent,
    )

    # Provenance: who, when, what commit
    try:
        user = getpass.getuser()
    except Exception:
        user = "unknown"

    provenance = Provenance(
        triggered_by=user,
        triggered_at_iso=datetime.now(UTC).isoformat(),
        commit_sha=_git_head_sha(),
    )

    # Snapshot of OrchestratorConfig (exclude non-serializable fields)
    orch_snapshot: dict[str, Any] = {
        "max_agents": config.max_agents,
        "poll_interval_s": config.poll_interval_s,
        "max_agent_runtime_s": config.max_agent_runtime_s,
        "max_tasks_per_agent": config.max_tasks_per_agent,
        "budget_usd": config.budget_usd,
        "evolution_enabled": config.evolution_enabled,
        "max_task_retries": config.max_task_retries,
        "merge_strategy": config.merge_strategy,
        "auto_merge": config.auto_merge,
        "approval": config.approval,
        "recovery": config.recovery,
        "workflow": config.workflow,
        "dry_run": config.dry_run,
        "plan_mode": config.plan_mode,
    }

    manifest = RunManifest(
        run_id=run_id,
        workflow_definition_hash=workflow_definition_hash,
        workflow_name=workflow_name,
        model_routing=model_routing,
        budget_ceiling_usd=config.budget_usd,
        approval_gates=approval_gates,
        agent_adapter=agent_adapter,
        provenance=provenance,
        orchestrator_config=orch_snapshot,
    )

    # Compute and attach the hash (frozen dataclass requires object.__setattr__)
    manifest_hash = manifest.compute_hash()
    object.__setattr__(manifest, "manifest_hash", manifest_hash)
    return manifest


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def save_manifest(manifest: RunManifest, sdd_dir: str | Any) -> str:
    """Write the manifest to ``.sdd/runtime/manifests/<run-id>.json``.

    Returns the path to the written file.
    """
    from pathlib import Path

    sdd = Path(str(sdd_dir))
    manifests_dir = sdd / "runtime" / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    path = manifests_dir / f"{manifest.run_id}.json"
    path.write_text(json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n")
    logger.info("Manifest written: %s (hash=%s)", path, manifest.manifest_hash)
    return str(path)


def load_manifest(sdd_dir: str | Any, run_id: str) -> RunManifest | None:
    """Load a manifest from disk, returning None if not found."""
    from pathlib import Path

    sdd = Path(str(sdd_dir))
    path = sdd / "runtime" / "manifests" / f"{run_id}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return RunManifest.from_dict(data)


def list_manifests(sdd_dir: str | Any) -> list[str]:
    """Return sorted run IDs that have manifests on disk."""
    from pathlib import Path

    sdd = Path(str(sdd_dir))
    manifests_dir = sdd / "runtime" / "manifests"
    if not manifests_dir.exists():
        return []
    return sorted(p.stem for p in manifests_dir.glob("*.json"))


def diff_manifests(a: RunManifest, b: RunManifest) -> dict[str, tuple[Any, Any]]:
    """Compare two manifests, returning fields that differ.

    Returns a dict of ``{field_name: (value_in_a, value_in_b)}`` for each
    top-level field that changed.  ``manifest_hash`` is always excluded
    (it's derived).
    """
    da = a._canonical_payload()
    db = b._canonical_payload()
    diffs: dict[str, tuple[Any, Any]] = {}
    all_keys = sorted(set(da) | set(db))
    for key in all_keys:
        va = da.get(key)
        vb = db.get(key)
        if va != vb:
            diffs[key] = (va, vb)
    return diffs
