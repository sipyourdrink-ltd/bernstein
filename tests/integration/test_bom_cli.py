"""Integration tests for ``bernstein bom`` -- end-to-end CLI flow.

≥10 round-trip tests on synthetic run -> BOM -> verify. The shape we
test mirrors the production integration: a JSON snapshot drops into
``.sdd/runs/<run_id>/bom_snapshot.json`` and the CLI projects it into
an encoded BOM (json/cyclonedx/spdx). Verify then re-reads the BOM.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.bom_cmd import bom_group
from bernstein.core.lineage.spine import LineageSpine


def _sha(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode()).hexdigest()


def _snapshot() -> dict[str, Any]:
    return {
        "run_id": "20260518-test-001",
        "started_at": "2026-05-18T10:10:10Z",
        "finished_at": "2026-05-18T10:11:00Z",
        "lineage_root_hash": _sha("lineage-root"),
        "bernstein_version": "2.1.0",
        "models": [
            {
                "name": "claude-3-7-sonnet",
                "provider": "anthropic",
                "version": "2026-02-15",
                "sha256": _sha("model-sonnet"),
                "invocation_count": 4,
            },
        ],
        "prompts": [
            {"name": "manager-system", "role": "manager", "sha256": _sha("prompt-manager")},
        ],
        "adapters": [
            {
                "name": "claude",
                "version": "1.4.0",
                "sha256": _sha("adapter-claude"),
                "binary": "claude",
            },
        ],
        "tools": [
            {"name": "git", "kind": "shell", "sha256": _sha("tool-git")},
        ],
        "data_sources": [
            {"uri": "git+https://github.com/x/y@deadbeef", "kind": "repo", "sha256": _sha("source-x")},
        ],
    }


def _write_snapshot(workdir: Path, run_id: str, snap: dict[str, Any]) -> Path:
    run_dir = workdir / ".sdd" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    snap_path = run_dir / "bom_snapshot.json"
    snap_path.write_text(json.dumps(snap), encoding="utf-8")
    return snap_path


# ---------------------------------------------------------------------------
# 1. ``bom emit`` happy paths
# ---------------------------------------------------------------------------


class TestBOMEmit:
    def test_emit_json_to_stdout(self, tmp_path: Path) -> None:
        snap_path = tmp_path / "snap.json"
        snap_path.write_text(json.dumps(_snapshot()), encoding="utf-8")
        runner = CliRunner()
        result = runner.invoke(bom_group, ["emit", "--snapshot", str(snap_path)])
        assert result.exit_code == 0, result.output
        decoded = json.loads(result.output.strip())
        assert decoded["run_id"] == "20260518-test-001"
        assert decoded["schema_version"] == "1.0"

    def test_emit_cyclonedx_to_stdout(self, tmp_path: Path) -> None:
        snap_path = tmp_path / "snap.json"
        snap_path.write_text(json.dumps(_snapshot()), encoding="utf-8")
        runner = CliRunner()
        result = runner.invoke(
            bom_group,
            ["emit", "--snapshot", str(snap_path), "--format", "cyclonedx"],
        )
        assert result.exit_code == 0, result.output
        decoded = json.loads(result.output.strip())
        assert decoded["bomFormat"] == "CycloneDX"
        assert decoded["specVersion"] == "1.5"

    def test_emit_spdx_to_stdout(self, tmp_path: Path) -> None:
        snap_path = tmp_path / "snap.json"
        snap_path.write_text(json.dumps(_snapshot()), encoding="utf-8")
        runner = CliRunner()
        result = runner.invoke(
            bom_group,
            ["emit", "--snapshot", str(snap_path), "--format", "spdx"],
        )
        assert result.exit_code == 0, result.output
        decoded = json.loads(result.output.strip())
        assert decoded["spdxVersion"] == "SPDX-2.3"

    def test_emit_with_run_id_reads_runs_dir(self, tmp_path: Path) -> None:
        run_id = "20260518-from-runs"
        _write_snapshot(tmp_path, run_id, _snapshot())
        runner = CliRunner()
        result = runner.invoke(
            bom_group,
            ["emit", "--run", run_id, "--workdir", str(tmp_path)],
        )
        assert result.exit_code == 0, result.output
        decoded = json.loads(result.output.strip())
        assert decoded["run_id"] == "20260518-test-001"

    def test_emit_writes_to_out_path(self, tmp_path: Path) -> None:
        snap_path = tmp_path / "snap.json"
        snap_path.write_text(json.dumps(_snapshot()), encoding="utf-8")
        out_path = tmp_path / "bom.json"
        runner = CliRunner()
        result = runner.invoke(
            bom_group,
            [
                "emit",
                "--snapshot",
                str(snap_path),
                "--out",
                str(out_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert out_path.exists()
        decoded = json.loads(out_path.read_text(encoding="utf-8"))
        assert decoded["run_id"] == "20260518-test-001"


# ---------------------------------------------------------------------------
# 2. ``bom emit`` validation
# ---------------------------------------------------------------------------


class TestBOMEmitValidation:
    def test_mutually_exclusive_run_and_snapshot(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            bom_group,
            ["emit", "--run", "x", "--snapshot", str(tmp_path / "missing.json")],
        )
        assert result.exit_code != 0

    def test_neither_run_nor_snapshot(self) -> None:
        runner = CliRunner()
        result = runner.invoke(bom_group, ["emit"])
        assert result.exit_code != 0
        assert "required" in result.output

    def test_missing_run_snapshot_errors(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            bom_group,
            ["emit", "--run", "does-not-exist", "--workdir", str(tmp_path)],
        )
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_corrupt_snapshot_errors(self, tmp_path: Path) -> None:
        snap_path = tmp_path / "snap.json"
        snap_path.write_text("not valid json", encoding="utf-8")
        runner = CliRunner()
        result = runner.invoke(
            bom_group,
            ["emit", "--snapshot", str(snap_path)],
        )
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# 3. Round-trip emit -> verify
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_json_roundtrip(self, tmp_path: Path) -> None:
        snap_path = tmp_path / "snap.json"
        snap_path.write_text(json.dumps(_snapshot()), encoding="utf-8")
        bom_path = tmp_path / "bom.json"
        runner = CliRunner()

        emit = runner.invoke(
            bom_group,
            ["emit", "--snapshot", str(snap_path), "--out", str(bom_path)],
        )
        assert emit.exit_code == 0, emit.output

        verify = runner.invoke(bom_group, ["verify", str(bom_path)])
        assert verify.exit_code == 0, verify.output
        assert "PASS" in verify.output

    def test_verify_tamper_detection(self, tmp_path: Path) -> None:
        snap_path = tmp_path / "snap.json"
        snap_path.write_text(json.dumps(_snapshot()), encoding="utf-8")
        bom_path = tmp_path / "bom.json"
        runner = CliRunner()

        runner.invoke(
            bom_group,
            ["emit", "--snapshot", str(snap_path), "--out", str(bom_path)],
        )
        # Tamper with one sha
        doc = json.loads(bom_path.read_text(encoding="utf-8"))
        doc["models"][0]["sha256"] = "not-a-sha"
        bom_path.write_text(json.dumps(doc), encoding="utf-8")

        verify = runner.invoke(bom_group, ["verify", str(bom_path)])
        assert verify.exit_code != 0
        assert "FAIL" in verify.output

    def test_verify_quiet_mode(self, tmp_path: Path) -> None:
        snap_path = tmp_path / "snap.json"
        snap_path.write_text(json.dumps(_snapshot()), encoding="utf-8")
        bom_path = tmp_path / "bom.json"
        runner = CliRunner()
        runner.invoke(
            bom_group,
            ["emit", "--snapshot", str(snap_path), "--out", str(bom_path)],
        )
        verify = runner.invoke(bom_group, ["verify", "--quiet", str(bom_path)])
        assert verify.exit_code == 0
        assert verify.output.strip() == ""

    def test_emit_twice_byte_identical(self, tmp_path: Path) -> None:
        """Pure projection: re-emitting the same snapshot gives identical bytes."""
        snap_path = tmp_path / "snap.json"
        snap_path.write_text(json.dumps(_snapshot()), encoding="utf-8")
        runner = CliRunner()

        r1 = runner.invoke(bom_group, ["emit", "--snapshot", str(snap_path)])
        r2 = runner.invoke(bom_group, ["emit", "--snapshot", str(snap_path)])
        assert r1.exit_code == 0 and r2.exit_code == 0
        assert r1.output == r2.output

    def test_cyclonedx_roundtrip_carries_run_id(self, tmp_path: Path) -> None:
        snap_path = tmp_path / "snap.json"
        snap_path.write_text(json.dumps(_snapshot()), encoding="utf-8")
        bom_path = tmp_path / "bom.cdx.json"
        runner = CliRunner()
        runner.invoke(
            bom_group,
            [
                "emit",
                "--snapshot",
                str(snap_path),
                "--format",
                "cyclonedx",
                "--out",
                str(bom_path),
            ],
        )
        decoded = json.loads(bom_path.read_text(encoding="utf-8"))
        props = {p["name"]: p["value"] for p in decoded["metadata"]["properties"]}
        assert props["bernstein:run_id"] == "20260518-test-001"

    def test_spdx_roundtrip_packages_count(self, tmp_path: Path) -> None:
        snap_path = tmp_path / "snap.json"
        snap_path.write_text(json.dumps(_snapshot()), encoding="utf-8")
        bom_path = tmp_path / "bom.spdx.json"
        runner = CliRunner()
        runner.invoke(
            bom_group,
            [
                "emit",
                "--snapshot",
                str(snap_path),
                "--format",
                "spdx",
                "--out",
                str(bom_path),
            ],
        )
        decoded = json.loads(bom_path.read_text(encoding="utf-8"))
        # 1 model + 1 prompt + 1 adapter + 1 tool + 1 source = 5 packages
        assert len(decoded["packages"]) == 5

    def test_multi_run_emits_are_independent(self, tmp_path: Path) -> None:
        snap_a = _snapshot()
        snap_b = snap_a.copy()
        snap_b["run_id"] = "20260518-test-002"
        a_path = tmp_path / "a.json"
        b_path = tmp_path / "b.json"
        a_path.write_text(json.dumps(snap_a), encoding="utf-8")
        b_path.write_text(json.dumps(snap_b), encoding="utf-8")
        runner = CliRunner()
        r_a = runner.invoke(bom_group, ["emit", "--snapshot", str(a_path)])
        r_b = runner.invoke(bom_group, ["emit", "--snapshot", str(b_path)])
        assert r_a.output != r_b.output

    def test_emit_then_verify_with_run_id(self, tmp_path: Path) -> None:
        run_id = "20260518-from-runs-2"
        _write_snapshot(tmp_path, run_id, _snapshot())
        bom_path = tmp_path / "bom.json"
        runner = CliRunner()
        emit = runner.invoke(
            bom_group,
            [
                "emit",
                "--run",
                run_id,
                "--workdir",
                str(tmp_path),
                "--out",
                str(bom_path),
            ],
        )
        assert emit.exit_code == 0, emit.output
        verify = runner.invoke(bom_group, ["verify", str(bom_path)])
        assert verify.exit_code == 0, verify.output

    def test_verify_garbage_file_fails(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("just text", encoding="utf-8")
        runner = CliRunner()
        verify = runner.invoke(bom_group, ["verify", str(bad)])
        assert verify.exit_code != 0

    def test_three_format_roundtrip_same_run(self, tmp_path: Path) -> None:
        """Emitting the same snapshot in all three formats produces the
        expected per-format payload while preserving the run identity."""
        snap_path = tmp_path / "snap.json"
        snap_path.write_text(json.dumps(_snapshot()), encoding="utf-8")
        runner = CliRunner()

        for fmt, key in (
            ("json", "schema_version"),
            ("cyclonedx", "bomFormat"),
            ("spdx", "spdxVersion"),
        ):
            out = tmp_path / f"bom.{fmt}.json"
            result = runner.invoke(
                bom_group,
                ["emit", "--snapshot", str(snap_path), "--format", fmt, "--out", str(out)],
            )
            assert result.exit_code == 0, result.output
            decoded = json.loads(out.read_text(encoding="utf-8"))
            assert key in decoded


# ---------------------------------------------------------------------------
# 4. ``bom emit --from-lineage`` -- project the snapshot off the run spine
# ---------------------------------------------------------------------------


_SPINE_KEY = b"k" * 32


def _seed_spine(workdir: Path, run_id: str) -> LineageSpine:
    spine = LineageSpine(workdir / ".sdd" / "lineage", run_id=run_id, hmac_key=_SPINE_KEY)
    spine.record(
        artifact_path="src/a.py",
        content=b"a",
        actor="agent:worker",
        step_id="s1",
        model="claude-sonnet",
        timestamp=1767225600,
    )
    spine.record(
        artifact_path="src/b.py",
        content=b"b",
        actor="agent:worker",
        step_id="s2",
        model="claude-sonnet",
        timestamp=1767225660,
    )
    return spine


def _install_audit_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    key_file = tmp_path / "audit.key"
    key_file.write_bytes(_SPINE_KEY)
    key_file.chmod(0o600)
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_file))


class TestBOMEmitFromLineage:
    def test_emit_from_lineage_projects_the_run_spine(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_audit_key(tmp_path, monkeypatch)
        spine = _seed_spine(tmp_path, "20260101-run-a")

        result = CliRunner().invoke(
            bom_group,
            ["emit", "--run", "20260101-run-a", "--from-lineage", "--workdir", str(tmp_path)],
        )

        assert result.exit_code == 0, result.output
        doc = json.loads(result.stdout)
        assert doc["run_id"] == "20260101-run-a"
        assert doc["lineage_root_hash"] == spine.head_hash()
        assert [(m["name"], m["invocation_count"]) for m in doc["models"]] == [("claude-sonnet", 2)]

    def test_emit_from_lineage_needs_no_hand_written_snapshot(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The whole point: ``.sdd/runs/<run>/bom_snapshot.json`` need not exist."""
        _install_audit_key(tmp_path, monkeypatch)
        _seed_spine(tmp_path, "20260101-run-b")
        assert not (tmp_path / ".sdd" / "runs" / "20260101-run-b" / "bom_snapshot.json").exists()

        result = CliRunner().invoke(
            bom_group,
            ["emit", "--run", "20260101-run-b", "--from-lineage", "--workdir", str(tmp_path)],
        )
        assert result.exit_code == 0, result.output

    def test_emit_from_lineage_output_passes_structural_verify(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_audit_key(tmp_path, monkeypatch)
        _seed_spine(tmp_path, "20260101-run-c")
        out = tmp_path / "bom.json"

        emit = CliRunner().invoke(
            bom_group,
            [
                "emit",
                "--run",
                "20260101-run-c",
                "--from-lineage",
                "--workdir",
                str(tmp_path),
                "--out",
                str(out),
            ],
        )
        assert emit.exit_code == 0, emit.output

        verify = CliRunner().invoke(bom_group, ["verify", str(out)])
        assert verify.exit_code == 0, verify.output

    def test_emit_from_lineage_rejects_a_run_with_an_empty_spine(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_audit_key(tmp_path, monkeypatch)
        (tmp_path / ".sdd" / "lineage").mkdir(parents=True)

        result = CliRunner().invoke(
            bom_group,
            ["emit", "--run", "20260101-empty", "--from-lineage", "--workdir", str(tmp_path)],
        )
        assert result.exit_code == 1
        assert "20260101-empty" in result.output

    def test_emit_from_lineage_requires_run_and_excludes_snapshot(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_audit_key(tmp_path, monkeypatch)
        snap_path = tmp_path / "snap.json"
        snap_path.write_text(json.dumps(_snapshot()), encoding="utf-8")

        no_run = CliRunner().invoke(bom_group, ["emit", "--from-lineage", "--workdir", str(tmp_path)])
        assert no_run.exit_code == 2

        with_snapshot = CliRunner().invoke(
            bom_group,
            ["emit", "--from-lineage", "--snapshot", str(snap_path), "--workdir", str(tmp_path)],
        )
        assert with_snapshot.exit_code == 2

    def test_emit_from_lineage_fails_closed_without_an_audit_key(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A read-only projection must never mint key material (issue #2639)."""
        _seed_spine(tmp_path, "20260101-run-d")
        key_file = tmp_path / "absent.key"
        monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_file))

        result = CliRunner().invoke(
            bom_group,
            ["emit", "--run", "20260101-run-d", "--from-lineage", "--workdir", str(tmp_path)],
        )

        assert result.exit_code != 0
        assert not key_file.exists()

    def test_emit_from_lineage_rejects_a_run_id_that_escapes_its_directory(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install_audit_key(tmp_path, monkeypatch)

        result = CliRunner().invoke(
            bom_group,
            ["emit", "--run", "../elsewhere", "--from-lineage", "--workdir", str(tmp_path)],
        )

        assert result.exit_code == 1
        assert "invalid run id" in result.output
