"""Deterministic OpenLineage projection of a Bernstein lineage chain (#4914).

``bernstein lineage export --format openlineage`` emits an OpenLineage event
stream derived from an existing run — a projection of the WAL lineage
records (and optional audit task outcomes), not a second source of truth.

Design choice (stated in the PR): **one custom run facet**
(``bernstein_chain``) carries the whole chain proof — ``chain_head_hash``,
``lineage_record_id``, and a detached projection ``signature``. Standard
OpenLineage ``parent`` / dataset facets still carry the job graph and I/O so
consumers that drop unknown facets keep a usable graph; offline verifiers
that have the chain check a single facet.

HTTP collector transport and live streaming are out of scope for this slice.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from bernstein.core.persistence.lineage import LineageReader, LineageRecord
from bernstein.core.security.agent_card_signer import canonicalize_jcs

PRODUCER_URI = (
    "https://github.com/sipyourdrink-ltd/bernstein/tree/main/src/bernstein/core/persistence/openlineage_export.py"
)
SCHEMA_URL = "https://openlineage.io/spec/1-0-5/OpenLineage.json#/$defs/RunEvent"
FACET_SCHEMA_URL = "https://bernstein.run/schemas/openlineage/1-0-5/BernsteinChainRunFacet.json"
NAMESPACE = "bernstein"

EventType = Literal["START", "COMPLETE", "FAIL"]
TaskOutcome = Literal["COMPLETE", "FAIL"]

_FACET_NAME = "bernstein_chain"


@dataclass(frozen=True)
class LineageChainPosition:
    """One lineage record plus its WAL chain coordinates."""

    record: LineageRecord
    entry_hash: str
    seq: int
    prev_hash: str


@dataclass(frozen=True)
class OpenLineageExportResult:
    """Bytes of the JSONL stream plus the parsed events (for tests)."""

    payload: bytes
    events: tuple[dict[str, Any], ...]


def _iso_utc(ts: float) -> str:
    """Format a recorded Unix timestamp as RFC3339 UTC (no wall-clock)."""
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _run_uuid(*parts: str) -> str:
    """Deterministic UUID5 so two exports of the same run match byte-for-byte."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "bernstein-openlineage:" + "|".join(parts)))


def _job_name_run(run_id: str) -> str:
    return f"run/{run_id}"


def _job_name_task(run_id: str, agent_id: str) -> str:
    return f"run/{run_id}/task/{agent_id}"


def _dataset(namespace: str, path: str, sha256: str) -> dict[str, Any]:
    return {
        "namespace": namespace,
        "name": path,
        "facets": {
            "dataSource": {
                "_producer": PRODUCER_URI,
                "_schemaURL": "https://openlineage.io/spec/1-0-5/OpenLineage.json#/$defs/DatasourceDatasetFacet",
                "name": "bernstein-workdir",
                "uri": f"file:///{path}",
            },
            "version": {
                "_producer": PRODUCER_URI,
                "_schemaURL": "https://openlineage.io/spec/1-0-5/OpenLineage.json#/$defs/DatasetVersionDatasetFacet",
                "datasetVersion": sha256,
            },
        },
    }


def iter_lineage_chain_positions(sdd_dir: Path, run_id: str) -> list[LineageChainPosition]:
    """Walk the run WAL and return lineage records with chain hashes."""
    reader = LineageReader(sdd_dir)
    return [
        LineageChainPosition(record=record, entry_hash=entry_hash, seq=seq, prev_hash=prev_hash)
        for record, entry_hash, seq, prev_hash in reader.iter_records_with_chain(run_id=run_id)
    ]


def load_task_outcomes_from_audit(sdd_dir: Path, run_id: str) -> dict[str, TaskOutcome]:
    """Best-effort map of task/agent id → COMPLETE|FAIL from the run audit file.

    Looks for ``activity.result`` rows whose ``terminal_state`` is a failure
    class, keyed by ``stage_id``. Missing audit → empty map (all tasks
    default to COMPLETE from lineage alone).
    """
    path = sdd_dir / "runtime" / "audit" / f"{run_id}.audit.jsonl"
    if not path.is_file():
        return {}
    outcomes: dict[str, TaskOutcome] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("event_type") != "activity.result":
            continue
        details = row.get("details") or {}
        if not isinstance(details, dict):
            continue
        stage_id = details.get("stage_id") or details.get("task_id")
        terminal = str(details.get("terminal_state", "")).lower()
        if not isinstance(stage_id, str) or not stage_id:
            continue
        if terminal in {"failed", "fail", "refused", "timed_out", "error"}:
            outcomes[stage_id] = "FAIL"
        elif terminal in {"completed", "complete", "succeeded", "success"}:
            outcomes.setdefault(stage_id, "COMPLETE")
    return outcomes


def _blank_signature(event: dict[str, Any]) -> dict[str, Any]:
    """Return a deep-ish copy with ``bernstein_chain.signature`` set to ``\"\"``."""
    clone: dict[str, Any] = json.loads(json.dumps(event, sort_keys=True))
    facets = clone.get("run", {}).get("facets", {})
    facet = facets.get(_FACET_NAME)
    if isinstance(facet, dict):
        facet["signature"] = ""
    return clone


def event_projection_signature(event: dict[str, Any]) -> str:
    """Detached content address of *event* with the facet signature blanked."""
    digest = hashlib.sha256(canonicalize_jcs(_blank_signature(event))).hexdigest()
    return f"sha256:{digest}"


def verify_openlineage_event(
    event: dict[str, Any],
    *,
    expected_chain_head: str | None = None,
) -> bool:
    """Return True when the ``bernstein_chain`` facet matches the event body.

    When *expected_chain_head* is set, also require the facet's
    ``chain_head_hash`` equal that value (offline check against the WAL).
    """
    try:
        facet = event["run"]["facets"][_FACET_NAME]
    except (KeyError, TypeError):
        return False
    if not isinstance(facet, dict):
        return False
    if expected_chain_head is not None and facet.get("chain_head_hash") != expected_chain_head:
        return False
    recorded = facet.get("signature")
    if not isinstance(recorded, str) or not recorded.startswith("sha256:"):
        return False
    return recorded == event_projection_signature(event)


def _parent_run_facet(*, parent_run_id: str, parent_job_name: str) -> dict[str, Any]:
    return {
        "_producer": PRODUCER_URI,
        "_schemaURL": "https://openlineage.io/spec/1-0-5/OpenLineage.json#/$defs/ParentRunFacet",
        "run": {"runId": parent_run_id},
        "job": {"namespace": NAMESPACE, "name": parent_job_name},
    }


def _chain_facet(
    *,
    chain_head_hash: str,
    lineage_record_id: str,
    wal_seq: int,
    customer_signature: str | None,
) -> dict[str, Any]:
    facet: dict[str, Any] = {
        "_producer": PRODUCER_URI,
        "_schemaURL": FACET_SCHEMA_URL,
        "chain_head_hash": chain_head_hash,
        "lineage_record_id": lineage_record_id,
        "signature": "",
        "wal_seq": wal_seq,
    }
    if customer_signature is not None:
        facet["customer_signature"] = customer_signature
    return facet


def _seal_event(event: dict[str, Any]) -> dict[str, Any]:
    facet = event["run"]["facets"][_FACET_NAME]
    facet["signature"] = event_projection_signature(event)
    return event


def _build_run_level_events(
    *,
    run_id: str,
    start_ts: float,
    end_ts: float,
    outcome: TaskOutcome,
    chain_head: str,
    seq: int,
) -> list[dict[str, Any]]:
    parent_uuid = _run_uuid(run_id, "parent")
    job = {"namespace": NAMESPACE, "name": _job_name_run(run_id)}
    events: list[dict[str, Any]] = []
    for event_type, ts in (("START", start_ts), (outcome, end_ts)):
        event: dict[str, Any] = {
            "eventType": event_type,
            "eventTime": _iso_utc(ts),
            "producer": PRODUCER_URI,
            "schemaURL": SCHEMA_URL,
            "run": {
                "runId": parent_uuid,
                "facets": {
                    _FACET_NAME: _chain_facet(
                        chain_head_hash=chain_head,
                        lineage_record_id=f"run:{run_id}:{event_type}",
                        wal_seq=seq,
                        customer_signature=None,
                    )
                },
            },
            "job": job,
            "inputs": [],
            "outputs": [],
        }
        events.append(_seal_event(event))
    return events


def _task_io(positions: list[LineageChainPosition]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    inputs: dict[tuple[str, str], dict[str, Any]] = {}
    outputs: dict[tuple[str, str], dict[str, Any]] = {}
    for pos in positions:
        rec = pos.record
        for inp in rec.inputs:
            key = (inp.path, inp.sha256)
            inputs[key] = _dataset(NAMESPACE, inp.path, inp.sha256)
        out = rec.output_artifact
        outputs[(out.path, out.sha256)] = _dataset(NAMESPACE, out.path, out.sha256)
    return (
        [inputs[k] for k in sorted(inputs)],
        [outputs[k] for k in sorted(outputs)],
    )


def _build_task_events(
    *,
    run_id: str,
    agent_id: str,
    positions: list[LineageChainPosition],
    parent_run_uuid: str,
    outcome: TaskOutcome,
) -> list[dict[str, Any]]:
    positions_sorted = sorted(positions, key=lambda p: (p.record.timestamp, p.seq))
    first = positions_sorted[0]
    last = positions_sorted[-1]
    start_ts = first.record.timestamp
    end_ts = last.record.timestamp
    if end_ts < start_ts:
        end_ts = start_ts
    inputs, outputs = _task_io(positions_sorted)
    job = {"namespace": NAMESPACE, "name": _job_name_task(run_id, agent_id)}
    task_uuid = _run_uuid(run_id, "task", agent_id)
    events: list[dict[str, Any]] = []
    for event_type, ts, pos, with_io in (
        ("START", start_ts, first, False),
        (outcome, end_ts, last, True),
    ):
        event: dict[str, Any] = {
            "eventType": event_type,
            "eventTime": _iso_utc(ts),
            "producer": PRODUCER_URI,
            "schemaURL": SCHEMA_URL,
            "run": {
                "runId": task_uuid,
                "facets": {
                    "parent": _parent_run_facet(
                        parent_run_id=parent_run_uuid,
                        parent_job_name=_job_name_run(run_id),
                    ),
                    _FACET_NAME: _chain_facet(
                        chain_head_hash=pos.entry_hash,
                        lineage_record_id=pos.entry_hash,
                        wal_seq=pos.seq,
                        customer_signature=pos.record.customer_signature,
                    ),
                },
            },
            "job": job,
            "inputs": inputs if with_io else [],
            "outputs": outputs if with_io else [],
        }
        events.append(_seal_event(event))
    return events


def task_dependency_edges(positions: list[LineageChainPosition]) -> list[tuple[str, str]]:
    """Return ``(producer_agent, consumer_agent)`` edges from dataset flow."""
    producers: dict[str, str] = {}
    by_agent: dict[str, list[LineageChainPosition]] = {}
    for pos in positions:
        agent_id = pos.record.producer.agent_id
        by_agent.setdefault(agent_id, []).append(pos)
        producers[pos.record.output_artifact.path] = agent_id
    edges: list[tuple[str, str]] = []
    for agent_id, agent_positions in sorted(by_agent.items()):
        deps: set[str] = set()
        for pos in agent_positions:
            for inp in pos.record.inputs:
                src = producers.get(inp.path)
                if src is not None and src != agent_id:
                    deps.add(src)
        for src in sorted(deps):
            edges.append((src, agent_id))
    return edges


def build_openlineage_events(
    positions: list[LineageChainPosition],
    *,
    run_id: str,
    task_outcomes: dict[str, TaskOutcome] | None = None,
) -> list[dict[str, Any]]:
    """Project lineage chain positions into a deterministic OpenLineage event list."""
    if not positions:
        return []
    outcomes = task_outcomes or {}
    by_agent: dict[str, list[LineageChainPosition]] = {}
    for pos in positions:
        by_agent.setdefault(pos.record.producer.agent_id, []).append(pos)

    all_ts = [p.record.timestamp for p in positions]
    start_ts = min(all_ts)
    end_ts = max(all_ts)
    last_pos = max(positions, key=lambda p: (p.seq, p.record.timestamp))
    run_failed = any(outcomes.get(a) == "FAIL" for a in by_agent)
    run_outcome: TaskOutcome = "FAIL" if run_failed else "COMPLETE"

    events = _build_run_level_events(
        run_id=run_id,
        start_ts=start_ts,
        end_ts=end_ts,
        outcome=run_outcome,
        chain_head=last_pos.entry_hash,
        seq=last_pos.seq,
    )
    parent_uuid = _run_uuid(run_id, "parent")
    for agent_id in sorted(by_agent):
        agent_outcome = outcomes.get(agent_id, "COMPLETE")
        events.extend(
            _build_task_events(
                run_id=run_id,
                agent_id=agent_id,
                positions=by_agent[agent_id],
                parent_run_uuid=parent_uuid,
                outcome=agent_outcome,
            )
        )

    # Stable order: eventTime, job name, eventType rank
    type_rank = {"START": 0, "COMPLETE": 1, "FAIL": 1, "ABORT": 1, "RUNNING": 2, "OTHER": 3}
    events.sort(
        key=lambda e: (
            e["eventTime"],
            e["job"]["name"],
            type_rank.get(str(e["eventType"]), 9),
            e["run"]["runId"],
        )
    )
    return events


def render_openlineage_jsonl(events: list[dict[str, Any]]) -> bytes:
    """Canonical JSONL bytes (sorted keys; matches Article 12 event-log discipline)."""
    lines = [json.dumps(ev, sort_keys=True, ensure_ascii=False) for ev in events]
    return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")


def export_openlineage(
    sdd_dir: Path,
    run_id: str,
    *,
    task_outcomes: dict[str, TaskOutcome] | None = None,
) -> OpenLineageExportResult:
    """Build the OpenLineage JSONL projection for *run_id* under *sdd_dir*."""
    positions = iter_lineage_chain_positions(sdd_dir, run_id)
    if task_outcomes is None:
        task_outcomes = load_task_outcomes_from_audit(sdd_dir, run_id)
    events = build_openlineage_events(positions, run_id=run_id, task_outcomes=task_outcomes)
    payload = render_openlineage_jsonl(events)
    return OpenLineageExportResult(payload=payload, events=tuple(events))


def openlineage_schema_path() -> Path:
    """Path to the vendored OpenLineage 1-0-5 schema."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "schemas" / "openlineage" / "1-0-5" / "OpenLineage.json"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("vendored schemas/openlineage/1-0-5/OpenLineage.json not found")


__all__ = [
    "FACET_SCHEMA_URL",
    "NAMESPACE",
    "PRODUCER_URI",
    "SCHEMA_URL",
    "LineageChainPosition",
    "OpenLineageExportResult",
    "build_openlineage_events",
    "event_projection_signature",
    "export_openlineage",
    "iter_lineage_chain_positions",
    "load_task_outcomes_from_audit",
    "openlineage_schema_path",
    "render_openlineage_jsonl",
    "task_dependency_edges",
    "verify_openlineage_event",
]
