"""Tests for reading postmortem and insights evidence via ``--from-archive`` (#5443)."""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.main import cli
from bernstein.cli.run_archive import (
    ARCHIVE_SECTIONS,
    ArchiveManifestError,
    collect_archive_files,
    create_archive,
    open_verified_archive_workdir,
    resolve_run_source,
)
from bernstein.core.observability.run_source import ArchiveRunSource, LiveRunSource


def _populate_failed_run_workspace(workdir: Path, run_id: str = "run-42") -> None:
    """Create a realistic live ``.sdd/`` workspace with a failed task."""
    run_dir = workdir / ".sdd" / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "summary.json").write_text(
        json.dumps({"run_id": run_id, "goal": "Fix broken parser"}),
        encoding="utf-8",
    )

    metrics_dir = workdir / ".sdd" / "metrics"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "task_t1.json").write_text(
        json.dumps(
            {
                "task_id": "t1",
                "role": "backend",
                "model": "sonnet",
                "session_id": "sess-001",
                "success": True,
                "start_time": 1700000000.0,
                "end_time": 1700000030.0,
            }
        ),
        encoding="utf-8",
    )
    (metrics_dir / "task_t2.json").write_text(
        json.dumps(
            {
                "task_id": "t2",
                "role": "backend",
                "model": "sonnet",
                "session_id": "sess-002",
                "success": False,
                "start_time": 1700000040.0,
                "end_time": 1700000090.0,
            }
        ),
        encoding="utf-8",
    )

    runtime_dir = workdir / ".sdd" / "runtime"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "run_id").write_text(f"{run_id}\n", encoding="utf-8")
    (runtime_dir / "sess-002.log").write_text(
        "[2026-11-15 08:00:00] ERROR SyntaxError: invalid syntax in parser.py\nModified: src/parser.py\n",
        encoding="utf-8",
    )


def _rewrite_zip_member(src: Path, dst: Path, member: str, payload: bytes | None) -> None:
    """Rewrite *src* into *dst*, replacing or removing *member*."""
    with zipfile.ZipFile(src) as source, zipfile.ZipFile(dst, "w") as out:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == member:
                if payload is None:
                    continue
                data = payload
            out.writestr(info, data)


def test_resolve_run_source_selects_live_or_archive(tmp_path: Path) -> None:
    archive = tmp_path / "run.zip"
    live = resolve_run_source(tmp_path, None)
    bundled = resolve_run_source(tmp_path, archive)

    assert isinstance(live, LiveRunSource)
    assert live.kind == "live"
    with live.open_workdir() as resolved:
        assert resolved == tmp_path.resolve()

    assert isinstance(bundled, ArchiveRunSource)
    assert bundled.kind == "archive"
    assert bundled.location == archive.resolve()


def test_archive_sections_capture_postmortem_inputs(tmp_path: Path) -> None:
    _populate_failed_run_workspace(tmp_path)
    (tmp_path / ".sdd" / "logs").mkdir(parents=True)
    (tmp_path / ".sdd" / "logs" / "sess-003.log").write_text("log\n", encoding="utf-8")

    collected = {p.relative_to(tmp_path).as_posix() for p in collect_archive_files(tmp_path)}
    assert ".sdd/runs/run-42/summary.json" in collected
    assert ".sdd/metrics/task_t1.json" in collected
    assert ".sdd/metrics/task_t2.json" in collected
    assert ".sdd/runtime/sess-002.log" in collected
    assert ".sdd/logs/sess-003.log" in collected
    assert set(ARCHIVE_SECTIONS) == {"tasks", "logs", "costs", "audit", "metrics", "traces", "config"}


def test_postmortem_from_archive_matches_live_after_workspace_deleted(tmp_path: Path) -> None:
    workspace = tmp_path / "ephemeral-ws"
    _populate_failed_run_workspace(workspace)

    runner = CliRunner()
    live_out = tmp_path / "live.md"
    live_res = runner.invoke(
        cli,
        ["report", "postmortem", "--workdir", str(workspace), "-o", str(live_out)],
    )
    assert live_res.exit_code == 0, live_res.output

    archive = tmp_path / "run.zip"
    create_archive(workspace, archive)
    shutil.rmtree(workspace)
    assert not workspace.exists()

    archived_out = tmp_path / "archived.md"
    arch_res = runner.invoke(
        cli,
        ["report", "postmortem", "--from-archive", str(archive), "-o", str(archived_out)],
    )
    assert arch_res.exit_code == 0, arch_res.output

    def _strip_generated_line(text: str) -> list[str]:
        return [line for line in text.splitlines() if not line.startswith("Generated:")]

    assert _strip_generated_line(archived_out.read_text(encoding="utf-8")) == _strip_generated_line(
        live_out.read_text(encoding="utf-8")
    )
    report_text = archived_out.read_text(encoding="utf-8")
    assert "run-42" in report_text
    assert "Fix broken parser" in report_text
    assert "compile_error" in report_text
    assert "SyntaxError: invalid syntax in parser.py" in report_text


def test_insights_from_archive_succeeds_for_verified_bundle(tmp_path: Path) -> None:
    workspace = tmp_path / "ephemeral-ws"
    _populate_failed_run_workspace(workspace)
    archive = tmp_path / "run.zip"
    create_archive(workspace, archive)
    shutil.rmtree(workspace)

    res = CliRunner().invoke(
        cli,
        ["insights", "--from-archive", str(archive), "--format", "json", "--since", "14d"],
    )
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["since"] == "14d"
    assert payload["source"] == "archive"


def test_postmortem_and_insights_refuse_tampered_archive(tmp_path: Path) -> None:
    workspace = tmp_path / "ephemeral-ws"
    _populate_failed_run_workspace(workspace)
    archive = tmp_path / "run.zip"
    create_archive(workspace, archive)

    tampered = tmp_path / "tampered.zip"
    _rewrite_zip_member(archive, tampered, ".sdd/runs/run-42/summary.json", b'{"goal": "forged"}')

    runner = CliRunner()
    pm_res = runner.invoke(cli, ["report", "postmortem", "--from-archive", str(tampered)])
    assert pm_res.exit_code == 1
    assert "Archive verification failed" in pm_res.output

    in_res = runner.invoke(cli, ["insights", "--from-archive", str(tampered)])
    assert in_res.exit_code == 1
    assert "Archive verification failed" in in_res.output


def test_open_verified_archive_rejects_path_traversal(tmp_path: Path) -> None:
    import hashlib

    archive = tmp_path / "traversal.zip"
    payload = b"evil\n"
    digest = hashlib.sha256(payload).hexdigest()
    manifest = {
        "created_at": "2026-11-15T00:00:00+00:00",
        "bernstein_version": "3.24.1",
        "run_id": None,
        "file_count": 1,
        "total_size_bytes": len(payload),
        "sections": ["tasks"],
        "files": {"../escape.txt": digest},
    }
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../escape.txt", payload)
        zf.writestr("manifest.json", json.dumps(manifest))

    try:
        with open_verified_archive_workdir(archive):
            raise AssertionError("Expected ArchiveManifestError for traversal member")
    except ArchiveManifestError as exc:
        assert "traversal" in str(exc) or "unsafe" in str(exc)
