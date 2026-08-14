"""Tests for ``bernstein debug bundle`` (cli/debug_bundle.py).

Covers the acceptance criteria of the debug-bundle export ticket:

- Empty project: the command still produces a valid bundle.
- Missing run id: ``--last`` works even when no ``run_id`` file exists.
- Redaction roundtrip: secrets in ``bernstein.yaml`` are scrubbed in
  the archived copy, and the manifest counts the redactions.
- Manifest schema: every required field is present with the expected
  types, and ``schema_version`` is set.
- File-inclusion budget: ``--include-source-snippets`` does not exceed
  the documented byte budget.
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from bernstein.core.traces import TraceStep, TraceStore, new_trace
from click.testing import CliRunner

from bernstein.cli.debug_bundle import (
    _SOURCE_SNIPPET_BYTE_BUDGET,
    DebugManifest,
    Selection,
    build_debug_bundle,
    debug_group,
    detect_install_method,
    resolve_selection,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_doctor(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any] | None = None) -> None:
    """Replace doctor-snapshot collection with a stub so tests do not shell out."""
    monkeypatch.setattr(
        "bernstein.cli.debug_bundle.collect_doctor_snapshot",
        lambda: payload if payload is not None else {"status": "ok"},
    )


def _stub_source_snippets(monkeypatch: pytest.MonkeyPatch, files: list[Path]) -> None:
    """Replace git-based source collection with a fixed list."""
    monkeypatch.setattr(
        "bernstein.cli.debug_bundle.collect_source_snippets",
        lambda _workdir, _limit: files,
    )


# ---------------------------------------------------------------------------
# Empty project
# ---------------------------------------------------------------------------


def test_empty_project_produces_valid_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty project still yields a manifest and a readable ZIP."""
    _stub_doctor(monkeypatch)
    result = build_debug_bundle(tmp_path)

    assert result.output_path is not None
    assert result.output_path.is_file()

    with zipfile.ZipFile(result.output_path) as zf:
        names = set(zf.namelist())
        assert "manifest.json" in names
        # Even an empty project records a (missing-marker) bernstein.yaml
        # and the doctor snapshot.
        assert "bernstein.yaml" in names
        assert "doctor.json" in names
        manifest_raw = zf.read("manifest.json").decode("utf-8")
        manifest = json.loads(manifest_raw)
        assert manifest["schema_version"] == 1
        assert manifest["selection"]["mode"] == "last"
        assert manifest["selection"]["id"] is None


# ---------------------------------------------------------------------------
# Selection resolution / missing run id
# ---------------------------------------------------------------------------


def test_resolve_selection_prefers_task_over_run(tmp_path: Path) -> None:
    """Explicit ``--task`` wins over ``--run`` and ``--last``."""
    sel = resolve_selection(tmp_path, task="t-1", run="r-1", last=True)
    assert sel == Selection(mode="task", ident="t-1")


def test_resolve_selection_prefers_run_over_last(tmp_path: Path) -> None:
    """Explicit ``--run`` wins over the default ``--last``."""
    sel = resolve_selection(tmp_path, task=None, run="r-9", last=True)
    assert sel == Selection(mode="run", ident="r-9")


def test_last_with_missing_run_id_returns_none(tmp_path: Path) -> None:
    """``--last`` with no recorded run id yields mode=last and ident=None."""
    sel = resolve_selection(tmp_path, task=None, run=None, last=True)
    assert sel.mode == "last"
    assert sel.ident is None


def test_last_reads_run_id_from_runtime_file(tmp_path: Path) -> None:
    """``--last`` picks up ``.sdd/runtime/run_id`` when present."""
    runtime = tmp_path / ".sdd" / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "run_id").write_text("run-abc\n", encoding="utf-8")
    sel = resolve_selection(tmp_path, task=None, run=None, last=True)
    assert sel == Selection(mode="last", ident="run-abc")


# ---------------------------------------------------------------------------
# Redaction roundtrip
# ---------------------------------------------------------------------------


def test_bernstein_yaml_secrets_are_redacted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A real API key in ``bernstein.yaml`` does not survive into the ZIP."""
    secret_value = "sk-ant-api03-nopenopelongtokenvalue"
    (tmp_path / "bernstein.yaml").write_text(
        f"providers:\n  ANTHROPIC_API_KEY: {secret_value}\n  base_url: https://api.example.com\n",
        encoding="utf-8",
    )
    _stub_doctor(monkeypatch)

    result = build_debug_bundle(tmp_path)

    assert result.output_path is not None
    with zipfile.ZipFile(result.output_path) as zf:
        body = zf.read("bernstein.yaml").decode("utf-8")
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))

    assert secret_value not in body
    assert "REDACTED" in body
    assert manifest["redactions_applied"] >= 1


def test_persisted_trace_secret_cannot_reappear_in_debug_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canary = "ghp_0123456789abcdefghijklmnopqrstuv"
    store = TraceStore(tmp_path / ".sdd" / "traces")
    trace = new_trace("run-secret", ["task-secret"], "backend", "sonnet", "high")
    trace.steps.append(
        TraceStep(type="verify", timestamp=1.0, detail=f"Authorization: Bearer {canary}")
    )
    store.write(trace)
    (tmp_path / ".sdd" / "runtime").mkdir(parents=True)
    (tmp_path / ".sdd" / "runtime" / "run_id").write_text("task-secret\n", encoding="utf-8")
    _stub_doctor(monkeypatch)

    result = build_debug_bundle(tmp_path, task="task-secret")

    assert result.output_path is not None
    with zipfile.ZipFile(result.output_path) as zf:
        trace_payloads = b"\n".join(zf.read(name) for name in zf.namelist() if name.startswith("traces/"))
    assert canary.encode() not in trace_payloads
    assert b"REDACTED" in trace_payloads


# ---------------------------------------------------------------------------
# Manifest schema validation
# ---------------------------------------------------------------------------


_REQUIRED_MANIFEST_FIELDS: dict[str, type] = {
    "schema_version": int,
    "created_at": str,
    "bernstein_version": str,
    "python_version": str,
    "os": str,
    "install_method": str,
    "selection": dict,
    "files": list,
    "redactions_applied": int,
}


def test_manifest_schema_has_all_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``manifest.json`` carries every documented field with the right type."""
    _stub_doctor(monkeypatch)
    result = build_debug_bundle(tmp_path, manifest_only=True)

    payload = asdict(result.manifest)
    for name, expected_type in _REQUIRED_MANIFEST_FIELDS.items():
        assert name in payload, f"manifest missing field {name!r}"
        assert isinstance(payload[name], expected_type), (
            f"manifest field {name!r} has wrong type: {type(payload[name]).__name__} != {expected_type.__name__}"
        )

    # Selection sub-keys are also part of the schema contract.
    assert "mode" in payload["selection"]
    assert "id" in payload["selection"]

    # install_method must be one of the documented enum values.
    assert payload["install_method"] in {"pip", "pipx", "uv-tool", "editable", "unknown"}


def test_manifest_only_skips_zip_creation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--manifest-only`` does not write a ZIP."""
    _stub_doctor(monkeypatch)
    result = build_debug_bundle(tmp_path, manifest_only=True)
    assert result.output_path is None
    # No zip files in the tmpdir
    assert not list(tmp_path.glob("*.zip"))


def test_manifest_dataclass_round_trip() -> None:
    """:class:`DebugManifest` round-trips through ``asdict`` cleanly."""
    m = DebugManifest(
        schema_version=1,
        created_at="2026-05-19T00:00:00+00:00",
        bernstein_version="9.9.9",
        python_version="3.13.1",
        os="Darwin-25.0.0",
        install_method="pip",
        selection={"mode": "last", "id": None},
        files=["bernstein.yaml", "doctor.json"],
        redactions_applied=2,
    )
    payload = asdict(m)
    assert payload["selection"] == {"mode": "last", "id": None}
    assert payload["files"] == ["bernstein.yaml", "doctor.json"]


# ---------------------------------------------------------------------------
# File-inclusion budget
# ---------------------------------------------------------------------------


def test_source_snippet_budget_is_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--include-source-snippets`` never exceeds the byte budget."""
    src = tmp_path / "src" / "pkg"
    src.mkdir(parents=True)
    # Each file is ~40 KiB; with a 64 KiB budget at most 2 should land.
    # Use a benign repeating line so the redactor's secret patterns don't
    # have to backtrack on a 40 KiB run of a single character.
    big_files: list[Path] = []
    line = "def some_function_name(arg):\n    return arg + 1\n"
    payload = line * (40 * 1024 // len(line) + 1)
    for index in range(5):
        path = src / f"big_{index}.py"
        path.write_text(payload, encoding="utf-8")
        big_files.append(path)
    _stub_source_snippets(monkeypatch, big_files)
    _stub_doctor(monkeypatch)

    result = build_debug_bundle(
        tmp_path,
        include_source_snippets=5,
    )

    assert result.output_path is not None
    with zipfile.ZipFile(result.output_path) as zf:
        source_entries = [n for n in zf.namelist() if n.startswith("source/")]
        total_bytes = sum(zf.getinfo(n).file_size for n in source_entries)
    assert total_bytes <= _SOURCE_SNIPPET_BYTE_BUDGET, (
        f"source/ entries used {total_bytes} bytes, budget is {_SOURCE_SNIPPET_BYTE_BUDGET}"
    )
    # At least one snippet should have been included if the budget allows.
    assert source_entries, "expected at least one source snippet under the budget"


def test_source_snippets_off_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``source/`` entries when the flag is omitted."""
    src = tmp_path / "src" / "pkg"
    src.mkdir(parents=True)
    (src / "x.py").write_text("print('hi')\n", encoding="utf-8")
    _stub_doctor(monkeypatch)

    result = build_debug_bundle(tmp_path)
    assert result.output_path is not None
    with zipfile.ZipFile(result.output_path) as zf:
        assert not any(n.startswith("source/") for n in zf.namelist())


# ---------------------------------------------------------------------------
# Logs windowing
# ---------------------------------------------------------------------------


def test_runtime_logs_tail_to_200_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each ``.sdd/runtime/*.log`` is truncated to its last 200 lines."""
    runtime = tmp_path / ".sdd" / "runtime"
    runtime.mkdir(parents=True)
    log_lines = [f"line-{n}" for n in range(500)]
    (runtime / "orchestrator.log").write_text("\n".join(log_lines), encoding="utf-8")
    _stub_doctor(monkeypatch)

    result = build_debug_bundle(tmp_path)

    assert result.output_path is not None
    with zipfile.ZipFile(result.output_path) as zf:
        body = zf.read("logs/orchestrator.log").decode("utf-8").splitlines()
    assert len(body) == 200
    assert body[0] == "line-300"
    assert body[-1] == "line-499"


# ---------------------------------------------------------------------------
# Install-method detection
# ---------------------------------------------------------------------------


def test_install_method_returns_known_value() -> None:
    """``detect_install_method`` returns one of the documented enum values."""
    method = detect_install_method()
    assert method in {"pip", "pipx", "uv-tool", "editable", "unknown"}


# ---------------------------------------------------------------------------
# Click integration
# ---------------------------------------------------------------------------


def test_cli_bundle_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Invoking ``debug bundle`` end-to-end through Click produces a ZIP."""
    _stub_doctor(monkeypatch)
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(debug_group, ["bundle", "--last"])

    assert result.exit_code == 0, result.output
    zips = list(tmp_path.glob("bernstein-debug-*.zip"))
    assert len(zips) == 1
    with zipfile.ZipFile(zips[0]) as zf:
        assert "manifest.json" in zf.namelist()


def test_cli_manifest_only_prints_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--manifest-only`` prints valid JSON and does not write a ZIP."""
    _stub_doctor(monkeypatch)
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(debug_group, ["bundle", "--manifest-only"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == 1
    assert payload["selection"]["mode"] == "last"
    assert not list(tmp_path.glob("*.zip"))
