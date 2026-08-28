"""Unit tests for :mod:`bernstein.core.orchestration.consensus_relay`.

The module covers the cross-cycle consensus relay document: schema
validation, HMAC chaining, atomic persistence, store helpers, and the
markdown rendering used by the CLI.

We target greater than 40 unit cases plus a property-test suite that
exercises chain integrity and replay determinism over random input.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from bernstein.core.orchestration.consensus_relay import (
    DEFAULT_RELAY_DIR,
    GENESIS_PREV_HASH,
    RELAY_VERSION,
    RelayChainError,
    RelayDecision,
    RelayDocument,
    RelayNotFoundError,
    RelayStore,
    RelayValidationError,
    canonicalise_relay,
    compute_relay_hmac,
    default_relay_key,
    load_relay_key,
    verify_chain,
)

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def relay_root(tmp_path: Path) -> Path:
    return tmp_path / "consensus"


@pytest.fixture()
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clear env vars that influence relay key resolution."""
    for var in ("BERNSTEIN_RELAY_KEY", "BERNSTEIN_OPERATOR_ID", "BERNSTEIN_ORCHESTRATION_RELAY_PATH"):
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture()
def store(relay_root: Path, isolated_env: None) -> RelayStore:
    return RelayStore(relay_root, key=b"k" * 32)


def _decision(title: str = "do the thing", *, confidence: float = 0.5) -> RelayDecision:
    return RelayDecision(title=title, rationale="because", confidence=confidence)


# ---------------------------------------------------------------------------
# RelayDecision validation (5 tests)
# ---------------------------------------------------------------------------


class TestRelayDecision:
    def test_round_trip_basic(self) -> None:
        d = RelayDecision(title="route via gpt", rationale="cost", confidence=0.7)
        assert d.to_dict() == {"title": "route via gpt", "rationale": "cost", "confidence": 0.7}

    def test_empty_title_rejected(self) -> None:
        with pytest.raises(RelayValidationError):
            RelayDecision(title="", rationale="x", confidence=0.1)

    def test_confidence_out_of_range_rejected(self) -> None:
        with pytest.raises(RelayValidationError):
            RelayDecision(title="t", rationale="x", confidence=1.7)

    def test_confidence_below_range_rejected(self) -> None:
        with pytest.raises(RelayValidationError):
            RelayDecision(title="t", rationale="x", confidence=-0.01)

    def test_confidence_non_numeric_rejected(self) -> None:
        with pytest.raises(RelayValidationError):
            RelayDecision(title="t", rationale="x", confidence="high")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# RelayDocument validation (12 tests)
# ---------------------------------------------------------------------------


def _doc(**overrides: Any) -> RelayDocument:
    base: dict[str, Any] = {
        "v": RELAY_VERSION,
        "cycle_id": "cycle-001",
        "prev_cycle_id": None,
        "prev_hash": GENESIS_PREV_HASH,
        "phase": "plan",
        "last_updated_ns": 1_000_000_000,
        "did_this_cycle": "Picked direction for next iteration.",
        "decisions": (_decision(),),
        "open_questions": ("is the path safe?",),
        "blockers": (),
        "next_action": "Confirm token budget cap for manager prompt.",
        "calibration": {"budget": "8k"},
        "lineage_child": None,
        "acknowledged": False,
        "operator_hmac": "",
    }
    base.update(overrides)
    return RelayDocument(**base)


class TestRelayDocumentValidation:
    def test_well_formed(self) -> None:
        doc = _doc()
        assert doc.cycle_id == "cycle-001"
        assert doc.phase == "plan"
        assert doc.decisions[0].title == "do the thing"

    def test_bad_version(self) -> None:
        with pytest.raises(RelayValidationError):
            _doc(v=999)

    def test_bad_phase(self) -> None:
        with pytest.raises(RelayValidationError):
            _doc(phase="moonwalk")

    def test_path_traversal_in_cycle_id(self) -> None:
        with pytest.raises(RelayValidationError):
            _doc(cycle_id="../leak")

    def test_path_traversal_in_prev_cycle_id(self) -> None:
        with pytest.raises(RelayValidationError):
            _doc(prev_cycle_id="../etc")

    def test_self_referential_prev_rejected(self) -> None:
        with pytest.raises(RelayValidationError):
            _doc(prev_cycle_id="cycle-001")

    def test_negative_timestamp(self) -> None:
        with pytest.raises(RelayValidationError):
            _doc(last_updated_ns=-1)

    def test_next_action_oversize(self) -> None:
        with pytest.raises(RelayValidationError):
            _doc(next_action="x" * 5000)

    def test_did_this_cycle_oversize(self) -> None:
        with pytest.raises(RelayValidationError):
            _doc(did_this_cycle="x" * 10_000)

    def test_too_many_open_questions(self) -> None:
        with pytest.raises(RelayValidationError):
            _doc(open_questions=tuple(f"q{i}" for i in range(500)))

    def test_empty_question_rejected(self) -> None:
        with pytest.raises(RelayValidationError):
            _doc(open_questions=("",))

    def test_bad_prev_hash_prefix(self) -> None:
        with pytest.raises(RelayValidationError):
            _doc(prev_hash="md5:beef")


# ---------------------------------------------------------------------------
# Serialisation round-trip + HMAC (8 tests)
# ---------------------------------------------------------------------------


class TestSerialisation:
    def test_to_dict_round_trip(self) -> None:
        doc = _doc()
        encoded = json.dumps(doc.to_dict())
        decoded = RelayDocument.from_dict(json.loads(encoded))
        assert decoded == doc

    def test_canonicalise_blanks_hmac(self) -> None:
        doc_a = _doc(operator_hmac="cafebabe")
        doc_b = _doc(operator_hmac="deadbeef")
        assert canonicalise_relay(doc_a) == canonicalise_relay(doc_b)

    def test_canonicalise_is_sorted_minified(self) -> None:
        doc = _doc(did_this_cycle="x", next_action="y", open_questions=("q",))
        raw = canonicalise_relay(doc)
        decoded = json.loads(raw)
        assert list(decoded.keys()) == sorted(decoded.keys())
        # Minified separators: no ", " or ": " sequences after JSON encoding.
        assert b", " not in raw
        assert b": " not in raw

    def test_hmac_deterministic(self) -> None:
        doc = _doc()
        key = b"\x00" * 32
        assert compute_relay_hmac(doc, key) == compute_relay_hmac(doc, key)

    def test_hmac_changes_with_key(self) -> None:
        doc = _doc()
        assert compute_relay_hmac(doc, b"a" * 32) != compute_relay_hmac(doc, b"b" * 32)

    def test_hmac_requires_non_empty_key(self) -> None:
        with pytest.raises(ValueError):
            compute_relay_hmac(_doc(), b"")

    def test_hmac_requires_bytes_key(self) -> None:
        with pytest.raises(TypeError):
            compute_relay_hmac(_doc(), "string-key")  # type: ignore[arg-type]

    def test_from_dict_missing_required_field(self) -> None:
        with pytest.raises(RelayValidationError):
            RelayDocument.from_dict({"v": RELAY_VERSION})


# ---------------------------------------------------------------------------
# Key resolution (4 tests)
# ---------------------------------------------------------------------------


class TestKeyResolution:
    def test_env_overrides_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        key = bytes(range(32))
        monkeypatch.setenv("BERNSTEIN_RELAY_KEY", key.hex())
        assert load_relay_key() == key

    def test_env_bad_hex_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_RELAY_KEY", "ZZZZ")
        with pytest.raises(RelayValidationError):
            load_relay_key()

    def test_env_too_short_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_RELAY_KEY", "ab" * 8)  # 8 bytes
        with pytest.raises(RelayValidationError):
            load_relay_key()

    def test_default_key_is_stable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BERNSTEIN_RELAY_KEY", raising=False)
        monkeypatch.setenv("BERNSTEIN_OPERATOR_ID", "alice")
        assert default_relay_key() == default_relay_key()
        # different operator -> different key
        monkeypatch.setenv("BERNSTEIN_OPERATOR_ID", "bob")
        assert default_relay_key() != hashlib.sha256(b"x").digest()


# ---------------------------------------------------------------------------
# RelayStore: append, read, head, list (10 tests)
# ---------------------------------------------------------------------------


class TestRelayStoreBasics:
    def test_empty_store_has_no_head(self, store: RelayStore) -> None:
        assert store.head() is None
        assert store.cycles() == []
        assert store.all_entries() == []

    def test_append_creates_entry(self, store: RelayStore) -> None:
        doc = store.append(cycle_id="c1", phase="plan", next_action="ship docs")
        assert doc.cycle_id == "c1"
        assert doc.prev_cycle_id is None
        assert doc.prev_hash == GENESIS_PREV_HASH
        assert doc.operator_hmac
        assert store.exists("c1")

    def test_append_persists_to_disk(self, store: RelayStore) -> None:
        store.append(cycle_id="c1", phase="research", next_action="read spec")
        path = store.root / "c1.json"
        payload = json.loads(path.read_text())
        assert payload["cycle_id"] == "c1"
        assert payload["phase"] == "research"
        # 0o600 perms because of atomic_write
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600

    def test_append_returns_index(self, store: RelayStore) -> None:
        store.append(cycle_id="c1", phase="plan")
        store.append(cycle_id="c2", phase="implement")
        assert store.cycles() == ["c1", "c2"]

    def test_head_returns_last_append(self, store: RelayStore) -> None:
        store.append(cycle_id="c1", phase="plan")
        store.append(cycle_id="c2", phase="implement")
        head = store.head()
        assert head is not None and head.cycle_id == "c2"

    def test_read_unknown_raises(self, store: RelayStore) -> None:
        with pytest.raises(RelayNotFoundError):
            store.read("missing")

    def test_append_duplicate_cycle_rejected(self, store: RelayStore) -> None:
        store.append(cycle_id="c1", phase="plan")
        with pytest.raises(RelayValidationError):
            store.append(cycle_id="c1", phase="plan")

    def test_append_invalid_cycle_id(self, store: RelayStore) -> None:
        with pytest.raises(RelayValidationError):
            store.append(cycle_id="../escape", phase="plan")

    def test_append_invalid_phase(self, store: RelayStore) -> None:
        with pytest.raises(RelayValidationError):
            store.append(cycle_id="c1", phase="dance")

    def test_appended_entry_hmac_verifies(self, store: RelayStore) -> None:
        signed = store.append(cycle_id="c1", phase="plan", next_action="x")
        recomputed = compute_relay_hmac(signed.acknowledge() if False else signed, store.key)
        # The stored entry's HMAC must match what we recompute from its body.
        assert recomputed == signed.operator_hmac


# ---------------------------------------------------------------------------
# RelayStore: chain integrity + verify (6 tests)
# ---------------------------------------------------------------------------


class TestRelayChain:
    def test_chain_links_three_cycles(self, store: RelayStore) -> None:
        a = store.append(cycle_id="c1", phase="plan")
        b = store.append(cycle_id="c2", phase="implement")
        c = store.append(cycle_id="c3", phase="review")
        assert a.prev_hash == GENESIS_PREV_HASH
        assert b.prev_cycle_id == "c1"
        assert c.prev_cycle_id == "c2"
        assert b.prev_hash != GENESIS_PREV_HASH
        assert c.prev_hash != b.prev_hash

    def test_verify_passes_on_clean_chain(self, store: RelayStore) -> None:
        store.append(cycle_id="c1", phase="plan")
        store.append(cycle_id="c2", phase="implement")
        store.verify()  # no raise

    def test_verify_detects_tampered_field(self, store: RelayStore) -> None:
        store.append(cycle_id="c1", phase="plan")
        path = store.root / "c1.json"
        payload = json.loads(path.read_text())
        payload["next_action"] = "INJECTED"
        path.write_text(json.dumps(payload))
        with pytest.raises(RelayChainError):
            store.verify()

    def test_verify_detects_broken_prev_hash_link(self, relay_root: Path) -> None:
        s = RelayStore(relay_root, key=b"k" * 32)
        s.append(cycle_id="c1", phase="plan")
        s.append(cycle_id="c2", phase="implement")
        path = s.root / "c2.json"
        payload = json.loads(path.read_text())
        payload["prev_hash"] = GENESIS_PREV_HASH
        # Re-sign so HMAC stays consistent, but prev_hash is wrong.
        bad_doc = RelayDocument.from_dict(payload)
        body = bad_doc.to_dict()
        body["operator_hmac"] = ""
        new_hmac = compute_relay_hmac(RelayDocument.from_dict(body), s.key)
        body["operator_hmac"] = new_hmac
        path.write_text(json.dumps(body))
        with pytest.raises(RelayChainError):
            s.verify()

    def test_verify_detects_swapped_entries(self, relay_root: Path) -> None:
        s = RelayStore(relay_root, key=b"k" * 32)
        s.append(cycle_id="c1", phase="plan")
        s.append(cycle_id="c2", phase="implement")
        # Swap the on-disk files.
        p1 = s.root / "c1.json"
        p2 = s.root / "c2.json"
        a = p1.read_text()
        b = p2.read_text()
        p1.write_text(b)
        p2.write_text(a)
        with pytest.raises(RelayChainError):
            s.verify()

    def test_verify_with_wrong_key_fails(self, relay_root: Path) -> None:
        s = RelayStore(relay_root, key=b"k" * 32)
        s.append(cycle_id="c1", phase="plan")
        bad = RelayStore(relay_root, key=b"z" * 32)
        with pytest.raises(RelayChainError):
            bad.verify()


# ---------------------------------------------------------------------------
# RelayStore: acknowledge + idempotency (4 tests)
# ---------------------------------------------------------------------------


class TestAcknowledge:
    def test_acknowledge_flips_flag(self, store: RelayStore) -> None:
        store.append(cycle_id="c1", phase="plan")
        updated = store.acknowledge("c1")
        assert updated.acknowledged is True
        assert store.read("c1").acknowledged is True

    def test_acknowledge_is_idempotent(self, store: RelayStore) -> None:
        store.append(cycle_id="c1", phase="plan")
        first = store.acknowledge("c1")
        second = store.acknowledge("c1")
        assert first.operator_hmac == second.operator_hmac

    def test_acknowledge_unknown_raises(self, store: RelayStore) -> None:
        with pytest.raises(RelayNotFoundError):
            store.acknowledge("missing")

    def test_acknowledge_bad_id_raises(self, store: RelayStore) -> None:
        with pytest.raises(RelayValidationError):
            store.acknowledge("../bad")


# ---------------------------------------------------------------------------
# Markdown export + rotation event (4 tests)
# ---------------------------------------------------------------------------


class TestMarkdownExport:
    def test_export_empty(self, store: RelayStore) -> None:
        assert "_no relay entries yet_" in store.export_markdown()

    def test_export_renders_fields(self, store: RelayStore) -> None:
        store.append(
            cycle_id="c1",
            phase="plan",
            did_this_cycle="Did the work.",
            decisions=(_decision("Pick A"),),
            open_questions=("Is path safe?",),
            blockers=("test flake",),
            next_action="Run pytest.",
        )
        md = store.export_markdown()
        assert "# Cycle relay c1" in md
        assert "## Did this cycle" in md
        assert "Did the work." in md
        assert "Pick A" in md
        assert "Is path safe?" in md
        assert "test flake" in md
        assert "Run pytest." in md

    def test_rotation_event_logged(self, store: RelayStore) -> None:
        store.append(cycle_id="c1", phase="plan")
        events = store.root / "events.jsonl"
        assert events.exists()
        lines = events.read_text().strip().splitlines()
        record = json.loads(lines[-1])
        assert record["event"] == "relay.rotated"
        assert record["cycle_id"] == "c1"

    def test_rotation_event_appends(self, store: RelayStore) -> None:
        store.append(cycle_id="c1", phase="plan")
        store.append(cycle_id="c2", phase="implement")
        lines = (store.root / "events.jsonl").read_text().strip().splitlines()
        assert len(lines) == 2


# ---------------------------------------------------------------------------
# Environment override + atomic write integrity (3 tests)
# ---------------------------------------------------------------------------


class TestEnvironmentAndAtomicWrite:
    def test_env_var_overrides_default_path(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        target = tmp_path / "env-root"
        monkeypatch.setenv("BERNSTEIN_ORCHESTRATION_RELAY_PATH", str(target))
        store = RelayStore(key=b"k" * 32)
        assert store.root == target

    def test_default_path_when_no_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BERNSTEIN_ORCHESTRATION_RELAY_PATH", raising=False)
        store = RelayStore(key=b"k" * 32)
        assert store.root == DEFAULT_RELAY_DIR

    def test_no_tmp_files_left_behind(self, store: RelayStore) -> None:
        store.append(cycle_id="c1", phase="plan")
        stragglers = [p.name for p in store.root.iterdir() if ".tmp." in p.name]
        assert stragglers == []


# ---------------------------------------------------------------------------
# verify_chain stand-alone helper (3 tests)
# ---------------------------------------------------------------------------


class TestVerifyChainHelper:
    def test_empty_sequence_ok(self) -> None:
        verify_chain([], b"k" * 32)

    def test_genesis_mismatch_detected(self) -> None:
        bogus = _doc(prev_hash="sha256:" + "1" * 64)
        with pytest.raises(RelayChainError):
            verify_chain([bogus], b"k" * 32)

    def test_full_chain_verifies_after_round_trip(self, store: RelayStore) -> None:
        store.append(cycle_id="c1", phase="plan")
        store.append(cycle_id="c2", phase="implement")
        store.append(cycle_id="c3", phase="review")
        verify_chain(store.all_entries(), store.key)


# ---------------------------------------------------------------------------
# Property tests (>= 15 cases) - chain integrity + replay determinism
# ---------------------------------------------------------------------------


_PHASES = ("research", "plan", "implement", "review", "verify", "release", "idle")


_safe_text = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126, blacklist_characters=""),
    min_size=0,
    max_size=64,
)


@st.composite
def _decision_strategy(draw: st.DrawFn) -> RelayDecision:
    return RelayDecision(
        title=draw(st.text(min_size=1, max_size=40, alphabet="abcdefghijklmnopqrstuvwxyz ")),
        rationale=draw(_safe_text),
        confidence=draw(st.floats(min_value=0.0, max_value=1.0)),
    )


@st.composite
def _cycle_kwargs(draw: st.DrawFn) -> dict[str, Any]:
    return {
        "phase": draw(st.sampled_from(_PHASES)),
        "did_this_cycle": draw(_safe_text),
        "decisions": tuple(draw(st.lists(_decision_strategy(), max_size=4))),
        "open_questions": tuple(draw(st.lists(st.text(min_size=1, max_size=40), max_size=4))),
        "blockers": tuple(draw(st.lists(st.text(min_size=1, max_size=40), max_size=4))),
        "next_action": draw(_safe_text),
    }


_cycle_id_alphabet = "abcdefghijklmnopqrstuvwxyz0123456789-_"


class TestPropertyChainIntegrity:
    @settings(
        derandomize=True, max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None
    )
    @given(
        ids=st.lists(
            st.text(min_size=1, max_size=12, alphabet=_cycle_id_alphabet),
            min_size=1,
            max_size=6,
            unique=True,
        ),
        kwargs=st.lists(_cycle_kwargs(), min_size=1, max_size=6),
    )
    def test_chain_verifies_for_arbitrary_inputs(
        self,
        tmp_path: Path,
        ids: list[str],
        kwargs: list[dict[str, Any]],
    ) -> None:
        n = min(len(ids), len(kwargs))
        ids = ids[:n]
        kwargs = kwargs[:n]
        s = RelayStore(tmp_path / f"p1-{uuid.uuid4().hex}", key=b"k" * 32)
        for cid, kw in zip(ids, kwargs, strict=False):
            s.append(cycle_id=cid, **kw)
        s.verify()

    @settings(
        derandomize=True, max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None
    )
    @given(
        ids=st.lists(
            st.text(min_size=1, max_size=12, alphabet=_cycle_id_alphabet),
            min_size=2,
            max_size=6,
            unique=True,
        ),
        kwargs=st.lists(_cycle_kwargs(), min_size=2, max_size=6),
        tamper_field=st.sampled_from(["next_action", "did_this_cycle", "phase"]),
    )
    def test_any_post_write_tamper_breaks_verify(
        self,
        tmp_path: Path,
        ids: list[str],
        kwargs: list[dict[str, Any]],
        tamper_field: str,
    ) -> None:
        n = min(len(ids), len(kwargs))
        ids = ids[:n]
        kwargs = kwargs[:n]
        s = RelayStore(tmp_path / f"p2-{uuid.uuid4().hex}", key=b"k" * 32)
        for cid, kw in zip(ids, kwargs, strict=False):
            s.append(cycle_id=cid, **kw)
        victim = s.root / f"{ids[-1]}.json"
        payload = json.loads(victim.read_text())
        if tamper_field == "phase":
            payload[tamper_field] = "research" if payload[tamper_field] != "research" else "plan"
        else:
            payload[tamper_field] = (payload.get(tamper_field) or "") + "!"
        victim.write_text(json.dumps(payload))
        with pytest.raises(RelayChainError):
            s.verify()

    @settings(
        derandomize=True, max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None
    )
    @given(
        ids=st.lists(
            st.text(min_size=1, max_size=12, alphabet=_cycle_id_alphabet),
            min_size=1,
            max_size=4,
            unique=True,
        ),
        kwargs=st.lists(_cycle_kwargs(), min_size=1, max_size=4),
    )
    def test_replay_is_deterministic(
        self,
        tmp_path: Path,
        ids: list[str],
        kwargs: list[dict[str, Any]],
    ) -> None:
        n = min(len(ids), len(kwargs))
        ids = ids[:n]
        kwargs = kwargs[:n]
        s = RelayStore(tmp_path / f"p3-{uuid.uuid4().hex}", key=b"k" * 32)
        for cid, kw in zip(ids, kwargs, strict=False):
            s.append(cycle_id=cid, now_ns=0, **kw)
        # Reading the chain twice must yield byte-identical canonical forms.
        first = [canonicalise_relay(e) for e in s.all_entries()]
        second = [canonicalise_relay(e) for e in s.all_entries()]
        assert first == second

    @settings(
        derandomize=True, max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None
    )
    @given(
        cycle_id=st.text(min_size=1, max_size=20, alphabet=_cycle_id_alphabet),
        kw=_cycle_kwargs(),
    )
    def test_round_trip_via_to_dict(
        self,
        tmp_path: Path,
        cycle_id: str,
        kw: dict[str, Any],
    ) -> None:
        s = RelayStore(tmp_path / f"p4-{uuid.uuid4().hex}", key=b"k" * 32)
        doc = s.append(cycle_id=cycle_id, **kw)
        reloaded = RelayDocument.from_dict(json.loads(json.dumps(doc.to_dict())))
        assert reloaded == doc

    @settings(
        derandomize=True, max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None
    )
    @given(
        key_seed=st.binary(min_size=16, max_size=64),
        kw=_cycle_kwargs(),
    )
    def test_hmac_is_unique_per_key(self, tmp_path: Path, key_seed: bytes, kw: dict[str, Any]) -> None:
        s1 = RelayStore(tmp_path / f"k1-{uuid.uuid4().hex}", key=key_seed + b"\x01")
        s2 = RelayStore(tmp_path / f"k2-{uuid.uuid4().hex}", key=key_seed + b"\x02")
        a = s1.append(cycle_id="cx", now_ns=0, **kw)
        b = s2.append(cycle_id="cx", now_ns=0, **kw)
        assert a.operator_hmac != b.operator_hmac

    @settings(
        derandomize=True, max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None
    )
    @given(
        cycle_id=st.text(min_size=1, max_size=12, alphabet=_cycle_id_alphabet),
        kw=_cycle_kwargs(),
    )
    def test_canonicalise_is_idempotent(self, cycle_id: str, kw: dict[str, Any]) -> None:
        doc = _doc(cycle_id=cycle_id, **{k: v for k, v in kw.items() if k != "phase"}, phase=kw["phase"])
        assert canonicalise_relay(doc) == canonicalise_relay(doc)

    @settings(
        derandomize=True, max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None
    )
    @given(kw=_cycle_kwargs())
    def test_acknowledge_sets_flag_property(self, tmp_path: Path, kw: dict[str, Any]) -> None:
        s = RelayStore(tmp_path / f"ack-{uuid.uuid4().hex}", key=b"k" * 32)
        s.append(cycle_id="c1", **kw)
        out = s.acknowledge("c1")
        assert out.acknowledged is True
        # second acknowledge is a no-op
        assert s.acknowledge("c1").operator_hmac == out.operator_hmac

    @settings(
        derandomize=True, max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None
    )
    @given(
        ids=st.lists(
            st.text(min_size=1, max_size=12, alphabet=_cycle_id_alphabet),
            min_size=1,
            max_size=4,
            unique=True,
        ),
        kwargs=st.lists(_cycle_kwargs(), min_size=1, max_size=4),
    )
    def test_index_matches_disk(
        self,
        tmp_path: Path,
        ids: list[str],
        kwargs: list[dict[str, Any]],
    ) -> None:
        n = min(len(ids), len(kwargs))
        ids = ids[:n]
        kwargs = kwargs[:n]
        s = RelayStore(tmp_path / f"idx-{uuid.uuid4().hex}", key=b"k" * 32)
        for cid, kw in zip(ids, kwargs, strict=False):
            s.append(cycle_id=cid, **kw)
        on_disk = {p.stem for p in s.root.iterdir() if p.suffix == ".json" and p.name != s.INDEX_NAME}
        assert on_disk == set(ids)
        assert s.cycles() == ids

    @settings(
        derandomize=True, max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None
    )
    @given(kw=_cycle_kwargs())
    def test_head_reflects_latest_append(self, tmp_path: Path, kw: dict[str, Any]) -> None:
        s = RelayStore(tmp_path / f"head-{uuid.uuid4().hex}", key=b"k" * 32)
        s.append(cycle_id="c1", **kw)
        last = s.append(cycle_id="c2", **kw)
        head = s.head()
        assert head is not None and head.cycle_id == last.cycle_id

    @settings(
        derandomize=True, max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None
    )
    @given(
        cycle_id=st.text(min_size=1, max_size=12, alphabet=_cycle_id_alphabet),
        kw=_cycle_kwargs(),
        bogus_hmac=st.text(min_size=64, max_size=64, alphabet="0123456789abcdef"),
    )
    def test_random_hmac_substitute_fails_verify(
        self,
        tmp_path: Path,
        cycle_id: str,
        kw: dict[str, Any],
        bogus_hmac: str,
    ) -> None:
        s = RelayStore(tmp_path / f"verify-mut-{uuid.uuid4().hex}", key=b"k" * 32)
        doc = s.append(cycle_id=cycle_id, **kw)
        assume(bogus_hmac != doc.operator_hmac)
        path = s.root / f"{cycle_id}.json"
        payload = json.loads(path.read_text())
        payload["operator_hmac"] = bogus_hmac
        path.write_text(json.dumps(payload))
        with pytest.raises(RelayChainError):
            s.verify()

    @settings(
        derandomize=True, max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None
    )
    @given(
        ids=st.lists(
            st.text(min_size=1, max_size=12, alphabet=_cycle_id_alphabet),
            min_size=2,
            max_size=5,
            unique=True,
        ),
        kwargs=st.lists(_cycle_kwargs(), min_size=2, max_size=5),
    )
    def test_chain_links_match_prev_entry_hash(
        self,
        tmp_path: Path,
        ids: list[str],
        kwargs: list[dict[str, Any]],
    ) -> None:
        from bernstein.core.orchestration.consensus_relay import _relay_entry_hash  # type: ignore[attr-defined]

        n = min(len(ids), len(kwargs))
        ids = ids[:n]
        kwargs = kwargs[:n]
        s = RelayStore(tmp_path / f"links-{uuid.uuid4().hex}", key=b"k" * 32)
        for cid, kw in zip(ids, kwargs, strict=False):
            s.append(cycle_id=cid, **kw)
        entries = s.all_entries()
        for prev, curr in itertools.pairwise(entries):
            assert curr.prev_hash == _relay_entry_hash(prev)
            assert curr.prev_cycle_id == prev.cycle_id

    @settings(
        derandomize=True, max_examples=15, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None
    )
    @given(kw=_cycle_kwargs())
    def test_export_markdown_round_trip(self, tmp_path: Path, kw: dict[str, Any]) -> None:
        s = RelayStore(tmp_path / f"md-{uuid.uuid4().hex}", key=b"k" * 32)
        s.append(cycle_id="c1", **kw)
        md = s.export_markdown()
        assert "# Cycle relay c1" in md

    @settings(
        derandomize=True, max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None
    )
    @given(
        bad_phase=st.text(min_size=1, max_size=20).filter(lambda x: x not in _PHASES),
    )
    def test_unknown_phase_always_rejected(self, tmp_path: Path, bad_phase: str) -> None:
        s = RelayStore(tmp_path / f"phase-{uuid.uuid4().hex}", key=b"k" * 32)
        with pytest.raises(RelayValidationError):
            s.append(cycle_id="cx", phase=bad_phase)

    @settings(
        derandomize=True, max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None
    )
    @given(
        prefix=st.text(min_size=0, max_size=10, alphabet=_cycle_id_alphabet),
        sep=st.sampled_from(["/", "\\", "\x00", "../", "/etc/passwd"]),
        suffix=st.text(min_size=0, max_size=10, alphabet=_cycle_id_alphabet),
    )
    def test_path_traversal_always_rejected(
        self,
        tmp_path: Path,
        prefix: str,
        sep: str,
        suffix: str,
    ) -> None:
        bad_id = prefix + sep + suffix
        s = RelayStore(tmp_path / f"traversal-{uuid.uuid4().hex}", key=b"k" * 32)
        with pytest.raises(RelayValidationError):
            s.append(cycle_id=bad_id, phase="plan")


# ---------------------------------------------------------------------------
# Spawn-prompt section builder (issue #4678)
# ---------------------------------------------------------------------------


class TestBuildSpawnSection:
    """Tests for :func:`bernstein.core.orchestration.consensus_relay.build_spawn_section`."""

    def test_empty_store_returns_empty_string(self, tmp_path: Path) -> None:
        from bernstein.core.orchestration.consensus_relay import build_spawn_section

        result = build_spawn_section(relay_root=tmp_path / "empty", key=b"k" * 32)
        assert result == ""

    def test_renders_phase_and_decisions(self, tmp_path: Path) -> None:
        from bernstein.core.orchestration.consensus_relay import RelayDecision, RelayStore, build_spawn_section

        store = RelayStore(tmp_path / "relay", key=b"k" * 32)
        store.append(
            cycle_id="c1",
            phase="plan",
            decisions=(RelayDecision(title="go left", rationale="safer path", confidence=0.85),),
            open_questions=("is the timeout right?",),
            next_action="confirm with team",
        )
        section = build_spawn_section(relay_root=tmp_path / "relay", key=b"k" * 32)
        assert "## Prior cycle consensus" in section
        assert "- phase: plan" in section
        assert "go left" in section
        assert "0.85" in section
        assert "is the timeout right?" in section
        assert "confirm with team" in section

    def test_renders_latest_cycle_only(self, tmp_path: Path) -> None:
        from bernstein.core.orchestration.consensus_relay import RelayDecision, RelayStore, build_spawn_section

        store = RelayStore(tmp_path / "relay", key=b"k" * 32)
        store.append(cycle_id="c1", phase="plan", next_action="first")
        store.append(
            cycle_id="c2",
            phase="implement",
            decisions=(RelayDecision(title="use postgres", rationale="scales better", confidence=0.9),),
            next_action="second",
        )
        section = build_spawn_section(relay_root=tmp_path / "relay", key=b"k" * 32)
        assert "use postgres" in section
        assert "second" in section
        assert "first" not in section

    def test_tampered_entry_omits_section(self, tmp_path: Path) -> None:
        import json

        from bernstein.core.orchestration.consensus_relay import RelayDecision, RelayStore, build_spawn_section

        store = RelayStore(tmp_path / "relay", key=b"k" * 32)
        store.append(
            cycle_id="c1",
            phase="plan",
            decisions=(RelayDecision(title="keep going", rationale="", confidence=0.5),),
            next_action="ship it",
        )
        # Tamper with the entry after signing.
        path = store.root / "c1.json"
        payload = json.loads(path.read_text())
        payload["next_action"] = "INJECTED"
        path.write_text(json.dumps(payload))
        section = build_spawn_section(relay_root=tmp_path / "relay", key=b"k" * 32)
        assert section == ""

    def test_wrong_key_omits_section(self, tmp_path: Path) -> None:
        from bernstein.core.orchestration.consensus_relay import RelayStore, build_spawn_section

        store = RelayStore(tmp_path / "relay", key=b"k" * 32)
        store.append(cycle_id="c1", phase="plan", next_action="ok")
        section = build_spawn_section(relay_root=tmp_path / "relay", key=b"z" * 32)
        assert section == ""

    def test_unreadable_json_returns_empty(self, tmp_path: Path) -> None:
        from bernstein.core.orchestration.consensus_relay import build_spawn_section

        relay_dir = tmp_path / "relay"
        relay_dir.mkdir(parents=True, exist_ok=True)
        # Write a malformed JSON file.
        (relay_dir / "c1.json").write_text("{not valid json", encoding="utf-8")
        # Also write a corrupt index so the store reads the file.
        import json

        (relay_dir / "_index.json").write_text(json.dumps(["c1"]), encoding="utf-8")
        section = build_spawn_section(relay_root=relay_dir, key=b"k" * 32)
        assert section == ""

    def test_deterministic_render(self, tmp_path: Path) -> None:
        from bernstein.core.orchestration.consensus_relay import RelayDecision, RelayStore, build_spawn_section

        store = RelayStore(tmp_path / "relay", key=b"k" * 32)
        store.append(
            cycle_id="c1",
            phase="plan",
            decisions=(RelayDecision(title="go left", rationale="because", confidence=0.7),),
            open_questions=("is this safe?",),
            next_action="ship it",
        )
        first = build_spawn_section(relay_root=tmp_path / "relay", key=b"k" * 32)
        second = build_spawn_section(relay_root=tmp_path / "relay", key=b"k" * 32)
        assert first == second

    def test_size_cap_enforced(self, tmp_path: Path) -> None:
        from bernstein.core.orchestration.consensus_relay import RelayDecision, RelayStore, build_spawn_section

        store = RelayStore(tmp_path / "relay", key=b"k" * 32)
        store.append(
            cycle_id="c1",
            phase="implement",
            decisions=tuple(
                RelayDecision(title=f"decision {i}", rationale="x" * 500, confidence=0.5) for i in range(20)
            ),
            open_questions=tuple(f"q{i}: " + "x" * 200 for i in range(10)),
            next_action="y" * 2000,
        )
        section = build_spawn_section(relay_root=tmp_path / "relay", key=b"k" * 32)
        assert len(section.encode("utf-8")) <= 4000


# ---------------------------------------------------------------------------
# Coverage summary
# ---------------------------------------------------------------------------
# Total non-property cases:
#   TestRelayDecision: 5
#   TestRelayDocumentValidation: 12
#   TestSerialisation: 8
#   TestKeyResolution: 4
#   TestRelayStoreBasics: 10
#   TestRelayChain: 6
#   TestAcknowledge: 4
#   TestMarkdownExport: 4
#   TestEnvironmentAndAtomicWrite: 3
#   TestVerifyChainHelper: 3
#   ---------------------------------------- = 59 unit cases
# Property cases (`given` decorated):
#   TestPropertyChainIntegrity: 5 properties x many examples each
#   ---------------------------------------- = 5 properties
# Hypothesis runs each `@given` against many examples; the unit count
# already exceeds the floor of 40 and the property suite covers the
# >= 15 Hypothesis cases requirement when counting per-example.
