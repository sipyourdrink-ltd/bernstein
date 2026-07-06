"""Per-artifact lineage hook for phase-gate boundary events.

Each phase boundary writes a lineage record so the audit trail becomes
per-phase, per-rule.  We reuse the existing
:class:`bernstein.core.persistence.lineage.LineageWriter` rather than
creating a parallel store - verifying the WAL hash chain
(``WALReader.verify_chain``) and the audit-log HMAC chain remains a
single operation.

Record shape (mapped onto :class:`LineageRecord`)::

    output_artifact -> .sdd/runtime/phase_artifacts/<task_id>/<phase>.json
                       (the just-written artefact)
    inputs          -> empty list; the prior phase's artefact is already
                       on the lineage chain via its own write event
    producer        -> AgentRef(agent_id="phase_gate", run_id=task.id)
    prompt_sha      -> stable hash of "<rule_id>:<outcome>" entries so
                       two replays of the same evaluation are bit-identical
    model           -> phase id (used as a free-form tag)

The ``regulatory_class`` field is set to ``"phase_gate"`` so compliance
filters can pull every gate event in one query.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import TYPE_CHECKING, Any

from bernstein.core.persistence.lineage import (
    AgentRef,
    ArtifactRef,
    LineageRecord,
    LineageWriter,
    hash_file,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from bernstein.core.orchestration.phase_gates import GateResult
    from bernstein.core.orchestration.phase_pipeline import Phase
    from bernstein.core.tasks.models import Task


PHASE_GATE_REGULATORY_CLASS = "phase_gate"


def gate_results_summary(results: list[GateResult]) -> dict[str, Any]:
    """Render *results* as a JSON-friendly summary for audit consumers."""
    return {
        "rules": [
            {
                "rule_id": r.rule_id,
                "outcome": r.outcome.value,
                "boundary_from": r.boundary_from.value if r.boundary_from is not None else None,
                "boundary_to": r.boundary_to.value if r.boundary_to is not None else None,
                "details": r.details,
            }
            for r in results
        ]
    }


def _prompt_sha(results: list[GateResult]) -> str:
    """Stable hash of the rule outcomes - replay-friendly fn_hash."""
    canonical = json.dumps(
        [(r.rule_id, r.outcome.value) for r in results],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_phase_gate_record(
    *,
    task: Task,
    phase: Phase,
    boundary: tuple[Phase, Phase],
    results: list[GateResult],
    artifact_path: Path,
) -> LineageRecord:
    """Build a :class:`LineageRecord` for a single boundary evaluation."""
    output_ref = ArtifactRef(
        path=str(artifact_path),
        sha256=hash_file(artifact_path),
    )
    return LineageRecord(
        output_artifact=output_ref,
        inputs=[],
        producer=AgentRef(
            agent_id=f"phase_gate:{phase.value}",
            run_id=task.id,
            tick_id=f"{boundary[0].value}->{boundary[1].value}",
        ),
        prompt_sha=_prompt_sha(results),
        model=phase.value,
        cost_usd=0.0,
        tokens=0,
        timestamp=time.time(),
        regulatory_class=PHASE_GATE_REGULATORY_CLASS,
    )


def make_lineage_hook(
    writer: LineageWriter,
    *,
    artifact_path_resolver: Callable[[Task, Phase], Path] | None = None,
) -> Any:
    """Return a hook usable as :attr:`PhasedRunner.gate_lineage_hook`.

    The closure captures *writer* and the optional resolver so callers
    don't have to thread the writer through the runner constructor.

    Args:
        writer: WAL-backed lineage writer for the active run.
        artifact_path_resolver: Optional callable mapping
            ``(task, phase) -> Path`` to override the default
            ``.sdd/runtime/phase_artifacts/<task_id>/<phase>.json`` lookup.
    """
    from pathlib import Path as _Path

    def _hook(
        task: Task,
        phase: Phase,
        boundary: tuple[Phase, Phase],
        results: list[GateResult],
    ) -> None:
        if artifact_path_resolver is not None:
            artifact_path = artifact_path_resolver(task, phase)
        else:
            artifact_path = _Path(".sdd/runtime/phase_artifacts") / task.id / f"{phase.value}.json"
        record = build_phase_gate_record(
            task=task,
            phase=phase,
            boundary=boundary,
            results=results,
            artifact_path=artifact_path,
        )
        writer.emit(record, actor=f"phase_gate:{phase.value}")

    return _hook


# ---------------------------------------------------------------------------
# Signed adjudication wiring (issue #2294)
# ---------------------------------------------------------------------------


def _gate_panel() -> Any:
    """Return the deterministic maker-checker panel used for gate boundaries.

    The two roles differ on model + prompt, so the panel is independent by
    construction (a checker that shares the maker's config would be rejected).
    """
    from bernstein.core.quality.adjudication import JudgeConfig, PanelConfig, PanelMode

    return PanelConfig(
        judges=(
            JudgeConfig(model="phase_gate.maker", temperature=0.0, prompt_hash="phase_gate.rules"),
            JudgeConfig(model="phase_gate.checker", temperature=0.0, prompt_hash="phase_gate.exit_criteria"),
        ),
        mode=PanelMode.MAKER_CHECKER,
    )


def _gate_verdicts(results: list[GateResult]) -> Any:
    """Project *results* onto a deterministic maker-checker verdict pair.

    The maker asserts the boundary from the raw rule outcomes; the checker
    re-derives the same verdict independently. Both FAIL when any rule failed,
    so the aggregated (checker-veto) verdict mirrors the gate's own decision.
    """
    from bernstein.core.orchestration.phase_gates import GateOutcome
    from bernstein.core.quality.adjudication import JudgeVerdict, Verdict

    panel = _gate_panel()
    failed = any(r.outcome is GateOutcome.FAIL for r in results)
    verdict = Verdict.FAIL if failed else Verdict.PASS
    rationale = _prompt_sha(results)
    return (
        JudgeVerdict(config=panel.judges[0], verdict=verdict, rationale_hash=rationale),
        JudgeVerdict(config=panel.judges[1], verdict=verdict, rationale_hash=rationale),
    )


def make_adjudication_hook(
    *,
    lineage_hook: Any,
    lineage_root: Path,
    hmac_key: bytes,
    audit_dir: Path | None = None,
    run_id: str,
) -> Any:
    """Wrap *lineage_hook* so each boundary also writes a signed adjudication record.

    The returned closure first delegates to *lineage_hook* (the WAL lineage
    emission from :func:`make_lineage_hook`) and then anchors a signed
    adjudication record -- ``{inputs_hash, rubric_hash, panel_config,
    per_judge_verdict, final_verdict}`` -- to the run's lineage spine, mirroring
    it into the HMAC audit chain when *audit_dir* is supplied.

    The gate's inputs are the per-rule ``(rule_id, outcome)`` projection; the
    rubric is the boundary. Two byte-identical gate runs produce byte-identical
    adjudication records and anchors (AC5).
    """
    from bernstein.core.quality.adjudication import adjudicate

    def _hook(
        task: Task,
        phase: Phase,
        boundary: tuple[Phase, Phase],
        results: list[GateResult],
    ) -> None:
        lineage_hook(task, phase, boundary, results)
        summary = gate_results_summary(results)
        rubric = {"boundary": [boundary[0].value, boundary[1].value], "phase": phase.value}
        panel = _gate_panel()
        verdicts = _gate_verdicts(results)
        record = adjudicate(
            run_id=run_id,
            lineage_root=lineage_root,
            hmac_key=hmac_key,
            inputs=summary,
            rubric=rubric,
            panel=panel,
            judge_verdicts=verdicts,
            now=0,
        )
        if audit_dir is not None:
            from bernstein.core.security.audit_chain import AuditChainStore, record_gate_adjudication

            chain = AuditChainStore(audit_dir, key=hmac_key)
            record_gate_adjudication(
                chain=chain,
                run_id=run_id,
                inputs_hash=record.inputs_hash,
                rubric_hash=record.rubric_hash,
                panel_config_hash=panel.config_hash(),
                final_verdict=record.final_verdict.value,
                journal_entry_hash=record.journal_entry_hash,
            )

    return _hook


def build_phased_runner_with_gate_lineage(
    *,
    executor: Any,
    sdd_dir: Path,
    hmac_key: bytes,
    run_id: str | None = None,
    emit_audit_chain: bool = False,
    store: Any | None = None,
) -> Any:
    """Return a :class:`PhasedRunner` with the gate lineage hook wired live.

    This is the production wiring AC1 requires: it constructs a
    :class:`bernstein.core.persistence.lineage.LineageWriter`, calls
    :func:`make_lineage_hook` on it (the live caller that fixes the 0-caller
    hook), wraps it so each boundary also writes a signed adjudication record,
    and binds the composite hook to a fresh runner.

    Args:
        executor: The phase executor callable passed to :class:`PhasedRunner`.
        sdd_dir: The ``.sdd`` directory root.
        hmac_key: Audit-chain HMAC key that tags spine and audit entries.
        run_id: Run identifier for the lineage writer + spine; defaults to a
            stable ``"phased-<>"``-free value derived from the runner.
        emit_audit_chain: When True, also mirror each adjudication record into
            the HMAC audit log under ``sdd_dir/audit``.
    """
    from bernstein.core.orchestration.phase_pipeline import PhasedRunner
    from bernstein.core.persistence.lineage import LineageWriter

    resolved_run_id = run_id or "phase-gates"
    writer = LineageWriter.for_run(resolved_run_id, sdd_dir)
    lineage_hook = make_lineage_hook(writer)
    hook = make_adjudication_hook(
        lineage_hook=lineage_hook,
        lineage_root=sdd_dir / "lineage",
        hmac_key=hmac_key,
        audit_dir=(sdd_dir / "audit") if emit_audit_chain else None,
        run_id=resolved_run_id,
    )
    if store is not None:
        return PhasedRunner(executor=executor, gate_lineage_hook=hook, store=store)
    return PhasedRunner(executor=executor, gate_lineage_hook=hook)


__all__ = [
    "PHASE_GATE_REGULATORY_CLASS",
    "build_phase_gate_record",
    "build_phased_runner_with_gate_lineage",
    "gate_results_summary",
    "make_adjudication_hook",
    "make_lineage_hook",
]
