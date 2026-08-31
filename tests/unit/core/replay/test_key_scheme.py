"""Key-scheme derivation and cross-scheme replay classification (#4867)."""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from bernstein.core.replay import (
    GatewayMode,
    ReplayGateway,
    ReplayKeySchemeMismatchError,
    ReplayMissError,
    derive_replay_key,
    parse_stored_key,
)


def _explode() -> object:
    raise AssertionError("replay must not call invoke()")


# ---------------------------------------------------------------------------
# Derivation properties (each its own test — issue acceptance)
# ---------------------------------------------------------------------------


def test_absent_and_empty_derive_different_keys() -> None:
    """``None`` and ``""`` must not share an identity (#4867)."""
    assert derive_replay_key(None) != derive_replay_key("")


def test_int_and_float_share_numeric_identity() -> None:
    """Fixed-precision numeric rendering: ``600`` and ``600.0`` are one key."""
    assert derive_replay_key(600) == derive_replay_key(600.0)


def test_sequence_boundary_cannot_be_forged_by_element_content() -> None:
    """Length-prefixed components: element content cannot forge a split."""
    assert derive_replay_key("ab", "c") != derive_replay_key("a", "bc")
    assert derive_replay_key(["ab", "c"]) != derive_replay_key(["a", "bc"])
    # Hostile payload that would collide under a bare delimiter join.
    assert derive_replay_key("a\x00b", "c") != derive_replay_key("a", "b\x00c")


@given(
    left=st.text(max_size=32),
    right=st.text(max_size=32),
)
def test_property_adjacent_splits_do_not_collide(left: str, right: str) -> None:
    """Any adjacent split of two strings yields a distinct multi-arg key."""
    joined = left + right
    # Skip the trivial empty-right case where ("joined",) vs ("joined", "") differ
    # by arity; the forge case is two-vs-two with the same concatenation.
    assume_split = left or right
    if not assume_split:
        return
    for i in range(len(joined) + 1):
        other_left, other_right = joined[:i], joined[i:]
        if (other_left, other_right) == (left, right):
            continue
        assert derive_replay_key(left, right) != derive_replay_key(other_left, other_right)


def test_stored_key_is_scheme_prefixed_digest() -> None:
    key = derive_replay_key("caller-opaque")
    scheme, digest = parse_stored_key(key)
    assert scheme == "v1"
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


def test_v1_and_v2_differ_for_same_components() -> None:
    assert derive_replay_key("same", scheme="v1") != derive_replay_key("same", scheme="v2")


# ---------------------------------------------------------------------------
# Gateway: same-scheme match path + older-scheme outcome
# ---------------------------------------------------------------------------


def test_recording_writes_scheme_prefixed_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BERNSTEIN_RECORD", "1")
    rec = ReplayGateway("run-prefix", tmp_path)
    rec.dispatch(kind="llm", key="plain", invoke=lambda: "ok")
    row = rec.path.read_text(encoding="utf-8").splitlines()[0]
    import json

    stored = json.loads(row)["key"]
    assert stored == derive_replay_key("plain")
    assert stored.startswith("v1:")


def test_same_scheme_corpus_replays_byte_identical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BERNSTEIN_RECORD", "1")
    rec = ReplayGateway("run-same", tmp_path)
    recorded = [rec.dispatch(kind="llm", key=f"k-{i}", invoke=lambda i=i: {"i": i, "body": f"r{i}"}) for i in range(3)]

    replay = ReplayGateway("run-same", tmp_path, mode=GatewayMode.REPLAY)
    replayed = [replay.dispatch(kind="llm", key=f"k-{i}", invoke=_explode) for i in range(3)]
    assert replayed == recorded


def test_older_scheme_corpus_yields_scheme_mismatch_on_every_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Corpus under v1, verifier at v2: older-scheme outcome, no FIFO serve."""
    monkeypatch.setenv("BERNSTEIN_RECORD", "1")
    keys = [f"row-{i}" for i in range(5)]
    rec = ReplayGateway("run-v1", tmp_path, key_scheme="v1")
    for key in keys:
        rec.dispatch(kind="llm", key=key, invoke=lambda key=key: f"resp-{key}")

    replay = ReplayGateway("run-v1", tmp_path, mode=GatewayMode.REPLAY, key_scheme="v2")
    for key in keys:
        with pytest.raises(ReplayKeySchemeMismatchError) as excinfo:
            replay.dispatch(kind="llm", key=key, invoke=_explode)
        assert excinfo.value.recorded_scheme == "v1"
        assert excinfo.value.current_scheme == "v2"
        assert "re-record to compare" in str(excinfo.value)

    # No row was consumed via silent FIFO: a same-scheme verifier still drains.
    same = ReplayGateway("run-v1", tmp_path, mode=GatewayMode.REPLAY, key_scheme="v1")
    for key in keys:
        assert same.dispatch(kind="llm", key=key, invoke=_explode) == f"resp-{key}"
    with pytest.raises(ReplayMissError):
        same.dispatch(kind="llm", key="extra", invoke=_explode)


def test_unversioned_corpus_is_older_scheme(
    tmp_path: Path,
) -> None:
    """Legacy rows without a scheme prefix classify as unversioned, not FIFO."""
    import json

    from bernstein.core.replay import EVENTS_FILENAME

    run_dir = tmp_path / "runs" / "legacy"
    path = run_dir / EVENTS_FILENAME
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"seq": 1, "kind": "llm", "key": "raw", "response": "x"}) + "\n",
        encoding="utf-8",
    )
    replay = ReplayGateway("legacy", tmp_path, mode=GatewayMode.REPLAY)
    with pytest.raises(ReplayKeySchemeMismatchError) as excinfo:
        replay.dispatch(kind="llm", key="raw", invoke=_explode)
    assert excinfo.value.recorded_scheme == "unversioned"
    assert excinfo.value.current_scheme == "v1"
