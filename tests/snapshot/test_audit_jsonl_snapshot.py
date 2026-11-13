"""Snapshot tests for audit-log and lineage JSONL serialisation.

A snapshot test asserts that the serialised wire format of a known
fixture matches a stored ``.ambr`` snapshot under
``tests/snapshot/__snapshots__/``. If the wire format drifts (e.g. a
new optional field is added or field order changes), the test fails
loudly so the change is reviewed instead of silently breaking
downstream parsers.

Catches:

- audit-log JSONL field-order drift (parser regressions in compliance
  pipelines were caused by silent reorders historically)
- lineage record schema-v2 field absence on v1 records (regression
  from PR #996 that took a week to surface)
- prompt-cache canonicalisation drift (vendor adapters re-cache on any
  prefix change; a silent reorder destroys cache hit rate)

Update workflow: when an intentional schema change lands, run
``uv run pytest tests/snapshot/ --snapshot-update`` and commit the
updated ``.ambr`` files alongside the source change.
"""

from __future__ import annotations

from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from syrupy.assertion import SnapshotAssertion

from bernstein.core.persistence.lineage import (
    AgentRef,
    ArtifactRef,
    LineageReader,
    LineageRecord,
    LineageWriter,
    record_to_dict,
)
from bernstein.core.persistence.lineage_signer import Ed25519FileKeySigner
from bernstein.core.security.audit import AuditLog


def _fixture_lineage_record() -> LineageRecord:
    """Return a deterministic record so the snapshot stays stable."""
    return LineageRecord(
        output_artifact=ArtifactRef(
            path="src/foo.py",
            sha256="a" * 64,
            line_start=10,
            line_end=20,
        ),
        inputs=[
            ArtifactRef(
                path="src/bar.py",
                sha256="b" * 64,
                line_start=1,
                line_end=5,
            ),
        ],
        producer=AgentRef(agent_id="claude", run_id="run-1", tick_id="tick-1"),
        prompt_sha="c" * 64,
        model="claude-sonnet-4.7",
        cost_usd=0.0123,
        tokens=42,
        timestamp=1700000000.0,
        regulatory_class="public",
    )


def test_lineage_record_dict_shape(snapshot: SnapshotAssertion) -> None:
    """Lineage record's plain-dict view must keep the field set + order.

    Downstream compliance exporters parse this dict; a silent rename or
    reorder breaks audit deserialisers in the field. Schema-v2 fields
    (regulatory_class, customer_signature) MUST appear even when
    ``None`` so v1 readers can detect a v2-source-without-data case.
    """
    record = _fixture_lineage_record()
    assert record_to_dict(record) == snapshot


def test_lineage_writer_signed_roundtrip(snapshot: SnapshotAssertion, tmp_path: Path) -> None:
    """Signed lineage record's wire form must match the stored snapshot.

    The signature itself is non-deterministic (Ed25519 is randomised),
    so we mask the customer_signature field before snapshotting. What
    we DO want to lock in: the dict's other field set + order, the
    schema_version, the inputs/output split, and the ``producer``
    nested-dict shape.
    """
    sdd = tmp_path
    private = Ed25519PrivateKey.generate()
    key_path = sdd / "key"
    key_path.write_bytes(private.private_bytes_raw())
    key_path.chmod(0o600)
    signer = Ed25519FileKeySigner.from_path(key_path)
    writer = LineageWriter.for_run("snap-run", sdd, signer=signer)
    writer.emit(_fixture_lineage_record())

    reader = LineageReader(sdd)
    [record] = list(reader.iter_records(run_id="snap-run"))
    payload = record_to_dict(record)
    payload["customer_signature"] = "<masked>"  # non-deterministic
    assert payload == snapshot


def test_audit_log_event_structure(snapshot: SnapshotAssertion, tmp_path: Path) -> None:
    """AuditLog.log() must keep its on-disk JSONL field layout stable.

    Mask the timestamp + hmac fields (non-deterministic) so the
    snapshot covers field set + order + values for the deterministic
    fields (event_type, actor, resource_type, resource_id, details).
    """
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    key_path = tmp_path / "key"
    key_path.write_bytes(b"x" * 40)
    key_path.chmod(0o600)
    log = AuditLog(audit_dir=audit_dir, key_path=key_path)
    log.log(
        "task.start",
        actor="claude",
        resource_type="task",
        resource_id="t-1",
        details={"reason": "scheduled"},
    )
    [jsonl_path] = list(audit_dir.glob("*.jsonl"))
    line = jsonl_path.read_text().strip().splitlines()[0]
    import json

    parsed = json.loads(line)
    parsed["timestamp"] = "<masked>"
    parsed["hmac"] = "<masked>"
    parsed["prev_hmac"] = "<masked>"
    assert parsed == snapshot
