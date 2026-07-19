"""CLI + audit-verify integration tests for agent-posted artifacts (#2553).

``bernstein artifacts list <task>`` lists posted versions with verify state;
``bernstein artifacts show <task> <key>`` renders a key's latest version and
withholds content when tampered. ``bernstein audit verify`` is extended so a
tampered artifact blob is detected exactly like a tampered chain entry, with the
artifact key and journal position named.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.artifacts_cmd import artifacts_group
from bernstein.cli.commands.audit_cmd import audit_group
from bernstein.core.evidence.bundle import EvidenceStore
from bernstein.core.evidence.run_artifacts import ArtifactPayload, post_run_artifact
from bernstein.core.security.audit import load_or_create_audit_key
from bernstein.core.security.audit_chain import AuditChainStore


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    (tmp_path / ".sdd").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _post(
    project: Path,
    *,
    task_id: str = "task-1",
    key: str = "summary",
    body: str = "hello",
    chain: AuditChainStore | None = None,
) -> None:
    post_run_artifact(
        sdd_dir=project / ".sdd",
        task_id=task_id,
        key=key,
        payload=ArtifactPayload.report(body),
        actor="worker-a",
        hmac_key=load_or_create_audit_key(),
        audit_chain=chain,
    )


def _chain(project: Path) -> AuditChainStore:
    return AuditChainStore(project / ".sdd" / "audit", key=load_or_create_audit_key())


def test_artifacts_list_renders(project: Path) -> None:
    _post(project)
    result = CliRunner().invoke(artifacts_group, ["list", "task-1", "-w", str(project)])
    assert result.exit_code == 0, result.output
    assert "summary" in result.output
    assert "ok" in result.output


def test_artifacts_list_missing(project: Path) -> None:
    result = CliRunner().invoke(artifacts_group, ["list", "nope", "-w", str(project)])
    assert result.exit_code == 1, result.output


def test_artifacts_show_renders_content_and_history(project: Path) -> None:
    _post(project, body="first")
    _post(project, body="second")  # v2 of the same key
    result = CliRunner().invoke(artifacts_group, ["show", "task-1", "summary", "-w", str(project)])
    assert result.exit_code == 0, result.output
    assert "version=2" in result.output
    assert "second" in result.output
    assert "Version history" in result.output


def test_artifacts_show_tampered_withholds_content(project: Path) -> None:
    _post(project, body="secret-content")
    # Flip a byte in the stored blob.
    store = EvidenceStore(project / ".sdd" / "evidence")
    from bernstein.core.evidence.run_artifacts import read_artifact_rows

    rec = read_artifact_rows(project / ".sdd", "task-1")[0]
    blob_path = store.blob_path(rec.content_hash)
    data = bytearray(blob_path.read_bytes())
    data[-2] ^= 0x01
    blob_path.write_bytes(bytes(data))

    result = CliRunner().invoke(artifacts_group, ["show", "task-1", "summary", "-w", str(project)])
    assert result.exit_code == 2, result.output
    assert "TAMPERED" in result.output
    assert "secret-content" not in result.output


def test_audit_verify_passes_with_intact_artifacts(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _post(project, chain=_chain(project))
    # The audit group resolves ``.sdd`` relative to CWD; run inside the project.
    monkeypatch.chdir(project)
    result = CliRunner().invoke(audit_group, ["verify"])
    # The artifact pillar passes for intact artifacts. (The overall exit code
    # also reflects unrelated pillars -- e.g. the Merkle seal, absent in this
    # minimal fixture -- so we assert the artifact-pillar result specifically.)
    assert "Run Artifact Verification Passed" in result.output
    assert "Run Artifact Verification FAILED" not in result.output


def test_audit_verify_fails_on_tampered_artifact(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _post(project, body="tamper-me", chain=_chain(project))
    store = EvidenceStore(project / ".sdd" / "evidence")
    from bernstein.core.evidence.run_artifacts import read_artifact_rows

    rec = read_artifact_rows(project / ".sdd", "task-1")[0]
    blob_path = store.blob_path(rec.content_hash)
    data = bytearray(blob_path.read_bytes())
    data[0] ^= 0x01
    blob_path.write_bytes(bytes(data))

    monkeypatch.chdir(project)
    result = CliRunner().invoke(audit_group, ["verify"])
    assert result.exit_code == 1, result.output
    assert "Run Artifact Verification FAILED" in result.output
    assert "summary" in result.output


class TestMalformedTaskIdIsHandledNotRaised:
    """A task id comes straight off the command line, so a typo or a pasted
    path is ordinary input, not a programming error.

    The read helpers validate the id before it addresses a path; that
    validation must not turn a mistyped argument into an uncaught traceback.
    """

    @pytest.mark.parametrize(
        "task_id",
        [
            "no/such/task",
            "task with spaces",
            "../../etc/passwd",
            "task\nid",
        ],
    )
    def test_list_reports_no_artifacts_instead_of_crashing(self, project: Path, task_id: str) -> None:
        res = CliRunner().invoke(artifacts_group, ["list", "--workdir", str(project), task_id])
        assert not isinstance(res.exception, Exception) or isinstance(res.exception, SystemExit), (
            f"uncaught {type(res.exception).__name__}: {res.exception}"
        )
        assert res.exit_code == 1
        assert "No artifacts found" in res.output

    @pytest.mark.parametrize("task_id", ["no/such/task", "task with spaces"])
    def test_show_reports_no_artifact_instead_of_crashing(self, project: Path, task_id: str) -> None:
        res = CliRunner().invoke(artifacts_group, ["show", "--workdir", str(project), task_id, "report"])
        assert not isinstance(res.exception, Exception) or isinstance(res.exception, SystemExit), (
            f"uncaught {type(res.exception).__name__}: {res.exception}"
        )
        assert res.exit_code == 1
        assert "No artifact" in res.output

    def test_readers_return_empty_for_an_id_that_names_no_task(self, project: Path) -> None:
        """The helper contract the CLI depends on, asserted directly."""
        from bernstein.core.evidence.run_artifacts import (
            latest_versions,
            read_artifact_rows,
            verify_run_artifacts,
        )

        sdd = project / ".sdd"
        assert read_artifact_rows(sdd, "no/such/task") == []
        assert latest_versions(sdd, "no/such/task") == {}
        assert verify_run_artifacts(sdd, "no/such/task", hmac_key=b"k" * 32) == []

    def test_the_write_path_still_refuses_a_malformed_id(self, project: Path) -> None:
        """Readers absorbing a bad id must not soften the writer: posting is
        where a typed refusal belongs, and it still fires."""
        from bernstein.core.evidence.run_artifacts import (
            ArtifactValidationError,
            post_run_artifact,
        )

        with pytest.raises(ArtifactValidationError):
            post_run_artifact(
                sdd_dir=project / ".sdd",
                task_id="no/such/task",
                key="report",
                payload=ArtifactPayload.report("body"),
                actor="w",
                hmac_key=b"k" * 32,
            )
