"""Tests for the ACP session store and per-session state machine."""

from __future__ import annotations

import asyncio

import pytest

from bernstein.core.protocols.acp.session import ACPSession, ACPSessionStore


def test_set_mode_rejects_unknown() -> None:
    session = ACPSession(session_id="s1", cwd="/tmp")
    with pytest.raises(ValueError):
        session.set_mode("yolo")


def test_set_mode_persists() -> None:
    session = ACPSession(session_id="s1", cwd="/tmp")
    session.set_mode("auto")
    assert session.mode == "auto"
    session.set_mode("manual")
    assert session.mode == "manual"


def test_open_and_resolve_permission_roundtrip() -> None:
    session = ACPSession(session_id="s1", cwd="/tmp")
    waiter = session.open_permission_waiter("write_file", "edit foo.py")
    assert session.resolve_permission(waiter.prompt_id, "approved") is True
    assert waiter.decision == "approved"
    session.discard_waiter(waiter.prompt_id)


def test_resolve_permission_returns_false_for_unknown_id() -> None:
    session = ACPSession(session_id="s1", cwd="/tmp")
    assert session.resolve_permission("missing", "approved") is False


def test_session_store_roundtrip() -> None:
    async def _run() -> None:
        store = ACPSessionStore()
        s1 = ACPSession(session_id="s1", cwd="/tmp")
        await store.add(s1)
        assert await store.get("s1") is s1
        assert await store.count() == 1
        with pytest.raises(ValueError):
            await store.add(ACPSession(session_id="s1", cwd="/x"))
        removed = await store.remove("s1")
        assert removed is s1
        assert await store.get("s1") is None
        assert await store.count() == 0

    asyncio.run(_run())


def test_snapshot_includes_acp_source() -> None:
    async def _run() -> None:
        store = ACPSessionStore()
        await store.add(ACPSession(session_id="s1", cwd="/work", role="qa", mode="auto"))
        snap = store.snapshot()
        assert len(snap) == 1
        entry = snap[0]
        assert entry["session_id"] == "s1"
        assert entry["source"] == "acp"
        assert entry["mode"] == "auto"
        assert entry["role"] == "qa"

    asyncio.run(_run())
