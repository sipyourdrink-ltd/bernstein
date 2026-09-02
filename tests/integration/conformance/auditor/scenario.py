"""The recorded scenario the auditor suite asks its questions about.

The scenario is deliberately the awkward one:

    A person starts agent A. A delegates part of the work to sub-agent B.
    B calls a tool served over MCP. That tool reads a file marked
    sensitive. B sends content to an external model endpoint. The
    endpoint returns output. A uses that output to take an action that
    changes the repository.

Nothing here is hand-written evidence. Every byte the vectors read is
produced by the same writers production uses - :class:`AuditLog` and
:func:`emit_run_audit_event` for the HMAC chain, :class:`EventJournal`
for the run journal, :class:`LineageSpine` and :class:`LineageWriter`
for lineage - and the export goes through :func:`build_receipt`,
:func:`build_run_receipt` and :func:`assemble_from_run`. If a production
writer stops recording a field, the fixture stops carrying it and the
vector that reads it goes red. That is the whole point of the
instrument: it measures the code at HEAD, not a snapshot of the code
that was at HEAD when somebody last regenerated a checked-in bundle.

Regenerate an inspectable copy with::

    uv run python -m tests.integration.conformance.auditor.scenario --out /tmp/auditor-bundle

The test suite calls :func:`build_fixture` directly into a temporary
directory, so the two paths cannot drift.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bernstein.core.lineage.spine import LineageSpine
from bernstein.core.persistence.lineage import (
    AgentRef,
    ArtifactRef,
    LineageRecord,
    LineageWriter,
)
from bernstein.core.replay.journal import EventJournal
from bernstein.core.replay.run_receipt import build_run_receipt
from bernstein.core.security.article12_bundle import assemble_from_run, emit_run_audit_event
from bernstein.core.security.audit import AuditLog
from bernstein.core.security.audit_receipt import build_receipt
from bernstein.core.security.key_custody import FileBasedKMSAdapter

# ---------------------------------------------------------------------------
# Scenario constants. Vector tests assert against these by name, so a
# question is always asked about a value the scenario actually produced.
# ---------------------------------------------------------------------------

RUN_ID = "auditor-conformance-run"

#: Fixture key material. Both values are constants in a test module and
#: are never resolved from the operator's keychain, so regenerating the
#: bundle cannot touch - or be shaped by - real key custody.
AUDIT_HMAC_KEY = b"auditor-conformance-audit-key-01"
SIGNING_SEED = b"auditor-conformance-sign-seed-01"
SIGNING_KID = "auditor-conformance-key"

#: The human who started the run.
PRINCIPAL = "person:dana@example.org"
#: The agent that principal started, and the sub-agent it delegated to.
AGENT_A = "agent-A"
AGENT_B = "agent-B"

#: The MCP-served tool B called, and the server that served it.
TOOL_NAME = "read_file"
MCP_SERVER = "mcp://filesystem.internal"

#: The file the tool read. It is marked sensitive *in its own bytes* -
#: nothing in the evidence carries the classification outward today,
#: which is the gap question 8 exists to measure.
SENSITIVE_PATH = "docs/customer-list.csv"
SENSITIVITY_MARKER = "classification: restricted"

#: The endpoint B sent content to.
ENDPOINT_ADAPTER = "claude"
ENDPOINT_MODEL = "claude-sonnet-4.5"
ENDPOINT_BASE_URL = "https://api.anthropic.com"
ENDPOINT_PROFILE = "default"

#: The repository change A made with the model's output.
CHANGED_PATH = "src/summary.py"

#: Wall-clock anchor for every timestamp the scenario controls.
BASE_TIMESTAMP = 1_767_225_600  # 2026-01-01T00:00:00Z

#: The audit window the receipts attest. Wide on purpose: the audit and
#: journal writers stamp their own wall clock, so a narrow window would
#: make the fixture depend on when it was built.
AUDIT_SINCE = "2020-01-01T00:00:00.000000Z"
AUDIT_UNTIL = "2100-01-01T00:00:00.000000Z"

#: Files the export writes. The vectors read these names and no others.
BUNDLE_MANIFEST_NAME = "bundle.json"
AUDIT_RECEIPT_NAME = "audit-receipt.json"
RUN_RECEIPT_NAME = "run-receipt.json"
ARTICLE12_NAME = "article12.zip"


@dataclass(frozen=True, slots=True)
class ScenarioFixture:
    """Where a regenerated fixture landed.

    Attributes:
        root: The directory holding both halves.
        workspace: The project workspace the scenario ran in. Vectors
            must never read from here - it is the machine that produced
            the evidence, which the auditor does not have.
        bundle: The exported bundle. This is the auditor's whole world.
        run_id: The run the bundle attests.
    """

    root: Path
    workspace: Path
    bundle: Path
    run_id: str


def _write_signing_key(workspace: Path) -> Path:
    """Materialise the fixture Ed25519 seed and return its path."""
    key_path = workspace / "signing-key.raw"
    key_path.write_bytes(SIGNING_SEED)
    key_path.chmod(0o600)
    return key_path


def run_scenario(workspace: Path) -> str:
    """Run the scenario in *workspace*, writing through production writers.

    Args:
        workspace: A project root. ``.sdd`` is created underneath it.

    Returns:
        The run id the scenario recorded under.
    """
    sdd_dir = workspace / ".sdd"
    sdd_dir.mkdir(parents=True, exist_ok=True)
    audit_dir = sdd_dir / "audit"
    audit = AuditLog(audit_dir, key=AUDIT_HMAC_KEY)
    journal = EventJournal(RUN_ID, sdd_dir)
    spine = LineageSpine(sdd_dir / "lineage", run_id=RUN_ID, hmac_key=AUDIT_HMAC_KEY)
    lineage = LineageWriter.for_run(run_id=RUN_ID, sdd_dir=sdd_dir)

    def chained(event_type: str, actor: str, resource_type: str, resource_id: str, **details: object) -> None:
        """Emit one event into both the daily chain and the per-run slice."""
        audit.log(event_type, actor, resource_type, resource_id, dict(details))
        emit_run_audit_event(
            sdd_dir=sdd_dir,
            run_id=RUN_ID,
            event_type=event_type,
            actor=actor,
            resource_type=resource_type,
            resource_id=resource_id,
            details=dict(details),
            audit_key=AUDIT_HMAC_KEY,
        )

    # 1. A person starts agent A.
    chained("run.started", PRINCIPAL, "run", RUN_ID, agent=AGENT_A)
    journal.record(
        "agent_spawned",
        agent_id=AGENT_A,
        started_by=PRINCIPAL,
        endpoint_adapter_name=ENDPOINT_ADAPTER,
        endpoint_model=ENDPOINT_MODEL,
        endpoint_base_url=ENDPOINT_BASE_URL,
        endpoint_profile_name=ENDPOINT_PROFILE,
    )

    # 2. A delegates part of the work to sub-agent B.
    chained("agent.delegated", AGENT_A, "agent", AGENT_B, parent=AGENT_A)
    journal.record(
        "agent_spawned",
        agent_id=AGENT_B,
        started_by=AGENT_A,
        endpoint_adapter_name=ENDPOINT_ADAPTER,
        endpoint_model=ENDPOINT_MODEL,
        endpoint_base_url=ENDPOINT_BASE_URL,
        endpoint_profile_name=ENDPOINT_PROFILE,
    )

    # 3. B calls a tool served over MCP, and 4. that tool reads a file
    #    whose own bytes mark it sensitive.
    sensitive_file = workspace / SENSITIVE_PATH
    sensitive_file.parent.mkdir(parents=True, exist_ok=True)
    sensitive_body = f"# {SENSITIVITY_MARKER}\nname,email\nDana,dana@example.org\n"
    sensitive_file.write_text(sensitive_body, encoding="utf-8")
    sensitive_sha = hashlib.sha256(sensitive_body.encode("utf-8")).hexdigest()
    chained(
        "tool.invoked",
        AGENT_B,
        "tool",
        TOOL_NAME,
        server=MCP_SERVER,
        path=SENSITIVE_PATH,
    )
    journal.record("tool_call", agent_id=AGENT_B, tool=TOOL_NAME, server=MCP_SERVER, path=SENSITIVE_PATH)
    chained("data.read", AGENT_B, "file", SENSITIVE_PATH, sha256=sensitive_sha)

    # 5. B sends content to an external model endpoint, which answers.
    chained(
        "model.request",
        AGENT_B,
        "endpoint",
        ENDPOINT_BASE_URL,
        model=ENDPOINT_MODEL,
        adapter=ENDPOINT_ADAPTER,
    )
    journal.record(
        "model_call",
        agent_id=AGENT_B,
        model=ENDPOINT_MODEL,
        base_url=ENDPOINT_BASE_URL,
        input_sha256=sensitive_sha,
    )

    # 6. A uses the output to change the repository.
    changed_file = workspace / CHANGED_PATH
    changed_file.parent.mkdir(parents=True, exist_ok=True)
    changed_body = '"""Summary generated from the customer list."""\n'
    changed_file.write_text(changed_body, encoding="utf-8")
    changed_bytes = changed_body.encode("utf-8")
    spine.record(
        artifact_path=CHANGED_PATH,
        content=changed_bytes,
        actor=AGENT_A,
        step_id="step-write-summary",
        model=ENDPOINT_MODEL,
        timestamp=BASE_TIMESTAMP,
    )
    lineage.emit(
        LineageRecord(
            output_artifact=ArtifactRef(
                path=CHANGED_PATH,
                sha256=hashlib.sha256(changed_bytes).hexdigest(),
            ),
            inputs=[ArtifactRef(path=SENSITIVE_PATH, sha256=sensitive_sha)],
            producer=AgentRef(agent_id=AGENT_A, run_id=RUN_ID, tick_id="step-write-summary"),
            prompt_sha=hashlib.sha256(b"summarise the customer list").hexdigest(),
            model=ENDPOINT_MODEL,
            cost_usd=0.02,
            tokens=512,
            timestamp=float(BASE_TIMESTAMP),
            regulatory_class="high-risk",
        ),
    )
    chained("repo.changed", AGENT_A, "file", CHANGED_PATH, sha256=hashlib.sha256(changed_bytes).hexdigest())
    journal.record("repo_changed", agent_id=AGENT_A, path=CHANGED_PATH)

    return RUN_ID


def export_bundle(workspace: Path, out_dir: Path, *, run_id: str = RUN_ID) -> Path:
    """Export everything an auditor is handed, and nothing else.

    Args:
        workspace: The project root the scenario ran in.
        out_dir: Where to write the bundle. Created if absent.
        run_id: The run to export.

    Returns:
        *out_dir*, now holding the bundle.
    """
    sdd_dir = workspace / ".sdd"
    out_dir.mkdir(parents=True, exist_ok=True)
    kms = FileBasedKMSAdapter(_write_signing_key(workspace), kid=SIGNING_KID)

    audit_receipt = build_receipt(
        sdd_dir / "audit",
        since=AUDIT_SINCE,
        until=AUDIT_UNTIL,
        key=AUDIT_HMAC_KEY,
        kms_adapter=kms,
        output_dir=sdd_dir / "evidence",
        write=True,
    )
    if audit_receipt.receipt_path is None:  # pragma: no cover - write=True always writes
        raise RuntimeError("audit receipt was not written")

    run_receipt = build_run_receipt(
        run_id,
        sdd_dir,
        kms,
        include_audit_range=True,
        audit_hmac_key=AUDIT_HMAC_KEY,
        audit_since=AUDIT_SINCE,
        audit_until=AUDIT_UNTIL,
        write=True,
    )
    if run_receipt.receipt_path is None:  # pragma: no cover - write=True always writes
        raise RuntimeError("run receipt was not written")

    now = datetime.now(tz=UTC)
    article12 = assemble_from_run(
        run_id,
        since=now - timedelta(days=1),
        until=now + timedelta(days=1),
        sdd_dir=sdd_dir,
        workdir=workspace,
        risk_class="high",
        audit_key=AUDIT_HMAC_KEY,
    )
    if article12.bundle.archive_path is None:  # pragma: no cover - write defaults to True
        raise RuntimeError("Article 12 bundle was not written")

    copies = {
        AUDIT_RECEIPT_NAME: audit_receipt.receipt_path,
        RUN_RECEIPT_NAME: run_receipt.receipt_path,
        ARTICLE12_NAME: article12.bundle.archive_path,
    }
    for name, source in copies.items():
        shutil.copyfile(source, out_dir / name)

    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "scenario": (
            "A person starts agent A. A delegates part of the work to sub-agent B. "
            "B calls a tool served over MCP. That tool reads a file marked sensitive. "
            "B sends content to an external model endpoint. The endpoint returns output. "
            "A uses that output to take an action that changes the repository."
        ),
        "files": {name: hashlib.sha256((out_dir / name).read_bytes()).hexdigest() for name in sorted(copies)},
    }
    (out_dir / BUNDLE_MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return out_dir


def build_fixture(root: Path) -> ScenarioFixture:
    """Run the scenario under *root* and export its bundle.

    Args:
        root: Directory to build under. ``workspace/`` and ``bundle/``
            are created inside it.

    Returns:
        The :class:`ScenarioFixture` describing both halves.
    """
    workspace = root / "workspace"
    bundle = root / "bundle"
    workspace.mkdir(parents=True, exist_ok=True)
    run_id = run_scenario(workspace)
    export_bundle(workspace, bundle, run_id=run_id)
    return ScenarioFixture(root=root, workspace=workspace, bundle=bundle, run_id=run_id)


def main(argv: list[str] | None = None) -> int:
    """Regenerate an inspectable fixture. Returns a process exit code."""
    parser = argparse.ArgumentParser(description="Regenerate the auditor conformance fixture.")
    parser.add_argument("--out", type=Path, required=True, help="Directory to build the fixture in.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Remove an existing --out directory first.",
    )
    args = parser.parse_args(argv)

    out: Path = args.out
    if out.exists():
        if not args.force:
            print(f"ERROR: {out} exists; pass --force to replace it", file=sys.stderr)
            return 2
        shutil.rmtree(out)
    fixture = build_fixture(out)
    print(f"workspace: {fixture.workspace}")
    print(f"bundle:    {fixture.bundle}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
