"""Record the auditor scenario and export the bundle the vectors read.

The scenario is the awkward one:

    A person starts agent A. A delegates part of the work to sub-agent B.
    B calls a tool served over MCP. That tool reads a file marked
    sensitive. B sends content to an external model endpoint. The
    endpoint returns output. A uses that output to take an action that
    changes the repository.

Nothing here is hand-written evidence. The steps are driven through the
production writers - :class:`~bernstein.core.replay.journal.EventJournal`,
:class:`~bernstein.core.lineage.spine.LineageSpine`,
:class:`~bernstein.core.persistence.lineage.LineageWriter` and the HMAC
audit chain - and the export is assembled by the same functions an
operator's ``bernstein`` install would call. Regenerating the fixture
re-runs the scenario; it never edits the bundle.

Determinism
-----------
The journal projection the run receipt signs excludes wall-clock fields,
the spine entries carry pinned timestamps, and the signing key is a fixed
seed, so ``run-receipt.json`` is byte-identical across recordings. The
audit chain stamps real wall-clock time, so the audit receipt and the
Article 12 pack differ between recordings in their timestamps and in the
identifiers derived from them. ``test_harness.py`` pins the deterministic
half and compares the rest structurally.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.lineage.spine import LineageSpine
from bernstein.core.persistence.lineage import AgentRef, ArtifactRef, LineageRecord, LineageWriter
from bernstein.core.replay.journal import EventJournal
from bernstein.core.replay.run_receipt import build_run_receipt
from bernstein.core.security.article12_bundle import assemble_from_run, emit_run_audit_event
from bernstein.core.security.audit_receipt import build_receipt
from bernstein.core.security.key_custody import FileBasedKMSAdapter

#: Repository root, resolved from this file.
REPO_ROOT = Path(__file__).resolve().parents[3]

#: Where the committed recording lives, relative to :data:`REPO_ROOT`.
FIXTURE_RELATIVE_PATH = "tests/conformance/auditor/fixture"

#: The exported bundle, and the trust anchor deliberately kept outside it.
BUNDLE_DIR_NAME = "bundle"
TRUST_DIR_NAME = "trust"
OPERATOR_PUBLIC_KEY_NAME = "operator-public-key.pem"

#: Artefacts inside the bundle.
INDEX_NAME = "bundle.json"
RUN_RECEIPT_NAME = "run-receipt.json"
AUDIT_RECEIPT_NAME = "audit-receipt.json"
ARTICLE12_NAME = "article12-evidence.zip"

#: Fixed identifiers and key material. Test material, never operator keys:
#: the recording has to be reproducible, which rules out the keychain.
RUN_ID = "run-auditor-conformance"
AUDIT_HMAC_KEY = b"auditor-conformance-audit-key-01"
SIGNING_SEED = b"auditor-conformance-signing-seed"
SIGNING_KID = "auditor-conformance-key"
BUNDLE_SCHEMA_VERSION = 1

_PRINCIPAL = "user:operator@example.org"
_AGENT_A = "agent-a"
_AGENT_B = "agent-b"
_MCP_SERVER = "mcp://repo-tools.example.invalid"
_TOOL = "repo.read_file"
_SENSITIVE_FILE = "config/customer_records.yaml"
_ENDPOINT = "https://models.example.invalid/v1"
_CHANGED_ARTIFACT = "src/app/config.py"
_CHANGED_CONTENT = b"REQUEST_TIMEOUT_S = 30\n"

# One step of the scenario as the journal records it: (event, payload).
_JOURNAL_STEPS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("run_started", {"run_id": RUN_ID, "principal": _PRINCIPAL}),
    (
        "agent_spawned",
        {
            "agent_id": _AGENT_A,
            "parent_agent_id": "",
            "endpoint_adapter_name": "claude-code",
            "endpoint_model": "claude-sonnet-4-5",
            "endpoint_base_url": "https://agents.example.invalid/v1",
            "endpoint_profile_name": "operator",
        },
    ),
    (
        "agent_spawned",
        {
            "agent_id": _AGENT_B,
            "parent_agent_id": _AGENT_A,
            "endpoint_adapter_name": "openai-compatible",
            "endpoint_model": "delegated-worker",
            "endpoint_base_url": _ENDPOINT,
            "endpoint_profile_name": "delegated",
        },
    ),
    ("tool_called", {"agent_id": _AGENT_B, "tool": _TOOL, "server": _MCP_SERVER, "transport": "mcp"}),
    ("file_read", {"agent_id": _AGENT_B, "path": _SENSITIVE_FILE, "sensitivity": "restricted"}),
    ("model_request", {"agent_id": _AGENT_B, "endpoint": _ENDPOINT, "model": "delegated-worker"}),
    ("model_response", {"agent_id": _AGENT_B, "endpoint": _ENDPOINT, "finish_reason": "stop"}),
    ("artifact_written", {"agent_id": _AGENT_A, "path": _CHANGED_ARTIFACT}),
    ("run_completed", {"run_id": RUN_ID, "ticks": 8}),
)

# The same steps as the HMAC audit chain records them:
# (event_type, actor, resource_type, resource_id, details).
_AUDIT_STEPS: tuple[tuple[str, str, str, str, dict[str, Any]], ...] = (
    ("run.started", _PRINCIPAL, "run", RUN_ID, {"agent_id": _AGENT_A}),
    ("agent.delegated", _AGENT_A, "agent", _AGENT_B, {"parent_agent_id": _AGENT_A}),
    ("tool.called", _AGENT_B, "tool", _TOOL, {"server": _MCP_SERVER, "transport": "mcp"}),
    ("data.read", _TOOL, "file", _SENSITIVE_FILE, {"sensitivity": "restricted"}),
    ("model.request", _AGENT_B, "endpoint", _ENDPOINT, {"model": "delegated-worker"}),
    ("model.response", _ENDPOINT, "endpoint", _ENDPOINT, {"finish_reason": "stop"}),
    ("repo.changed", _AGENT_A, "artifact", _CHANGED_ARTIFACT, {"source": "model.response"}),
    ("run.completed", _PRINCIPAL, "run", RUN_ID, {"ticks": 8}),
)

#: Human-readable step labels, recorded into the bundle index so a drifted
#: scenario is visible without diffing signed bytes.
SCENARIO_STEPS: tuple[str, ...] = tuple(
    f"{event_type} {resource_id}" for event_type, _actor, _kind, resource_id, _details in _AUDIT_STEPS
)


@dataclass(frozen=True, slots=True)
class Recording:
    """A completed recording on disk.

    Attributes:
        root: Directory holding ``bundle/`` and ``trust/``.
        bundle_root: The exported bundle - all a vector may read.
        trust_anchor: The operator's public key, kept outside the bundle
            because an auditor receives it out of band.
        run_id: The recorded run.
    """

    root: Path
    bundle_root: Path
    trust_anchor: Path
    run_id: str


def _signing_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(SIGNING_SEED)


def _drive_scenario(sdd_dir: Path) -> None:
    """Drive the scenario through the production writers."""
    journal = EventJournal(RUN_ID, sdd_dir)
    for event, payload in _JOURNAL_STEPS:
        journal.record(event, **payload)

    spine = LineageSpine(sdd_dir / "lineage", run_id=RUN_ID, hmac_key=AUDIT_HMAC_KEY)
    spine.record(
        artifact_path=_CHANGED_ARTIFACT,
        content=_CHANGED_CONTENT,
        actor=_AGENT_A,
        step_id="T-1",
        model="claude-sonnet-4-5",
        timestamp=1_700_000_000,
    )

    for event_type, actor, resource_type, resource_id, details in _AUDIT_STEPS:
        emit_run_audit_event(
            sdd_dir=sdd_dir,
            run_id=RUN_ID,
            event_type=event_type,
            actor=actor,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details,
            audit_key=AUDIT_HMAC_KEY,
        )

    writer = LineageWriter.for_run(run_id=RUN_ID, sdd_dir=sdd_dir)
    writer.emit(
        LineageRecord(
            output_artifact=ArtifactRef(
                path=_CHANGED_ARTIFACT,
                sha256=hashlib.sha256(_CHANGED_CONTENT).hexdigest(),
            ),
            inputs=[ArtifactRef(path=_SENSITIVE_FILE, sha256="0" * 64)],
            producer=AgentRef(agent_id=_AGENT_A, run_id=RUN_ID, tick_id="t-8"),
            prompt_sha="ab" * 32,
            model="delegated-worker",
            cost_usd=0.02,
            tokens=420,
            timestamp=datetime.now(tz=UTC).timestamp(),
            regulatory_class="high-risk",
        )
    )


def record(destination: Path) -> Recording:
    """Run the scenario and export the auditor bundle into *destination*.

    Args:
        destination: Directory to write ``bundle/`` and ``trust/`` into.
            Any previous contents are replaced.

    Returns:
        The :class:`Recording` describing what was written.
    """
    destination = destination.resolve()
    workspace = destination / ".recording"
    for stale in (destination / BUNDLE_DIR_NAME, destination / TRUST_DIR_NAME, workspace):
        shutil.rmtree(stale, ignore_errors=True)

    sdd_dir = workspace / ".sdd"
    sdd_dir.mkdir(parents=True)
    bundle_root = destination / BUNDLE_DIR_NAME
    trust_root = destination / TRUST_DIR_NAME
    bundle_root.mkdir(parents=True)
    trust_root.mkdir(parents=True)

    _drive_scenario(sdd_dir)

    key_path = workspace / "operator-signing-key.pem"
    key_path.write_bytes(
        _signing_key().private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    (trust_root / OPERATOR_PUBLIC_KEY_NAME).write_bytes(
        _signing_key()
        .public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    kms = FileBasedKMSAdapter(key_path, kid=SIGNING_KID)

    build_run_receipt(RUN_ID, sdd_dir, kms, write=True, output_path=bundle_root / RUN_RECEIPT_NAME)

    now = datetime.now(tz=UTC)
    since = now - timedelta(hours=1)
    until = now + timedelta(hours=1)
    audit_receipt = build_receipt(
        sdd_dir / "runtime" / "audit",
        since=_audit_stamp(since),
        until=_audit_stamp(until),
        key=AUDIT_HMAC_KEY,
        kms_adapter=kms,
        write=False,
    )
    (bundle_root / AUDIT_RECEIPT_NAME).write_bytes(audit_receipt.receipt_bytes)

    assembled = assemble_from_run(
        RUN_ID,
        since=since,
        until=until,
        sdd_dir=sdd_dir,
        workdir=REPO_ROOT,
        risk_class="high",
        audit_key=AUDIT_HMAC_KEY,
        output_dir=workspace / "evidence",
    )
    if assembled.bundle.archive_path is None:  # pragma: no cover - write=True always writes
        raise RuntimeError("the Article 12 assembly produced no archive")
    shutil.copyfile(assembled.bundle.archive_path, bundle_root / ARTICLE12_NAME)

    _write_index(bundle_root)
    shutil.rmtree(workspace, ignore_errors=True)
    return Recording(
        root=destination,
        bundle_root=bundle_root,
        trust_anchor=trust_root / OPERATOR_PUBLIC_KEY_NAME,
        run_id=RUN_ID,
    )


def _audit_stamp(moment: datetime) -> str:
    """Format *moment* the way the per-run audit chain stamps its events."""
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _write_index(bundle_root: Path) -> None:
    """Write the bundle's packing index over everything already exported."""
    artefacts = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(bundle_root.iterdir())
        if path.is_file()
    }
    index = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "run_id": RUN_ID,
        "scenario": list(SCENARIO_STEPS),
        "artefacts": artefacts,
    }
    (bundle_root / INDEX_NAME).write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


__all__ = [
    "ARTICLE12_NAME",
    "AUDIT_HMAC_KEY",
    "AUDIT_RECEIPT_NAME",
    "BUNDLE_DIR_NAME",
    "BUNDLE_SCHEMA_VERSION",
    "FIXTURE_RELATIVE_PATH",
    "INDEX_NAME",
    "OPERATOR_PUBLIC_KEY_NAME",
    "REPO_ROOT",
    "RUN_ID",
    "RUN_RECEIPT_NAME",
    "SCENARIO_STEPS",
    "SIGNING_KID",
    "TRUST_DIR_NAME",
    "Recording",
    "record",
]
