"""CLI tests for ``bernstein plan compile`` (issue #2361).

The command runs the spec pipeline end to end and writes the compiled
artefacts under ``.sdd/spec/<name>/``. Tests are cwd-isolated via
``monkeypatch.chdir(tmp_path)`` so no state leaks into the repo.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.plan_compile_cmd import plan_compile

_SPEC = """# Reset

- [ ] When the user requests a reset, the system shall send an email.
- [ ] The system shall expire the token after 30 minutes.
"""


def _write_spec(tmp_path: Path) -> Path:
    spec = tmp_path / "spec.md"
    spec.write_text(_SPEC, encoding="utf-8")
    return spec


def test_compile_writes_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    spec = _write_spec(tmp_path)

    result = CliRunner().invoke(plan_compile, [str(spec), "--name", "reset", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["requirement_count"] == 2
    assert payload["node_count"] == 2
    assert payload["approved"] is False
    assert payload["requirement_set_hash"].startswith("sha256:")

    spec_dir = tmp_path / ".sdd" / "spec" / "reset"
    assert (spec_dir / "requirements.json").is_file()
    assert (spec_dir / "graph.json").is_file()
    assert not (spec_dir / "receipt.json").exists()


def test_compile_is_deterministic_across_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    spec = _write_spec(tmp_path)
    runner = CliRunner()

    first = json.loads(runner.invoke(plan_compile, [str(spec), "--json"]).output)
    second = json.loads(runner.invoke(plan_compile, [str(spec), "--json"]).output)
    assert first["graph_hash"] == second["graph_hash"]
    assert first["requirement_set_hash"] == second["requirement_set_hash"]


def test_compile_approve_records_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    spec = _write_spec(tmp_path)

    result = CliRunner().invoke(plan_compile, [str(spec), "--name", "reset", "--approve", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["approved"] is True

    receipt_path = tmp_path / ".sdd" / "spec" / "reset" / "receipt.json"
    assert receipt_path.is_file()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["decision"] == "approved"
    assert receipt["requirement_count"] == 2

    # The receipt is anchored in the HMAC audit chain.
    from bernstein.core.security.audit_chain import (
        EVENT_SPEC_REQUIREMENT_SET,
        AuditChainStore,
    )

    chain = AuditChainStore(tmp_path / ".sdd" / "audit")
    rows = chain.query(event_type=EVENT_SPEC_REQUIREMENT_SET)
    assert len(rows) == 1
    assert rows[0].details["requirement_set_hash"] == receipt["requirement_set_hash"]
    ok, errors = chain.verify()
    assert ok, errors


def test_compile_rejects_unsafe_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    spec = _write_spec(tmp_path)
    result = CliRunner().invoke(plan_compile, [str(spec), "--name", "../escape"])
    assert result.exit_code != 0
    assert not (tmp_path / ".sdd" / "spec").exists()


def test_compile_empty_spec_exits_nonzero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    spec = tmp_path / "empty.md"
    spec.write_text("# Heading only\n\nProse without any acceptance clause.\n", encoding="utf-8")
    result = CliRunner().invoke(plan_compile, [str(spec)])
    assert result.exit_code == 1
