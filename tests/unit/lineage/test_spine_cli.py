"""CLI tests for spine-backed ``bernstein lineage verify`` / ``replay``.

Issue #2292:

* ``lineage verify <run_id>`` walks the spine head hash and prints the
  head as the run's provenance identity.
* Against an empty run it prints a distinct ``no entries`` status and
  exits non-zero (AC5) rather than passing trivially.
* A single-byte mutation of any spine entry is reported as tamper.
* ``lineage replay <run_id>`` lists the chain in append order.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.commands.lineage_cmd import lineage_cmd
from bernstein.core.lineage.spine import LineageSpine

_KEY = b"k" * 32


def _seed(workdir: Path, run_id: str, n: int = 3) -> LineageSpine:
    root = workdir / ".sdd" / "lineage"
    spine = LineageSpine(root, run_id=run_id, hmac_key=_KEY)
    for i in range(n):
        spine.record(
            artifact_path=f"src/{i}.py",
            content=f"c{i}".encode(),
            actor="agent:worker",
            step_id=f"s{i}",
            model="claude",
            timestamp=i,
        )
    return spine


def _run(args: list[str]) -> object:
    # Widen the render width so table cells are not truncated with an ellipsis.
    return CliRunner().invoke(lineage_cmd, args, env={"COLUMNS": "200"})


def test_verify_ok_prints_head(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    key_file = tmp_path / "audit.key"
    key_file.write_bytes(_KEY)
    key_file.chmod(0o600)
    spine = _seed(tmp_path, "run-1")
    result = _run(["verify", "run-1", "--workdir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert spine.head_hash()[:16] in result.output
    assert "OK" in result.output


def test_verify_empty_run_distinct_status(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    key_file = tmp_path / "audit.key"
    key_file.write_bytes(_KEY)
    key_file.chmod(0o600)
    # Create the .sdd dir but no spine for this run.
    (tmp_path / ".sdd" / "lineage").mkdir(parents=True)
    result = _run(["verify", "no-such-run", "--workdir", str(tmp_path)])
    assert result.exit_code != 0
    assert "no entries" in result.output.lower()


def test_verify_detects_tamper(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    key_file = tmp_path / "audit.key"
    key_file.write_bytes(_KEY)
    key_file.chmod(0o600)
    spine = _seed(tmp_path, "run-1")
    raw = bytearray(spine.spine_path.read_bytes())
    idx = raw.index(b"src/1.py")
    raw[idx + 4] ^= 0x01
    spine.spine_path.write_bytes(bytes(raw))
    result = _run(["verify", "run-1", "--workdir", str(tmp_path)])
    assert result.exit_code != 0
    assert "tamper" in result.output.lower()


def test_replay_lists_entries(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    key_file = tmp_path / "audit.key"
    key_file.write_bytes(_KEY)
    key_file.chmod(0o600)
    _seed(tmp_path, "run-1", n=3)
    result = _run(["replay", "run-1", "--workdir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "src/0.py" in result.output
    assert "src/2.py" in result.output


def test_replay_empty_run(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    key_file = tmp_path / "audit.key"
    key_file.write_bytes(_KEY)
    key_file.chmod(0o600)
    (tmp_path / ".sdd" / "lineage").mkdir(parents=True)
    result = _run(["replay", "empty", "--workdir", str(tmp_path)])
    assert result.exit_code != 0
    assert "no entries" in result.output.lower()
