"""CLI + audit-verify integration tests for evidence bundles (issue #2362).

``bernstein evidence show <task>`` renders a sealed bundle; ``bernstein evidence
verify <task>`` recomputes it offline. ``bernstein audit verify`` is extended so
a tampered evidence file is detected exactly like a tampered chain entry, with
the offending file named.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.audit_cmd import audit_group
from bernstein.cli.commands.evidence_cmd import evidence_group
from bernstein.core.evidence.bundle import EvidenceProducer, EvidenceStore, run_evidence_gate


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    (tmp_path / ".sdd").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _seal(project: Path, *, task_id: str = "task-1", tests_exit: int = 0) -> None:
    producers = (
        EvidenceProducer(name="tests", kind="test", command=("run",), required=True),
        EvidenceProducer(name="lint", kind="lint", command=("lint",), required=False),
    )

    def runner(p: EvidenceProducer) -> tuple[int, bytes]:
        return (tests_exit, b"runner output\n") if p.name == "tests" else (0, b"0 findings\n")

    run_evidence_gate(
        workdir=project,
        task_id=task_id,
        producers=producers,
        runner=runner,
        timestamp=1000,
    )


def test_evidence_show_renders_bundle(project: Path) -> None:
    _seal(project)
    result = CliRunner().invoke(evidence_group, ["show", "task-1", "-w", str(project)])
    assert result.exit_code == 0, result.output
    assert "task-1" in result.output
    assert "tests" in result.output
    assert "lint" in result.output


def test_evidence_show_missing_task(project: Path) -> None:
    result = CliRunner().invoke(evidence_group, ["show", "nope", "-w", str(project)])
    assert result.exit_code == 1, result.output


def test_evidence_verify_ok(project: Path) -> None:
    _seal(project)
    result = CliRunner().invoke(evidence_group, ["verify", "task-1", "-w", str(project)])
    assert result.exit_code == 0, result.output
    assert "OK" in result.output


def test_evidence_verify_tamper_exit_2(project: Path) -> None:
    _seal(project)
    from bernstein.core.evidence.bundle import read_evidence_bundle

    bundle = read_evidence_bundle(project, "task-1")
    assert bundle is not None
    tests_item = next(i for i in bundle.items if i.name == "tests")
    store = EvidenceStore(project / ".sdd" / "evidence")
    store.blob_path(tests_item.content_hash).write_bytes(b"forged\n")

    result = CliRunner().invoke(evidence_group, ["verify", "task-1", "-w", str(project)])
    assert result.exit_code == 2, result.output
    assert "tests" in result.output


def test_audit_verify_names_tampered_evidence_file(project: Path) -> None:
    _seal(project)
    from bernstein.core.evidence.bundle import read_evidence_bundle

    bundle = read_evidence_bundle(project, "task-1")
    assert bundle is not None
    tests_item = next(i for i in bundle.items if i.name == "tests")
    store = EvidenceStore(project / ".sdd" / "evidence")
    store.blob_path(tests_item.content_hash).write_bytes(b"forged evidence\n")

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=project.parent):
        # ``audit verify`` reads .sdd relative to cwd, so run inside the project.
        import os

        os.chdir(project)
        result = runner.invoke(audit_group, ["verify", "--hmac-only"])
    # A tampered evidence report fails verify with the file named.
    assert result.exit_code == 1, result.output
    assert "tests" in result.output
    assert "Evidence" in result.output


def test_audit_verify_evidence_passes_when_intact(project: Path) -> None:
    _seal(project)
    runner = CliRunner()
    import os

    cwd = os.getcwd()
    try:
        os.chdir(project)
        result = runner.invoke(audit_group, ["verify", "--hmac-only"])
    finally:
        os.chdir(cwd)
    assert result.exit_code == 0, result.output
