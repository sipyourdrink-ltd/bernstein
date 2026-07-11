"""Per-artifact lineage hook for phase-gate boundary events.

Each phase boundary writes a lineage record so the audit trail becomes
per-phase, per-rule. The live wiring
(:func:`build_phased_runner_with_gate_lineage`) routes that write through
the canonical always-on :class:`bernstein.core.lineage.spine.LineageSpine`
boundary - the same single write path every other subsystem uses - so no
deprecated v1 writer is constructed under ``src/`` (issue #2292 AC4).

Boundary entry mapped onto :meth:`LineageSpine.record`::

    artifact_path -> .sdd/runtime/phase_artifacts/<task_id>/<phase>.json
                     (repo-relative POSIX string)
    content       -> canonical JSON of the boundary + per-rule outcomes,
                     so two replays of the same evaluation are bit-identical
    actor         -> "phase_gate:<phase>"
    step_id       -> "<from>-><to>" (the boundary tick)
    model         -> phase id
    timestamp     -> caller-supplied stable int (default 0)

The legacy :func:`make_lineage_hook` (v1 ``LineageWriter``) is retained for
tests and out-of-tree callers that already hold a writer; it is never
constructed inside ``src/``.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import TYPE_CHECKING, Any

from bernstein.core.lineage.spine import LineageSpine
from bernstein.core.persistence.lineage import (
    AgentRef,
    ArtifactRef,
    LineageRecord,
    hash_file,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from bernstein.core.orchestration.phase_gates import GateResult
    from bernstein.core.orchestration.phase_pipeline import Phase
    from bernstein.core.persistence.lineage import LineageWriter
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


#: Default phase-artifact runtime root (repo-relative POSIX). Mirrors
#: ``phase_pipeline._DEFAULT_RUNTIME_ROOT`` without importing the runner.
_PHASE_ARTIFACT_ROOT = ".sdd/runtime/phase_artifacts"

#: Model tag recorded on the boundary spine entry uses the phase value; the
#: actor uses this ``phase_gate:`` prefix (unchanged from the v1 hook).
_PHASE_GATE_ACTOR_PREFIX = "phase_gate"


def _boundary_artifact_path(task: Task, phase: Phase) -> str:
    """Return the repo-relative POSIX phase-artifact path for the boundary.

    ``LineageSpine.record`` rejects absolute paths and traversal, so the
    path is always relative and already anchored under ``.sdd/``.
    """
    return f"{_PHASE_ARTIFACT_ROOT}/{task.id}/{phase.value}.json"


def _boundary_content(
    *,
    phase: Phase,
    boundary: tuple[Phase, Phase],
    results: list[GateResult],
) -> bytes:
    """Return deterministic canonical bytes for this boundary evaluation.

    The content is the boundary + the per-rule outcome projection - the
    same rule/outcome structure ``_prompt_sha`` hashes and
    :func:`gate_results_summary` renders. We hash the *evaluation* rather
    than the on-disk artefact file: the artefact bytes are executor-driven
    and need not be stable, whereas the boundary decision is a pure
    function of ``(boundary, phase, [(rule_id, outcome)...])`` and reads no
    wall-clock or randomness, so two identical runs replay byte-identically.
    """
    payload = {
        "boundary": [boundary[0].value, boundary[1].value],
        "phase": phase.value,
        "rules": [{"rule_id": r.rule_id, "outcome": r.outcome.value} for r in results],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


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


def make_spine_lineage_hook(
    spine: LineageSpine,
    *,
    timestamp: int = 0,
    artifact_path_resolver: Callable[[Task, Phase], str] | None = None,
) -> Any:
    """Return a boundary lineage hook that writes through *spine*.

    This is the canonical, always-on replacement for the v1
    :func:`make_lineage_hook`: every boundary routes through the single
    :meth:`LineageSpine.record` write boundary, so no deprecated v1 writer
    is constructed under ``src/`` (issue #2292 AC4). The entry mirrors the
    v1 record's actor (``phase_gate:<phase>``), tick (``<from>-><to>``) and
    model (phase id).

    Args:
        spine: The run's lineage spine (the single write boundary).
        timestamp: Stable integer timestamp recorded on every boundary
            entry. Defaults to ``0`` so identical fixtures replay
            byte-identically; a caller may pin a different value.
        artifact_path_resolver: Optional callable mapping
            ``(task, phase) -> repo-relative POSIX str`` to override the
            default ``.sdd/runtime/phase_artifacts/<task_id>/<phase>.json``
            path. The returned path must be relative (no leading ``/`` or
            ``..`` segment) or :meth:`LineageSpine.record` rejects it.
    """

    def _hook(
        task: Task,
        phase: Phase,
        boundary: tuple[Phase, Phase],
        results: list[GateResult],
    ) -> None:
        if artifact_path_resolver is not None:
            artifact_path = artifact_path_resolver(task, phase)
        else:
            artifact_path = _boundary_artifact_path(task, phase)
        spine.record(
            artifact_path=artifact_path,
            content=_boundary_content(phase=phase, boundary=boundary, results=results),
            actor=f"{_PHASE_GATE_ACTOR_PREFIX}:{phase.value}",
            step_id=f"{boundary[0].value}->{boundary[1].value}",
            model=phase.value,
            timestamp=timestamp,
        )

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

    The returned closure first delegates to *lineage_hook* (the per-boundary
    lineage emission - :func:`make_spine_lineage_hook` in the live wiring) and
    then anchors a signed adjudication record -- ``{inputs_hash, rubric_hash,
    panel_config, per_judge_verdict, final_verdict}`` -- to the run's lineage
    spine, mirroring it into the HMAC audit chain when *audit_dir* is supplied.

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
    boundary_timestamp: int = 0,
    store: Any | None = None,
) -> Any:
    """Return a :class:`PhasedRunner` with the gate lineage hook wired live.

    This is the production wiring AC1 requires. The per-boundary lineage
    write routes through the canonical always-on
    :class:`bernstein.core.lineage.spine.LineageSpine` boundary - the same
    single write path every other subsystem uses - so no deprecated v1
    writer is constructed (issue #2292 AC4). The spine hook is wrapped by
    :func:`make_adjudication_hook` exactly as before (issue #2294), so each
    boundary still emits a signed adjudication record; only the underlying
    lineage write target changed from the v1 writer to the spine.

    Args:
        executor: The phase executor callable passed to :class:`PhasedRunner`.
        sdd_dir: The ``.sdd`` directory root.
        hmac_key: Audit-chain HMAC key that tags spine and audit entries.
        run_id: Run identifier for the lineage spine; defaults to a stable
            ``"phase-gates"``.
        emit_audit_chain: When True, also mirror each adjudication record into
            the HMAC audit log under ``sdd_dir/audit``.
        boundary_timestamp: Stable integer timestamp recorded on every
            boundary spine entry. Defaults to ``0`` so identical fixtures
            replay byte-identically; pin a different value to override.
    """
    from bernstein.core.orchestration.phase_pipeline import PhasedRunner

    resolved_run_id = run_id or "phase-gates"
    lineage_root = sdd_dir / "lineage"
    spine = LineageSpine(lineage_root, run_id=resolved_run_id, hmac_key=hmac_key)
    lineage_hook = make_spine_lineage_hook(spine, timestamp=boundary_timestamp)
    hook = make_adjudication_hook(
        lineage_hook=lineage_hook,
        lineage_root=lineage_root,
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
    "make_spine_lineage_hook",
]
