"""Tests for ``bernstein trace export --anchor-endpoint``.

The anchor step is opt-in: without the option nothing is sent. Every test
here replaces the HTTP transport with a fake, so none touches the network,
and the exported records are the signed vectors in
``tests/fixtures/trust-record-vectors/``.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.advanced_cmd import trace_cmd
from bernstein.core.observability.trust_record import HopExportResult, HopRecord

if TYPE_CHECKING:
    from collections.abc import Iterator

_VECTORS = Path(__file__).parents[2] / "fixtures" / "trust-record-vectors"
_ENDPOINT = "https://anchor.example.test/evidence/trace"
_RUN = "test-run-123"


def _vector(name: str) -> str:
    return (_VECTORS / name).read_text(encoding="utf-8").strip()


def _jcs_sha(record_json: str) -> str:
    from bernstein.core.security.agent_card_signer import canonicalize_jcs

    return hashlib.sha256(canonicalize_jcs(json.loads(record_json))).hexdigest()


class _FakeAnchor:
    def __init__(self, *, wrong_sha: bool = False) -> None:
        self.records: list[dict[str, Any]] = []
        self._wrong_sha = wrong_sha

    def __call__(self, url: str, body: bytes, timeout: float) -> tuple[int, bytes]:
        record = json.loads(body)["record"]
        self.records.append(record)
        sha = "0" * 64 if self._wrong_sha else _jcs_sha(json.dumps(record))
        return 201, json.dumps({"sha": sha, "status": "pending"}).encode("utf-8")


@contextmanager
def _exporting(result: HopExportResult, fake: _FakeAnchor | None = None) -> Iterator[MagicMock]:
    """Run inside an isolated run dir with emission mocked to return ``result``."""
    run_dir = Path(".sdd") / "runs" / _RUN
    run_dir.mkdir(parents=True)
    (run_dir / "journal.jsonl").write_text('{"type": "session_start", "ts": 1234567890.0}\n')
    with ExitStack() as stack:
        stack.enter_context(patch.dict("sys.modules", {"agentrust_trace": MagicMock()}))
        verify = stack.enter_context(patch("bernstein.core.replay.journal.verify_journal"))
        verify.return_value.chain_consistent = True
        emit = stack.enter_context(
            patch("bernstein.core.observability.trust_record.TrustRecordEmitter.emit_hop_records")
        )
        emit.return_value = result
        stack.enter_context(patch("bernstein.core.observability.time_anchor._urllib_post", fake or _FakeAnchor()))
        yield emit


def _single() -> HopExportResult:
    record = _vector("single-execution-trust-record.json")
    return HopExportResult(records=[HopRecord(exec_id=_RUN, record=record)], aggregate=record)


def test_no_anchor_endpoint_sends_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    fake = _FakeAnchor()
    monkeypatch.chdir(tmp_path)
    with _exporting(_single(), fake):
        result = runner.invoke(trace_cmd, ["export", _RUN, "--out", "trace.json"])
        assert result.exit_code == 0, result.output
        assert fake.records == []
        assert not Path("trace.anchor.json").exists()


def test_anchor_writes_receipt_beside_out_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    fake = _FakeAnchor()
    monkeypatch.chdir(tmp_path)
    with _exporting(_single(), fake):
        result = runner.invoke(trace_cmd, ["export", _RUN, "--out", "trace.json", "--anchor-endpoint", _ENDPOINT])
        assert result.exit_code == 0, result.output
        record = Path("trace.json").read_text(encoding="utf-8")
        assert fake.records == [json.loads(record)]
        receipt = json.loads(Path("trace.anchor.json").read_text(encoding="utf-8"))
        assert receipt["endpoint"] == _ENDPOINT
        assert receipt["status"] == "pending"
        assert receipt["record_sha256"] == _jcs_sha(record)
        assert receipt["response"]["sha"] == receipt["record_sha256"]


def test_anchor_receipt_goes_to_stderr_when_record_goes_to_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    with _exporting(_single()):
        result = runner.invoke(trace_cmd, ["export", _RUN, "--anchor-endpoint", _ENDPOINT])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == json.loads(_vector("single-execution-trust-record.json"))
        receipt = json.loads(result.stderr)
        assert receipt["record_sha256"] == "62e4a01503fb745974ffb550836882d3c017ad7c17395442de848150445ba672"


def test_anchor_out_dir_anchors_every_hop_and_the_aggregate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent = _vector("delegated-parent-trust-record.json")
    child = _vector("delegated-child-trust-record.json")
    aggregate = _vector("aggregate-trust-record.json")
    hops = HopExportResult(
        records=[HopRecord(exec_id="parent", record=parent), HopRecord(exec_id="child", record=child)],
        aggregate=aggregate,
    )
    runner = CliRunner()
    fake = _FakeAnchor()
    monkeypatch.chdir(tmp_path)
    with _exporting(hops, fake):
        result = runner.invoke(trace_cmd, ["export", _RUN, "--out-dir", "out", "--anchor-endpoint", _ENDPOINT])
        assert result.exit_code == 0, result.output
        assert len(fake.records) == 3
        for name, record in (("parent", parent), ("child", child), ("aggregate", aggregate)):
            receipt = json.loads((Path("out") / f"{name}.anchor.json").read_text(encoding="utf-8"))
            assert receipt["record_sha256"] == _jcs_sha(record)
        # The parent's anchored sha is the hash the child already names it by.
        parent_receipt = json.loads((Path("out") / "parent.anchor.json").read_text(encoding="utf-8"))
        assert json.loads(child)["delegation"]["parent_record_hash"] == "sha256:" + parent_receipt["record_sha256"]


def test_receipt_for_other_bytes_fails_but_keeps_the_export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    with _exporting(_single(), _FakeAnchor(wrong_sha=True)):
        result = runner.invoke(trace_cmd, ["export", _RUN, "--out", "trace.json", "--anchor-endpoint", _ENDPOINT])
        assert result.exit_code == 1, result.output
        assert "receipt names sha" in result.stderr
        assert Path("trace.json").exists()
        assert not Path("trace.anchor.json").exists()


def test_plain_http_endpoint_is_rejected_before_export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    with _exporting(_single()) as emit:
        result = runner.invoke(
            trace_cmd,
            ["export", _RUN, "--out", "trace.json", "--anchor-endpoint", "http://anchor.example.test/x"],
        )
        assert result.exit_code == 2, result.output
        assert "Invalid --anchor-endpoint" in result.output
        emit.assert_not_called()
        assert not Path("trace.json").exists()


def test_anchor_endpoint_is_refused_with_ocsf_format(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(trace_cmd, ["export", _RUN, "--format", "ocsf", "--anchor-endpoint", _ENDPOINT])
    assert result.exit_code == 2, result.output
    assert "--anchor-endpoint applies to Trust Records" in result.output
