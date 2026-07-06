"""CLI tests for ``bernstein gate verify <run>`` (issue #2294, AC3).

``gate verify`` recomputes ``inputs_hash`` from the claimed inputs and confirms
the recorded panel saw exactly those inputs. A mismatch exits non-zero.
"""

from __future__ import annotations

import json
from pathlib import Path

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
    assert "OK" in result.output


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
    assert result.exit_code != 0
