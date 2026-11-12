"""CLI tests for ``bernstein gate verify <run>`` (issue #2294, AC3).

``gate verify`` recomputes ``inputs_hash`` from the claimed inputs and confirms
the recorded panel saw exactly those inputs. A mismatch exits non-zero.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

_KEY = b"k" * 32


def _emit_record(root: Path, run_id: str, inputs: dict[str, object]) -> None:
    from bernstein.core.quality.adjudication import (
        JudgeConfig,
        JudgeVerdict,
        PanelConfig,
        Verdict,
        adjudicate,
    )

    cfg_m = JudgeConfig(model="cheap", temperature=0.0, prompt_hash="maker")
    cfg_c = JudgeConfig(model="capable", temperature=0.0, prompt_hash="checker")
    panel = PanelConfig(judges=(cfg_m, cfg_c))
    adjudicate(
        run_id=run_id,
        lineage_root=root / ".sdd" / "lineage",
        hmac_key=_KEY,
        inputs=inputs,
        rubric={"rule": "r"},
        panel=panel,
        judge_verdicts=(
            JudgeVerdict(config=cfg_m, verdict=Verdict.PASS, rationale_hash="rm"),
            JudgeVerdict(config=cfg_c, verdict=Verdict.PASS, rationale_hash="rc"),
        ),
        now=1234,
    )


def _write_key(tmp_path: Path, monkeypatch) -> None:
    key_file = tmp_path / "audit.key"
    key_file.write_bytes(_KEY)
    key_file.chmod(0o600)
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_file))


def test_gate_verify_ok(tmp_path: Path, monkeypatch) -> None:
    _write_key(tmp_path, monkeypatch)
    from bernstein.cli.commands.gate_cmd import gate_group

    inputs = {"diff": "abc"}
    _emit_record(tmp_path, "run-1", inputs)
    inputs_file = tmp_path / "inputs.json"
    inputs_file.write_text(json.dumps(inputs), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        gate_group,
        ["verify", "run-1", "--inputs", str(inputs_file), "--workdir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert "OK" in result.stdout
    assert result.stderr == ""


def test_gate_verify_detects_mismatch(tmp_path: Path, monkeypatch) -> None:
    _write_key(tmp_path, monkeypatch)
    from bernstein.cli.commands.gate_cmd import gate_group

    _emit_record(tmp_path, "run-1", {"diff": "abc"})
    inputs_file = tmp_path / "inputs.json"
    inputs_file.write_text(json.dumps({"diff": "TAMPERED"}), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        gate_group,
        ["verify", "run-1", "--inputs", str(inputs_file), "--workdir", str(tmp_path)],
    )
    assert result.exit_code == 2, result.output
    assert "MISMATCH" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    ("payload", "diagnostic"),
    [(b"{}", "NO RECORD"), (b"{", "Invalid JSON"), (b"\xff", "Invalid JSON")],
    ids=["missing-record", "invalid-json", "invalid-utf8"],
)
def test_gate_verify_bad_input_or_no_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: bytes, diagnostic: str
) -> None:
    from bernstein.cli.commands.gate_cmd import gate_group

    _write_key(tmp_path, monkeypatch)
    inputs_file = tmp_path / "inputs.json"
    inputs_file.write_bytes(payload)
    result = CliRunner().invoke(
        gate_group,
        ["verify", "run-1", "--inputs", str(inputs_file), "--workdir", str(tmp_path)],
    )

    assert result.exit_code == 1, result.output
    assert diagnostic in result.stderr
    assert result.stdout == ""
    assert isinstance(result.exception, SystemExit)


def test_gate_verify_rejects_non_utf8_json_escape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bernstein.cli.commands.gate_cmd import gate_group

    _write_key(tmp_path, monkeypatch)
    _emit_record(tmp_path, "run-1", {"diff": "abc"})
    inputs_file = tmp_path / "inputs.json"
    inputs_file.write_text(r'{"diff":"\ud800"}', encoding="utf-8")
    result = CliRunner().invoke(
        gate_group,
        ["verify", "run-1", "--inputs", str(inputs_file), "--workdir", str(tmp_path)],
    )

    assert result.exit_code == 1, result.output
    assert "Invalid JSON" in result.stderr
    assert result.stdout == ""
    assert isinstance(result.exception, SystemExit)


def test_gate_verify_missing_input_path_is_usage_error(tmp_path: Path) -> None:
    from bernstein.cli.commands.gate_cmd import gate_group

    result = CliRunner().invoke(gate_group, ["verify", "run-1", "--inputs", str(tmp_path / "missing.json")])

    assert result.exit_code == 2, result.output
    assert "does not exist" in result.stderr
    assert result.stdout == ""
