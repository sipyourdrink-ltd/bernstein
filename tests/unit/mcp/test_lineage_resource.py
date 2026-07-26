"""Tests for the MCP lineage resource + verify_chain tool.

Per ADR-009 §7 the MCP exposure is:

  * Resource ``lineage://artefact/<repo-relative-path>`` → JSONL stream of
    the chain for that artefact.
  * Resource ``lineage://stats`` → JSON with entry counts.
  * Tool ``bernstein_verify_lineage(path)`` → ``{"ok": bool, "reason":
    str|None}`` (with ``verify_chain`` kept as a deprecated alias, #3087).

Default off for remote MCP, on for local stdio - but registration itself
is unconditional; the gate lives at the registrar boundary.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP

from bernstein.core.lineage.identity import AgentCard, generate_keypair
from bernstein.core.lineage.signed_write import SignedLineageLog
from bernstein.core.lineage.store import LineageStore
from bernstein.mcp.resources.lineage import register_lineage_resources


@pytest.fixture
def seeded_store(tmp_path: Path) -> tuple[Path, LineageStore]:
    """Two-entry chain on ``src/foo.py``."""
    root = tmp_path / "lineage"
    store = LineageStore(root)
    recorder = SignedLineageLog(store=store, operator_hmac_key=b"k" * 32)
    priv, pub = generate_keypair()
    card = AgentCard(agent_id="agent:worker", kid="k1", public_key_pem=pub)
    recorder.record_write(
        artefact_path="src/foo.py",
        new_content=b"v1",
        agent_id=card.agent_id,
        agent_card=card,
        private_key_pem=priv,
        tool_call_id="tc-1",
        span_id="span-1",
    )
    recorder.record_write(
        artefact_path="src/foo.py",
        new_content=b"v2",
        agent_id=card.agent_id,
        agent_card=card,
        private_key_pem=priv,
        tool_call_id="tc-2",
        span_id="span-2",
    )
    return root, store


def _run(coro):  # pragma: no cover - tiny helper
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


# ---------------------------------------------------------------------------
# lineage://artefact/<path> resource
# ---------------------------------------------------------------------------


def test_artefact_resource_returns_jsonl_chain(seeded_store: tuple[Path, LineageStore]) -> None:
    root, _store = seeded_store
    mcp: FastMCP[None] = FastMCP("test")
    register_lineage_resources(mcp, lineage_root=root)

    contents = _run(mcp.read_resource("lineage://artefact/src/foo.py"))
    # FastMCP returns an iterable of ReadResourceContents; collapse to text.
    text = "\n".join(c.content for c in contents) if hasattr(next(iter(contents), None), "content") else str(contents)
    lines = [line for line in text.splitlines() if line.strip()]
    assert len(lines) == 2
    e1 = json.loads(lines[0])
    e2 = json.loads(lines[1])
    assert e1["artefact_path"] == "src/foo.py"
    assert e2["parent_hashes"] != []
    assert e2["parent_hashes"][0].startswith("sha256:")


def test_artefact_resource_unknown_path_returns_empty(seeded_store: tuple[Path, LineageStore]) -> None:
    root, _store = seeded_store
    mcp: FastMCP[None] = FastMCP("test")
    register_lineage_resources(mcp, lineage_root=root)
    contents = _run(mcp.read_resource("lineage://artefact/src/never-touched.py"))
    text = "\n".join(c.content for c in contents) if hasattr(next(iter(contents), None), "content") else str(contents)
    assert text.strip() == ""


# ---------------------------------------------------------------------------
# lineage://stats resource
# ---------------------------------------------------------------------------


def test_stats_resource_counts_entries(seeded_store: tuple[Path, LineageStore]) -> None:
    root, _store = seeded_store
    mcp: FastMCP[None] = FastMCP("test")
    register_lineage_resources(mcp, lineage_root=root)

    contents = _run(mcp.read_resource("lineage://stats"))
    text = "\n".join(c.content for c in contents) if hasattr(next(iter(contents), None), "content") else str(contents)
    payload = json.loads(text)
    assert payload["total_entries"] == 2
    assert payload["artefacts"] == 1
    assert payload["open_forks"] == 0
    # agents_seen is a list-or-int - accept either as long as it accounts for 1.
    seen = payload.get("agents_seen")
    if isinstance(seen, int):
        assert seen == 1
    else:
        assert seen == ["agent:worker"]


def test_stats_resource_on_empty_lineage(tmp_path: Path) -> None:
    root = tmp_path / "lineage"
    LineageStore(root)  # init only - no entries
    mcp: FastMCP[None] = FastMCP("test")
    register_lineage_resources(mcp, lineage_root=root)
    contents = _run(mcp.read_resource("lineage://stats"))
    text = "\n".join(c.content for c in contents) if hasattr(next(iter(contents), None), "content") else str(contents)
    payload = json.loads(text)
    assert payload["total_entries"] == 0
    assert payload["artefacts"] == 0


# ---------------------------------------------------------------------------
# verify_chain tool - happy + error paths
# ---------------------------------------------------------------------------


def test_verify_chain_ok(seeded_store: tuple[Path, LineageStore]) -> None:
    root, _store = seeded_store
    mcp: FastMCP[None] = FastMCP("test")
    register_lineage_resources(mcp, lineage_root=root)

    result = _run(mcp.call_tool("bernstein_verify_lineage", {"artefact_path": "src/foo.py"}))
    # FastMCP.call_tool returns (contents, structuredOutput) on newer versions.
    payload = _payload_from_tool_result(result)
    assert payload["ok"] is True
    assert payload.get("reason") in (None, "")


def test_verify_chain_unknown_path_returns_ok_empty(seeded_store: tuple[Path, LineageStore]) -> None:
    """No entries for a path → trivially valid empty chain.

    Mirrors how `bernstein-verify chain <unknown>` behaves: nothing to check
    is not the same as broken.
    """
    root, _store = seeded_store
    mcp: FastMCP[None] = FastMCP("test")
    register_lineage_resources(mcp, lineage_root=root)
    result = _run(mcp.call_tool("bernstein_verify_lineage", {"artefact_path": "src/never.py"}))
    payload = _payload_from_tool_result(result)
    assert payload["ok"] is True


def test_verify_chain_detects_tampered_log(seeded_store: tuple[Path, LineageStore]) -> None:
    root, _store = seeded_store
    log = root / "log.jsonl"
    raw = log.read_bytes()
    # Flip a byte in the artefact_path of the first record - must trip both
    # the parent-chain invariant (the child still references the original
    # entry_hash) and (if we were checking it) the HMAC.
    tampered = raw.replace(b"src/foo.py", b"src/BAD.py", 1)
    assert tampered != raw
    log.write_bytes(tampered)

    mcp: FastMCP[None] = FastMCP("test")
    register_lineage_resources(mcp, lineage_root=root)
    result = _run(mcp.call_tool("bernstein_verify_lineage", {"artefact_path": "src/foo.py"}))
    payload = _payload_from_tool_result(result)
    assert payload["ok"] is False
    assert payload["reason"]


# ---------------------------------------------------------------------------
# Local/remote gating
# ---------------------------------------------------------------------------


def test_register_returns_falsey_when_disabled(seeded_store: tuple[Path, LineageStore]) -> None:
    """The registrar is gated by an ``enabled`` flag (default True for local stdio)."""
    root, _store = seeded_store
    mcp: FastMCP[None] = FastMCP("test")
    registered = register_lineage_resources(mcp, lineage_root=root, enabled=False)
    assert registered is False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _payload_from_tool_result(result: object) -> dict[str, object]:
    """FastMCP.call_tool returns one of:

    * an iterable of TextContent
    * a tuple ``(contents, structuredOutput_dict)``

    Different mcp SDK versions ship different shapes; this picks the JSON
    payload regardless of which one we got.
    """
    # New API: (contents, structured)
    if isinstance(result, tuple) and len(result) == 2:
        contents, structured = result
        if isinstance(structured, dict):
            # FastMCP may wrap the dict under a "result" key.
            if "ok" in structured:
                return structured
            if "result" in structured and isinstance(structured["result"], dict):
                return structured["result"]
        return _payload_from_contents(contents)
    return _payload_from_contents(result)


def _payload_from_contents(contents: object) -> dict[str, object]:
    texts: list[str] = []
    with suppress(TypeError):
        for c in contents:  # type: ignore[union-attr]
            text = getattr(c, "text", None)
            if isinstance(text, str):
                texts.append(text)
    joined = "\n".join(texts)
    return json.loads(joined)


def test_verify_chain_alias_names_its_replacement(seeded_store: tuple[Path, LineageStore]) -> None:
    """The old name stays callable and answers with the deprecation wrapper."""
    root, _store = seeded_store
    mcp: FastMCP[None] = FastMCP("test")
    register_lineage_resources(mcp, lineage_root=root)
    result = _run(mcp.call_tool("verify_chain", {"artefact_path": "src/foo.py"}))
    payload = _payload_from_tool_result(result)
    assert payload["deprecated"] is True
    assert payload["replacement"] == "bernstein_verify_lineage"
    assert payload["result"]["ok"] is True
