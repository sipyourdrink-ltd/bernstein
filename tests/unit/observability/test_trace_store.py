"""Unit tests for the content-addressed local trace store.

The tests cover:

* sha256-keyed blob layout, gzip fallback when zstd is absent.
* idempotent ``put`` (writing the same bytes twice does not duplicate).
* metadata extraction from both single-JSON and JSONL trace formats.
* ``get``, ``verify``, ``reindex``, ``search`` correctness.
* viewer endpoints (FastAPI TestClient) render index, json, timeline.
* CLI surface registers ``trace serve``, ``trace verify``, ``trace reindex``.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from bernstein.cli.commands.advanced_cmd import trace_cmd
from bernstein.core.observability.trace_store import (
    ContentAddressedTraceStore,
    TraceIndexEntry,
    TraceMetadataHints,
    build_viewer_app,
)
from bernstein.core.persistence.cas_store import CASIntegrityError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _agent_trace_json(
    *,
    trace_id: str = "trace-001",
    task_id: str = "T-1",
    model: str = "sonnet",
    spawn_ts: float = 100.0,
    end_ts: float = 200.0,
    cost_usd: float = 0.0042,
) -> bytes:
    """Build a single-object trace payload that mirrors ``AgentTrace.write``."""
    return json.dumps(
        {
            "trace_id": trace_id,
            "session_id": "sess-1",
            "task_ids": [task_id],
            "agent_role": "backend",
            "model": model,
            "effort": "high",
            "spawn_ts": spawn_ts,
            "end_ts": end_ts,
            "outcome": "success",
            "cost_usd": cost_usd,
            "steps": [
                {"type": "orient", "timestamp": spawn_ts + 1, "detail": "read file"},
                {"type": "edit", "timestamp": spawn_ts + 2, "detail": "write file"},
                {"type": "verify", "timestamp": spawn_ts + 3, "detail": "ran tests"},
            ],
        }
    ).encode("utf-8")


def _jsonl_trace_bytes() -> bytes:
    """Build a JSONL trace (one event per line)."""
    lines = [
        {"type": "spawn", "trace_id": "tr-jsonl", "task_id": "T-9", "timestamp": 10.0, "model": "haiku"},
        {"type": "orient", "timestamp": 11.0},
        {"type": "complete", "timestamp": 12.0, "model": "haiku", "cost_usd": 0.01},
    ]
    return "\n".join(json.dumps(o) for o in lines).encode("utf-8")


# ---------------------------------------------------------------------------
# Core store behavior
# ---------------------------------------------------------------------------


def test_put_returns_sha256_indexed_entry(tmp_path: Path) -> None:
    store = ContentAddressedTraceStore(tmp_path)
    raw = _agent_trace_json()
    expected = hashlib.sha256(raw).hexdigest()

    entry = store.put(raw)

    assert entry.sha256 == expected
    assert entry.byte_size == len(raw)
    assert entry.trace_id == "trace-001"
    assert entry.task_id == "T-1"
    assert entry.model == "sonnet"
    assert entry.started_at == 100.0
    assert entry.ended_at == 200.0
    assert entry.codec in {"gzip", "zstd"}


def test_put_redacts_before_content_addressing_and_verification(tmp_path: Path) -> None:
    store = ContentAddressedTraceStore(tmp_path, prefer_zstd=False)
    canary = "sk-proj-0123456789abcdefghijklmnop"
    sha256 = "a3" * 32
    raw = json.dumps(
        {
            "trace_id": "trace-secret",
            "task_ids": ["T-secret"],
            "detail": f"Authorization: Bearer {canary}",
            "content_sha256": sha256,
        }
    ).encode()

    entry = store.put(raw)
    persisted = store.get("trace-secret")

    assert persisted is not None
    assert canary.encode() not in persisted
    assert b"[REDACTED]" in persisted
    assert sha256.encode() in persisted
    assert entry.sha256 == hashlib.sha256(persisted).hexdigest()
    assert entry.byte_size == len(persisted)
    assert store.verify("trace-secret") is True


def test_blob_uses_content_addressed_layout(tmp_path: Path) -> None:
    store = ContentAddressedTraceStore(tmp_path, prefer_zstd=False)
    raw = _agent_trace_json()
    entry = store.put(raw)

    blob_dir = tmp_path / "blobs" / entry.sha256[:2]
    blob_file = blob_dir / f"{entry.sha256}.jsonl.gz"
    assert blob_file.exists(), f"expected blob at {blob_file}"
    # And it is a valid gzip stream that round-trips back to ``raw``.
    assert gzip.decompress(blob_file.read_bytes()) == raw


def test_put_is_idempotent(tmp_path: Path) -> None:
    store = ContentAddressedTraceStore(tmp_path, prefer_zstd=False)
    raw = _agent_trace_json()

    first = store.put(raw)
    second = store.put(raw)

    # Same digest, single index entry, single blob on disk.
    assert first.sha256 == second.sha256
    assert len(store.index()) == 1
    blob_files = list((tmp_path / "blobs").rglob("*.jsonl.gz"))
    assert len(blob_files) == 1


def test_get_returns_decompressed_bytes(tmp_path: Path) -> None:
    store = ContentAddressedTraceStore(tmp_path, prefer_zstd=False)
    raw = _agent_trace_json()
    store.put(raw)

    assert store.get("trace-001") == raw
    # Look-up by sha256 also resolves.
    assert store.get(hashlib.sha256(raw).hexdigest()) == raw
    assert store.get("does-not-exist") is None


def test_verify_detects_digest_mismatch(tmp_path: Path) -> None:
    store = ContentAddressedTraceStore(tmp_path, prefer_zstd=False)
    raw = _agent_trace_json()
    entry = store.put(raw)

    assert store.verify("trace-001") is True

    # Corrupt the blob: replace the gzipped bytes with a different gzipped payload.
    blob = tmp_path / "blobs" / entry.sha256[:2] / f"{entry.sha256}.jsonl.gz"
    blob.write_bytes(gzip.compress(b"tampered"))
    assert store.verify("trace-001") is False


def test_verify_handles_missing_blob(tmp_path: Path) -> None:
    store = ContentAddressedTraceStore(tmp_path, prefer_zstd=False)
    raw = _agent_trace_json()
    entry = store.put(raw)

    # Delete the blob; the index still references it.
    (tmp_path / "blobs" / entry.sha256[:2] / f"{entry.sha256}.jsonl.gz").unlink()
    assert store.verify("trace-001") is False
    assert store.get("trace-001") is None


def test_get_raises_on_corrupted_blob(tmp_path: Path) -> None:
    # The default read path must fail the same way verify() reports, rather
    # than returning decompressed-but-wrong bytes.
    store = ContentAddressedTraceStore(tmp_path, prefer_zstd=False)
    raw = _agent_trace_json()
    entry = store.put(raw)

    blob = tmp_path / "blobs" / entry.sha256[:2] / f"{entry.sha256}.jsonl.gz"
    blob.write_bytes(gzip.compress(b"tampered"))

    assert store.verify("trace-001") is False
    with pytest.raises(CASIntegrityError) as exc_info:
        store.get("trace-001")
    # The error names the digest the bytes were supposed to match.
    assert entry.sha256 in str(exc_info.value)
    assert exc_info.value.expected == entry.sha256


def test_get_corrupted_blob_resolves_by_sha256_too(tmp_path: Path) -> None:
    store = ContentAddressedTraceStore(tmp_path, prefer_zstd=False)
    raw = _agent_trace_json()
    entry = store.put(raw)

    blob = tmp_path / "blobs" / entry.sha256[:2] / f"{entry.sha256}.jsonl.gz"
    blob.write_bytes(gzip.compress(b"tampered"))

    with pytest.raises(CASIntegrityError):
        store.get(entry.sha256)


def test_get_verify_off_returns_corrupted_bytes(tmp_path: Path) -> None:
    # Explicit opt-out skips the rehash for callers that verified upstream.
    store = ContentAddressedTraceStore(tmp_path, prefer_zstd=False)
    raw = _agent_trace_json()
    entry = store.put(raw)

    blob = tmp_path / "blobs" / entry.sha256[:2] / f"{entry.sha256}.jsonl.gz"
    blob.write_bytes(gzip.compress(b"tampered"))

    assert store.get("trace-001", verify=False) == b"tampered"


def test_get_intact_blob_passes_verification(tmp_path: Path) -> None:
    store = ContentAddressedTraceStore(tmp_path, prefer_zstd=False)
    raw = _agent_trace_json()
    store.put(raw)

    # Clean round-trip still returns the original bytes with verify on.
    assert store.get("trace-001") == raw
    assert store.get("trace-001", verify=True) == raw


def test_reindex_rebuilds_index_from_blobs(tmp_path: Path) -> None:
    store = ContentAddressedTraceStore(tmp_path, prefer_zstd=False)
    raw_a = _agent_trace_json(trace_id="a", task_id="T-A", spawn_ts=100.0)
    raw_b = _agent_trace_json(trace_id="b", task_id="T-B", spawn_ts=200.0)
    store.put(raw_a)
    store.put(raw_b)

    # Destroy the index and rebuild from disk.
    store.index_path.unlink()
    count = store.reindex()
    assert count == 2
    ids = sorted(e.trace_id for e in store.index())
    assert ids == ["a", "b"]
    # And the most recent ``started_at`` is first in the listing.
    assert store.index()[0].trace_id == "b"


def test_reindex_skips_corrupt_blob(tmp_path: Path) -> None:
    store = ContentAddressedTraceStore(tmp_path, prefer_zstd=False)
    raw = _agent_trace_json()
    entry = store.put(raw)
    # Drop a bogus blob into the same bucket.
    bucket = tmp_path / "blobs" / entry.sha256[:2]
    (bucket / "deadbeef.jsonl.gz").write_bytes(b"not gzip")
    store.index_path.unlink()
    assert store.reindex() == 1


def test_search_filters_by_task_model_and_text(tmp_path: Path) -> None:
    store = ContentAddressedTraceStore(tmp_path, prefer_zstd=False)
    store.put(_agent_trace_json(trace_id="a", task_id="T-1", model="sonnet"))
    store.put(_agent_trace_json(trace_id="b", task_id="T-2", model="haiku"))

    assert {e.trace_id for e in store.search(task_id="T-1")} == {"a"}
    assert {e.trace_id for e in store.search(model="haiku")} == {"b"}
    assert {e.trace_id for e in store.search(text="t-2")} == {"b"}
    # Empty filter returns everything.
    assert {e.trace_id for e in store.search()} == {"a", "b"}


def test_jsonl_payload_metadata_extraction(tmp_path: Path) -> None:
    store = ContentAddressedTraceStore(tmp_path, prefer_zstd=False)
    raw = _jsonl_trace_bytes()
    entry = store.put(raw)

    assert entry.trace_id == "tr-jsonl"
    assert entry.task_id == "T-9"
    assert entry.started_at == 10.0
    assert entry.ended_at == 12.0
    assert entry.model == "haiku"
    assert entry.cost_usd == pytest.approx(0.01)


def test_explicit_hints_override_derived_metadata(tmp_path: Path) -> None:
    store = ContentAddressedTraceStore(tmp_path, prefer_zstd=False)
    raw = b"{}"  # empty trace
    entry = store.put(
        raw,
        hints=TraceMetadataHints(
            trace_id="override-id",
            task_id="T-X",
            started_at=42.0,
            ended_at=43.0,
            model="opus",
            cost_usd=0.99,
        ),
    )
    assert entry.trace_id == "override-id"
    assert entry.task_id == "T-X"
    assert entry.started_at == 42.0
    assert entry.ended_at == 43.0
    assert entry.model == "opus"
    assert entry.cost_usd == 0.99


def test_index_round_trip_via_dataclass(tmp_path: Path) -> None:
    entry = TraceIndexEntry(
        trace_id="t",
        task_id="T",
        sha256="0" * 64,
        byte_size=1,
        started_at=1.0,
    )
    assert TraceIndexEntry.from_dict(entry.to_dict()) == entry


def test_put_rejects_non_bytes(tmp_path: Path) -> None:
    store = ContentAddressedTraceStore(tmp_path)
    with pytest.raises(TypeError):
        store.put("not bytes")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Viewer
# ---------------------------------------------------------------------------


def test_viewer_index_lists_entries(tmp_path: Path) -> None:
    store = ContentAddressedTraceStore(tmp_path, prefer_zstd=False)
    store.put(_agent_trace_json())
    client = TestClient(build_viewer_app(store))

    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    assert "Bernstein local trace viewer" in body
    assert "trace-001" in body
    assert "T-1" in body


def test_viewer_filters_by_task(tmp_path: Path) -> None:
    store = ContentAddressedTraceStore(tmp_path, prefer_zstd=False)
    store.put(_agent_trace_json(trace_id="a", task_id="T-1"))
    store.put(_agent_trace_json(trace_id="b", task_id="T-2"))
    client = TestClient(build_viewer_app(store))

    resp = client.get("/", params={"task": "T-1"})
    assert resp.status_code == 200
    # The filtered view should not list trace b.
    assert ">b<" not in resp.text


def test_viewer_api_returns_index(tmp_path: Path) -> None:
    store = ContentAddressedTraceStore(tmp_path, prefer_zstd=False)
    store.put(_agent_trace_json())
    client = TestClient(build_viewer_app(store))

    resp = client.get("/api/traces")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["traces"]) == 1
    assert data["traces"][0]["trace_id"] == "trace-001"


def test_viewer_returns_pretty_json_body(tmp_path: Path) -> None:
    store = ContentAddressedTraceStore(tmp_path, prefer_zstd=False)
    store.put(_agent_trace_json())
    client = TestClient(build_viewer_app(store))

    resp = client.get("/traces/trace-001")
    assert resp.status_code == 200
    parsed = json.loads(resp.text)
    assert parsed["trace_id"] == "trace-001"


def test_viewer_timeline_renders_steps(tmp_path: Path) -> None:
    store = ContentAddressedTraceStore(tmp_path, prefer_zstd=False)
    store.put(_agent_trace_json())
    client = TestClient(build_viewer_app(store))

    resp = client.get("/traces/trace-001/timeline")
    assert resp.status_code == 200
    assert "orient" in resp.text
    assert "edit" in resp.text
    assert "verify" in resp.text


def test_viewer_404_for_unknown_trace(tmp_path: Path) -> None:
    store = ContentAddressedTraceStore(tmp_path, prefer_zstd=False)
    client = TestClient(build_viewer_app(store))
    assert client.get("/traces/missing").status_code == 404
    assert client.get("/traces/missing/timeline").status_code == 404
    assert client.get("/api/traces/missing").status_code == 404


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_registers_subcommands() -> None:
    """The ``trace`` Click group exposes verify, reindex, serve, show."""
    sub = {name: cmd for name, cmd in trace_cmd.commands.items()}
    for required in ("verify", "reindex", "serve", "show"):
        assert required in sub, f"missing trace subcommand: {required}"


def test_cli_verify_passes_and_fails(tmp_path: Path) -> None:
    store = ContentAddressedTraceStore(tmp_path, prefer_zstd=False)
    store.put(_agent_trace_json())

    runner = CliRunner()
    ok = runner.invoke(trace_cmd, ["--traces-dir", str(tmp_path), "verify", "trace-001"])
    assert ok.exit_code == 0, ok.output

    bad = runner.invoke(trace_cmd, ["--traces-dir", str(tmp_path), "verify", "no-such"])
    assert bad.exit_code == 1


def test_cli_reindex_walks_blobs(tmp_path: Path) -> None:
    store = ContentAddressedTraceStore(tmp_path, prefer_zstd=False)
    store.put(_agent_trace_json(trace_id="a"))
    store.put(_agent_trace_json(trace_id="b", spawn_ts=200.0))
    store.index_path.unlink()

    runner = CliRunner()
    result = runner.invoke(trace_cmd, ["--traces-dir", str(tmp_path), "reindex"])
    assert result.exit_code == 0, result.output
    assert "2 entries" in result.output


def test_cli_legacy_task_id_routes_to_show(tmp_path: Path) -> None:
    """``bernstein trace <task-id>`` still pretty-prints the task trace."""
    traces_dir = tmp_path
    payload = _agent_trace_json()
    # Write a per-trace JSON file matching the legacy ``trace-<id>.json`` layout
    # so the show subcommand finds it via its existing glob.
    (traces_dir / "trace-T-1.json").write_bytes(payload)

    runner = CliRunner()
    result = runner.invoke(
        trace_cmd,
        ["--traces-dir", str(traces_dir), "T-1", "--as-json"],
    )
    assert result.exit_code == 0, result.output


def test_cli_serve_defaults_to_loopback() -> None:
    """``trace serve`` must default to ``127.0.0.1`` (no remote bind)."""
    serve_cmd = trace_cmd.commands["serve"]
    bind_param = next(p for p in serve_cmd.params if getattr(p, "name", "") == "bind")
    assert bind_param.default == "127.0.0.1"
    port_param = next(p for p in serve_cmd.params if getattr(p, "name", "") == "port")
    assert port_param.default == 8765
