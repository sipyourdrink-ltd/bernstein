"""Tracker-comment projection of a sealed evidence bundle (issue #2362).

The bundle is the artefact; the tracker/PR comment is a projection of it -- a
short gate verdict plus the offline verify command, never the evidence bytes.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.evidence.bundle import (
    EvidenceBundle,
    EvidenceProducer,
    ProducerOutcome,
    build_evidence_bundle,
    load_or_create_evidence_identity,
)
from bernstein.github_app.evidence_projection import (
    EVIDENCE_BUNDLE_MARKER,
    build_evidence_projection,
)

_KEY = b"0" * 32


def _bundle(tmp_path: Path) -> EvidenceBundle:
    priv, pub = load_or_create_evidence_identity(tmp_path / ".sdd" / "identity")
    outcomes = (
        ProducerOutcome(
            producer=EvidenceProducer(name="tests", kind="test", command=("run",), required=True),
            exit_code=0,
            output=b"12 passed\n",
        ),
        ProducerOutcome(
            producer=EvidenceProducer(name="lint", kind="lint", command=("lint",), required=False),
            exit_code=9,
            output=b"secret token = hunter2\n",
        ),
    )
    return build_evidence_bundle(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        private_key_pem=priv,
        public_key_pem=pub,
        task_id="task-1",
        outcomes=outcomes,
        timestamp=1000,
    )


def test_projection_references_bundle_without_body(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    text = build_evidence_projection(bundle)
    assert EVIDENCE_BUNDLE_MARKER in text
    assert "task-1" in text
    assert "bernstein evidence verify task-1" in text
    # The projection never embeds the captured evidence bytes.
    assert "hunter2" not in text
    assert "passed" not in text or "12 passed" not in text


def test_projection_surfaces_gate_verdict(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    text = build_evidence_projection(bundle)
    # Required producer passed, so the gate passed; advisory lint failure noted.
    assert bundle.gate_passed is True
    assert "pass" in text.lower()
