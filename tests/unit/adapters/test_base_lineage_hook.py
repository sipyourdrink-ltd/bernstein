"""Tests for the deprecated ``post_write_lineage_hook`` shim on adapters.

Issue #2292 routes the shim through the lineage spine
(:func:`bernstein.adapters.base.record_artifact_write`). The shim keeps
the v1 signature for backward-compatible imports but now writes a single
Merkle-chained, HMAC-tagged spine entry and honours the fail-closed
``BERNSTEIN_LINEAGE_ENABLED`` gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.adapters.base import (
    LINEAGE_ENABLED_ENV,
    post_write_lineage_hook,
)
from bernstein.core.lineage.identity import AgentCard, generate_keypair
from bernstein.core.lineage.spine import LineageSpine, SpineEntry


@pytest.fixture
def card_and_key() -> tuple[AgentCard, str]:
    priv, pub = generate_keypair()
    return AgentCard(agent_id="agent:worker", kid="k1", public_key_pem=pub), priv


def _spine_entries(root: Path, run_id: str = "default") -> int:
    spine = LineageSpine(root, run_id=run_id, hmac_key=b"k" * 32)
    return len(list(spine.iter_entries()))


def test_hook_records_when_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    card_and_key: tuple[AgentCard, str],
) -> None:
    monkeypatch.setenv(LINEAGE_ENABLED_ENV, "1")
    card, priv = card_and_key
    lineage_root = tmp_path / "lineage"
    h = post_write_lineage_hook(
        artefact_path="src/foo.py",
        new_content=b"hello",
        agent_id=card.agent_id,
        agent_card=card,
        private_key_pem=priv,
        tool_call_id="tc-1",
        span_id="span-1",
        lineage_root=lineage_root,
        operator_hmac_key=b"k" * 32,
    )
    assert h
    assert _spine_entries(lineage_root) == 1


def test_hook_noop_when_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    card_and_key: tuple[AgentCard, str],
) -> None:
    monkeypatch.setenv(LINEAGE_ENABLED_ENV, "false")
    card, priv = card_and_key
    lineage_root = tmp_path / "lineage"
    result = post_write_lineage_hook(
        artefact_path="src/foo.py",
        new_content=b"hello",
        agent_id=card.agent_id,
        agent_card=card,
        private_key_pem=priv,
        tool_call_id="tc-1",
        span_id="span-1",
        lineage_root=lineage_root,
        operator_hmac_key=b"k" * 32,
    )
    assert result is None
    # The root is never touched in disabled mode.
    assert not lineage_root.exists()


def test_hook_default_is_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    card_and_key: tuple[AgentCard, str],
) -> None:
    # Unset -> default behaviour is on.
    monkeypatch.delenv(LINEAGE_ENABLED_ENV, raising=False)
    card, priv = card_and_key
    lineage_root = tmp_path / "lineage"
    post_write_lineage_hook(
        artefact_path="src/foo.py",
        new_content=b"hello",
        agent_id=card.agent_id,
        agent_card=card,
        private_key_pem=priv,
        tool_call_id="tc-1",
        span_id="span-1",
        lineage_root=lineage_root,
        operator_hmac_key=b"k" * 32,
    )
    assert _spine_entries(lineage_root) == 1


def test_hook_fails_closed_when_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    card_and_key: tuple[AgentCard, str],
) -> None:
    """A spine failure must propagate when lineage is enabled (fail-closed)."""
    monkeypatch.setenv(LINEAGE_ENABLED_ENV, "1")
    card, priv = card_and_key
    from bernstein.adapters import base as base_module

    class _BoomSpine(LineageSpine):
        # The boundary appends through ``record_entry`` (issue #2559) so it can
        # project the production event off the entry it just wrote.
        def record_entry(self, **_kw: object) -> SpineEntry:  # type: ignore[override]
            raise RuntimeError("disk on fire")

    monkeypatch.setattr(base_module, "LineageSpine", _BoomSpine)

    with pytest.raises(RuntimeError, match="disk on fire"):
        post_write_lineage_hook(
            artefact_path="src/foo.py",
            new_content=b"hello",
            agent_id=card.agent_id,
            agent_card=card,
            private_key_pem=priv,
            tool_call_id="tc-1",
            span_id="span-1",
            lineage_root=tmp_path / "lineage",
            operator_hmac_key=b"k" * 32,
        )
